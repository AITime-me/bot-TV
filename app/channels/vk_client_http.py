"""VK CLIENT Callback webhook → durable ingress (shadow observer).

Default-off: registered only when VK_CLIENT_CALLBACK_ENABLED=true and fully
configured. ACK only after durable MESSAGE / MESSAGE_REPLY receipt.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.vk_client_config import VkClientCallbackConfig
from app.channels.vk_client_types import (
    VkClientNormalizedMessage,
    VkClientNormalizedMessageReply,
    VkClientWebhookKind,
)
from app.channels.vk_client_webhook import parse_vk_client_callback
from app.schemas.vk_client_ingress import (
    VkClientIngressEvent,
    VkClientMessageReplyIngressEvent,
)
from app.services.ingress import IngressPersistError
from app.services.vk_client_ingress import (
    VkClientIngressAdapter,
    VkClientIngressIdempotencyConflict,
)

VK_CLIENT_WEBHOOK_PATH = "/webhooks/vk/client"


@dataclass(frozen=True, slots=True, repr=False)
class VkClientWebhookHttpResult:
    body: str
    status_code: int = 200

    def __repr__(self) -> str:
        return (
            "VkClientWebhookHttpResult("
            f"status_code={self.status_code!r}, "
            "body=<redacted>)"
        )


def build_vk_client_router(
    *,
    config: VkClientCallbackConfig,
    session_factory: async_sessionmaker[AsyncSession],
) -> APIRouter:
    config.require_runtime()
    adapter = VkClientIngressAdapter(session_factory)
    router = APIRouter(tags=["vk-client"])

    @router.post(VK_CLIENT_WEBHOOK_PATH)
    async def vk_client_webhook(request: Request) -> PlainTextResponse:
        raw = await request.body()
        result = await handle_vk_client_callback(
            raw,
            config=config,
            adapter=adapter,
        )
        return PlainTextResponse(content=result.body, status_code=result.status_code)

    return router


async def handle_vk_client_callback(
    body: object,
    *,
    config: VkClientCallbackConfig,
    adapter: VkClientIngressAdapter,
) -> VkClientWebhookHttpResult:
    parsed = parse_vk_client_callback(body, config=config)
    if parsed.kind is VkClientWebhookKind.CONFIRMATION:
        assert parsed.confirmation_response is not None
        return VkClientWebhookHttpResult(body=parsed.confirmation_response)
    if parsed.kind is VkClientWebhookKind.REJECTED:
        return VkClientWebhookHttpResult(
            body="ok",
            status_code=status.HTTP_200_OK,
        )
    if parsed.kind is VkClientWebhookKind.IGNORED:
        return VkClientWebhookHttpResult(body="ok")

    try:
        if parsed.kind is VkClientWebhookKind.MESSAGE_REPLY:
            if parsed.message_reply is None:
                return VkClientWebhookHttpResult(body="ok")
            await adapter.accept_message_reply(
                _reply_to_ingress(parsed.message_reply)
            )
        elif parsed.kind is VkClientWebhookKind.MESSAGE:
            if parsed.message is None:
                return VkClientWebhookHttpResult(body="ok")
            await adapter.accept(_message_to_ingress(parsed.message))
        else:
            return VkClientWebhookHttpResult(body="ok")
    except VkClientIngressIdempotencyConflict:
        return VkClientWebhookHttpResult(
            body="conflict",
            status_code=status.HTTP_409_CONFLICT,
        )
    except IngressPersistError:
        return VkClientWebhookHttpResult(
            body="unavailable",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except ValidationError:
        return VkClientWebhookHttpResult(body="ok")

    return VkClientWebhookHttpResult(body="ok")


def _message_to_ingress(message: VkClientNormalizedMessage) -> VkClientIngressEvent:
    return VkClientIngressEvent(
        channel="vk",
        external_event_id=message.external_event_id,
        external_conversation_id=message.external_conversation_id,
        event_type="VK_CLIENT_MESSAGE",
        text=message.text,
        received_at=message.occurred_at,
    )


def _reply_to_ingress(
    reply: VkClientNormalizedMessageReply,
) -> VkClientMessageReplyIngressEvent:
    provenance = reply.provenance
    return VkClientMessageReplyIngressEvent(
        channel="vk",
        external_event_id=reply.external_event_id,
        external_conversation_id=reply.external_conversation_id,
        event_type="VK_CLIENT_MESSAGE_REPLY",
        group_id=reply.group_id,
        peer_id=reply.peer_id,
        conversation_message_id=reply.conversation_message_id,
        provider_message_id=reply.provider_message_id,
        occurred_at=reply.occurred_at,
        random_id=reply.random_id,
        provenance_kind=provenance.kind.value,
        provenance_v=provenance.v,
        provenance_ns=provenance.ns,
        provenance_oid=provenance.oid,
        provenance_mac=provenance.mac,
    )
