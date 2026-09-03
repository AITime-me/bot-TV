"""VK Callback API parse/auth for CLIENT private dialog (shadow observer)."""

from __future__ import annotations

import hmac
import json
from typing import Final

from app.channels.vk_client_config import VkClientCallbackConfig
from app.channels.vk_client_types import (
    VkClientNormalizedMessage,
    VkClientWebhookKind,
    VkClientWebhookResult,
    utc_from_unix,
    vk_client_text_is_ingress_safe,
)

__all__ = ("parse_vk_client_callback",)

# Stable across Callback retries. Do not fall back to id/timestamp/text/UUID.
_STABLE_MESSAGE_ID_FIELD: Final[str] = "conversation_message_id"


def parse_vk_client_callback(
    body: object,
    *,
    config: VkClientCallbackConfig,
) -> VkClientWebhookResult:
    """Validate Callback payload. Fail closed on auth/schema ambiguity."""

    try:
        config.require_callback_config()
    except Exception:
        return VkClientWebhookResult(kind=VkClientWebhookKind.REJECTED)

    payload = _coerce_object(body)
    if payload is None:
        return VkClientWebhookResult(kind=VkClientWebhookKind.REJECTED)

    secret = payload.get("secret")
    if type(secret) is not str or not hmac.compare_digest(
        secret, config.callback_secret or ""
    ):
        return VkClientWebhookResult(kind=VkClientWebhookKind.REJECTED)

    group_id = payload.get("group_id")
    if type(group_id) is not int or isinstance(group_id, bool):
        return VkClientWebhookResult(kind=VkClientWebhookKind.REJECTED)
    if group_id != config.group_id:
        return VkClientWebhookResult(kind=VkClientWebhookKind.REJECTED)

    event_type = payload.get("type")
    if type(event_type) is not str or not event_type:
        return VkClientWebhookResult(kind=VkClientWebhookKind.REJECTED)

    if event_type == "confirmation":
        assert config.confirmation is not None
        return VkClientWebhookResult(
            kind=VkClientWebhookKind.CONFIRMATION,
            confirmation_response=config.confirmation,
        )

    if event_type != "message_new":
        return VkClientWebhookResult(kind=VkClientWebhookKind.IGNORED)

    message = _extract_private_message(payload.get("object"), group_id=group_id)
    if message is None:
        return VkClientWebhookResult(kind=VkClientWebhookKind.IGNORED)
    return VkClientWebhookResult(kind=VkClientWebhookKind.MESSAGE, message=message)


def _coerce_object(body: object) -> dict[str, object] | None:
    if type(body) is dict:
        return body  # type: ignore[return-value]
    if type(body) is bytes:
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return None
        body = text
    if type(body) is str:
        try:
            parsed = json.loads(body)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if type(parsed) is dict:
            return parsed
    return None


def _extract_private_message(
    obj: object,
    *,
    group_id: int,
) -> VkClientNormalizedMessage | None:
    if type(obj) is not dict:
        return None
    message = obj.get("message")
    if type(message) is not dict:
        return None

    out = message.get("out")
    if out not in (0, False):
        return None
    if message.get("action") is not None:
        return None

    from_id = message.get("from_id")
    peer_id = message.get("peer_id")
    if type(from_id) is not int or isinstance(from_id, bool) or from_id <= 0:
        return None
    if type(peer_id) is not int or isinstance(peer_id, bool) or peer_id <= 0:
        return None
    # Direct private dialogue only (user ↔ community inbox appears as peer==from).
    if from_id != peer_id:
        return None

    text = message.get("text")
    if type(text) is not str:
        return None
    cleaned = " ".join(text.split())
    # Fail closed here: oversized / control-laden text must never become MESSAGE
    # and must never reach Pydantic (ValidationError embeds input_value).
    if not vk_client_text_is_ingress_safe(cleaned):
        return None

    cmid = message.get(_STABLE_MESSAGE_ID_FIELD)
    if type(cmid) is not int or isinstance(cmid, bool) or cmid <= 0:
        return None

    occurred = utc_from_unix(message.get("date"))
    if occurred is None:
        return None

    try:
        return VkClientNormalizedMessage(
            from_id=from_id,
            peer_id=peer_id,
            text=cleaned,
            conversation_message_id=cmid,
            occurred_at=occurred,
            group_id=group_id,
        )
    except ValueError:
        return None
