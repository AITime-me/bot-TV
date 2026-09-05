from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.clock import resolve_moment
from app.models.conversation import (
    Channel,
    Conversation,
    ConversationOwnership,
    ConversationStatus,
    HANDOFF_QUARANTINE_CLEAR_PATH_MANAGER_MESSAGE,
    HANDOFF_QUARANTINE_CLEAR_PATHS,
    HANDOFF_QUARANTINE_REASONS,
    HandoffState,
    handoff_expiry_quarantine_is_active,
)
from app.models.conversation_ops_event import (
    ConversationOpsEvent,
    ConversationOpsEventType,
)

# Lock order for every transaction that touches a dialog subtree:
#   conversations -> inbox_messages/manager_messages -> reply_plans
#   -> outbox_messages -> amocrm_mirror_jobs
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


async def claim_next_due_handoff(
    session: AsyncSession,
) -> Conversation | None:
    """Lock one due handoff using PostgreSQL time and SKIP LOCKED.

    No lease is necessary: the state transition is a single transaction. A
    process crash before commit releases the row lock and leaves the durable
    HANDOFF row due for the next worker; after commit it is no longer eligible.
    """
    stmt = (
        select(Conversation)
        .where(
            Conversation.status == ConversationStatus.HANDOFF.value,
            Conversation.ownership == ConversationOwnership.MANAGER.value,
            Conversation.handoff_state.in_(
                (
                    HandoffState.HUMAN_ACTIVE.value,
                    HandoffState.HUMAN_PAUSE.value,
                )
            ),
            Conversation.handoff_deadline_at.is_not(None),
            Conversation.handoff_deadline_at <= func.statement_timestamp(),
            or_(
                Conversation.handoff_quarantined_at.is_(None),
                Conversation.handoff_quarantine_cleared_at.is_not(None),
            ),
        )
        .order_by(Conversation.handoff_deadline_at, Conversation.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def return_due_handoff_to_bot(
    session: AsyncSession,
    *,
    conversation: Conversation,
    moment: datetime,
    active_reply_plan_id: uuid.UUID | None,
) -> Conversation:
    """Atomically return an already locked, still-due handoff to BOT_ACTIVE."""
    stmt = (
        update(Conversation)
        .where(
            Conversation.id == conversation.id,
            Conversation.status == ConversationStatus.HANDOFF.value,
            Conversation.ownership == ConversationOwnership.MANAGER.value,
            Conversation.handoff_state == conversation.handoff_state,
            Conversation.handoff_deadline_at.is_not(None),
            Conversation.handoff_deadline_at <= moment,
        )
        .values(
            status=ConversationStatus.OPEN.value,
            ownership=ConversationOwnership.BOT.value,
            handoff_state=HandoffState.BOT_ACTIVE.value,
            handoff_deadline_at=None,
            human_pause_anchor_at=None,
            manager_takeover_at=None,
            active_reply_plan_id=active_reply_plan_id,
            updated_at=moment,
        )
        .returning(Conversation.id)
        .execution_options(synchronize_session=False)
    )
    updated = await session.scalar(stmt)
    if updated is None:
        raise RuntimeError("HANDOFF_EXPIRY_STALE_CLAIM")
    await session.refresh(conversation)
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
            handoff_state=HandoffState.BOT_ACTIVE.value,
            manager_epoch=0,
            current_event_seq=0,
            manager_sequence_hwm=None,
            handoff_deadline_at=None,
            human_pause_anchor_at=None,
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


async def allocate_next_event_seq(
    session: AsyncSession,
    *,
    conversation: Conversation,
) -> Conversation:
    """Allocate the next dialog event number under the existing row lock."""
    stmt = (
        update(Conversation)
        .where(Conversation.id == conversation.id)
        .values(
            current_event_seq=Conversation.current_event_seq + 1,
            updated_at=func.now(),
        )
        .returning(Conversation.current_event_seq)
        .execution_options(synchronize_session=False)
    )
    allocated = await session.scalar(stmt)
    if allocated is None:
        raise RuntimeError("CONVERSATION_LOOKUP_FAILED")
    await session.refresh(conversation)
    return conversation


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


async def apply_chronologically_new_manager_message(
    session: AsyncSession,
    *,
    conversation: Conversation,
    provider_sequence: int,
    moment: datetime,
    handoff_pause_seconds: int,
) -> tuple[Conversation, bool]:
    """Apply one ordered manager event under the Conversation row lock."""
    if not 10 * 60 <= handoff_pause_seconds <= 15 * 60:
        raise ValueError("handoff_pause_seconds must be between 600 and 900")
    await clear_active_handoff_quarantine_for_manager_message(
        session,
        conversation=conversation,
        moment=moment,
    )
    entered_from_bot = conversation.handoff_state == HandoffState.BOT_ACTIVE.value
    stmt = (
        update(Conversation)
        .where(Conversation.id == conversation.id)
        .values(
            ownership=ConversationOwnership.MANAGER.value,
            status=ConversationStatus.HANDOFF.value,
            handoff_state=HandoffState.HUMAN_ACTIVE.value,
            context_version=Conversation.context_version + 1,
            manager_epoch=Conversation.manager_epoch + 1,
            manager_sequence_hwm=provider_sequence,
            manager_takeover_at=func.coalesce(
                Conversation.manager_takeover_at,
                moment,
            ),
            handoff_deadline_at=moment
            + timedelta(seconds=handoff_pause_seconds),
            human_pause_anchor_at=None,
            active_reply_plan_id=None,
            updated_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    await session.execute(stmt)
    await session.refresh(conversation)
    return conversation, entered_from_bot


async def apply_chronologically_new_vk_external_reply(
    session: AsyncSession,
    *,
    conversation: Conversation,
    provider_sequence: int,
    moment: datetime,
    handoff_pause_seconds: int,
) -> tuple[Conversation, bool]:
    """Apply ordered VK message_reply external activity (no fake manager text).

    Uses ``vk_client_external_reply_hwm`` (conversation_message_id namespace),
    not amoCRM ``manager_sequence_hwm``.
    """
    if not 10 * 60 <= handoff_pause_seconds <= 15 * 60:
        raise ValueError("handoff_pause_seconds must be between 600 and 900")
    await clear_active_handoff_quarantine_for_manager_message(
        session,
        conversation=conversation,
        moment=moment,
    )
    entered_from_bot = conversation.handoff_state == HandoffState.BOT_ACTIVE.value
    stmt = (
        update(Conversation)
        .where(Conversation.id == conversation.id)
        .values(
            ownership=ConversationOwnership.MANAGER.value,
            status=ConversationStatus.HANDOFF.value,
            handoff_state=HandoffState.HUMAN_ACTIVE.value,
            context_version=Conversation.context_version + 1,
            manager_epoch=Conversation.manager_epoch + 1,
            vk_client_external_reply_hwm=provider_sequence,
            manager_takeover_at=func.coalesce(
                Conversation.manager_takeover_at,
                moment,
            ),
            handoff_deadline_at=moment
            + timedelta(seconds=handoff_pause_seconds),
            human_pause_anchor_at=None,
            active_reply_plan_id=None,
            updated_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    await session.execute(stmt)
    await session.refresh(conversation)
    return conversation, entered_from_bot


async def append_conversation_ops_event(
    session: AsyncSession,
    *,
    conversation: Conversation,
    event_type: ConversationOpsEventType,
    reason_code: str,
    clear_path: str | None = None,
) -> ConversationOpsEvent:
    """Insert one append-only operational event. No update/delete API exists."""
    if reason_code not in HANDOFF_QUARANTINE_REASONS:
        raise ValueError("HANDOFF_QUARANTINE_REASON_INVALID")
    if event_type is ConversationOpsEventType.HANDOFF_EXPIRY_QUARANTINED:
        if clear_path is not None:
            raise ValueError("HANDOFF_QUARANTINE_CLEAR_PATH_UNEXPECTED")
    elif event_type is ConversationOpsEventType.HANDOFF_QUARANTINE_CLEARED:
        if clear_path not in HANDOFF_QUARANTINE_CLEAR_PATHS:
            raise ValueError("HANDOFF_QUARANTINE_CLEAR_PATH_INVALID")
    else:
        raise ValueError("CONVERSATION_OPS_EVENT_TYPE_INVALID")

    event = ConversationOpsEvent(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        event_type=event_type.value,
        reason_code=reason_code,
        clear_path=clear_path,
        manager_epoch=conversation.manager_epoch,
        context_version=conversation.context_version,
    )
    session.add(event)
    await session.flush()
    return event


async def quarantine_due_handoff_expiry(
    session: AsyncSession,
    *,
    conversation: Conversation,
    reason_code: str,
    moment: datetime,
) -> Conversation:
    """Mark a locked due handoff as actively quarantined. Never BOT_ACTIVE."""
    if reason_code not in HANDOFF_QUARANTINE_REASONS:
        raise ValueError("HANDOFF_QUARANTINE_REASON_INVALID")

    await append_conversation_ops_event(
        session,
        conversation=conversation,
        event_type=ConversationOpsEventType.HANDOFF_EXPIRY_QUARANTINED,
        reason_code=reason_code,
    )
    stmt = (
        update(Conversation)
        .where(Conversation.id == conversation.id)
        .values(
            handoff_quarantined_at=moment,
            handoff_quarantine_reason=reason_code,
            handoff_quarantine_cleared_at=None,
            handoff_quarantine_clear_path=None,
            updated_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    await session.execute(stmt)
    await session.refresh(conversation)
    return conversation


async def clear_active_handoff_quarantine_for_manager_message(
    session: AsyncSession,
    *,
    conversation: Conversation,
    moment: datetime,
) -> bool:
    """Clear active quarantine gate only; never NULL quarantined_at/reason."""
    if not handoff_expiry_quarantine_is_active(conversation):
        return False
    reason_code = conversation.handoff_quarantine_reason
    if reason_code is None or reason_code not in HANDOFF_QUARANTINE_REASONS:
        raise RuntimeError("HANDOFF_QUARANTINE_REASON_MISSING")

    await append_conversation_ops_event(
        session,
        conversation=conversation,
        event_type=ConversationOpsEventType.HANDOFF_QUARANTINE_CLEARED,
        reason_code=reason_code,
        clear_path=HANDOFF_QUARANTINE_CLEAR_PATH_MANAGER_MESSAGE,
    )
    stmt = (
        update(Conversation)
        .where(
            Conversation.id == conversation.id,
            Conversation.handoff_quarantined_at.is_not(None),
            Conversation.handoff_quarantine_cleared_at.is_(None),
            # Preserve original quarantined_at / reason — do not SET NULL.
            Conversation.handoff_quarantine_reason == reason_code,
        )
        .values(
            handoff_quarantine_cleared_at=moment,
            handoff_quarantine_clear_path=(
                HANDOFF_QUARANTINE_CLEAR_PATH_MANAGER_MESSAGE
            ),
            updated_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    updated = await session.execute(stmt)
    if updated.rowcount != 1:
        raise RuntimeError("HANDOFF_QUARANTINE_CLEAR_STALE")
    await session.refresh(conversation)
    return True


async def enter_or_continue_human_pause(
    session: AsyncSession,
    *,
    conversation: Conversation,
    moment: datetime,
    handoff_pause_seconds: int,
) -> tuple[Conversation, bool]:
    """Move HUMAN_ACTIVE to HUMAN_PAUSE; never extend an existing pause."""
    if not 10 * 60 <= handoff_pause_seconds <= 15 * 60:
        raise ValueError("handoff_pause_seconds must be between 600 and 900")
    if conversation.handoff_state == HandoffState.HUMAN_PAUSE.value:
        return conversation, False
    if conversation.handoff_state != HandoffState.HUMAN_ACTIVE.value:
        return conversation, False

    stmt = (
        update(Conversation)
        .where(Conversation.id == conversation.id)
        .values(
            handoff_state=HandoffState.HUMAN_PAUSE.value,
            handoff_deadline_at=moment
            + timedelta(seconds=handoff_pause_seconds),
            human_pause_anchor_at=moment,
            updated_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    await session.execute(stmt)
    await session.refresh(conversation)
    return conversation, True


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
    if conversation.status == ConversationStatus.CLOSED.value:
        return conversation, False
    already = (
        conversation.ownership == ConversationOwnership.MANAGER.value
        and conversation.manager_takeover_at is not None
        and conversation.status == ConversationStatus.HANDOFF.value
        and conversation.handoff_state == HandoffState.HUMAN_ACTIVE.value
        and conversation.handoff_deadline_at is not None
        and conversation.human_pause_anchor_at is None
    )
    if already:
        return conversation, False
    stmt = (
        update(Conversation)
        .where(Conversation.id == conversation.id)
        .values(
            ownership=ConversationOwnership.MANAGER.value,
            status=ConversationStatus.HANDOFF.value,
            handoff_state=HandoffState.HUMAN_ACTIVE.value,
            manager_epoch=Conversation.manager_epoch + 1,
            manager_takeover_at=moment,
            handoff_deadline_at=text("'infinity'::timestamptz"),
            human_pause_anchor_at=None,
            active_reply_plan_id=None,
            updated_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    await session.execute(stmt)
    await session.refresh(conversation)
    return conversation, True
