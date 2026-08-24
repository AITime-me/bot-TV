"""Yandex GPT / AI Studio LLM config (default-off, fail-closed).

Env contract:
- YANDEX_LLM_ENABLED=false (default) → disabled; no HTTP; secrets optional
- YANDEX_LLM_ENABLED=true → requires YANDEX_API_KEY + YANDEX_FOLDER_ID
- Optional: YANDEX_MODEL_URI, YANDEX_LLM_API_BASE_URL, timeout, max bytes,
  temperature, max_tokens

Partial enabled configuration fails closed (raises). Secrets never appear in repr.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit, urlunsplit

__all__ = (
    "DEFAULT_YANDEX_LLM_API_BASE_URL",
    "DEFAULT_YANDEX_LLM_MAX_RESPONSE_BYTES",
    "DEFAULT_YANDEX_LLM_MAX_TOKENS",
    "DEFAULT_YANDEX_LLM_TEMPERATURE",
    "DEFAULT_YANDEX_LLM_TIMEOUT_SECONDS",
    "YandexLlmConfig",
    "YandexLlmConfigError",
    "default_yandex_model_uri",
)

DEFAULT_YANDEX_LLM_API_BASE_URL: Final[str] = "https://llm.api.cloud.yandex.net"
COMPLETION_ROUTE_PATH: Final[str] = "/foundationModels/v1/completion"
DEFAULT_YANDEX_LLM_TIMEOUT_SECONDS: Final[float] = 15.0
DEFAULT_YANDEX_LLM_MAX_RESPONSE_BYTES: Final[int] = 65_536
DEFAULT_YANDEX_LLM_TEMPERATURE: Final[float] = 0.3
DEFAULT_YANDEX_LLM_MAX_TOKENS: Final[int] = 1024

_API_KEY_MIN: Final[int] = 8
_API_KEY_MAX: Final[int] = 512
_FOLDER_ID_MIN: Final[int] = 4
_FOLDER_ID_MAX: Final[int] = 64
_PRINTABLE_ASCII_RE: Final[re.Pattern[str]] = re.compile(r"^[\x21-\x7E]+$")
_FOLDER_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")
_MODEL_URI_RE: Final[re.Pattern[str]] = re.compile(
    r"^gpt://[A-Za-z0-9_-]+/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)?$"
)


class YandexLlmConfigError(ValueError):
    """Fail-closed config error. Message is a fixed code only."""

    def __init__(self, code: str = "YANDEX_LLM_CONFIG_INVALID") -> None:
        if type(code) is not str or not code:
            super().__init__("YANDEX_LLM_CONFIG_INVALID")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "YANDEX_LLM_CONFIG_INVALID"

    def __repr__(self) -> str:
        return f"YandexLlmConfigError({self.code!r})"

    def __str__(self) -> str:
        return self.code


def default_yandex_model_uri(folder_id: str) -> str:
    if type(folder_id) is not str or not folder_id:
        raise YandexLlmConfigError("YANDEX_FOLDER_ID_INVALID") from None
    return f"gpt://{folder_id}/yandexgpt/latest"


def _contains_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _require_api_key(value: str) -> str:
    if type(value) is not str or not value:
        raise YandexLlmConfigError("YANDEX_API_KEY_REQUIRED") from None
    if (
        len(value) < _API_KEY_MIN
        or len(value) > _API_KEY_MAX
        or _PRINTABLE_ASCII_RE.fullmatch(value) is None
    ):
        raise YandexLlmConfigError("YANDEX_API_KEY_INVALID") from None
    return value


def _require_folder_id(value: str) -> str:
    if type(value) is not str or not value:
        raise YandexLlmConfigError("YANDEX_FOLDER_ID_REQUIRED") from None
    if (
        len(value) < _FOLDER_ID_MIN
        or len(value) > _FOLDER_ID_MAX
        or _FOLDER_ID_RE.fullmatch(value) is None
    ):
        raise YandexLlmConfigError("YANDEX_FOLDER_ID_INVALID") from None
    return value


def _require_model_uri(value: str) -> str:
    if type(value) is not str or not value:
        raise YandexLlmConfigError("YANDEX_MODEL_URI_INVALID") from None
    if _contains_control_chars(value) or any(ch.isspace() for ch in value):
        raise YandexLlmConfigError("YANDEX_MODEL_URI_INVALID") from None
    if _MODEL_URI_RE.fullmatch(value) is None:
        raise YandexLlmConfigError("YANDEX_MODEL_URI_INVALID") from None
    return value


def _require_base_url(value: str) -> str:
    if type(value) is not str or not value or any(ch.isspace() for ch in value):
        raise YandexLlmConfigError("YANDEX_LLM_API_BASE_INVALID") from None
    if _contains_control_chars(value) or "\\" in value:
        raise YandexLlmConfigError("YANDEX_LLM_API_BASE_INVALID") from None
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise YandexLlmConfigError("YANDEX_LLM_API_BASE_INVALID") from None
    if not parts.hostname:
        raise YandexLlmConfigError("YANDEX_LLM_API_BASE_INVALID") from None
    if parts.username is not None or parts.password is not None:
        raise YandexLlmConfigError("YANDEX_LLM_API_BASE_INVALID") from None
    if parts.query or parts.fragment:
        raise YandexLlmConfigError("YANDEX_LLM_API_BASE_INVALID") from None
    path = parts.path if parts.path else ""
    if path not in ("", "/"):
        raise YandexLlmConfigError("YANDEX_LLM_API_BASE_INVALID") from None
    try:
        port = parts.port
    except ValueError:
        raise YandexLlmConfigError("YANDEX_LLM_API_BASE_INVALID") from None
    if port is not None and (port <= 0 or port > 65535):
        raise YandexLlmConfigError("YANDEX_LLM_API_BASE_INVALID") from None
    host = parts.hostname
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]" if port is None else f"[{host}]:{port}"
    else:
        netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, "", "", ""))


def _require_timeout(raw: object) -> float:
    if type(raw) is not float and type(raw) is not int:
        raise YandexLlmConfigError("YANDEX_LLM_TIMEOUT_INVALID") from None
    value = float(raw)
    if value != value or value <= 0.0 or value > 120.0:
        raise YandexLlmConfigError("YANDEX_LLM_TIMEOUT_INVALID") from None
    return value


def _require_max_response_bytes(raw: object) -> int:
    if type(raw) is not int or isinstance(raw, bool):
        raise YandexLlmConfigError("YANDEX_LLM_MAX_RESPONSE_BYTES_INVALID") from None
    if raw <= 0 or raw > 1_000_000:
        raise YandexLlmConfigError("YANDEX_LLM_MAX_RESPONSE_BYTES_INVALID") from None
    return raw


def _require_temperature(raw: object) -> float:
    if type(raw) is not float and type(raw) is not int:
        raise YandexLlmConfigError("YANDEX_LLM_TEMPERATURE_INVALID") from None
    value = float(raw)
    if value != value or value < 0.0 or value > 1.0:
        raise YandexLlmConfigError("YANDEX_LLM_TEMPERATURE_INVALID") from None
    return value


def _require_max_tokens(raw: object) -> int:
    if type(raw) is not int or isinstance(raw, bool):
        raise YandexLlmConfigError("YANDEX_LLM_MAX_TOKENS_INVALID") from None
    if raw < 1 or raw > 8000:
        raise YandexLlmConfigError("YANDEX_LLM_MAX_TOKENS_INVALID") from None
    return raw


def _parse_bool_enabled(raw: str) -> bool:
    if raw == "false":
        return False
    if raw == "true":
        return True
    raise YandexLlmConfigError("YANDEX_LLM_ENABLED_INVALID") from None


def _parse_float_env(name: str, raw: str, *, minimum: float, maximum: float) -> float:
    if raw in {"true", "false", "True", "False"}:
        raise YandexLlmConfigError(f"{name}_INVALID") from None
    try:
        parsed = float(raw)
    except ValueError:
        raise YandexLlmConfigError(f"{name}_INVALID") from None
    if parsed != parsed or parsed < minimum or parsed > maximum:
        raise YandexLlmConfigError(f"{name}_INVALID") from None
    return parsed


def _parse_int_env(name: str, raw: str, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(raw)
    except ValueError:
        raise YandexLlmConfigError(f"{name}_INVALID") from None
    if parsed < minimum or parsed > maximum:
        raise YandexLlmConfigError(f"{name}_INVALID") from None
    return parsed


@dataclass(frozen=True, slots=True, repr=False)
class YandexLlmConfig:
    """Constructor config for the Yandex GPT completion client. No HTTP I/O."""

    enabled: bool = False
    api_key: str | None = None
    folder_id: str | None = None
    model_uri: str | None = None
    api_base_url: str = DEFAULT_YANDEX_LLM_API_BASE_URL
    timeout_seconds: float = DEFAULT_YANDEX_LLM_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_YANDEX_LLM_MAX_RESPONSE_BYTES
    temperature: float = DEFAULT_YANDEX_LLM_TEMPERATURE
    max_tokens: int = DEFAULT_YANDEX_LLM_MAX_TOKENS

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise YandexLlmConfigError("YANDEX_LLM_CONFIG_INVALID") from None
        object.__setattr__(
            self, "api_base_url", _require_base_url(self.api_base_url)
        )
        object.__setattr__(
            self, "timeout_seconds", _require_timeout(self.timeout_seconds)
        )
        object.__setattr__(
            self,
            "max_response_bytes",
            _require_max_response_bytes(self.max_response_bytes),
        )
        object.__setattr__(
            self, "temperature", _require_temperature(self.temperature)
        )
        object.__setattr__(self, "max_tokens", _require_max_tokens(self.max_tokens))

        if not self.enabled:
            object.__setattr__(self, "api_key", None)
            object.__setattr__(self, "folder_id", None)
            object.__setattr__(self, "model_uri", None)
            return

        if self.api_key is None or self.api_key == "":
            raise YandexLlmConfigError("YANDEX_API_KEY_REQUIRED") from None
        if self.folder_id is None or self.folder_id == "":
            raise YandexLlmConfigError("YANDEX_FOLDER_ID_REQUIRED") from None
        api_key = _require_api_key(self.api_key)
        folder_id = _require_folder_id(self.folder_id)
        if self.model_uri is None or self.model_uri == "":
            model_uri = default_yandex_model_uri(folder_id)
        else:
            model_uri = _require_model_uri(self.model_uri)
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "folder_id", folder_id)
        object.__setattr__(self, "model_uri", model_uri)

    @property
    def completion_url(self) -> str:
        return f"{self.api_base_url}{COMPLETION_ROUTE_PATH}"

    def require_runtime(self) -> None:
        if not self.enabled:
            raise YandexLlmConfigError("YANDEX_LLM_DISABLED") from None
        if self.api_key is None or self.folder_id is None or self.model_uri is None:
            raise YandexLlmConfigError("YANDEX_LLM_CONFIG_INVALID") from None

    def __repr__(self) -> str:
        return (
            "YandexLlmConfig("
            f"enabled={self.enabled!r}, "
            "api_key=<redacted>, "
            "folder_id=<redacted>, "
            "model_uri=<redacted>, "
            "api_base_url=<redacted>, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"max_response_bytes={self.max_response_bytes!r}, "
            f"temperature={self.temperature!r}, "
            f"max_tokens={self.max_tokens!r})"
        )

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> YandexLlmConfig:
        source = os.environ if environ is None else environ
        enabled_raw = source.get("YANDEX_LLM_ENABLED", "false")
        enabled = _parse_bool_enabled(enabled_raw)
        if not enabled:
            return cls(enabled=False)

        api_key_raw = source.get("YANDEX_API_KEY")
        folder_raw = source.get("YANDEX_FOLDER_ID")
        if api_key_raw is None or api_key_raw == "":
            raise YandexLlmConfigError("YANDEX_API_KEY_REQUIRED") from None
        if folder_raw is None or folder_raw == "":
            raise YandexLlmConfigError("YANDEX_FOLDER_ID_REQUIRED") from None

        model_uri_raw = source.get("YANDEX_MODEL_URI")
        if model_uri_raw is not None and model_uri_raw == "":
            model_uri_raw = None

        base_raw = source.get(
            "YANDEX_LLM_API_BASE_URL",
            DEFAULT_YANDEX_LLM_API_BASE_URL,
        )

        timeout_raw = source.get("YANDEX_LLM_TIMEOUT_SECONDS")
        if timeout_raw is None:
            timeout_seconds = DEFAULT_YANDEX_LLM_TIMEOUT_SECONDS
        else:
            timeout_seconds = _parse_float_env(
                "YANDEX_LLM_TIMEOUT",
                timeout_raw,
                minimum=0.0,
                maximum=120.0,
            )
            if timeout_seconds <= 0.0:
                raise YandexLlmConfigError("YANDEX_LLM_TIMEOUT_INVALID") from None

        max_bytes_raw = source.get("YANDEX_LLM_MAX_RESPONSE_BYTES")
        if max_bytes_raw is None:
            max_response_bytes = DEFAULT_YANDEX_LLM_MAX_RESPONSE_BYTES
        else:
            max_response_bytes = _parse_int_env(
                "YANDEX_LLM_MAX_RESPONSE_BYTES",
                max_bytes_raw,
                minimum=1,
                maximum=1_000_000,
            )

        temperature_raw = source.get("YANDEX_LLM_TEMPERATURE")
        if temperature_raw is None:
            temperature = DEFAULT_YANDEX_LLM_TEMPERATURE
        else:
            temperature = _parse_float_env(
                "YANDEX_LLM_TEMPERATURE",
                temperature_raw,
                minimum=0.0,
                maximum=1.0,
            )

        max_tokens_raw = source.get("YANDEX_LLM_MAX_TOKENS")
        if max_tokens_raw is None:
            max_tokens = DEFAULT_YANDEX_LLM_MAX_TOKENS
        else:
            max_tokens = _parse_int_env(
                "YANDEX_LLM_MAX_TOKENS",
                max_tokens_raw,
                minimum=1,
                maximum=8000,
            )

        return cls(
            enabled=True,
            api_key=api_key_raw,
            folder_id=folder_raw,
            model_uri=model_uri_raw,
            api_base_url=base_raw,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            temperature=temperature,
            max_tokens=max_tokens,
        )
