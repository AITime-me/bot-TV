"""VK CLIENT Callback types — inbound message_new and outgoing message_reply."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Final

__all__ = (
    "VK_CLIENT_TEXT_MAX_LEN",
    "VkClientWebhookKind",
    "VkClientNormalizedMessage",
    "VkClientNormalizedMessageReply",
    "VkClientWebhookResult",
    "utc_from_unix",
    "vk_client_external_conversation_id",
    "vk_client_external_event_id",
    "vk_client_external_reply_event_id",
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
    MESSAGE_REPLY = "MESSAGE_REPLY"
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


def vk_client_external_reply_event_id(
    *,
    group_id: int,
    user_id: int,
    conversation_message_id: int,
) -> str:
    """Stable message_reply identity (separate namespace from message_new)."""

    if (
        type(conversation_message_id) is not int
        or isinstance(conversation_message_id, bool)
        or conversation_message_id <= 0
    ):
        raise ValueError("INVALID_VK_CLIENT_ID") from None
    base = vk_client_external_conversation_id(group_id=group_id, user_id=user_id)
    value = f"{base}-r-{conversation_message_id}"
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
class VkClientNormalizedMessageReply:
    """Technical outgoing community reply. No text / attachments / PII."""

    group_id: int
    peer_id: int
    conversation_message_id: int
    provider_message_id: int
    occurred_at: datetime
    random_id: int | None
    payload: object | None

    def __post_init__(self) -> None:
        if type(self.group_id) is not int or self.group_id <= 0:
            raise ValueError("INVALID_VK_CLIENT_REPLY") from None
        if type(self.peer_id) is not int or self.peer_id <= 0:
            raise ValueError("INVALID_VK_CLIENT_REPLY") from None
        if (
            type(self.conversation_message_id) is not int
            or self.conversation_message_id <= 0
        ):
            raise ValueError("INVALID_VK_CLIENT_REPLY") from None
        if type(self.provider_message_id) is not int or self.provider_message_id <= 0:
            raise ValueError("INVALID_VK_CLIENT_REPLY") from None
        if type(self.occurred_at) is not datetime or self.occurred_at.tzinfo is None:
            raise ValueError("INVALID_VK_CLIENT_REPLY") from None
        if self.random_id is not None and (
            type(self.random_id) is not int
            or isinstance(self.random_id, bool)
            or self.random_id < 0
        ):
            raise ValueError("INVALID_VK_CLIENT_REPLY") from None

    @property
    def external_conversation_id(self) -> str:
        return vk_client_external_conversation_id(
            group_id=self.group_id,
            user_id=self.peer_id,
        )

    @property
    def external_event_id(self) -> str:
        return vk_client_external_reply_event_id(
            group_id=self.group_id,
            user_id=self.peer_id,
            conversation_message_id=self.conversation_message_id,
        )

    def technical_envelope(self) -> dict[str, Any]:
        """Storage-only technical fields — never includes text/raw body."""

        envelope: dict[str, Any] = {
            "schema": "vk.client.message_reply.v1",
            "event_type": "VK_CLIENT_MESSAGE_REPLY",
            "group_id": self.group_id,
            "peer_id": self.peer_id,
            "conversation_message_id": self.conversation_message_id,
            "provider_message_id": self.provider_message_id,
            "occurred_at": self.occurred_at.isoformat(),
            "external_conversation_id": self.external_conversation_id,
        }
        if self.random_id is not None:
            envelope["random_id"] = self.random_id
        # Bounded provenance only: keep raw payload object if small dict/str,
        # never text/attachments. Verification reads this field.
        if self.payload is not None:
            if type(self.payload) is dict and len(self.payload) <= 8:
                envelope["payload"] = self.payload
            elif type(self.payload) is str and 0 < len(self.payload) <= 1000:
                envelope["payload"] = self.payload
        return envelope

    def __repr__(self) -> str:
        return (
            "VkClientNormalizedMessageReply("
            f"group_id={self.group_id!r}, "
            "peer_id=<redacted>, "
            "conversation_message_id=<redacted>, "
            "provider_message_id=<redacted>, "
            f"occurred_at={self.occurred_at.isoformat()!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class VkClientWebhookResult:
    kind: VkClientWebhookKind
    message: VkClientNormalizedMessage | None = None
    message_reply: VkClientNormalizedMessageReply | None = None
    confirmation_response: str | None = None

    def __repr__(self) -> str:
        return (
            "VkClientWebhookResult("
            f"kind={self.kind.value!r}, "
            f"message={'<set>' if self.message is not None else None}, "
            f"message_reply={'<set>' if self.message_reply is not None else None}, "
            "confirmation_response=<redacted>)"
        )


def utc_from_unix(ts: object) -> datetime | None:
    if type(ts) is not int or isinstance(ts, bool) or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
