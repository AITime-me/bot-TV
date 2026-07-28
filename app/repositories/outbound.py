from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.clock import resolve_moment
from app.models.outbox import (
    DeliveryStatus,
    DestinationType,
    OutboxMessage,
    outbound_transition_allowed,
)

DEFAULT_LEASE_SECONDS = 30
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_DELAY_SECONDS = 1


class OutboundStateError(RuntimeError):
    """Raised when an outbound status transition is not allowed."""


class StaleOutboundLeaseError(RuntimeError):
    """Raised when a worker uses an expired or superseded outbound lease."""


@dataclass(frozen=True, repr=False)
class OutboundClaim:
    outbound_id: uuid.UUID
    conversation_id: uuid.UUID
    reply_plan_id: uuid.UUID | None
    context_version: int | None
    idempotency_key: str | None
    destination_type: str
    delivery_status: str
    not_before: datetime | None
    attempt_count: int
    max_attempts: int
    lease_owner: str
    lease_token: uuid.UUID
    lease_version: int
    lease_until: datetime
    correlation_id: uuid.UUID | None
    payload_json: dict[str, Any]

    def __repr__(self) -> str:
        return (
            f"OutboundClaim(outbound_id={self.outbound_id!r}, "
            f"conversation_id={self.conversation_id!r}, "
            f"reply_plan_id={self.reply_plan_id!r}, "
            f"context_version={self.context_version!r}, "
            f"delivery_status={self.delivery_status!r}, "
            f"lease_version={self.lease_version!r}, payload=<redacted>)"
        )


def _row_to_claim(row: OutboxMessage) -> OutboundClaim:
    if row.lease_token is None or row.lease_owner is None or row.lease_until is None:
        raise RuntimeError("OUTBOUND_LEASE_INCOMPLETE")
    return OutboundClaim(
        outbound_id=row.id,
        conversation_id=row.conversation_id,
        reply_plan_id=row.reply_plan_id,
        context_version=row.context_version,
        idempotency_key=row.idempotency_key,
        destination_type=row.destination_type,
        delivery_status=row.delivery_status,
        not_before=row.not_before,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        lease_owner=row.lease_owner,
        lease_token=row.lease_token,
        lease_version=row.lease_version,
        lease_until=row.lease_until,
        correlation_id=row.correlation_id,
        payload_json=dict(row.payload_json),
    )


def synthetic_outbound_idempotency_key(reply_plan_id: uuid.UUID) -> str:
    return f"synthetic-outbound:reply-plan:{reply_plan_id}"


async def get_by_id(
    session: AsyncSession,
    *,
    outbound_id: uuid.UUID,
) -> OutboxMessage | None:
    return await session.get(OutboxMessage, outbound_id)


async def get_by_idempotency_key(
    session: AsyncSession,
    *,
    idempotency_key: str,
) -> OutboxMessage | None:
    stmt = select(OutboxMessage).where(OutboxMessage.idempotency_key == idempotency_key)
    return await session.scalar(stmt)


async def insert_synthetic_outbound_if_absent(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    reply_plan_id: uuid.UUID,
    context_version: int,
    payload_json: dict[str, Any],
    correlation_id: uuid.UUID,
    not_before: datetime,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> tuple[OutboxMessage, bool]:
    """Idempotently create SYNTHETIC_OUTBOUND for a ReplyPlan. Does not commit."""
    key = synthetic_outbound_idempotency_key(reply_plan_id)
    existing = await get_by_idempotency_key(session, idempotency_key=key)
    if existing is not None:
        return existing, False

    new_id = uuid.uuid4()
    stmt = (
        insert(OutboxMessage)
        .values(
            id=new_id,
            conversation_id=conversation_id,
            source_inbox_id=None,
            reply_plan_id=reply_plan_id,
            idempotency_key=key,
            context_version=context_version,
            destination_type=DestinationType.SYNTHETIC_OUTBOUND.value,
            payload_json=payload_json,
            delivery_status=DeliveryStatus.PENDING.value,
            not_before=not_before,
            attempt_count=0,
            max_attempts=max_attempts,
            lease_owner=None,
            lease_token=None,
            lease_version=0,
            lease_until=None,
            correlation_id=correlation_id,
        )
        .on_conflict_do_nothing(constraint="uq_outbox_idempotency_key")
        .returning(OutboxMessage.id)
    )
    inserted = await session.scalar(stmt)
    row = await get_by_idempotency_key(session, idempotency_key=key)
    if row is None:
        raise RuntimeError("OUTBOUND_LOOKUP_FAILED")
    return row, inserted is not None


async def recover_exhausted_leases(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """Terminalize expired final attempts without invoking the outbound sink."""
    moment = await resolve_moment(session, now)
    stmt = (
        update(OutboxMessage)
        .where(
            OutboxMessage.destination_type
            == DestinationType.SYNTHETIC_OUTBOUND.value,
            OutboxMessage.delivery_status == DeliveryStatus.PROCESSING.value,
            OutboxMessage.lease_until.is_not(None),
            OutboxMessage.lease_until < moment,
            OutboxMessage.attempt_count >= OutboxMessage.max_attempts,
        )
        .values(
            delivery_status=DeliveryStatus.DEAD.value,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
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
) -> OutboundClaim | None:
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
            FROM outbox_messages
            WHERE destination_type = 'SYNTHETIC_OUTBOUND'
              AND attempt_count < max_attempts
              AND (not_before IS NULL OR not_before <= :now)
              AND (
                    delivery_status = 'PENDING'
                 OR (delivery_status = 'FAILED'
                     AND (lease_until IS NULL OR lease_until < :now))
                 OR (delivery_status = 'PROCESSING'
                     AND lease_until IS NOT NULL
                     AND lease_until < :now)
              )
            ORDER BY created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE outbox_messages AS o
        SET
            delivery_status = 'PROCESSING',
            lease_owner = :worker_id,
            lease_token = CAST(:lease_token AS uuid),
            lease_version = o.lease_version + 1,
            lease_until = :lease_until,
            attempt_count = o.attempt_count + 1,
            updated_at = :now
        FROM candidate
        WHERE o.id = candidate.id
        RETURNING o.id
        """
    )
    outbound_id = await session.scalar(
        stmt,
        {
            "now": moment,
            "worker_id": worker_id,
            "lease_token": str(lease_token),
            "lease_until": lease_until,
        },
    )
    if outbound_id is None:
        return None
    row = await get_by_id(session, outbound_id=outbound_id)
    if row is None:
        raise RuntimeError("OUTBOUND_CLAIM_LOOKUP_FAILED")
    return _row_to_claim(row)


async def mark_delivered_with_lease(
    session: AsyncSession,
    *,
    outbound_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
) -> OutboxMessage:
    if not outbound_transition_allowed(
        DeliveryStatus.PROCESSING,
        DeliveryStatus.DELIVERED,
    ):
        raise OutboundStateError("OUTBOUND_TRANSITION_DENIED")
    stmt = (
        update(OutboxMessage)
        .where(
            OutboxMessage.id == outbound_id,
            OutboxMessage.delivery_status == DeliveryStatus.PROCESSING.value,
            OutboxMessage.lease_token == lease_token,
            OutboxMessage.lease_version == lease_version,
        )
        .values(
            delivery_status=DeliveryStatus.DELIVERED.value,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            updated_at=func.now(),
        )
        .returning(OutboxMessage.id)
    )
    updated = await session.scalar(stmt)
    if updated is None:
        raise StaleOutboundLeaseError("OUTBOUND_STALE_LEASE")
    row = await get_by_id(session, outbound_id=outbound_id)
    if row is None:
        raise RuntimeError("OUTBOUND_LOOKUP_FAILED")
    return row


async def fail_with_lease(
    session: AsyncSession,
    *,
    outbound_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    now: datetime | None = None,
) -> OutboxMessage:
    moment = await resolve_moment(session, now)
    row = await get_by_id(session, outbound_id=outbound_id)
    if row is None:
        raise RuntimeError("OUTBOUND_LOOKUP_FAILED")
    if (
        row.delivery_status != DeliveryStatus.PROCESSING.value
        or row.lease_token != lease_token
        or row.lease_version != lease_version
    ):
        raise StaleOutboundLeaseError("OUTBOUND_STALE_LEASE")

    if row.attempt_count >= row.max_attempts:
        target = DeliveryStatus.DEAD
        not_before = row.not_before
    else:
        target = DeliveryStatus.FAILED
        not_before = moment + timedelta(seconds=retry_delay_seconds)

    if not outbound_transition_allowed(DeliveryStatus.PROCESSING, target):
        raise OutboundStateError("OUTBOUND_TRANSITION_DENIED")

    stmt = (
        update(OutboxMessage)
        .where(
            OutboxMessage.id == outbound_id,
            OutboxMessage.delivery_status == DeliveryStatus.PROCESSING.value,
            OutboxMessage.lease_token == lease_token,
            OutboxMessage.lease_version == lease_version,
        )
        .values(
            delivery_status=target.value,
            not_before=not_before,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            updated_at=moment,
        )
        .returning(OutboxMessage.id)
    )
    updated = await session.scalar(stmt)
    if updated is None:
        raise StaleOutboundLeaseError("OUTBOUND_STALE_LEASE")
    refreshed = await get_by_id(session, outbound_id=outbound_id)
    if refreshed is None:
        raise RuntimeError("OUTBOUND_LOOKUP_FAILED")
    return refreshed
