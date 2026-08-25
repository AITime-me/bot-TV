"""Yandex Cloud AI Studio native Text Generation completion client.

Contract (sync, non-streaming):
  POST {api_base_url}/foundationModels/v1/completion
  Authorization: Api-Key <YANDEX_API_KEY>
  Body: modelUri + completionOptions(stream=false) + messages[{role,text}]

Uses S2sHttpTransport (stdlib live / fake in tests). No SDK. No tool execution.
No CRM/booking/channel writes. Secrets and bodies never appear in exceptions/logs.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import Final, Mapping, Sequence

from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransport,
    S2sHttpTransportError,
)
from app.core.text_generation_port import (
    TextGenerationMessage,
    TextGenerationResult,
)
from app.core.yandex_llm_config import YandexLlmConfig, YandexLlmConfigError

logger = logging.getLogger(__name__)

_ALLOWED_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "CONFIG_INVALID",
        "DISABLED",
        "TRANSPORT_ERROR",
        "TIMEOUT",
        "REMOTE_REJECTED",
        "RESPONSE_TOO_LARGE",
        "RESPONSE_INVALID",
        "EMPTY_COMPLETION",
    }
)

_FINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "ALTERNATIVE_STATUS_FINAL",
        "ALTERNATIVE_STATUS_TRUNCATED_FINAL",
    }
)

_MAX_MESSAGES: Final[int] = 64
_MAX_MESSAGE_CHARS: Final[int] = 12_000
_MAX_REQUEST_BYTES: Final[int] = 256_000


class YandexGptErrorCode(StrEnum):
    CONFIG_INVALID = "CONFIG_INVALID"
    DISABLED = "DISABLED"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    TIMEOUT = "TIMEOUT"
    REMOTE_REJECTED = "REMOTE_REJECTED"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    RESPONSE_INVALID = "RESPONSE_INVALID"
    EMPTY_COMPLETION = "EMPTY_COMPLETION"


class YandexGptHttpError(RuntimeError):
    """Provider failure. Message is a fixed code only — never secrets/bodies."""

    def __init__(self, code: object) -> None:
        if type(code) is not str or code not in _ALLOWED_ERROR_CODES:
            super().__init__("TRANSPORT_ERROR")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "TRANSPORT_ERROR"

    def __repr__(self) -> str:
        return f"YandexGptHttpError({self.code!r})"

    def __str__(self) -> str:
        return self.code


def _log_event(event: str, code: str) -> None:
    if type(event) is not str or not event:
        return
    if type(code) is not str or code not in _ALLOWED_ERROR_CODES:
        return
    try:
        logger.info("%s code=%s", event, code)
    except Exception:
        return


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if type(key) is str and key.lower() == target:
            return value if type(value) is str else None
    return None


def _content_type_is_json(content_type: str | None) -> bool:
    if type(content_type) is not str or not content_type:
        return False
    media = content_type.split(";", 1)[0].strip().lower()
    return media == "application/json"


def _validate_messages(
    messages: Sequence[TextGenerationMessage],
) -> list[dict[str, str]]:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise YandexGptHttpError("CONFIG_INVALID") from None
    if len(messages) < 1 or len(messages) > _MAX_MESSAGES:
        raise YandexGptHttpError("CONFIG_INVALID") from None
    total_chars = 0
    mapped: list[dict[str, str]] = []
    for item in messages:
        if type(item) is not TextGenerationMessage:
            raise YandexGptHttpError("CONFIG_INVALID") from None
        total_chars += len(item.text)
        if total_chars > _MAX_MESSAGE_CHARS:
            raise YandexGptHttpError("CONFIG_INVALID") from None
        mapped.append({"role": item.role, "text": item.text})
    return mapped


def _extract_completion_payload(raw: object) -> dict | None:
    """Accept top-level CompletionResponse or {result: CompletionResponse}."""

    if type(raw) is not dict:
        return None
    if "alternatives" in raw:
        return raw
    result = raw.get("result")
    if type(result) is dict and "alternatives" in result:
        return result
    return None


def parse_completion_assistant_text(raw: object) -> str | None:
    """Strict response parser. Returns assistant text or None on any violation."""

    payload = _extract_completion_payload(raw)
    if payload is None:
        return None
    alternatives = payload.get("alternatives")
    if type(alternatives) is not list or not alternatives:
        return None
    first = alternatives[0]
    if type(first) is not dict:
        return None
    status = first.get("status")
    if type(status) is not str or status not in _FINAL_STATUSES:
        return None
    message = first.get("message")
    if type(message) is not dict:
        return None
    role = message.get("role")
    if role != "assistant":
        return None
    text = message.get("text")
    if type(text) is not str:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    return stripped


class YandexGptHttpClient:
    """Live/fake-transport Yandex GPT completion client implementing TextGenerationPort."""

    def __init__(
        self,
        config: YandexLlmConfig,
        transport: S2sHttpTransport,
    ) -> None:
        if type(config) is not YandexLlmConfig:
            raise YandexGptHttpError("CONFIG_INVALID") from None
        if transport is None:
            raise YandexGptHttpError("CONFIG_INVALID") from None
        try:
            config.require_runtime()
        except YandexLlmConfigError as exc:
            code = exc.code
            if code == "YANDEX_LLM_DISABLED":
                raise YandexGptHttpError("DISABLED") from None
            raise YandexGptHttpError("CONFIG_INVALID") from None
        self._config = config
        self._transport = transport

    def __repr__(self) -> str:
        return "YandexGptHttpClient(config=<redacted>, transport=<redacted>)"

    def generate(
        self,
        messages: Sequence[TextGenerationMessage],
    ) -> TextGenerationResult:
        mapped = _validate_messages(messages)
        assert self._config.api_key is not None
        assert self._config.model_uri is not None

        body_obj = {
            "modelUri": self._config.model_uri,
            "completionOptions": {
                "stream": False,
                "temperature": self._config.temperature,
                "maxTokens": str(self._config.max_tokens),
            },
            "messages": mapped,
        }
        try:
            body = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        except (TypeError, ValueError):
            raise YandexGptHttpError("CONFIG_INVALID") from None
        if len(body) > _MAX_REQUEST_BYTES:
            raise YandexGptHttpError("CONFIG_INVALID") from None

        request = S2sHttpRequest(
            method="POST",
            url=self._config.completion_url,
            headers={
                "Authorization": f"Api-Key {self._config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=body,
            timeout_seconds=self._config.timeout_seconds,
            allow_redirects=False,
            max_response_bytes=self._config.max_response_bytes,
        )

        try:
            response = self._transport.request(request)
        except S2sHttpTransportError as exc:
            code = exc.code
            if code == "TIMEOUT":
                _log_event("yandex_gpt_http_fail_closed", "TIMEOUT")
                raise YandexGptHttpError("TIMEOUT") from None
            if code == "RESPONSE_TOO_LARGE":
                _log_event("yandex_gpt_http_fail_closed", "RESPONSE_TOO_LARGE")
                raise YandexGptHttpError("RESPONSE_TOO_LARGE") from None
            _log_event("yandex_gpt_http_fail_closed", "TRANSPORT_ERROR")
            raise YandexGptHttpError("TRANSPORT_ERROR") from None
        except YandexGptHttpError:
            raise
        except Exception:
            _log_event("yandex_gpt_http_fail_closed", "TRANSPORT_ERROR")
            raise YandexGptHttpError("TRANSPORT_ERROR") from None

        return self._parse_response(response)

    def _parse_response(self, response: S2sHttpResponse) -> TextGenerationResult:
        if type(response) is not S2sHttpResponse:
            raise YandexGptHttpError("TRANSPORT_ERROR") from None
        status = response.status_code
        if type(status) is not int:
            raise YandexGptHttpError("TRANSPORT_ERROR") from None
        if status < 200 or status >= 300:
            _log_event("yandex_gpt_http_fail_closed", "REMOTE_REJECTED")
            raise YandexGptHttpError("REMOTE_REJECTED") from None

        content_type = _header_value(response.headers, "Content-Type")
        if not _content_type_is_json(content_type):
            _log_event("yandex_gpt_http_fail_closed", "RESPONSE_INVALID")
            raise YandexGptHttpError("RESPONSE_INVALID") from None

        body = response.body
        if type(body) is not bytes:
            raise YandexGptHttpError("RESPONSE_INVALID") from None
        if len(body) > self._config.max_response_bytes:
            _log_event("yandex_gpt_http_fail_closed", "RESPONSE_TOO_LARGE")
            raise YandexGptHttpError("RESPONSE_TOO_LARGE") from None

        try:
            raw = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _log_event("yandex_gpt_http_fail_closed", "RESPONSE_INVALID")
            raise YandexGptHttpError("RESPONSE_INVALID") from None

        text = parse_completion_assistant_text(raw)
        if text is None:
            # Distinguish empty assistant text from schema violations when possible.
            payload = _extract_completion_payload(raw)
            if (
                type(payload) is dict
                and type(payload.get("alternatives")) is list
                and payload["alternatives"]
                and type(payload["alternatives"][0]) is dict
            ):
                message = payload["alternatives"][0].get("message")
                if (
                    type(message) is dict
                    and message.get("role") == "assistant"
                    and type(message.get("text")) is str
                    and not str(message.get("text")).strip()
                ):
                    _log_event("yandex_gpt_http_fail_closed", "EMPTY_COMPLETION")
                    raise YandexGptHttpError("EMPTY_COMPLETION") from None
            _log_event("yandex_gpt_http_fail_closed", "RESPONSE_INVALID")
            raise YandexGptHttpError("RESPONSE_INVALID") from None

        try:
            return TextGenerationResult(text=text)
        except ValueError:
            _log_event("yandex_gpt_http_fail_closed", "EMPTY_COMPLETION")
            raise YandexGptHttpError("EMPTY_COMPLETION") from None
