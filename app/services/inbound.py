from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import (
    Conversation,
    conversation_allows_automatic_reply,
)
from app.models.inbox import InboxMessage
from app.models.outbox import DestinationType, OutboxMessage
from app.models.reply_plan import (
    BOT_RESPONSE_DELAY_MS,
    ReplyPlan,
    ReplyPlanType,
)
from app.repositories import conversations as conversation_repo
from app.repositories import messages as message_repo
from app.repositories import reply_plans as reply_plan_repo
from app.schemas.inbound import SyntheticInboundEvent
from app.services.amocrm_mirror import enqueue_client_message_received


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
    context_version: int
    reply_plan: ReplyPlan | None
    reply_plan_created: bool


class InboundService:
    """Persist synthetic inbound events and advance reply orchestration.

    No AI, booking, or client send. INTERNAL_DRAFT remains a manager-hint
    artifact; CLIENT_REPLY ReplyPlan is the orchestration unit for 01C.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def accept(self, event: SyntheticInboundEvent) -> InboundAcceptResult:
        """Find/create conversation, idempotently store inbox and INTERNAL_DRAFT.

        Statement order is fixed: get/create conversation, lock the conversation,
        insert inbox, then bump/supersede/plan/draft. On a newly created inbox
        message (not a duplicate delivery) the context_version is bumped exactly
        once, prior open ReplyPlans are superseded, and a CLIENT_REPLY plan is
        created while the bot still owns the dialog.

        Duplicate ingress/inbox deliveries do not bump context_version.
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
        # Single, always-first lock point of this transaction. The inbox/plan/
        # outbox INSERTs below take FOR KEY SHARE on this same row through their
        # foreign keys, so locking afterwards would escalate KEY SHARE to FOR
        # UPDATE and deadlock two concurrent messages of one dialog.
        conversation = await conversation_repo.lock_for_update(
            self._session,
            conversation_id=conversation.id,
        )

        inbox, created_inbox = await message_repo.insert_inbox_if_absent(
            self._session,
            conversation_id=conversation.id,
            channel=channel,
            external_message_id=event.external_message_id,
            payload_json=payload,
            received_at=received_at,
        )

        reply_plan: ReplyPlan | None = None
        reply_plan_created = False
        if created_inbox:
            conversation = await conversation_repo.bump_context_for_new_message(
                self._session,
                conversation=conversation,
                activity_at=received_at,
            )
            await reply_plan_repo.supersede_open_plans(
                self._session,
                conversation_id=conversation.id,
            )
            if conversation_allows_automatic_reply(conversation):
                reply_plan = await reply_plan_repo.create_client_reply_plan(
                    self._session,
                    conversation_id=conversation.id,
                    context_version=conversation.context_version,
                    correlation_id=uuid.uuid4(),
                    delay_ms=BOT_RESPONSE_DELAY_MS,
                    payload_json={
                        "schema": "synthetic.reply_plan.v1",
                        "plan_type": ReplyPlanType.CLIENT_REPLY.value,
                        "synthetic_token": "SYNTHETIC_OK",
                        "inbox_id": str(inbox.id),
                    },
                )
                conversation.active_reply_plan_id = reply_plan.id
                await self._session.flush()
                reply_plan_created = True
            else:
                conversation.active_reply_plan_id = None
                await self._session.flush()

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
                "context_version": conversation.context_version,
            },
        )

        if created_inbox:
            # Last table in the lock order: enqueued only after every other row
            # of this dialog subtree has been written, and only for a genuinely
            # new client message.
            await enqueue_client_message_received(
                self._session,
                conversation_id=conversation.id,
                inbox_id=inbox.id,
                context_version=conversation.context_version,
                correlation_id=uuid.uuid4(),
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
            context_version=conversation.context_version,
            reply_plan=reply_plan,
            reply_plan_created=reply_plan_created,
        )


def assert_no_client_outbound_path() -> None:
    """Static guard used by tests: this module must not grow a sender."""
    forbidden = ("send_message", "publish_to_channel", "transport_send")
    source = Path(__file__).read_text(encoding="utf-8")
    for name in forbidden:
        if f"def {name}" in source or f".{name}(" in source:
            raise AssertionError(f"forbidden outbound symbol present: {name}")
