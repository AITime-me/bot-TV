from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Channel
from app.models.inbox import (
    InboxMessage,
    MessageDirection,
    MessageType,
    ProcessingStatus,
)
from app.models.outbox import DeliveryStatus, DestinationType, OutboxMessage


async def get_inbox_by_external(
    session: AsyncSession,
    *,
    channel: Channel,
    external_message_id: str,
) -> InboxMessage | None:
    stmt = select(InboxMessage).where(
        InboxMessage.channel == channel.value,
        InboxMessage.external_message_id == external_message_id,
    )
    return await session.scalar(stmt)


async def insert_inbox_if_absent(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    channel: Channel,
    external_message_id: str,
    conversation_event_seq: int,
    payload_json: dict[str, Any],
    received_at: datetime,
) -> tuple[InboxMessage, bool]:
    """Insert inbox row or return existing. Does not commit.

    Assumes READ COMMITTED (see app.db.session). Returns (message, created).
    """
    existing = await get_inbox_by_external(
        session,
        channel=channel,
        external_message_id=external_message_id,
    )
    if existing is not None:
        return existing, False

    new_id = uuid.uuid4()
    stmt = (
        insert(InboxMessage)
        .values(
            id=new_id,
            conversation_id=conversation_id,
            channel=channel.value,
            external_message_id=external_message_id,
            conversation_event_seq=conversation_event_seq,
            direction=MessageDirection.INBOUND.value,
            message_type=MessageType.TEXT.value,
            payload_json=payload_json,
            received_at=received_at,
            processing_status=ProcessingStatus.RECEIVED.value,
            error_code=None,
        )
        .on_conflict_do_nothing(
            constraint="uq_inbox_channel_external_message_id",
        )
        .returning(InboxMessage.id)
    )
    inserted = await session.scalar(stmt)
    message = await get_inbox_by_external(
        session,
        channel=channel,
        external_message_id=external_message_id,
    )
    if message is None:
        raise RuntimeError("INBOX_LOOKUP_FAILED")
    return message, inserted is not None


async def get_internal_draft_for_inbox(
    session: AsyncSession,
    *,
    source_inbox_id: uuid.UUID,
) -> OutboxMessage | None:
    """Return the single INTERNAL_DRAFT for an inbox, if present."""
    stmt = select(OutboxMessage).where(
        OutboxMessage.source_inbox_id == source_inbox_id,
        OutboxMessage.destination_type == DestinationType.INTERNAL_DRAFT.value,
    )
    return await session.scalar(stmt)


async def create_internal_draft_outbox(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    source_inbox_id: uuid.UUID,
    payload_json: dict[str, Any],
) -> tuple[OutboxMessage, bool]:
    """Idempotently create INTERNAL_DRAFT outbox with PENDING status.

    Does not commit. Uses INSERT ... ON CONFLICT DO NOTHING then SELECT.
    Assumes READ COMMITTED (see app.db.session). DB unique constraint
    uq_outbox_source_inbox_destination prevents a second draft per inbox.
    No SENT status and no transport/sender path exist in this stage.

    Returns (outbox, created).
    """
    existing = await get_internal_draft_for_inbox(
        session,
        source_inbox_id=source_inbox_id,
    )
    if existing is not None:
        return existing, False

    new_id = uuid.uuid4()
    stmt = (
        insert(OutboxMessage)
        .values(
            id=new_id,
            conversation_id=conversation_id,
            source_inbox_id=source_inbox_id,
            destination_type=DestinationType.INTERNAL_DRAFT.value,
            payload_json=payload_json,
            delivery_status=DeliveryStatus.PENDING.value,
        )
        .on_conflict_do_nothing(
            constraint="uq_outbox_source_inbox_destination",
        )
        .returning(OutboxMessage.id)
    )
    inserted = await session.scalar(stmt)
    outbox = await get_internal_draft_for_inbox(
        session,
        source_inbox_id=source_inbox_id,
    )
    if outbox is None:
        raise RuntimeError("OUTBOX_LOOKUP_FAILED")
    return outbox, inserted is not None
