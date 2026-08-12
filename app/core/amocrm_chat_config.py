"""amoCRM Chat webhook exposure gate (AMO-01A).

Default-off. Channel secret is server-only and never appears in repr/logs.
Does not authorize CRM REST, OAuth, or outbound amoCRM HTTP.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

__all__ = (
    "AmoCrmChatConfig",
    "AmoCrmChatConfigError",
    "AMOCRM_CHAT_SIGNATURE_HEADER",
)

AMOCRM_CHAT_SIGNATURE_HEADER: Final[str] = "X-Signature"
_SECRET_MIN: Final[int] = 16
_SECRET_MAX: Final[int] = 256


class AmoCrmChatConfigError(ValueError):
    """Fixed message only — never embed the channel secret."""

    def __init__(self, code: str = "AMOCRM_CHAT_CONFIG_INVALID") -> None:
        super().__init__(code)

    def __repr__(self) -> str:
        return f"AmoCrmChatConfigError({self.args[0]!r})"


def _require_channel_secret(value: str) -> str:
    if type(value) is not str or not value:
        raise AmoCrmChatConfigError("AMOCRM_CHAT_SECRET_INVALID") from None
    if len(value) < _SECRET_MIN or len(value) > _SECRET_MAX:
        raise AmoCrmChatConfigError("AMOCRM_CHAT_SECRET_INVALID") from None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise AmoCrmChatConfigError("AMOCRM_CHAT_SECRET_INVALID") from None
    if any(ch.isspace() for ch in value):
        raise AmoCrmChatConfigError("AMOCRM_CHAT_SECRET_INVALID") from None
    return value


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmChatConfig:
    """Trusted amoCRM Chat webhook settings. Secret never appears in repr."""

    enabled: bool = False
    channel_secret: str | None = None

    def __repr__(self) -> str:
        return (
            "AmoCrmChatConfig("
            f"enabled={self.enabled!r}, "
            "channel_secret=<redacted>)"
        )

    def require_runtime(self) -> None:
        if not self.enabled:
            raise AmoCrmChatConfigError("AMOCRM_CHAT_DISABLED") from None
        if self.channel_secret is None:
            raise AmoCrmChatConfigError("AMOCRM_CHAT_SECRET_INVALID") from None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AmoCrmChatConfig:
        source = os.environ if environ is None else environ
        enabled_raw = source.get("AMOCRM_CHAT_WEBHOOK_ENABLED", "false")
        if enabled_raw == "false":
            return cls(enabled=False, channel_secret=None)
        if enabled_raw != "true":
            raise AmoCrmChatConfigError("AMOCRM_CHAT_CONFIG_INVALID") from None

        secret_raw = source.get("AMOCRM_CHAT_CHANNEL_SECRET")
        if secret_raw is None or secret_raw == "":
            raise AmoCrmChatConfigError("AMOCRM_CHAT_SECRET_REQUIRED") from None
        secret = _require_channel_secret(secret_raw)
        return cls(enabled=True, channel_secret=secret)
