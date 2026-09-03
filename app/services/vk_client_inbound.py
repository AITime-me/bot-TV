"""VK CLIENT observer inbound persistence.

Creates/updates real VK conversation + inbox for RuntimeContextBuilder.
Explicitly does NOT enqueue amoCRM mirror/projection, CLIENT_REPLY plans,
INTERNAL_DRAFT outbox, or any client send path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.inbox import InboxMessage
from app.repositories import conversations as conversation_repo
from app.repositories import messages as message_repo
from app.schemas.vk_client_ingress import VkClientInboundEvent


@dataclass(frozen=True)
class VkClientInboundAcceptResult:
    conversation: Conversation
    inbox: InboxMessage
    created_conversation: bool
    created_inbox: bool
    duplicate: bool
    context_version: int


class VkClientInboundService:
    """Shadow-observer client inbound: conversation + inbox only."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def accept(self, event: VkClientInboundEvent) -> VkClientInboundAcceptResult:
        if event.channel != "vk":
            raise ValueError("UNSUPPORTED_CHANNEL")

        channel = event.channel_enum()
        payload = event.safe_payload()
        received_at = event.received_at_utc()

        conversation, created_conversation = await conversation_repo.get_or_create(
            self._session,
            channel=channel,
            external_conversation_id=event.external_conversation_id,
        )
        conversation = await conversation_repo.lock_for_update(
            self._session,
            conversation_id=conversation.id,
        )

        inbox = await message_repo.get_inbox_by_external(
            self._session,
            channel=channel,
            external_message_id=event.external_message_id,
        )
        created_inbox = inbox is None
        if created_inbox:
            conversation = await conversation_repo.allocate_next_event_seq(
                self._session,
                conversation=conversation,
            )
            inbox, created_inbox = await message_repo.insert_inbox_if_absent(
                self._session,
                conversation_id=conversation.id,
                channel=channel,
                external_message_id=event.external_message_id,
                conversation_event_seq=conversation.current_event_seq,
                payload_json=payload,
                received_at=received_at,
            )
        if inbox is None:
            raise RuntimeError("INBOX_LOOKUP_FAILED")
        if inbox.conversation_id != conversation.id:
            raise RuntimeError("INBOX_CONVERSATION_MISMATCH")

        if created_inbox:
            conversation = await conversation_repo.bump_context_for_new_message(
                self._session,
                conversation=conversation,
                activity_at=received_at,
            )
            # Observer mode: no reply-plan supersede, no CRM enqueue, no outbox.

        return VkClientInboundAcceptResult(
            conversation=conversation,
            inbox=inbox,
            created_conversation=created_conversation,
            created_inbox=created_inbox,
            duplicate=not created_inbox,
            context_version=conversation.context_version,
        )


def assert_no_client_outbound_path() -> None:
    """Static guard: VK client observer must not grow a sender or CRM enqueue."""

    forbidden = (
        "send_message",
        "publish_to_channel",
        "transport_send",
        "enqueue_client_message_received",
        "enqueue_client_inbound_projection",
        "create_client_reply_plan",
        "create_internal_draft_outbox",
    )
    source = Path(__file__).read_text(encoding="utf-8")
    for name in forbidden:
        if f"def {name}" in source or f".{name}(" in source:
            raise AssertionError(f"forbidden symbol present: {name}")
