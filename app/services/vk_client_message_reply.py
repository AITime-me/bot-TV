"""Apply VK CLIENT message_reply as own-echo ignore or external takeover."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.vk_client_external_takeover_config import (
    VkClientExternalTakeoverConfig,
)
from app.channels.vk_client_outbound_provenance import (
    verify_vk_outbound_provenance_payload,
)
from app.db.clock import db_statement_now
from app.db.session import session_scope
from app.models.conversation import (
    Channel,
    Conversation,
    ConversationStatus,
)
from app.repositories import conversations as conversation_repo
from app.repositories import outbound as outbound_repo
from app.repositories import reply_plans as reply_plan_repo
from app.services.amocrm_mirror import enqueue_manager_takeover
from app.services.manager_messages import (
    ManagerEventClassification,
    classify_manager_sequence,
)


class VkClientReplyClassification(str, enum.Enum):
    OWN_TEYA_ECHO = "OWN_TEYA_ECHO"
    EXTERNAL_ACTOR = "EXTERNAL_ACTOR"
    OWN_ECHO_RACE_RETRY = "OWN_ECHO_RACE_RETRY"
    FEATURE_OFF = "FEATURE_OFF"
    UNRESOLVED_CONVERSATION = "UNRESOLVED_CONVERSATION"
    CONVERSATION_CLOSED = "CONVERSATION_CLOSED"
    STALE = "STALE"
    DUPLICATE = "DUPLICATE"


@dataclass(frozen=True, slots=True, repr=False)
class VkClientMessageReplyApplyResult:
    classification: VkClientReplyClassification
    conversation_id: uuid.UUID | None
    fsm_changed: bool
    cancelled_plans: int
    cancelled_outbound: int

    def __repr__(self) -> str:
        return (
            "VkClientMessageReplyApplyResult("
            f"classification={self.classification.value!r}, "
            f"conversation_id={self.conversation_id!r}, "
            f"fsm_changed={self.fsm_changed!r}, "
            f"cancelled_plans={self.cancelled_plans!r}, "
            f"cancelled_outbound={self.cancelled_outbound!r})"
        )


class VkClientMessageReplyOwnEchoRace(RuntimeError):
    """Retryable: send receipt may not be durable yet."""

    def __init__(self) -> None:
        super().__init__("VK_CLIENT_OWN_ECHO_RACE")


async def apply_vk_client_message_reply_in_session(
    session: AsyncSession,
    *,
    envelope: dict[str, object],
    handoff_pause_seconds: int,
    takeover_config: VkClientExternalTakeoverConfig,
) -> VkClientMessageReplyApplyResult:
    """Classify + optionally apply external takeover. No conversation create."""

    external_conversation_id = envelope.get("external_conversation_id")
    provider_message_id = envelope.get("provider_message_id")
    conversation_message_id = envelope.get("conversation_message_id")
    payload = envelope.get("payload")
    if (
        type(external_conversation_id) is not str
        or not external_conversation_id
        or type(provider_message_id) is not int
        or isinstance(provider_message_id, bool)
        or provider_message_id <= 0
        or type(conversation_message_id) is not int
        or isinstance(conversation_message_id, bool)
        or conversation_message_id <= 0
    ):
        raise ValueError("VK_CLIENT_REPLY_ENVELOPE_INVALID")

    if not takeover_config.fsm_mutation_allowed(
        external_conversation_id=external_conversation_id
    ):
        return VkClientMessageReplyApplyResult(
            classification=VkClientReplyClassification.FEATURE_OFF,
            conversation_id=None,
            fsm_changed=False,
            cancelled_plans=0,
            cancelled_outbound=0,
        )

    conversation = await conversation_repo.get_by_channel_external(
        session,
        channel=Channel.VK,
        external_conversation_id=external_conversation_id,
    )
    if conversation is None:
        return VkClientMessageReplyApplyResult(
            classification=VkClientReplyClassification.UNRESOLVED_CONVERSATION,
            conversation_id=None,
            fsm_changed=False,
            cancelled_plans=0,
            cancelled_outbound=0,
        )

    conversation = await conversation_repo.lock_for_update(
        session,
        conversation_id=conversation.id,
    )

    own = await _classify_own_echo(
        session,
        conversation=conversation,
        provider_message_id=provider_message_id,
        payload=payload,
        provenance_key=takeover_config.provenance_key,
    )
    if own is VkClientReplyClassification.OWN_ECHO_RACE_RETRY:
        raise VkClientMessageReplyOwnEchoRace()
    if own is VkClientReplyClassification.OWN_TEYA_ECHO:
        return VkClientMessageReplyApplyResult(
            classification=VkClientReplyClassification.OWN_TEYA_ECHO,
            conversation_id=conversation.id,
            fsm_changed=False,
            cancelled_plans=0,
            cancelled_outbound=0,
        )

    if conversation.status == ConversationStatus.CLOSED.value:
        return VkClientMessageReplyApplyResult(
            classification=VkClientReplyClassification.CONVERSATION_CLOSED,
            conversation_id=conversation.id,
            fsm_changed=False,
            cancelled_plans=0,
            cancelled_outbound=0,
        )

    ordering = classify_manager_sequence(
        current_hwm=conversation.vk_client_external_reply_hwm,
        provider_sequence=conversation_message_id,
    )
    if ordering is ManagerEventClassification.STALE:
        return VkClientMessageReplyApplyResult(
            classification=VkClientReplyClassification.STALE,
            conversation_id=conversation.id,
            fsm_changed=False,
            cancelled_plans=0,
            cancelled_outbound=0,
        )
    if ordering is ManagerEventClassification.QUARANTINED:
        return VkClientMessageReplyApplyResult(
            classification=VkClientReplyClassification.STALE,
            conversation_id=conversation.id,
            fsm_changed=False,
            cancelled_plans=0,
            cancelled_outbound=0,
        )

    moment = await db_statement_now(session)
    conversation, entered_from_bot = (
        await conversation_repo.apply_chronologically_new_vk_external_reply(
            session,
            conversation=conversation,
            provider_sequence=conversation_message_id,
            moment=moment,
            handoff_pause_seconds=handoff_pause_seconds,
        )
    )
    cancelled = await reply_plan_repo.cancel_open_plans_for_takeover(
        session,
        conversation_id=conversation.id,
        reason="VK_CLIENT_EXTERNAL_REPLY",
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
    return VkClientMessageReplyApplyResult(
        classification=VkClientReplyClassification.EXTERNAL_ACTOR,
        conversation_id=conversation.id,
        fsm_changed=True,
        cancelled_plans=cancelled,
        cancelled_outbound=cancelled_outbound,
    )


async def _classify_own_echo(
    session: AsyncSession,
    *,
    conversation: Conversation,
    provider_message_id: int,
    payload: object,
    provenance_key: str | None,
) -> VkClientReplyClassification:
    payload_present = (
        (type(payload) is dict and len(payload) > 0)
        or (type(payload) is str and len(payload) > 0)
    )

    # A: authenticated provenance marker (foreign payload never matches).
    if type(provenance_key) is str and provenance_key:
        outbound_id = verify_vk_outbound_provenance_payload(
            payload,
            provenance_key=provenance_key,
        )
        if outbound_id is not None:
            row = await outbound_repo.find_vk_outbound_by_id(
                session,
                conversation_id=conversation.id,
                outbound_id=outbound_id,
            )
            if row is not None:
                return VkClientReplyClassification.OWN_TEYA_ECHO
            # Marker authenticates but outbound missing for this conversation:
            # fail closed as external (do not trust orphan marker alone).
            return VkClientReplyClassification.EXTERNAL_ACTOR
        if payload_present:
            # Explicit non-Bot-TV payload (e.g. SalesBot known_event) → external.
            # Do not wait on in-flight own send receipt.
            return VkClientReplyClassification.EXTERNAL_ACTOR

    # B: durable provider message id receipt.
    by_id = await outbound_repo.find_vk_outbound_by_provider_message_id(
        session,
        conversation_id=conversation.id,
        provider_message_id=provider_message_id,
    )
    if by_id is not None:
        return VkClientReplyClassification.OWN_TEYA_ECHO

    # Race only when payload absent/unknown and ADMITTED send may still
    # be persisting provider_message_id. random_id alone is never proof.
    if await outbound_repo.has_admitted_vk_outbound_without_provider_id(
        session,
        conversation_id=conversation.id,
    ):
        return VkClientReplyClassification.OWN_ECHO_RACE_RETRY

    return VkClientReplyClassification.EXTERNAL_ACTOR


class VkClientMessageReplyService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        handoff_pause_seconds: int,
        takeover_config: VkClientExternalTakeoverConfig,
    ) -> None:
        if not 10 * 60 <= handoff_pause_seconds <= 15 * 60:
            raise ValueError("handoff_pause_seconds must be between 600 and 900")
        self._session_factory = session_factory
        self._handoff_pause_seconds = handoff_pause_seconds
        self._takeover_config = takeover_config

    async def apply_envelope(
        self,
        envelope: dict[str, object],
    ) -> VkClientMessageReplyApplyResult:
        async with session_scope(self._session_factory) as session:
            return await apply_vk_client_message_reply_in_session(
                session,
                envelope=envelope,
                handoff_pause_seconds=self._handoff_pause_seconds,
                takeover_config=self._takeover_config,
            )
