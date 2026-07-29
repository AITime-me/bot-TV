from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.clock import db_statement_now
from app.db.session import session_scope
from app.models.conversation import Conversation, ConversationStatus
from app.models.manager_message import ManagerMessage
from app.repositories import conversations as conversation_repo
from app.repositories import manager_messages as manager_message_repo
from app.repositories import outbound as outbound_repo
from app.repositories import reply_plans as reply_plan_repo
from app.schemas.manager_message import SyntheticManagerMessageEvent
from app.services.amocrm_mirror import enqueue_manager_takeover


class ManagerEventClassification(str, enum.Enum):
    CHRONOLOGICALLY_NEW = "CHRONOLOGICALLY_NEW"
    STALE = "STALE"
    QUARANTINED = "QUARANTINED"


def classify_manager_sequence(
    *,
    current_hwm: int | None,
    provider_sequence: int | None,
) -> ManagerEventClassification:
    """Classify ordering without using provider timestamps or DB receipt time."""
    if provider_sequence is None:
        return ManagerEventClassification.QUARANTINED
    if current_hwm is None or provider_sequence > current_hwm:
        return ManagerEventClassification.CHRONOLOGICALLY_NEW
    return ManagerEventClassification.STALE


@dataclass(frozen=True, repr=False)
class ManagerMessageApplyResult:
    conversation_id: uuid.UUID
    manager_message_id: uuid.UUID
    status: str
    duplicate: bool
    fsm_changed: bool
    entered_from_bot: bool
    cancelled_plans: int
    cancelled_outbound: int
    context_version: int
    manager_epoch: int
    event_seq_hwm: int
    manager_sequence_hwm: int | None

    def __repr__(self) -> str:
        return (
            "ManagerMessageApplyResult("
            f"conversation_id={self.conversation_id!r}, "
            f"manager_message_id={self.manager_message_id!r}, "
            f"status={self.status!r}, duplicate={self.duplicate!r}, "
            f"fsm_changed={self.fsm_changed!r}, "
            f"cancelled_outbound={self.cancelled_outbound!r}, "
            f"context_version={self.context_version!r}, "
            f"manager_epoch={self.manager_epoch!r}, "
            f"event_seq_hwm={self.event_seq_hwm!r})"
        )


class SyntheticManagerMessageService:
    """Transactional synthetic manager ingress; no live provider adapter."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        handoff_pause_seconds: int = 15 * 60,
    ) -> None:
        if not 10 * 60 <= handoff_pause_seconds <= 15 * 60:
            raise ValueError("handoff_pause_seconds must be between 600 and 900")
        self._session_factory = session_factory
        self._handoff_pause_seconds = handoff_pause_seconds

    async def apply(
        self,
        event: SyntheticManagerMessageEvent,
    ) -> ManagerMessageApplyResult:
        async with session_scope(self._session_factory) as session:
            return await apply_manager_message_in_session(
                session,
                event=event,
                handoff_pause_seconds=self._handoff_pause_seconds,
            )


async def apply_manager_message_in_session(
    session: AsyncSession,
    *,
    event: SyntheticManagerMessageEvent,
    handoff_pause_seconds: int = 15 * 60,
) -> ManagerMessageApplyResult:
    """Persist and classify one manager event under a Conversation row lock."""
    if event.channel != "synthetic":
        raise ValueError("UNSUPPORTED_CHANNEL")

    channel = event.channel_enum()
    conversation, _ = await conversation_repo.get_or_create(
        session,
        channel=channel,
        external_conversation_id=event.external_conversation_id,
    )
    conversation = await conversation_repo.lock_for_update(
        session,
        conversation_id=conversation.id,
    )

    message, created = await manager_message_repo.insert_quarantined_if_absent(
        session,
        conversation_id=conversation.id,
        channel=channel,
        external_message_id=event.external_message_id,
        provider_sequence=event.provider_sequence,
        provider_occurred_at=event.provider_occurred_at_utc(),
        body_text=event.text,
        classification_reason="CLASSIFICATION_PENDING",
    )
    if message.conversation_id != conversation.id:
        raise RuntimeError("MANAGER_MESSAGE_CONVERSATION_MISMATCH")
    if not created:
        return _result(
            conversation=conversation,
            message=message,
            duplicate=True,
            fsm_changed=False,
            entered_from_bot=False,
            cancelled_plans=0,
            cancelled_outbound=0,
        )

    if conversation.status == ConversationStatus.CLOSED.value:
        message.classification_reason = "CONVERSATION_CLOSED"
        await session.flush()
        return _result(
            conversation=conversation,
            message=message,
            duplicate=False,
            fsm_changed=False,
            entered_from_bot=False,
            cancelled_plans=0,
            cancelled_outbound=0,
        )

    classification = classify_manager_sequence(
        current_hwm=conversation.manager_sequence_hwm,
        provider_sequence=event.provider_sequence,
    )
    if classification is ManagerEventClassification.QUARANTINED:
        message.classification_reason = "MISSING_PROVIDER_SEQUENCE"
        await session.flush()
        return _result(
            conversation=conversation,
            message=message,
            duplicate=False,
            fsm_changed=False,
            entered_from_bot=False,
            cancelled_plans=0,
            cancelled_outbound=0,
        )
    if classification is ManagerEventClassification.STALE:
        await manager_message_repo.mark_stale(session, message=message)
        return _result(
            conversation=conversation,
            message=message,
            duplicate=False,
            fsm_changed=False,
            entered_from_bot=False,
            cancelled_plans=0,
            cancelled_outbound=0,
        )

    if event.provider_sequence is None:
        raise RuntimeError("MANAGER_SEQUENCE_CLASSIFICATION_BROKEN")
    conversation = await conversation_repo.allocate_next_event_seq(
        session,
        conversation=conversation,
    )
    await manager_message_repo.mark_applied(
        session,
        message=message,
        conversation_event_seq=conversation.current_event_seq,
    )
    moment = await db_statement_now(session)
    conversation, entered_from_bot = (
        await conversation_repo.apply_chronologically_new_manager_message(
            session,
            conversation=conversation,
            provider_sequence=event.provider_sequence,
            moment=moment,
            handoff_pause_seconds=handoff_pause_seconds,
        )
    )
    cancelled = await reply_plan_repo.cancel_open_plans_for_takeover(
        session,
        conversation_id=conversation.id,
        reason="MANAGER_MESSAGE",
    )
    cancelled_outbound = await outbound_repo.cancel_unadmitted_for_manager_message(
        session,
        conversation_id=conversation.id,
    )
    if entered_from_bot:
        await enqueue_manager_takeover(
            session,
            conversation_id=conversation.id,
            correlation_id=uuid.uuid4(),
        )
    return _result(
        conversation=conversation,
        message=message,
        duplicate=False,
        fsm_changed=True,
        entered_from_bot=entered_from_bot,
        cancelled_plans=cancelled,
        cancelled_outbound=cancelled_outbound,
    )


def _result(
    *,
    conversation: Conversation,
    message: ManagerMessage,
    duplicate: bool,
    fsm_changed: bool,
    entered_from_bot: bool,
    cancelled_plans: int,
    cancelled_outbound: int,
) -> ManagerMessageApplyResult:
    return ManagerMessageApplyResult(
        conversation_id=conversation.id,
        manager_message_id=message.id,
        status=message.status,
        duplicate=duplicate,
        fsm_changed=fsm_changed,
        entered_from_bot=entered_from_bot,
        cancelled_plans=cancelled_plans,
        cancelled_outbound=cancelled_outbound,
        context_version=conversation.context_version,
        manager_epoch=conversation.manager_epoch,
        event_seq_hwm=conversation.current_event_seq,
        manager_sequence_hwm=conversation.manager_sequence_hwm,
    )
