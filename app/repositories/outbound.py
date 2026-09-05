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
    manager_epoch: int
    event_seq_hwm: int
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


def _db_uuid(value: object) -> uuid.UUID:
    """Canonical stdlib UUID from ORM/asyncpg driver values.

    asyncpg may return ``pgproto.UUID`` (subclass of ``uuid.UUID``). Transport
    contracts use ``type(x) is uuid.UUID``, so normalize at the repository
    claim boundary — not in senders or helpers.
    """

    if type(value) is uuid.UUID:
        return value
    if isinstance(value, uuid.UUID):
        try:
            return uuid.UUID(int=value.int)
        except Exception:
            raise RuntimeError("OUTBOUND_UUID_INVALID") from None
    if type(value) is str:
        if not value or any(ch.isspace() for ch in value):
            raise RuntimeError("OUTBOUND_UUID_INVALID") from None
        try:
            return uuid.UUID(value)
        except ValueError:
            raise RuntimeError("OUTBOUND_UUID_INVALID") from None
    raise RuntimeError("OUTBOUND_UUID_INVALID") from None


def _db_uuid_optional(value: object) -> uuid.UUID | None:
    if value is None:
        return None
    return _db_uuid(value)


def _row_to_claim(row: OutboxMessage) -> OutboundClaim:
    if row.lease_token is None or row.lease_owner is None or row.lease_until is None:
        raise RuntimeError("OUTBOUND_LEASE_INCOMPLETE")
    return OutboundClaim(
        outbound_id=_db_uuid(row.id),
        conversation_id=_db_uuid(row.conversation_id),
        reply_plan_id=_db_uuid_optional(row.reply_plan_id),
        context_version=row.context_version,
        manager_epoch=row.manager_epoch,
        event_seq_hwm=row.event_seq_hwm,
        idempotency_key=row.idempotency_key,
        destination_type=row.destination_type,
        delivery_status=row.delivery_status,
        not_before=row.not_before,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        lease_owner=row.lease_owner,
        lease_token=_db_uuid(row.lease_token),
        lease_version=row.lease_version,
        lease_until=row.lease_until,
        correlation_id=_db_uuid_optional(row.correlation_id),
        payload_json=dict(row.payload_json),
    )


def synthetic_outbound_idempotency_key(reply_plan_id: uuid.UUID) -> str:
    return f"synthetic-outbound:reply-plan:{reply_plan_id}"


def vk_client_outbound_idempotency_key(reply_plan_id: uuid.UUID) -> str:
    return f"vk-client-outbound:reply-plan:{reply_plan_id}"


_CLAIMABLE_DESTINATIONS = (
    DestinationType.SYNTHETIC_OUTBOUND.value,
    DestinationType.VK_CLIENT_OUTBOUND.value,
)


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
    manager_epoch: int = 0,
    event_seq_hwm: int = 0,
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
            manager_epoch=manager_epoch,
            event_seq_hwm=event_seq_hwm,
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


async def insert_vk_client_outbound_if_absent(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    reply_plan_id: uuid.UUID,
    context_version: int,
    payload_json: dict[str, Any],
    correlation_id: uuid.UUID,
    not_before: datetime,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    manager_epoch: int = 0,
    event_seq_hwm: int = 0,
) -> tuple[OutboxMessage, bool]:
    """Idempotently create VK_CLIENT_OUTBOUND for a ReplyPlan. Does not commit."""

    key = vk_client_outbound_idempotency_key(reply_plan_id)
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
            manager_epoch=manager_epoch,
            event_seq_hwm=event_seq_hwm,
            destination_type=DestinationType.VK_CLIENT_OUTBOUND.value,
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


async def cancel_unadmitted_for_manager_message(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
) -> int:
    """Cancel synthetic outbound that has not crossed durable admission."""
    return await cancel_unadmitted_for_conversation(
        session,
        conversation_id=conversation_id,
    )


async def cancel_unadmitted_for_conversation(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
) -> int:
    """Cancel every claimable outbound still on the reversible side of admission."""
    stmt = (
        update(OutboxMessage)
        .where(
            OutboxMessage.conversation_id == conversation_id,
            OutboxMessage.destination_type.in_(_CLAIMABLE_DESTINATIONS),
            OutboxMessage.delivery_status.in_(
                (
                    DeliveryStatus.PENDING.value,
                    DeliveryStatus.PROCESSING.value,
                    DeliveryStatus.FAILED.value,
                )
            ),
        )
        .values(
            delivery_status=DeliveryStatus.CANCELLED.value,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            updated_at=func.now(),
        )
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


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
            OutboxMessage.destination_type.in_(_CLAIMABLE_DESTINATIONS),
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
            WHERE destination_type IN ('SYNTHETIC_OUTBOUND', 'VK_CLIENT_OUTBOUND')
              AND (
                    delivery_status = 'ADMITTED'
                 OR attempt_count < max_attempts
              )
              AND (not_before IS NULL OR not_before <= :now)
              AND (
                    delivery_status = 'PENDING'
                 OR (delivery_status = 'FAILED'
                     AND (lease_until IS NULL OR lease_until < :now))
                 OR (delivery_status = 'PROCESSING'
                     AND lease_until IS NOT NULL
                     AND lease_until < :now)
                 OR (delivery_status = 'ADMITTED'
                     AND (lease_until IS NULL OR lease_until < :now))
              )
            ORDER BY created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE outbox_messages AS o
        SET
            delivery_status = CASE
                WHEN o.delivery_status = 'ADMITTED' THEN 'ADMITTED'
                ELSE 'PROCESSING'
            END,
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


async def mark_admitted_with_lease(
    session: AsyncSession,
    *,
    outbound_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    now: datetime | None = None,
) -> OutboxMessage:
    """Commit the irreversible admission point while retaining the lease."""
    moment = await resolve_moment(session, now)
    if not outbound_transition_allowed(
        DeliveryStatus.PROCESSING,
        DeliveryStatus.ADMITTED,
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
            delivery_status=DeliveryStatus.ADMITTED.value,
            admitted_at=moment,
            updated_at=moment,
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


async def mark_delivered_with_lease(
    session: AsyncSession,
    *,
    outbound_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    now: datetime | None = None,
    provider_message_id: int | None = None,
) -> OutboxMessage:
    """Finalize a previously admitted outbound after the sink returns success."""
    moment = await resolve_moment(session, now)
    if not outbound_transition_allowed(
        DeliveryStatus.ADMITTED,
        DeliveryStatus.DELIVERED,
    ):
        raise OutboundStateError("OUTBOUND_TRANSITION_DENIED")
    values: dict[str, object] = {
        "delivery_status": DeliveryStatus.DELIVERED.value,
        "lease_owner": None,
        "lease_token": None,
        "lease_until": None,
        "updated_at": moment,
    }
    if (
        type(provider_message_id) is int
        and not isinstance(provider_message_id, bool)
        and provider_message_id > 0
    ):
        values["provider_message_id"] = provider_message_id
    stmt = (
        update(OutboxMessage)
        .where(
            OutboxMessage.id == outbound_id,
            OutboxMessage.delivery_status == DeliveryStatus.ADMITTED.value,
            OutboxMessage.admitted_at.is_not(None),
            OutboxMessage.lease_token == lease_token,
            OutboxMessage.lease_version == lease_version,
        )
        .values(**values)
        .returning(OutboxMessage.id)
    )
    updated = await session.scalar(stmt)
    if updated is None:
        raise StaleOutboundLeaseError("OUTBOUND_STALE_LEASE")
    row = await get_by_id(session, outbound_id=outbound_id)
    if row is None:
        raise RuntimeError("OUTBOUND_LOOKUP_FAILED")
    return row


async def set_vk_provider_message_id_with_lease(
    session: AsyncSession,
    *,
    outbound_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    provider_message_id: int,
    now: datetime | None = None,
) -> OutboxMessage:
    """Persist VK provider message id on ADMITTED row before DELIVERED (race close)."""

    if (
        type(provider_message_id) is not int
        or isinstance(provider_message_id, bool)
        or provider_message_id <= 0
    ):
        raise ValueError("PROVIDER_MESSAGE_ID_INVALID")
    moment = await resolve_moment(session, now)
    stmt = (
        update(OutboxMessage)
        .where(
            OutboxMessage.id == outbound_id,
            OutboxMessage.destination_type
            == DestinationType.VK_CLIENT_OUTBOUND.value,
            OutboxMessage.delivery_status == DeliveryStatus.ADMITTED.value,
            OutboxMessage.admitted_at.is_not(None),
            OutboxMessage.lease_token == lease_token,
            OutboxMessage.lease_version == lease_version,
            OutboxMessage.provider_message_id.is_(None),
        )
        .values(
            provider_message_id=provider_message_id,
            updated_at=moment,
        )
        .returning(OutboxMessage.id)
    )
    updated = await session.scalar(stmt)
    if updated is None:
        row = await get_by_id(session, outbound_id=outbound_id)
        if (
            row is not None
            and row.provider_message_id == provider_message_id
            and row.lease_token == lease_token
            and row.lease_version == lease_version
        ):
            return row
        raise StaleOutboundLeaseError("OUTBOUND_STALE_LEASE")
    row = await get_by_id(session, outbound_id=outbound_id)
    if row is None:
        raise RuntimeError("OUTBOUND_LOOKUP_FAILED")
    return row


async def find_vk_outbound_by_provider_message_id(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    provider_message_id: int,
) -> OutboxMessage | None:
    if (
        type(provider_message_id) is not int
        or isinstance(provider_message_id, bool)
        or provider_message_id <= 0
    ):
        return None
    stmt = select(OutboxMessage).where(
        OutboxMessage.conversation_id == conversation_id,
        OutboxMessage.destination_type == DestinationType.VK_CLIENT_OUTBOUND.value,
        OutboxMessage.provider_message_id == provider_message_id,
    )
    return await session.scalar(stmt)


async def find_vk_outbound_by_id(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    outbound_id: uuid.UUID,
) -> OutboxMessage | None:
    row = await get_by_id(session, outbound_id=outbound_id)
    if row is None:
        return None
    if row.conversation_id != conversation_id:
        return None
    if row.destination_type != DestinationType.VK_CLIENT_OUTBOUND.value:
        return None
    return row


async def has_admitted_vk_outbound_without_provider_id(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
) -> bool:
    """True when a send may still be racing receipt persist."""

    stmt = (
        select(OutboxMessage.id)
        .where(
            OutboxMessage.conversation_id == conversation_id,
            OutboxMessage.destination_type
            == DestinationType.VK_CLIENT_OUTBOUND.value,
            OutboxMessage.delivery_status == DeliveryStatus.ADMITTED.value,
            OutboxMessage.provider_message_id.is_(None),
        )
        .limit(1)
    )
    return await session.scalar(stmt) is not None


async def fail_admitted_delivery_with_lease(
    session: AsyncSession,
    *,
    outbound_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    permanent: bool,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    now: datetime | None = None,
) -> OutboxMessage:
    """Retry or terminalize sink delivery without crossing back before ADMITTED."""
    moment = await resolve_moment(session, now)
    row = await get_by_id(session, outbound_id=outbound_id)
    if row is None:
        raise RuntimeError("OUTBOUND_LOOKUP_FAILED")
    if (
        row.delivery_status != DeliveryStatus.ADMITTED.value
        or row.admitted_at is None
        or row.lease_token != lease_token
        or row.lease_version != lease_version
    ):
        raise StaleOutboundLeaseError("OUTBOUND_STALE_LEASE")

    terminal = permanent or row.attempt_count >= row.max_attempts
    target = DeliveryStatus.DEAD if terminal else DeliveryStatus.ADMITTED
    if target is DeliveryStatus.DEAD and not outbound_transition_allowed(
        DeliveryStatus.ADMITTED,
        target,
    ):
        raise OutboundStateError("OUTBOUND_TRANSITION_DENIED")
    retry_at = row.not_before if terminal else moment + timedelta(
        seconds=retry_delay_seconds
    )
    stmt = (
        update(OutboxMessage)
        .where(
            OutboxMessage.id == outbound_id,
            OutboxMessage.delivery_status == DeliveryStatus.ADMITTED.value,
            OutboxMessage.admitted_at.is_not(None),
            OutboxMessage.lease_token == lease_token,
            OutboxMessage.lease_version == lease_version,
        )
        .values(
            delivery_status=target.value,
            not_before=retry_at,
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
