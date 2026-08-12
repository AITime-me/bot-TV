"""Repository for amocrm_message_projections (AMO-01B1).

Lock order: conversations → … → amocrm_message_projections (after bindings).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.clock import resolve_moment
from app.models.amocrm_message_projection import (
    DEFAULT_PROJECTION_MAX_ATTEMPTS,
    AmocrmMessageProjection,
    AmocrmProjectionSkipReason,
    AmocrmProjectionSourceKind,
    AmocrmProjectionStatus,
    amocrm_projection_transition_allowed,
    integration_msgid_for_source,
)

DEFAULT_LEASE_SECONDS = 30
DEFAULT_MAX_ATTEMPTS = DEFAULT_PROJECTION_MAX_ATTEMPTS
DEFAULT_RETRY_DELAY_SECONDS = 1


class AmocrmProjectionStateError(RuntimeError):
    """Raised when a projection status transition is not allowed."""


class StaleAmocrmProjectionLeaseError(RuntimeError):
    """Raised when a worker uses an expired or superseded projection lease."""


@dataclass(frozen=True, repr=False)
class AmocrmProjectionClaim:
    projection_id: uuid.UUID
    conversation_id: uuid.UUID
    source_kind: str
    source_id: uuid.UUID
    integration_msgid: str
    amocrm_message_id: str | None
    status: str
    attempt_count: int
    max_attempts: int
    lease_owner: str
    lease_token: uuid.UUID
    lease_version: int
    lease_until: datetime
    correlation_id: uuid.UUID

    def __repr__(self) -> str:
        return (
            "AmocrmProjectionClaim("
            f"projection_id={self.projection_id!r}, "
            f"source_kind={self.source_kind!r}, "
            f"status={self.status!r}, "
            f"attempt_count={self.attempt_count!r}, "
            f"lease_version={self.lease_version!r}, "
            "integration_msgid=<redacted>, "
            f"amocrm_message_id={'set' if self.amocrm_message_id else None})"
        )


def _row_to_claim(row: AmocrmMessageProjection) -> AmocrmProjectionClaim:
    if row.lease_token is None or row.lease_owner is None or row.lease_until is None:
        raise RuntimeError("AMOCRM_PROJECTION_LEASE_INCOMPLETE")
    return AmocrmProjectionClaim(
        projection_id=row.id,
        conversation_id=row.conversation_id,
        source_kind=row.source_kind,
        source_id=row.source_id,
        integration_msgid=row.integration_msgid,
        amocrm_message_id=row.amocrm_message_id,
        status=row.status,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        lease_owner=row.lease_owner,
        lease_token=row.lease_token,
        lease_version=row.lease_version,
        lease_until=row.lease_until,
        correlation_id=row.correlation_id,
    )


async def get_by_id(
    session: AsyncSession,
    *,
    projection_id: uuid.UUID,
) -> AmocrmMessageProjection | None:
    return await session.get(AmocrmMessageProjection, projection_id)


async def get_projected_by_amocrm_message_id(
    session: AsyncSession,
    *,
    amocrm_message_id: str,
) -> AmocrmMessageProjection | None:
    """Return any projection row already carrying this amo message id.

    Used for echo suppression: status may still be PROCESSING after HTTP
    success but before the final PROJECTED transition.
    """

    stmt = select(AmocrmMessageProjection).where(
        AmocrmMessageProjection.amocrm_message_id == amocrm_message_id,
    )
    return await session.scalar(stmt)


async def enqueue_if_absent(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    source_kind: AmocrmProjectionSourceKind,
    source_id: uuid.UUID,
    correlation_id: uuid.UUID,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> tuple[AmocrmMessageProjection, bool]:
    """Idempotent enqueue. No text stored."""

    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    msgid = integration_msgid_for_source(source_kind=source_kind, source_id=source_id)
    existing = await session.scalar(
        select(AmocrmMessageProjection).where(
            AmocrmMessageProjection.source_kind == source_kind.value,
            AmocrmMessageProjection.source_id == source_id,
        )
    )
    if existing is not None:
        return existing, False

    new_id = uuid.uuid4()
    stmt = (
        insert(AmocrmMessageProjection)
        .values(
            id=new_id,
            conversation_id=conversation_id,
            source_kind=source_kind.value,
            source_id=source_id,
            integration_msgid=msgid,
            amocrm_message_id=None,
            status=AmocrmProjectionStatus.PENDING.value,
            attempt_count=0,
            max_attempts=max_attempts,
            next_attempt_at=None,
            lease_owner=None,
            lease_token=None,
            lease_version=0,
            lease_until=None,
            skip_reason=None,
            error_code=None,
            correlation_id=correlation_id,
        )
        .on_conflict_do_nothing(constraint="uq_amocrm_message_projections_source")
        .returning(AmocrmMessageProjection.id)
    )
    inserted = await session.scalar(stmt)
    row = await session.scalar(
        select(AmocrmMessageProjection).where(
            AmocrmMessageProjection.source_kind == source_kind.value,
            AmocrmMessageProjection.source_id == source_id,
        )
    )
    if row is None:
        raise RuntimeError("AMOCRM_PROJECTION_LOOKUP_FAILED")
    return row, inserted is not None


async def recover_exhausted_leases(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    moment = await resolve_moment(session, now)
    stmt = (
        update(AmocrmMessageProjection)
        .where(
            AmocrmMessageProjection.status
            == AmocrmProjectionStatus.PROCESSING.value,
            AmocrmMessageProjection.lease_until.is_not(None),
            AmocrmMessageProjection.lease_until < moment,
            AmocrmMessageProjection.attempt_count
            >= AmocrmMessageProjection.max_attempts,
        )
        .values(
            status=AmocrmProjectionStatus.DEAD.value,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            next_attempt_at=None,
            error_code="AMOCRM_PROJECTION_ATTEMPTS_EXHAUSTED",
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
) -> AmocrmProjectionClaim | None:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    await recover_exhausted_leases(session, now=now)
    moment = await resolve_moment(session, now)
    lease_until = moment + timedelta(seconds=lease_seconds)
    lease_token = uuid.uuid4()

    candidate_id = await session.scalar(
        text(
            """
            SELECT id FROM amocrm_message_projections
            WHERE (
                status = 'PENDING'
                AND (next_attempt_at IS NULL OR next_attempt_at <= :moment)
            ) OR (
                status = 'FAILED'
                AND (next_attempt_at IS NULL OR next_attempt_at <= :moment)
                AND attempt_count < max_attempts
            ) OR (
                status = 'PROCESSING'
                AND lease_until IS NOT NULL
                AND lease_until < :moment
                AND attempt_count < max_attempts
            )
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        ),
        {"moment": moment},
    )
    if candidate_id is None:
        return None

    stmt = (
        update(AmocrmMessageProjection)
        .where(AmocrmMessageProjection.id == candidate_id)
        .values(
            status=AmocrmProjectionStatus.PROCESSING.value,
            attempt_count=AmocrmMessageProjection.attempt_count + 1,
            lease_owner=worker_id,
            lease_token=lease_token,
            lease_version=AmocrmMessageProjection.lease_version + 1,
            lease_until=lease_until,
            next_attempt_at=None,
            error_code=None,
            updated_at=moment,
        )
        .returning(AmocrmMessageProjection.id)
    )
    updated = await session.scalar(stmt)
    if updated is None:
        return None
    row = await get_by_id(session, projection_id=updated)
    if row is None:
        raise RuntimeError("AMOCRM_PROJECTION_CLAIM_LOOKUP_FAILED")
    return _row_to_claim(row)


async def require_processing_lease(
    session: AsyncSession,
    *,
    projection_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    lease_owner: str,
    now: datetime | None = None,
) -> AmocrmMessageProjection:
    """Prove the caller's lease before any Chat HTTP side-effect."""

    moment = await resolve_moment(session, now)
    stmt = (
        select(AmocrmMessageProjection)
        .where(AmocrmMessageProjection.id == projection_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = await session.scalar(stmt)
    if (
        row is None
        or row.status != AmocrmProjectionStatus.PROCESSING.value
        or row.lease_token != lease_token
        or row.lease_version != lease_version
        or row.lease_owner != lease_owner
        or row.lease_until is None
        or row.lease_until <= moment
    ):
        raise StaleAmocrmProjectionLeaseError("AMOCRM_PROJECTION_STALE_LEASE")
    return row


async def renew_processing_lease(
    session: AsyncSession,
    *,
    projection_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    lease_owner: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> AmocrmMessageProjection:
    """Atomically re-fence lease_until immediately before Chat HTTP."""

    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    moment = await resolve_moment(session, now)
    await require_processing_lease(
        session,
        projection_id=projection_id,
        lease_token=lease_token,
        lease_version=lease_version,
        lease_owner=lease_owner,
        now=moment,
    )
    lease_until = moment + timedelta(seconds=lease_seconds)
    stmt = (
        update(AmocrmMessageProjection)
        .where(
            AmocrmMessageProjection.id == projection_id,
            AmocrmMessageProjection.status
            == AmocrmProjectionStatus.PROCESSING.value,
            AmocrmMessageProjection.lease_token == lease_token,
            AmocrmMessageProjection.lease_version == lease_version,
            AmocrmMessageProjection.lease_owner == lease_owner,
            AmocrmMessageProjection.lease_until.is_not(None),
            AmocrmMessageProjection.lease_until > moment,
        )
        .values(lease_until=lease_until, updated_at=moment)
        .returning(AmocrmMessageProjection.id)
    )
    updated = await session.scalar(stmt)
    if updated is None:
        raise StaleAmocrmProjectionLeaseError("AMOCRM_PROJECTION_STALE_LEASE")
    row = await get_by_id(session, projection_id=projection_id)
    if row is None:
        raise RuntimeError("AMOCRM_PROJECTION_LOOKUP_FAILED")
    return row


async def attach_amocrm_message_id_with_lease(
    session: AsyncSession,
    *,
    projection_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    lease_owner: str,
    amocrm_message_id: str,
    now: datetime | None = None,
) -> AmocrmMessageProjection:
    """Persist amo msgid under lease while still PROCESSING (echo-safe)."""

    moment = await resolve_moment(session, now)
    await require_processing_lease(
        session,
        projection_id=projection_id,
        lease_token=lease_token,
        lease_version=lease_version,
        lease_owner=lease_owner,
        now=moment,
    )
    if type(amocrm_message_id) is not str or not amocrm_message_id:
        raise ValueError("AMOCRM_MESSAGE_ID_INVALID")
    stmt = (
        update(AmocrmMessageProjection)
        .where(
            AmocrmMessageProjection.id == projection_id,
            AmocrmMessageProjection.status
            == AmocrmProjectionStatus.PROCESSING.value,
            AmocrmMessageProjection.lease_token == lease_token,
            AmocrmMessageProjection.lease_version == lease_version,
        )
        .values(
            amocrm_message_id=amocrm_message_id,
            updated_at=moment,
        )
        .returning(AmocrmMessageProjection.id)
    )
    updated = await session.scalar(stmt)
    if updated is None:
        raise StaleAmocrmProjectionLeaseError("AMOCRM_PROJECTION_STALE_LEASE")
    row = await get_by_id(session, projection_id=projection_id)
    if row is None:
        raise RuntimeError("AMOCRM_PROJECTION_LOOKUP_FAILED")
    return row


async def _terminate_with_lease(
    session: AsyncSession,
    *,
    projection_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    target: AmocrmProjectionStatus,
    skip_reason: str | None,
    error_code: str | None,
    amocrm_message_id: str | None,
    moment: datetime,
) -> AmocrmMessageProjection:
    if not amocrm_projection_transition_allowed(
        AmocrmProjectionStatus.PROCESSING,
        target,
    ):
        raise AmocrmProjectionStateError("AMOCRM_PROJECTION_TRANSITION_DENIED")
    values: dict[str, object] = {
        "status": target.value,
        "lease_owner": None,
        "lease_token": None,
        "lease_until": None,
        "next_attempt_at": None,
        "skip_reason": skip_reason,
        "error_code": error_code,
        "updated_at": moment,
    }
    if amocrm_message_id is not None:
        values["amocrm_message_id"] = amocrm_message_id
    stmt = (
        update(AmocrmMessageProjection)
        .where(
            AmocrmMessageProjection.id == projection_id,
            AmocrmMessageProjection.status
            == AmocrmProjectionStatus.PROCESSING.value,
            AmocrmMessageProjection.lease_token == lease_token,
            AmocrmMessageProjection.lease_version == lease_version,
        )
        .values(**values)
        .returning(AmocrmMessageProjection.id)
    )
    updated = await session.scalar(stmt)
    if updated is None:
        raise StaleAmocrmProjectionLeaseError("AMOCRM_PROJECTION_STALE_LEASE")
    row = await get_by_id(session, projection_id=projection_id)
    if row is None:
        raise RuntimeError("AMOCRM_PROJECTION_LOOKUP_FAILED")
    return row


async def complete_projected_with_lease(
    session: AsyncSession,
    *,
    projection_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    amocrm_message_id: str,
    now: datetime | None = None,
) -> AmocrmMessageProjection:
    moment = await resolve_moment(session, now)
    return await _terminate_with_lease(
        session,
        projection_id=projection_id,
        lease_token=lease_token,
        lease_version=lease_version,
        target=AmocrmProjectionStatus.PROJECTED,
        skip_reason=None,
        error_code=None,
        amocrm_message_id=amocrm_message_id,
        moment=moment,
    )


async def skip_with_lease(
    session: AsyncSession,
    *,
    projection_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    skip_reason: AmocrmProjectionSkipReason,
    now: datetime | None = None,
) -> AmocrmMessageProjection:
    moment = await resolve_moment(session, now)
    return await _terminate_with_lease(
        session,
        projection_id=projection_id,
        lease_token=lease_token,
        lease_version=lease_version,
        target=AmocrmProjectionStatus.SKIPPED,
        skip_reason=skip_reason.value,
        error_code=None,
        amocrm_message_id=None,
        moment=moment,
    )


async def fail_with_lease(
    session: AsyncSession,
    *,
    projection_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    error_code: str,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    permanent: bool = False,
    now: datetime | None = None,
) -> AmocrmMessageProjection:
    moment = await resolve_moment(session, now)
    current = await get_by_id(session, projection_id=projection_id)
    if current is None:
        raise RuntimeError("AMOCRM_PROJECTION_LOOKUP_FAILED")
    if (
        current.status != AmocrmProjectionStatus.PROCESSING.value
        or current.lease_token != lease_token
        or current.lease_version != lease_version
        or current.lease_until is None
        or current.lease_until <= moment
    ):
        raise StaleAmocrmProjectionLeaseError("AMOCRM_PROJECTION_STALE_LEASE")

    exhausted = permanent or current.attempt_count >= current.max_attempts
    target = (
        AmocrmProjectionStatus.DEAD
        if exhausted
        else AmocrmProjectionStatus.FAILED
    )
    if not amocrm_projection_transition_allowed(
        AmocrmProjectionStatus.PROCESSING,
        target,
    ):
        raise AmocrmProjectionStateError("AMOCRM_PROJECTION_TRANSITION_DENIED")
    next_attempt = None
    if target is AmocrmProjectionStatus.FAILED:
        next_attempt = moment + timedelta(seconds=retry_delay_seconds)
    stmt = (
        update(AmocrmMessageProjection)
        .where(
            AmocrmMessageProjection.id == projection_id,
            AmocrmMessageProjection.status
            == AmocrmProjectionStatus.PROCESSING.value,
            AmocrmMessageProjection.lease_token == lease_token,
            AmocrmMessageProjection.lease_version == lease_version,
        )
        .values(
            status=target.value,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            next_attempt_at=next_attempt,
            error_code=error_code[:64],
            updated_at=moment,
        )
        .returning(AmocrmMessageProjection.id)
    )
    updated = await session.scalar(stmt)
    if updated is None:
        raise StaleAmocrmProjectionLeaseError("AMOCRM_PROJECTION_STALE_LEASE")
    out = await get_by_id(session, projection_id=projection_id)
    if out is None:
        raise RuntimeError("AMOCRM_PROJECTION_LOOKUP_FAILED")
    return out
