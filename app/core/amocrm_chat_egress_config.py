"""amoCRM Chat egress gate (AMO-01B1a).

Default-off. CLIENT_INBOUND projection only. BOT_OUTBOUND deferred.
Reuses AMOCRM_CHAT_CHANNEL_SECRET. No OAuth / CRM REST.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

__all__ = (
    "AmoCrmChatEgressConfig",
    "AmoCrmChatEgressConfigError",
    "DEFAULT_AMOCRM_CHAT_API_BASE_URL",
)

DEFAULT_AMOCRM_CHAT_API_BASE_URL: Final[str] = "https://amojo.amocrm.ru"
_SECRET_MIN: Final[int] = 16
_SECRET_MAX: Final[int] = 256
_SCOPE_MIN: Final[int] = 8
_SCOPE_MAX: Final[int] = 128


class AmoCrmChatEgressConfigError(ValueError):
    """Fixed codes only — never embed secrets or scope values."""

    def __init__(self, code: str = "AMOCRM_CHAT_EGRESS_CONFIG_INVALID") -> None:
        super().__init__(code)

    def __repr__(self) -> str:
        return f"AmoCrmChatEgressConfigError({self.args[0]!r})"


def _require_channel_secret(value: str) -> str:
    if type(value) is not str or not value:
        raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_SECRET_INVALID") from None
    if len(value) < _SECRET_MIN or len(value) > _SECRET_MAX:
        raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_SECRET_INVALID") from None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_SECRET_INVALID") from None
    if any(ch.isspace() for ch in value):
        raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_SECRET_INVALID") from None
    return value


def _require_scope_id(value: str) -> str:
    if type(value) is not str or not value:
        raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_SCOPE_INVALID") from None
    if len(value) < _SCOPE_MIN or len(value) > _SCOPE_MAX:
        raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_SCOPE_INVALID") from None
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in value):
        raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_SCOPE_INVALID") from None
    if any(ch.isspace() for ch in value):
        raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_SCOPE_INVALID") from None
    return value


def _require_base_url(value: str) -> str:
    if type(value) is not str or not value:
        raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_API_BASE_INVALID") from None
    if any(ch.isspace() for ch in value) or any(ord(ch) < 32 for ch in value):
        raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_API_BASE_INVALID") from None
    if not value.startswith("https://"):
        raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_API_BASE_INVALID") from None
    if value.endswith("/"):
        value = value.rstrip("/")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmChatEgressConfig:
    """Trusted Chat egress settings. Secret never appears in repr."""

    enabled: bool = False
    channel_secret: str | None = None
    scope_id: str | None = None
    api_base_url: str = DEFAULT_AMOCRM_CHAT_API_BASE_URL

    def __repr__(self) -> str:
        return (
            "AmoCrmChatEgressConfig("
            f"enabled={self.enabled!r}, "
            "channel_secret=<redacted>, "
            "scope_id=<redacted>, "
            f"api_base_url={self.api_base_url!r})"
        )

    def require_runtime(self) -> None:
        if not self.enabled:
            raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_EGRESS_DISABLED") from None
        if self.channel_secret is None:
            raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_SECRET_INVALID") from None
        if self.scope_id is None:
            raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_SCOPE_INVALID") from None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> AmoCrmChatEgressConfig:
        source = os.environ if environ is None else environ
        enabled_raw = source.get("AMOCRM_CHAT_EGRESS_ENABLED", "false")
        if enabled_raw == "false":
            return cls(enabled=False)
        if enabled_raw != "true":
            raise AmoCrmChatEgressConfigError(
                "AMOCRM_CHAT_EGRESS_CONFIG_INVALID"
            ) from None

        secret_raw = source.get("AMOCRM_CHAT_CHANNEL_SECRET")
        if secret_raw is None or secret_raw == "":
            raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_SECRET_REQUIRED") from None
        secret = _require_channel_secret(secret_raw)

        scope_raw = source.get("AMOCRM_CHAT_SCOPE_ID")
        if scope_raw is None or scope_raw == "":
            raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_SCOPE_REQUIRED") from None
        scope_id = _require_scope_id(scope_raw)

        base_raw = source.get(
            "AMOCRM_CHAT_API_BASE_URL",
            DEFAULT_AMOCRM_CHAT_API_BASE_URL,
        )
        api_base_url = _require_base_url(base_raw)
        return cls(
            enabled=True,
            channel_secret=secret,
            scope_id=scope_id,
            api_base_url=api_base_url,
        )
