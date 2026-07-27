from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Channel, Conversation, ConversationStatus


async def get_by_channel_external(
    session: AsyncSession,
    *,
    channel: Channel,
    external_conversation_id: str,
) -> Conversation | None:
    stmt = select(Conversation).where(
        Conversation.channel == channel.value,
        Conversation.external_conversation_id == external_conversation_id,
    )
    return await session.scalar(stmt)


async def get_or_create(
    session: AsyncSession,
    *,
    channel: Channel,
    external_conversation_id: str,
) -> tuple[Conversation, bool]:
    """Return conversation and whether it was created in this call.

    Does not commit. Uses INSERT ... ON CONFLICT DO NOTHING then SELECT.
    Assumes READ COMMITTED (see app.db.session).
    """
    existing = await get_by_channel_external(
        session,
        channel=channel,
        external_conversation_id=external_conversation_id,
    )
    if existing is not None:
        return existing, False

    new_id = uuid.uuid4()
    stmt = (
        insert(Conversation)
        .values(
            id=new_id,
            channel=channel.value,
            external_conversation_id=external_conversation_id,
            status=ConversationStatus.OPEN.value,
            manager_takeover_at=None,
        )
        .on_conflict_do_nothing(
            constraint="uq_conversations_channel_external_id",
        )
        .returning(Conversation.id)
    )
    inserted = await session.scalar(stmt)
    conversation = await get_by_channel_external(
        session,
        channel=channel,
        external_conversation_id=external_conversation_id,
    )
    if conversation is None:
        raise RuntimeError("CONVERSATION_LOOKUP_FAILED")
    return conversation, inserted is not None
