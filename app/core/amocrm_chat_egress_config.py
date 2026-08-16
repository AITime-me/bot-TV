"""amoCRM Chat egress gate (AMO-01B1).

Default-off. CLIENT_INBOUND (B1a) and BOT_OUTBOUND (B1b) share this gate.
BOT_OUTBOUND projects durable ``payload_json.text`` only after DELIVERED.
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
    "AMOCRM_CHAT_BOT_SENDER_ID",
    "AMOCRM_CHAT_BOT_SENDER_NAME",
    "DEFAULT_AMOCRM_CHAT_API_BASE_URL",
)

DEFAULT_AMOCRM_CHAT_API_BASE_URL: Final[str] = "https://amojo.amocrm.ru"
# Stable integration-side bot sender.id (not conversation-scoped).
AMOCRM_CHAT_BOT_SENDER_ID: Final[str] = "teya-bot"
AMOCRM_CHAT_BOT_SENDER_NAME: Final[str] = "Teya Bot (бот Тея)"
_SECRET_MIN: Final[int] = 16
_SECRET_MAX: Final[int] = 256
_SCOPE_MIN: Final[int] = 8
_SCOPE_MAX: Final[int] = 128
_BOT_ID_MIN: Final[int] = 8
_BOT_ID_MAX: Final[int] = 128


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


def _require_bot_id(value: str) -> str:
    """Registered amoCRM Chat bot id (sender.ref_id). Not a secret."""

    if type(value) is not str or not value:
        raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_BOT_ID_INVALID") from None
    if len(value) < _BOT_ID_MIN or len(value) > _BOT_ID_MAX:
        raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_BOT_ID_INVALID") from None
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in value):
        raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_BOT_ID_INVALID") from None
    if any(ch.isspace() for ch in value):
        raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_BOT_ID_INVALID") from None
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
    bot_id: str | None = None
    api_base_url: str = DEFAULT_AMOCRM_CHAT_API_BASE_URL

    def __repr__(self) -> str:
        return (
            "AmoCrmChatEgressConfig("
            f"enabled={self.enabled!r}, "
            "channel_secret=<redacted>, "
            "scope_id=<redacted>, "
            f"bot_id={'set' if self.bot_id else None}, "
            f"api_base_url={self.api_base_url!r})"
        )

    def require_runtime(self) -> None:
        if not self.enabled:
            raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_EGRESS_DISABLED") from None
        if self.channel_secret is None:
            raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_SECRET_INVALID") from None
        if self.scope_id is None:
            raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_SCOPE_INVALID") from None

    def require_bot_id_for_outbound(self) -> str:
        """Fail closed when BOT_OUTBOUND needs the registered Chat bot id."""

        if self.bot_id is None:
            raise AmoCrmChatEgressConfigError("AMOCRM_CHAT_BOT_ID_REQUIRED") from None
        return self.bot_id

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

        bot_raw = source.get("AMOCRM_CHAT_BOT_ID")
        bot_id: str | None = None
        if bot_raw is not None and bot_raw != "":
            bot_id = _require_bot_id(bot_raw)

        base_raw = source.get(
            "AMOCRM_CHAT_API_BASE_URL",
            DEFAULT_AMOCRM_CHAT_API_BASE_URL,
        )
        api_base_url = _require_base_url(base_raw)
        return cls(
            enabled=True,
            channel_secret=secret,
            scope_id=scope_id,
            bot_id=bot_id,
            api_base_url=api_base_url,
        )
