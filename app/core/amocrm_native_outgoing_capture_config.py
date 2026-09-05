"""Native amoCRM CRM Platform outgoing-message CAPTURE gate.

Default-off. Ephemeral path_token is Bot-TV URL secrecy only — not Chat HMAC
and not a claimed amoCRM CRM Platform signature. Never put token in repr/logs.
Does not authorize FSM, Chat bindings, CRM writes, or VK outbound.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

__all__ = (
    "AmoCrmNativeOutgoingCaptureConfig",
    "AmoCrmNativeOutgoingCaptureConfigError",
)

_TOKEN_MIN: Final[int] = 32
_TOKEN_MAX: Final[int] = 256
_CHAT_ID_MAX: Final[int] = 128
_ORIGIN_MAX: Final[int] = 64


class AmoCrmNativeOutgoingCaptureConfigError(ValueError):
    """Fixed message only — never embed the path token."""

    def __init__(
        self, code: str = "AMOCRM_NATIVE_OUTGOING_CAPTURE_CONFIG_INVALID"
    ) -> None:
        super().__init__(code)

    def __repr__(self) -> str:
        return f"AmoCrmNativeOutgoingCaptureConfigError({self.args[0]!r})"


def _require_path_token(value: str) -> str:
    if type(value) is not str or not value:
        raise AmoCrmNativeOutgoingCaptureConfigError(
            "AMOCRM_NATIVE_OUTGOING_CAPTURE_PATH_TOKEN_INVALID"
        ) from None
    if len(value) < _TOKEN_MIN or len(value) > _TOKEN_MAX:
        raise AmoCrmNativeOutgoingCaptureConfigError(
            "AMOCRM_NATIVE_OUTGOING_CAPTURE_PATH_TOKEN_INVALID"
        ) from None
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in value):
        raise AmoCrmNativeOutgoingCaptureConfigError(
            "AMOCRM_NATIVE_OUTGOING_CAPTURE_PATH_TOKEN_INVALID"
        ) from None
    if any(ch.isspace() for ch in value):
        raise AmoCrmNativeOutgoingCaptureConfigError(
            "AMOCRM_NATIVE_OUTGOING_CAPTURE_PATH_TOKEN_INVALID"
        ) from None
    return value


def _require_positive_int_token(value: str, *, code: str) -> int:
    if type(value) is not str or not value:
        raise AmoCrmNativeOutgoingCaptureConfigError(code) from None
    if not value.isdigit() or value.startswith("0"):
        raise AmoCrmNativeOutgoingCaptureConfigError(code) from None
    parsed = int(value)
    if parsed <= 0:
        raise AmoCrmNativeOutgoingCaptureConfigError(code) from None
    return parsed


def _require_chat_id(value: str) -> str:
    if type(value) is not str or not value:
        raise AmoCrmNativeOutgoingCaptureConfigError(
            "AMOCRM_NATIVE_OUTGOING_CAPTURE_CHAT_ID_INVALID"
        ) from None
    if len(value) > _CHAT_ID_MAX:
        raise AmoCrmNativeOutgoingCaptureConfigError(
            "AMOCRM_NATIVE_OUTGOING_CAPTURE_CHAT_ID_INVALID"
        ) from None
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in value):
        raise AmoCrmNativeOutgoingCaptureConfigError(
            "AMOCRM_NATIVE_OUTGOING_CAPTURE_CHAT_ID_INVALID"
        ) from None
    if any(ch.isspace() for ch in value):
        raise AmoCrmNativeOutgoingCaptureConfigError(
            "AMOCRM_NATIVE_OUTGOING_CAPTURE_CHAT_ID_INVALID"
        ) from None
    # UUID-shaped technical id: alnum + hyphen only.
    stripped = value.replace("-", "")
    if not stripped or not stripped.isalnum():
        raise AmoCrmNativeOutgoingCaptureConfigError(
            "AMOCRM_NATIVE_OUTGOING_CAPTURE_CHAT_ID_INVALID"
        ) from None
    return value


def _require_origin(value: str) -> str:
    if type(value) is not str or not value:
        raise AmoCrmNativeOutgoingCaptureConfigError(
            "AMOCRM_NATIVE_OUTGOING_CAPTURE_ORIGIN_INVALID"
        ) from None
    if len(value) > _ORIGIN_MAX:
        raise AmoCrmNativeOutgoingCaptureConfigError(
            "AMOCRM_NATIVE_OUTGOING_CAPTURE_ORIGIN_INVALID"
        ) from None
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in value):
        raise AmoCrmNativeOutgoingCaptureConfigError(
            "AMOCRM_NATIVE_OUTGOING_CAPTURE_ORIGIN_INVALID"
        ) from None
    if any(ch.isspace() for ch in value):
        raise AmoCrmNativeOutgoingCaptureConfigError(
            "AMOCRM_NATIVE_OUTGOING_CAPTURE_ORIGIN_INVALID"
        ) from None
    return value


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmNativeOutgoingCaptureConfig:
    """Trusted CAPTURE-ONLY allowlist. Path token never appears in repr."""

    enabled: bool = False
    path_token: str | None = None
    talk_id: int | None = None
    chat_id: str | None = None
    contact_id: int | None = None
    origin: str | None = None
    source_id: int | None = None

    def __repr__(self) -> str:
        return (
            "AmoCrmNativeOutgoingCaptureConfig("
            f"enabled={self.enabled!r}, "
            "path_token=<redacted>, "
            f"talk_id={self.talk_id!r}, "
            f"chat_id={self.chat_id!r}, "
            f"contact_id={self.contact_id!r}, "
            f"origin={self.origin!r}, "
            f"source_id={self.source_id!r})"
        )

    def require_runtime(self) -> None:
        if not self.enabled:
            raise AmoCrmNativeOutgoingCaptureConfigError(
                "AMOCRM_NATIVE_OUTGOING_CAPTURE_DISABLED"
            ) from None
        if self.path_token is None:
            raise AmoCrmNativeOutgoingCaptureConfigError(
                "AMOCRM_NATIVE_OUTGOING_CAPTURE_PATH_TOKEN_INVALID"
            ) from None
        if (
            self.talk_id is None
            or self.chat_id is None
            or self.contact_id is None
            or self.origin is None
            or self.source_id is None
        ):
            raise AmoCrmNativeOutgoingCaptureConfigError(
                "AMOCRM_NATIVE_OUTGOING_CAPTURE_CONFIG_INVALID"
            ) from None

    def matches_path_token(self, provided: object) -> bool:
        if not self.enabled or self.path_token is None:
            return False
        if type(provided) is not str or not provided:
            return False
        try:
            return hmac.compare_digest(provided, self.path_token)
        except (TypeError, ValueError):
            return False

    def matches_allowlist(
        self,
        *,
        talk_id: int,
        chat_id: str,
        contact_id: int,
        origin: str,
        source_id: int | None,
    ) -> bool:
        """Exact fail-closed target match. Absent webhook source_id is allowed."""

        if not self.enabled:
            return False
        if (
            self.talk_id is None
            or self.chat_id is None
            or self.contact_id is None
            or self.origin is None
            or self.source_id is None
        ):
            return False
        if talk_id != self.talk_id:
            return False
        if chat_id != self.chat_id:
            return False
        if contact_id != self.contact_id:
            return False
        if origin != self.origin:
            return False
        if source_id is not None and source_id != self.source_id:
            return False
        return True

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None
    ) -> AmoCrmNativeOutgoingCaptureConfig:
        source = os.environ if environ is None else environ
        enabled_raw = source.get(
            "AMOCRM_NATIVE_OUTGOING_CAPTURE_ENABLED", "false"
        )
        if enabled_raw == "false":
            return cls(enabled=False)
        if enabled_raw != "true":
            raise AmoCrmNativeOutgoingCaptureConfigError(
                "AMOCRM_NATIVE_OUTGOING_CAPTURE_CONFIG_INVALID"
            ) from None

        token_raw = source.get("AMOCRM_NATIVE_OUTGOING_CAPTURE_PATH_TOKEN")
        if token_raw is None or token_raw == "":
            raise AmoCrmNativeOutgoingCaptureConfigError(
                "AMOCRM_NATIVE_OUTGOING_CAPTURE_PATH_TOKEN_REQUIRED"
            ) from None
        path_token = _require_path_token(token_raw)

        talk_raw = source.get("AMOCRM_NATIVE_OUTGOING_CAPTURE_TALK_ID")
        if talk_raw is None or talk_raw == "":
            raise AmoCrmNativeOutgoingCaptureConfigError(
                "AMOCRM_NATIVE_OUTGOING_CAPTURE_TALK_ID_REQUIRED"
            ) from None
        talk_id = _require_positive_int_token(
            talk_raw,
            code="AMOCRM_NATIVE_OUTGOING_CAPTURE_TALK_ID_INVALID",
        )

        chat_raw = source.get("AMOCRM_NATIVE_OUTGOING_CAPTURE_CHAT_ID")
        if chat_raw is None or chat_raw == "":
            raise AmoCrmNativeOutgoingCaptureConfigError(
                "AMOCRM_NATIVE_OUTGOING_CAPTURE_CHAT_ID_REQUIRED"
            ) from None
        chat_id = _require_chat_id(chat_raw)

        contact_raw = source.get("AMOCRM_NATIVE_OUTGOING_CAPTURE_CONTACT_ID")
        if contact_raw is None or contact_raw == "":
            raise AmoCrmNativeOutgoingCaptureConfigError(
                "AMOCRM_NATIVE_OUTGOING_CAPTURE_CONTACT_ID_REQUIRED"
            ) from None
        contact_id = _require_positive_int_token(
            contact_raw,
            code="AMOCRM_NATIVE_OUTGOING_CAPTURE_CONTACT_ID_INVALID",
        )

        origin_raw = source.get("AMOCRM_NATIVE_OUTGOING_CAPTURE_ORIGIN")
        if origin_raw is None or origin_raw == "":
            raise AmoCrmNativeOutgoingCaptureConfigError(
                "AMOCRM_NATIVE_OUTGOING_CAPTURE_ORIGIN_REQUIRED"
            ) from None
        origin = _require_origin(origin_raw)

        source_raw = source.get("AMOCRM_NATIVE_OUTGOING_CAPTURE_SOURCE_ID")
        if source_raw is None or source_raw == "":
            raise AmoCrmNativeOutgoingCaptureConfigError(
                "AMOCRM_NATIVE_OUTGOING_CAPTURE_SOURCE_ID_REQUIRED"
            ) from None
        source_id = _require_positive_int_token(
            source_raw,
            code="AMOCRM_NATIVE_OUTGOING_CAPTURE_SOURCE_ID_INVALID",
        )

        return cls(
            enabled=True,
            path_token=path_token,
            talk_id=talk_id,
            chat_id=chat_id,
            contact_id=contact_id,
            origin=origin,
            source_id=source_id,
        )
