from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.clock import resolve_moment
from app.models.conversation import Channel
from app.models.ingress import (
    IngressChannel,
    IngressEvent,
    IngressEventType,
    IngressStatus,
    ingress_transition_allowed,
)

IngressChannelLike = Channel | IngressChannel

DEFAULT_LEASE_SECONDS = 30
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_DELAY_SECONDS = 1


class IngressStateError(RuntimeError):
    """Raised when a status transition is not allowed."""


class StaleIngressLeaseError(RuntimeError):
    """Raised when a worker uses an expired or superseded lease token/version."""


@dataclass(frozen=True, repr=False)
class IngressClaim:
    event_id: uuid.UUID
    channel: str
    external_event_id: str
    external_conversation_id: str
    event_type: str
    status: str
    attempt_count: int
    max_attempts: int
    lease_owner: str
    lease_token: uuid.UUID
    lease_version: int
    lease_until: datetime
    correlation_id: uuid.UUID
    envelope_json: dict[str, Any]

    def __repr__(self) -> str:
        return (
            f"IngressClaim(event_id={self.event_id!r}, "
            f"channel={self.channel!r}, "
            f"external_event_id={self.external_event_id!r}, "
            f"status={self.status!r}, attempt_count={self.attempt_count!r}, "
            f"lease_owner={self.lease_owner!r}, "
            f"lease_version={self.lease_version!r}, "
            f"correlation_id={self.correlation_id!r}, envelope=<redacted>)"
        )


def _row_to_claim(row: IngressEvent) -> IngressClaim:
    if row.lease_token is None or row.lease_owner is None or row.lease_until is None:
        raise RuntimeError("INGRESS_LEASE_INCOMPLETE")
    return IngressClaim(
        event_id=row.id,
        channel=row.channel,
        external_event_id=row.external_event_id,
        external_conversation_id=row.external_conversation_id,
        event_type=row.event_type,
        status=row.status,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        lease_owner=row.lease_owner,
        lease_token=row.lease_token,
        lease_version=row.lease_version,
        lease_until=row.lease_until,
        correlation_id=row.correlation_id,
        envelope_json=dict(row.envelope_json),
    )


async def get_by_channel_external(
    session: AsyncSession,
    *,
    channel: IngressChannelLike,
    external_event_id: str,
) -> IngressEvent | None:
    stmt = select(IngressEvent).where(
        IngressEvent.channel == channel.value,
        IngressEvent.external_event_id == external_event_id,
    )
    return await session.scalar(stmt)


async def get_by_id(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
) -> IngressEvent | None:
    return await session.get(IngressEvent, event_id)


async def insert_if_absent(
    session: AsyncSession,
    *,
    channel: IngressChannelLike,
    external_event_id: str,
    external_conversation_id: str,
    event_type: IngressEventType,
    envelope_json: dict[str, Any],
    correlation_id: uuid.UUID,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> tuple[IngressEvent, bool]:
    """Idempotently insert RECEIVED ingress row. Does not commit.

    Uses INSERT ... ON CONFLICT DO NOTHING on
    uq_ingress_channel_external_event_id, then SELECT. Assumes READ COMMITTED.
    """
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    _assert_channel_event_pairing(channel=channel, event_type=event_type)

    existing = await get_by_channel_external(
        session,
        channel=channel,
        external_event_id=external_event_id,
    )
    if existing is not None:
        return existing, False

    new_id = uuid.uuid4()
    stmt = (
        insert(IngressEvent)
        .values(
            id=new_id,
            channel=channel.value,
            external_event_id=external_event_id,
            external_conversation_id=external_conversation_id,
            event_type=event_type.value,
            status=IngressStatus.RECEIVED.value,
            attempt_count=0,
            max_attempts=max_attempts,
            next_attempt_at=None,
            lease_owner=None,
            lease_token=None,
            lease_version=0,
            lease_until=None,
            correlation_id=correlation_id,
            envelope_json=envelope_json,
            error_code=None,
        )
        .on_conflict_do_nothing(
            constraint="uq_ingress_channel_external_event_id",
        )
        .returning(IngressEvent.id)
    )
    inserted = await session.scalar(stmt)
    event = await get_by_channel_external(
        session,
        channel=channel,
        external_event_id=external_event_id,
    )
    if event is None:
        raise RuntimeError("INGRESS_LOOKUP_FAILED")
    return event, inserted is not None


def _assert_channel_event_pairing(
    *,
    channel: IngressChannelLike,
    event_type: IngressEventType,
) -> None:
    """Runtime mirror of ck_ingress_channel_event_pairing (fail closed)."""

    channel_value = channel.value
    if channel_value == IngressChannel.AMOCRM.value:
        if event_type is not IngressEventType.AMOCRM_MANAGER_MESSAGE:
            raise ValueError("INGRESS_CHANNEL_EVENT_MISMATCH")
        return
    if channel_value == IngressChannel.SYNTHETIC.value:
        if event_type is not IngressEventType.SYNTHETIC_MESSAGE:
            raise ValueError("INGRESS_CHANNEL_EVENT_MISMATCH")
        return
    if channel_value == IngressChannel.VK.value:
        if event_type not in {
            IngressEventType.VK_CLIENT_MESSAGE,
            IngressEventType.VK_CLIENT_MESSAGE_REPLY,
        }:
            raise ValueError("INGRESS_CHANNEL_EVENT_MISMATCH")
        return
    raise ValueError("INGRESS_CHANNEL_EVENT_MISMATCH")


async def recover_exhausted_leases(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """Terminalize expired final attempts without running business processing."""
    moment = await resolve_moment(session, now)
    stmt = (
        update(IngressEvent)
        .where(
            IngressEvent.status == IngressStatus.PROCESSING.value,
            IngressEvent.lease_until.is_not(None),
            IngressEvent.lease_until < moment,
            IngressEvent.attempt_count >= IngressEvent.max_attempts,
        )
        .values(
            status=IngressStatus.DEAD.value,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            next_attempt_at=None,
            error_code="LEASE_ATTEMPTS_EXHAUSTED",
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
) -> IngressClaim | None:
    """Atomically claim one claimable ingress event for a worker.

    Uses FOR UPDATE SKIP LOCKED so concurrent workers cannot claim the same
    row. Issues a new lease_token and increments lease_version (fencing).
    Does not commit.
    """
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")

    moment = await resolve_moment(session, now)
    await recover_exhausted_leases(session, now=moment)
    lease_until = moment + timedelta(seconds=lease_seconds)
    lease_token = uuid.uuid4()

    # Candidate selection and update run in one statement so the row lock from
    # SKIP LOCKED is held until the UPDATE assigns the new fencing token.
    stmt = text(
        """
        WITH candidate AS (
            SELECT id
            FROM ingress_events
            WHERE attempt_count < max_attempts
              AND (
                    status = 'RECEIVED'
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
        UPDATE ingress_events AS e
        SET
            status = 'PROCESSING',
            lease_owner = :worker_id,
            lease_token = CAST(:lease_token AS uuid),
            lease_version = e.lease_version + 1,
            lease_until = :lease_until,
            attempt_count = e.attempt_count + 1,
            next_attempt_at = NULL,
            error_code = NULL,
            updated_at = :now
        FROM candidate
        WHERE e.id = candidate.id
        RETURNING e.id
        """
    )
    event_id = await session.scalar(
        stmt,
        {
            "now": moment,
            "worker_id": worker_id,
            "lease_token": str(lease_token),
            "lease_until": lease_until,
        },
    )
    if event_id is None:
        return None
    event = await get_by_id(session, event_id=event_id)
    if event is None:
        raise RuntimeError("INGRESS_CLAIM_LOOKUP_FAILED")
    return _row_to_claim(event)


async def complete_with_lease(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
) -> IngressEvent:
    """Mark PROCESSING→PROCESSED only when fencing token/version match."""
    moment = await resolve_moment(session, None)
    if not ingress_transition_allowed(
        IngressStatus.PROCESSING,
        IngressStatus.PROCESSED,
    ):
        raise IngressStateError("INGRESS_TRANSITION_DENIED")

    stmt = (
        update(IngressEvent)
        .where(
            IngressEvent.id == event_id,
            IngressEvent.status == IngressStatus.PROCESSING.value,
            IngressEvent.lease_token == lease_token,
            IngressEvent.lease_version == lease_version,
        )
        .values(
            status=IngressStatus.PROCESSED.value,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            next_attempt_at=None,
            error_code=None,
            updated_at=moment,
        )
        .returning(IngressEvent.id)
    )
    updated_id = await session.scalar(stmt)
    if updated_id is None:
        raise StaleIngressLeaseError("INGRESS_STALE_LEASE")
    event = await get_by_id(session, event_id=event_id)
    if event is None:
        raise RuntimeError("INGRESS_LOOKUP_FAILED")
    return event


async def fail_with_lease(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    error_code: str,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    now: datetime | None = None,
) -> IngressEvent:
    """Mark PROCESSING→FAILED or PROCESSING→DEAD under fencing control."""
    moment = await resolve_moment(session, now)
    event = await get_by_id(session, event_id=event_id)
    if event is None:
        raise RuntimeError("INGRESS_LOOKUP_FAILED")
    if (
        event.status != IngressStatus.PROCESSING.value
        or event.lease_token != lease_token
        or event.lease_version != lease_version
    ):
        raise StaleIngressLeaseError("INGRESS_STALE_LEASE")

    if event.attempt_count >= event.max_attempts:
        target = IngressStatus.DEAD
        next_attempt_at = None
    else:
        target = IngressStatus.FAILED
        next_attempt_at = moment + timedelta(seconds=retry_delay_seconds)

    if not ingress_transition_allowed(IngressStatus.PROCESSING, target):
        raise IngressStateError("INGRESS_TRANSITION_DENIED")

    stmt = (
        update(IngressEvent)
        .where(
            IngressEvent.id == event_id,
            IngressEvent.status == IngressStatus.PROCESSING.value,
            IngressEvent.lease_token == lease_token,
            IngressEvent.lease_version == lease_version,
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
        .returning(IngressEvent.id)
    )
    updated_id = await session.scalar(stmt)
    if updated_id is None:
        raise StaleIngressLeaseError("INGRESS_STALE_LEASE")
    refreshed = await get_by_id(session, event_id=event_id)
    if refreshed is None:
        raise RuntimeError("INGRESS_LOOKUP_FAILED")
    return refreshed
