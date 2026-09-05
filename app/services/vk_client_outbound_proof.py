"""Closed-proof VK CLIENT ReplyPlan creator.

Isolated from shadow drafts and from production generation→plan.
Creates CLIENT_REPLY only for one allowlisted conversation + exact trigger text.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.vk_client_outbound_config import (
    VkClientOutboundConfig,
    vk_client_outbound_proof_allowed,
)
from app.config import Settings
from app.models.conversation import Channel, Conversation
from app.models.inbox import InboxMessage
from app.models.reply_plan import ReplyPlan
from app.repositories import outbound as outbound_repo
from app.repositories import reply_plans as reply_plan_repo
from app.repositories.reply_plans import BOT_RESPONSE_DELAY_MS

PROOF_REPLY_PLAN_SCHEMA = "vk.client.proof.reply_plan.v1"
VK_CLIENT_OUTBOUND_PAYLOAD_SCHEMA = "vk.client.outbound.v1"


def vk_client_proof_reply_plan_payload(*, text: str) -> dict[str, Any]:
    return {
        "schema": PROOF_REPLY_PLAN_SCHEMA,
        "destination": "VK_CLIENT_OUTBOUND",
        "text": text,
    }


def vk_client_outbound_payload(*, text: str) -> dict[str, Any]:
    return {
        "schema": VK_CLIENT_OUTBOUND_PAYLOAD_SCHEMA,
        "text": text,
    }


def is_vk_client_proof_reply_plan(payload: dict[str, Any] | None) -> bool:
    if type(payload) is not dict:
        return False
    return (
        payload.get("schema") == PROOF_REPLY_PLAN_SCHEMA
        and payload.get("destination") == "VK_CLIENT_OUTBOUND"
        and type(payload.get("text")) is str
        and bool(payload.get("text"))
    )


async def maybe_create_vk_client_proof_reply_plan(
    session: AsyncSession,
    *,
    settings: Settings,
    config: VkClientOutboundConfig | None,
    conversation: Conversation,
    inbox: InboxMessage,
    inbound_text: str,
    created_inbox: bool,
) -> ReplyPlan | None:
    """Create at most one proof CLIENT_REPLY when all closed-proof gates pass.

    Must run in the same UoW as VK observer accept (after durable inbox insert).
    Never sends HTTP. Never touches shadow drafts.
    """

    if not created_inbox:
        return None
    if config is None:
        return None
    if conversation.channel != Channel.VK.value:
        return None
    if not vk_client_outbound_proof_allowed(
        settings,
        config,
        external_conversation_id=conversation.external_conversation_id,
        inbound_text=inbound_text,
    ):
        return None
    assert config.proof_reply is not None

    await reply_plan_repo.supersede_open_plans(
        session,
        conversation_id=conversation.id,
        reason="VK_CLIENT_PROOF_TRIGGER",
    )
    await outbound_repo.cancel_unadmitted_for_conversation(
        session,
        conversation_id=conversation.id,
    )
    plan = await reply_plan_repo.create_client_reply_plan(
        session,
        conversation_id=conversation.id,
        context_version=conversation.context_version,
        correlation_id=uuid.uuid4(),
        delay_ms=BOT_RESPONSE_DELAY_MS,
        manager_epoch=conversation.manager_epoch,
        event_seq_hwm=conversation.current_event_seq,
        payload_json=vk_client_proof_reply_plan_payload(text=config.proof_reply),
    )
    conversation.active_reply_plan_id = plan.id
    await session.flush()
    return plan
