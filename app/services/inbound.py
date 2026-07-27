from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import (
    Conversation,
    conversation_allows_automatic_reply,
)
from app.models.inbox import InboxMessage
from app.models.outbox import DestinationType, OutboxMessage
from app.repositories import conversations as conversation_repo
from app.repositories import messages as message_repo
from app.schemas.inbound import SyntheticInboundEvent


@dataclass(frozen=True)
class InboundAcceptResult:
    conversation: Conversation
    inbox: InboxMessage
    outbox: OutboxMessage
    created_conversation: bool
    created_inbox: bool
    created_outbox: bool
    duplicate: bool
    automatic_reply_allowed: bool


class InboundService:
    """Persist synthetic inbound events. No AI, booking, or client send."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def accept(self, event: SyntheticInboundEvent) -> InboundAcceptResult:
        """Find/create conversation, idempotently store inbox and INTERNAL_DRAFT.

        Transaction boundaries are owned by the caller via session_scope (or an
        explicit begin/commit). This method only flush()es via repositories and
        never commits or rolls back. On success outbox is always present: the
        same unique INTERNAL_DRAFT for the inbox, whether newly created or
        recovered after a concurrent insert.
        """
        if event.channel != "synthetic":
            raise ValueError("UNSUPPORTED_CHANNEL")

        channel = event.channel_enum()
        payload = event.safe_payload()
        received_at = event.received_at_utc()

        conversation, created_conversation = await conversation_repo.get_or_create(
            self._session,
            channel=channel,
            external_conversation_id=event.external_conversation_id,
        )

        inbox, created_inbox = await message_repo.insert_inbox_if_absent(
            self._session,
            conversation_id=conversation.id,
            channel=channel,
            external_message_id=event.external_message_id,
            payload_json=payload,
            received_at=received_at,
        )

        automatic_reply_allowed = conversation_allows_automatic_reply(conversation)
        outbox, created_outbox = await message_repo.create_internal_draft_outbox(
            self._session,
            conversation_id=conversation.id,
            source_inbox_id=inbox.id,
            payload_json={
                "schema": "internal.draft.v1",
                "source": "inbound",
                "inbox_id": str(inbox.id),
                "conversation_id": str(conversation.id),
                "destination_type": DestinationType.INTERNAL_DRAFT.value,
                # Draft text mirror for manager hints — not a client send.
                "draft_text": event.text,
                "automatic_reply_allowed": automatic_reply_allowed,
            },
        )

        return InboundAcceptResult(
            conversation=conversation,
            inbox=inbox,
            outbox=outbox,
            created_conversation=created_conversation,
            created_inbox=created_inbox,
            created_outbox=created_outbox,
            duplicate=not created_inbox,
            automatic_reply_allowed=automatic_reply_allowed,
        )


def assert_no_client_outbound_path() -> None:
    """Static guard used by tests: this module must not grow a sender."""
    forbidden = ("send_message", "publish_to_channel", "transport_send")
    source = Path(__file__).read_text(encoding="utf-8")
    for name in forbidden:
        if f"def {name}" in source or f".{name}(" in source:
            raise AssertionError(f"forbidden outbound symbol present: {name}")
