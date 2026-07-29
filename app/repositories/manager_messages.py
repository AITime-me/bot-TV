from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Channel
from app.models.manager_message import ManagerMessage, ManagerMessageStatus


async def get_by_external(
    session: AsyncSession,
    *,
    channel: Channel,
    external_message_id: str,
) -> ManagerMessage | None:
    stmt = select(ManagerMessage).where(
        ManagerMessage.channel == channel.value,
        ManagerMessage.external_message_id == external_message_id,
    )
    return await session.scalar(stmt)


async def insert_quarantined_if_absent(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    channel: Channel,
    external_message_id: str,
    provider_sequence: int | None,
    provider_occurred_at: datetime | None,
    body_text: str,
    classification_reason: str,
) -> tuple[ManagerMessage, bool]:
    """Reserve provider identity before any Conversation side effect.

    The provisional QUARANTINED row makes cross-conversation duplicate races
    harmless. The caller classifies a newly inserted row under the already-held
    Conversation lock and updates it to APPLIED or STALE in the same
    transaction.
    """
    stmt = (
        insert(ManagerMessage)
        .values(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            channel=channel.value,
            external_message_id=external_message_id,
            provider_sequence=provider_sequence,
            provider_occurred_at=provider_occurred_at,
            body_text=body_text,
            status=ManagerMessageStatus.QUARANTINED.value,
            conversation_event_seq=None,
            classification_reason=classification_reason[:64],
        )
        .on_conflict_do_nothing(
            constraint="uq_manager_messages_channel_external_message_id",
        )
        .returning(ManagerMessage.id)
    )
    inserted_id = await session.scalar(stmt)
    message = await get_by_external(
        session,
        channel=channel,
        external_message_id=external_message_id,
    )
    if message is None:
        raise RuntimeError("MANAGER_MESSAGE_LOOKUP_FAILED")
    return message, inserted_id is not None


async def mark_applied(
    session: AsyncSession,
    *,
    message: ManagerMessage,
    conversation_event_seq: int,
) -> ManagerMessage:
    message.status = ManagerMessageStatus.APPLIED.value
    message.conversation_event_seq = conversation_event_seq
    message.classification_reason = "CHRONOLOGICALLY_NEW"
    await session.flush()
    return message


async def mark_stale(
    session: AsyncSession,
    *,
    message: ManagerMessage,
    reason: str = "PROVIDER_SEQUENCE_NOT_NEWER",
) -> ManagerMessage:
    message.status = ManagerMessageStatus.STALE.value
    message.conversation_event_seq = None
    message.classification_reason = reason[:64]
    await session.flush()
    return message
