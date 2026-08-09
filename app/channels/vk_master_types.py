"""Normalized VK master inbound DTOs (CURSOR-29). No SDK."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

__all__ = (
    "VkMasterWebhookKind",
    "VkMasterNormalizedMessage",
    "VkMasterWebhookResult",
)


class VkMasterWebhookKind(StrEnum):
    CONFIRMATION = "CONFIRMATION"
    MESSAGE = "MESSAGE"
    IGNORED = "IGNORED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True, repr=False)
class VkMasterNormalizedMessage:
    """Trusted private-dialog fields only. Reply target is peer_id."""

    from_id: int
    peer_id: int
    text: str
    external_message_id: str
    occurred_at: datetime
    group_id: int

    def __post_init__(self) -> None:
        if type(self.from_id) is not int or self.from_id <= 0:
            raise ValueError("INVALID_VK_MESSAGE") from None
        if type(self.peer_id) is not int or self.peer_id <= 0:
            raise ValueError("INVALID_VK_MESSAGE") from None
        if self.from_id != self.peer_id:
            raise ValueError("INVALID_VK_MESSAGE") from None
        if type(self.text) is not str or not self.text.strip():
            raise ValueError("INVALID_VK_MESSAGE") from None
        if type(self.external_message_id) is not str or not self.external_message_id:
            raise ValueError("INVALID_VK_MESSAGE") from None
        if type(self.group_id) is not int or self.group_id <= 0:
            raise ValueError("INVALID_VK_MESSAGE") from None
        if type(self.occurred_at) is not datetime or self.occurred_at.tzinfo is None:
            raise ValueError("INVALID_VK_MESSAGE") from None

    @property
    def external_account_id(self) -> str:
        return str(self.from_id)

    def __repr__(self) -> str:
        return (
            "VkMasterNormalizedMessage("
            "from_id=<redacted>, "
            "peer_id=<redacted>, "
            "text=<redacted>, "
            "external_message_id=<redacted>, "
            f"occurred_at={self.occurred_at.isoformat()!r}, "
            f"group_id={self.group_id!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class VkMasterWebhookResult:
    kind: VkMasterWebhookKind
    message: VkMasterNormalizedMessage | None = None
    confirmation_response: str | None = None

    def __repr__(self) -> str:
        return (
            "VkMasterWebhookResult("
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
