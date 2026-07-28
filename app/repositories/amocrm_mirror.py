from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.clock import resolve_moment
from app.models.amocrm_mirror import (
    DEFAULT_MIRROR_MAX_ATTEMPTS,
    MIRROR_KEY_MAX_LENGTH,
    AmoCrmMirrorJob,
    AmoCrmMirrorJobType,
    AmoCrmMirrorSkipReason,
    AmoCrmMirrorStatus,
    AmoCrmMirrorSubjectKind,
    amocrm_mirror_transition_allowed,
    assert_mirror_payload_is_safe,
)

# amocrm_mirror_jobs is always the LAST table in the lock order
#   conversations -> inbox_messages -> reply_plans -> outbox_messages
#   -> amocrm_mirror_jobs
# Its foreign key takes FOR KEY SHARE on the dialog row, so every enqueue runs
# inside a transaction that already holds conversations FOR UPDATE.

DEFAULT_LEASE_SECONDS = 30
DEFAULT_MAX_ATTEMPTS = DEFAULT_MIRROR_MAX_ATTEMPTS
DEFAULT_RETRY_DELAY_SECONDS = 1


class AmoCrmMirrorStateError(RuntimeError):
    """Raised when a mirror job status transition is not allowed."""


class StaleAmoCrmMirrorLeaseError(RuntimeError):
    """Raised when a worker uses an expired or superseded mirror lease."""


@dataclass(frozen=True, repr=False)
class AmoCrmMirrorClaim:
    job_id: uuid.UUID
    job_type: str
    subject_kind: str
    subject_id: uuid.UUID
    conversation_id: uuid.UUID
    context_version: int | None
    mirror_key: str
    status: str
    attempt_count: int
    max_attempts: int
    lease_owner: str
    lease_token: uuid.UUID
    lease_version: int
    lease_until: datetime
    correlation_id: uuid.UUID
    payload_json: dict[str, Any]

    def __repr__(self) -> str:
        return (
            f"AmoCrmMirrorClaim(job_id={self.job_id!r}, "
            f"job_type={self.job_type!r}, subject_kind={self.subject_kind!r}, "
            f"subject_id={self.subject_id!r}, "
            f"conversation_id={self.conversation_id!r}, "
            f"context_version={self.context_version!r}, "
            f"status={self.status!r}, attempt_count={self.attempt_count!r}, "
            f"lease_version={self.lease_version!r}, payload=<redacted>)"
        )


def _row_to_claim(row: AmoCrmMirrorJob) -> AmoCrmMirrorClaim:
    if row.lease_token is None or row.lease_owner is None or row.lease_until is None:
        raise RuntimeError("AMOCRM_MIRROR_LEASE_INCOMPLETE")
    return AmoCrmMirrorClaim(
        job_id=row.id,
        job_type=row.job_type,
        subject_kind=row.subject_kind,
        subject_id=row.subject_id,
        conversation_id=row.conversation_id,
        context_version=row.context_version,
        mirror_key=row.mirror_key,
        status=row.status,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        lease_owner=row.lease_owner,
        lease_token=row.lease_token,
        lease_version=row.lease_version,
        lease_until=row.lease_until,
        correlation_id=row.correlation_id,
        payload_json=dict(row.payload_json),
    )


async def get_by_id(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
) -> AmoCrmMirrorJob | None:
    """Read one job, discarding attributes loaded before the last statement."""
    stmt = (
        select(AmoCrmMirrorJob)
        .where(AmoCrmMirrorJob.id == job_id)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def require_processing_lease(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    lease_owner: str,
) -> AmoCrmMirrorJob:
    """Lock the job row and prove the caller's lease is still current.

    Must run after the conversation ``FOR UPDATE`` lock so the order stays
    ``conversations → … → amocrm_mirror_jobs``. Call this *before* any sink
    side-effect: a superseded token must never reach the adapter.
    """
    stmt = (
        select(AmoCrmMirrorJob)
        .where(AmoCrmMirrorJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = await session.scalar(stmt)
    if (
        job is None
        or job.status != AmoCrmMirrorStatus.PROCESSING.value
        or job.lease_token != lease_token
        or job.lease_version != lease_version
        or job.lease_owner != lease_owner
    ):
        raise StaleAmoCrmMirrorLeaseError("AMOCRM_MIRROR_STALE_LEASE")
    return job


async def get_by_mirror_key(
    session: AsyncSession,
    *,
    mirror_key: str,
) -> AmoCrmMirrorJob | None:
    stmt = select(AmoCrmMirrorJob).where(AmoCrmMirrorJob.mirror_key == mirror_key)
    return await session.scalar(stmt)


async def enqueue_if_absent(
    session: AsyncSession,
    *,
    job_type: AmoCrmMirrorJobType,
    subject_kind: AmoCrmMirrorSubjectKind,
    subject_id: uuid.UUID,
    conversation_id: uuid.UUID,
    mirror_key: str,
    payload_json: dict[str, Any],
    correlation_id: uuid.UUID,
    context_version: int | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> tuple[AmoCrmMirrorJob, bool]:
    """Idempotently enqueue a PENDING job. Does not commit.

    Runs inside the caller's domain transaction, so the job exists if and only
    if the domain change commits. Uses INSERT ... ON CONFLICT DO NOTHING on
    uq_amocrm_mirror_key, then SELECT. Assumes READ COMMITTED.
    """
    if not mirror_key or len(mirror_key) > MIRROR_KEY_MAX_LENGTH:
        raise ValueError("mirror_key must be non-empty and within column length")
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    assert_mirror_payload_is_safe(payload_json)

    existing = await get_by_mirror_key(session, mirror_key=mirror_key)
    if existing is not None:
        return existing, False

    stmt = (
        insert(AmoCrmMirrorJob)
        .values(
            id=uuid.uuid4(),
            job_type=job_type.value,
            subject_kind=subject_kind.value,
            subject_id=subject_id,
            conversation_id=conversation_id,
            context_version=context_version,
            mirror_key=mirror_key,
            status=AmoCrmMirrorStatus.PENDING.value,
            payload_json=payload_json,
            attempt_count=0,
            max_attempts=max_attempts,
            next_attempt_at=None,
            lease_owner=None,
            lease_token=None,
            lease_version=0,
            lease_until=None,
            correlation_id=correlation_id,
            error_code=None,
            skip_reason=None,
        )
        .on_conflict_do_nothing(constraint="uq_amocrm_mirror_key")
        .returning(AmoCrmMirrorJob.id)
    )
    inserted = await session.scalar(stmt)
    job = await get_by_mirror_key(session, mirror_key=mirror_key)
    if job is None:
        raise RuntimeError("AMOCRM_MIRROR_LOOKUP_FAILED")
    return job, inserted is not None


async def recover_exhausted_leases(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """Terminalize expired final attempts without invoking the mirror adapter."""
    moment = await resolve_moment(session, now)
    stmt = (
        update(AmoCrmMirrorJob)
        .where(
            AmoCrmMirrorJob.status == AmoCrmMirrorStatus.PROCESSING.value,
            AmoCrmMirrorJob.lease_until.is_not(None),
            AmoCrmMirrorJob.lease_until < moment,
            AmoCrmMirrorJob.attempt_count >= AmoCrmMirrorJob.max_attempts,
        )
        .values(
            status=AmoCrmMirrorStatus.DEAD.value,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            next_attempt_at=None,
            error_code="LEASE_ATTEMPTS_EXHAUSTED",
            skip_reason=None,
            updated_at=moment,
        )
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def claim_next(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> AmoCrmMirrorClaim | None:
    """Claim one due mirror job with FOR UPDATE SKIP LOCKED + fencing.

    Terminal jobs (MIRRORED / SKIPPED / DEAD) are never claimable.
    """
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")

    moment = await resolve_moment(session, now)
    await recover_exhausted_leases(session, now=moment)
    lease_until = moment + timedelta(seconds=lease_seconds)
    lease_token = uuid.uuid4()
    stmt = text(
        """
        WITH candidate AS (
            SELECT id
            FROM amocrm_mirror_jobs
            WHERE attempt_count < max_attempts
              AND (
                    status = 'PENDING'
                 OR (status = 'FAILED'
                     AND next_attempt_at IS NOT NULL
                     AND next_attempt_at <= :now)
                 OR (status = 'PROCESSING'
                     AND lease_until IS NOT NULL
                     AND lease_until < :now)
              )
            ORDER BY created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE amocrm_mirror_jobs AS j
        SET
            status = 'PROCESSING',
            lease_owner = :worker_id,
            lease_token = CAST(:lease_token AS uuid),
            lease_version = j.lease_version + 1,
            lease_until = :lease_until,
            attempt_count = j.attempt_count + 1,
            next_attempt_at = NULL,
            error_code = NULL,
            updated_at = :now
        FROM candidate
        WHERE j.id = candidate.id
        RETURNING j.id
        """
    )
    job_id = await session.scalar(
        stmt,
        {
            "now": moment,
            "worker_id": worker_id,
            "lease_token": str(lease_token),
            "lease_until": lease_until,
        },
    )
    if job_id is None:
        return None
    job = await get_by_id(session, job_id=job_id)
    if job is None:
        raise RuntimeError("AMOCRM_MIRROR_CLAIM_LOOKUP_FAILED")
    return _row_to_claim(job)


async def _terminate_with_lease(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    target: AmoCrmMirrorStatus,
    skip_reason: str | None,
    error_code: str | None,
    moment: datetime,
) -> AmoCrmMirrorJob:
    if not amocrm_mirror_transition_allowed(AmoCrmMirrorStatus.PROCESSING, target):
        raise AmoCrmMirrorStateError("AMOCRM_MIRROR_TRANSITION_DENIED")
    stmt = (
        update(AmoCrmMirrorJob)
        .where(
            AmoCrmMirrorJob.id == job_id,
            AmoCrmMirrorJob.status == AmoCrmMirrorStatus.PROCESSING.value,
            AmoCrmMirrorJob.lease_token == lease_token,
            AmoCrmMirrorJob.lease_version == lease_version,
        )
        .values(
            status=target.value,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            next_attempt_at=None,
            skip_reason=skip_reason,
            error_code=error_code,
            updated_at=moment,
        )
        .returning(AmoCrmMirrorJob.id)
    )
    updated = await session.scalar(stmt)
    if updated is None:
        raise StaleAmoCrmMirrorLeaseError("AMOCRM_MIRROR_STALE_LEASE")
    job = await get_by_id(session, job_id=job_id)
    if job is None:
        raise RuntimeError("AMOCRM_MIRROR_LOOKUP_FAILED")
    return job


async def complete_with_lease(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    now: datetime | None = None,
) -> AmoCrmMirrorJob:
    """PROCESSING → MIRRORED: accepted by the local no-op sink only."""
    moment = await resolve_moment(session, now)
    return await _terminate_with_lease(
        session,
        job_id=job_id,
        lease_token=lease_token,
        lease_version=lease_version,
        target=AmoCrmMirrorStatus.MIRRORED,
        skip_reason=None,
        error_code=None,
        moment=moment,
    )


async def skip_with_lease(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    skip_reason: AmoCrmMirrorSkipReason,
    now: datetime | None = None,
) -> AmoCrmMirrorJob:
    """PROCESSING → SKIPPED: the event is outdated, and that is not an error."""
    moment = await resolve_moment(session, now)
    return await _terminate_with_lease(
        session,
        job_id=job_id,
        lease_token=lease_token,
        lease_version=lease_version,
        target=AmoCrmMirrorStatus.SKIPPED,
        skip_reason=skip_reason.value,
        error_code=None,
        moment=moment,
    )


async def fail_with_lease(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    error_code: str,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    now: datetime | None = None,
) -> AmoCrmMirrorJob:
    """PROCESSING → FAILED (retry scheduled) or → DEAD (terminal)."""
    moment = await resolve_moment(session, now)
    job = await get_by_id(session, job_id=job_id)
    if job is None:
        raise RuntimeError("AMOCRM_MIRROR_LOOKUP_FAILED")
    if (
        job.status != AmoCrmMirrorStatus.PROCESSING.value
        or job.lease_token != lease_token
        or job.lease_version != lease_version
    ):
        raise StaleAmoCrmMirrorLeaseError("AMOCRM_MIRROR_STALE_LEASE")

    if job.attempt_count >= job.max_attempts:
        target = AmoCrmMirrorStatus.DEAD
        next_attempt_at = None
    else:
        target = AmoCrmMirrorStatus.FAILED
        next_attempt_at = moment + timedelta(seconds=retry_delay_seconds)

    if not amocrm_mirror_transition_allowed(AmoCrmMirrorStatus.PROCESSING, target):
        raise AmoCrmMirrorStateError("AMOCRM_MIRROR_TRANSITION_DENIED")

    stmt = (
        update(AmoCrmMirrorJob)
        .where(
            AmoCrmMirrorJob.id == job_id,
            AmoCrmMirrorJob.status == AmoCrmMirrorStatus.PROCESSING.value,
            AmoCrmMirrorJob.lease_token == lease_token,
            AmoCrmMirrorJob.lease_version == lease_version,
        )
        .values(
            status=target.value,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            next_attempt_at=next_attempt_at,
            error_code=error_code[:64],
            updated_at=moment,
        )
        .returning(AmoCrmMirrorJob.id)
    )
    updated = await session.scalar(stmt)
    if updated is None:
        raise StaleAmoCrmMirrorLeaseError("AMOCRM_MIRROR_STALE_LEASE")
    refreshed = await get_by_id(session, job_id=job_id)
    if refreshed is None:
        raise RuntimeError("AMOCRM_MIRROR_LOOKUP_FAILED")
    return refreshed
