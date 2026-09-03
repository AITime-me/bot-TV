"""Normalized VK CLIENT inbound DTOs. No SDK. Secrets never in repr."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final

__all__ = (
    "VK_CLIENT_TEXT_MAX_LEN",
    "VkClientWebhookKind",
    "VkClientNormalizedMessage",
    "VkClientWebhookResult",
    "utc_from_unix",
    "vk_client_external_conversation_id",
    "vk_client_external_event_id",
    "vk_client_text_is_ingress_safe",
)

# Must stay aligned with VkClientIngressEvent / VkClientInboundEvent max_length.
VK_CLIENT_TEXT_MAX_LEN: Final[int] = 2000


def vk_client_text_is_ingress_safe(text: str) -> bool:
    """True when text is safe to pass into the Pydantic ingress envelope."""

    if type(text) is not str or not text.strip():
        return False
    if len(text) > VK_CLIENT_TEXT_MAX_LEN:
        return False
    if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in text):
        return False
    return True


class VkClientWebhookKind(StrEnum):
    CONFIRMATION = "CONFIRMATION"
    MESSAGE = "MESSAGE"
    IGNORED = "IGNORED"
    REJECTED = "REJECTED"


def vk_client_external_conversation_id(*, group_id: int, user_id: int) -> str:
    """Deterministic dialog id: community + user, collision-safe across groups."""

    if type(group_id) is not int or isinstance(group_id, bool) or group_id <= 0:
        raise ValueError("INVALID_VK_CLIENT_ID") from None
    if type(user_id) is not int or isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("INVALID_VK_CLIENT_ID") from None
    value = f"vk-{group_id}-{user_id}"
    if len(value) > 128:
        raise ValueError("INVALID_VK_CLIENT_ID") from None
    return value


def vk_client_external_event_id(
    *,
    group_id: int,
    user_id: int,
    conversation_message_id: int,
) -> str:
    """Stable Callback identity: group + user + conversation_message_id."""

    if (
        type(conversation_message_id) is not int
        or isinstance(conversation_message_id, bool)
        or conversation_message_id <= 0
    ):
        raise ValueError("INVALID_VK_CLIENT_ID") from None
    base = vk_client_external_conversation_id(group_id=group_id, user_id=user_id)
    value = f"{base}-{conversation_message_id}"
    if len(value) > 128:
        raise ValueError("INVALID_VK_CLIENT_ID") from None
    return value


@dataclass(frozen=True, slots=True, repr=False)
class VkClientNormalizedMessage:
    """Trusted private-dialog fields only. No tokens / raw provider blobs."""

    from_id: int
    peer_id: int
    text: str
    conversation_message_id: int
    occurred_at: datetime
    group_id: int

    def __post_init__(self) -> None:
        if type(self.from_id) is not int or self.from_id <= 0:
            raise ValueError("INVALID_VK_CLIENT_MESSAGE") from None
        if type(self.peer_id) is not int or self.peer_id <= 0:
            raise ValueError("INVALID_VK_CLIENT_MESSAGE") from None
        if self.from_id != self.peer_id:
            raise ValueError("INVALID_VK_CLIENT_MESSAGE") from None
        if type(self.text) is not str or not vk_client_text_is_ingress_safe(self.text):
            raise ValueError("INVALID_VK_CLIENT_MESSAGE") from None
        if (
            type(self.conversation_message_id) is not int
            or self.conversation_message_id <= 0
        ):
            raise ValueError("INVALID_VK_CLIENT_MESSAGE") from None
        if type(self.group_id) is not int or self.group_id <= 0:
            raise ValueError("INVALID_VK_CLIENT_MESSAGE") from None
        if type(self.occurred_at) is not datetime or self.occurred_at.tzinfo is None:
            raise ValueError("INVALID_VK_CLIENT_MESSAGE") from None

    @property
    def external_conversation_id(self) -> str:
        return vk_client_external_conversation_id(
            group_id=self.group_id,
            user_id=self.from_id,
        )

    @property
    def external_event_id(self) -> str:
        return vk_client_external_event_id(
            group_id=self.group_id,
            user_id=self.from_id,
            conversation_message_id=self.conversation_message_id,
        )

    def __repr__(self) -> str:
        return (
            "VkClientNormalizedMessage("
            "from_id=<redacted>, "
            "peer_id=<redacted>, "
            "text=<redacted>, "
            "conversation_message_id=<redacted>, "
            f"occurred_at={self.occurred_at.isoformat()!r}, "
            f"group_id={self.group_id!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class VkClientWebhookResult:
    kind: VkClientWebhookKind
    message: VkClientNormalizedMessage | None = None
    confirmation_response: str | None = None

    def __repr__(self) -> str:
        return (
            "VkClientWebhookResult("
            f"kind={self.kind.value!r}, "
            f"message={'<set>' if self.message is not None else None}, "
            "confirmation_response=<redacted>)"
        )


def utc_from_unix(ts: object) -> datetime | None:
    if type(ts) is not int or isinstance(ts, bool) or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
