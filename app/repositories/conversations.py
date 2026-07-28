from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.clock import resolve_moment
from app.models.conversation import (
    Channel,
    Conversation,
    ConversationOwnership,
    ConversationStatus,
)

# Lock order for every transaction that touches a dialog subtree:
#   conversations -> inbox_messages -> reply_plans -> outbox_messages
# The conversation row must be locked with lock_for_update() before any INSERT
# whose foreign key silently takes FOR KEY SHARE on that same row. Escalating
# KEY SHARE to FOR UPDATE from two concurrent transactions deadlocks.


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


async def get_by_id_for_update(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
) -> Conversation | None:
    """Lock one conversation row for the remainder of the transaction.

    ``populate_existing`` is mandatory: without it SQLAlchemy keeps the
    attributes an earlier statement loaded, so a waiter that acquires the lock
    after a concurrent commit would still read the pre-lock ``context_version``.
    """
    stmt = (
        select(Conversation)
        .where(Conversation.id == conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def lock_for_update(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
) -> Conversation:
    """Take the dialog's exclusive row lock — the first lock of the transaction.

    Every writer of a dialog subtree funnels through this single point so all
    concurrent transactions acquire conversation locks in the same order.
    """
    conversation = await get_by_id_for_update(session, conversation_id=conversation_id)
    if conversation is None:
        raise RuntimeError("CONVERSATION_LOOKUP_FAILED")
    return conversation


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
            ownership=ConversationOwnership.BOT.value,
            context_version=0,
            last_client_activity_at=None,
            manager_takeover_at=None,
            active_reply_plan_id=None,
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


async def bump_context_for_new_message(
    session: AsyncSession,
    *,
    conversation: Conversation,
    activity_at: datetime,
) -> Conversation:
    """Atomically increment context_version. Does not commit.

    The caller must already hold the row lock from lock_for_update(); this
    function never escalates a lock. The increment is evaluated by PostgreSQL on
    the locked row, so concurrent messages of one dialog receive strictly
    monotonic versions even if a waiter loaded the row before the lock.
    """
    stmt = (
        update(Conversation)
        .where(Conversation.id == conversation.id)
        .values(
            context_version=Conversation.context_version + 1,
            last_client_activity_at=activity_at,
            updated_at=func.now(),
        )
        .returning(Conversation.context_version)
        .execution_options(synchronize_session=False)
    )
    bumped_version = await session.scalar(stmt)
    if bumped_version is None:
        raise RuntimeError("CONVERSATION_LOOKUP_FAILED")
    await session.refresh(conversation)
    return conversation


async def apply_manager_takeover(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    now: datetime | None = None,
) -> tuple[Conversation, bool]:
    """Idempotently mark conversation as manager-owned under row lock.

    Returns (conversation, changed).
    """
    moment = await resolve_moment(session, now)
    conversation = await lock_for_update(session, conversation_id=conversation_id)
    already = (
        conversation.ownership == ConversationOwnership.MANAGER.value
        and conversation.manager_takeover_at is not None
        and conversation.status == ConversationStatus.HANDOFF.value
    )
    if already:
        return conversation, False
    conversation.ownership = ConversationOwnership.MANAGER.value
    conversation.status = ConversationStatus.HANDOFF.value
    conversation.manager_takeover_at = moment
    conversation.active_reply_plan_id = None
    conversation.updated_at = moment
    await session.flush()
    return conversation, True
