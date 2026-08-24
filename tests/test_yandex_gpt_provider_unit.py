"""Unit tests for Yandex GPT provider foundation (config + HTTP + factory).

Fake S2sHttpTransport only. No live network, Docker, deploy, or reply wiring.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransportError,
)
from app.core.text_generation_port import (
    TextGenerationMessage,
    TextGenerationResult,
)
from app.core.yandex_gpt_http import (
    YandexGptHttpClient,
    YandexGptHttpError,
    parse_completion_assistant_text,
)
from app.core.yandex_llm_config import (
    COMPLETION_ROUTE_PATH,
    DEFAULT_YANDEX_LLM_API_BASE_URL,
    YandexLlmConfig,
    YandexLlmConfigError,
    default_yandex_model_uri,
)
from app.core.yandex_llm_factory import (
    build_text_generation_port,
    build_yandex_llm_config,
)

_API_KEY = "test-api-key-value-01"
_FOLDER_ID = "b1gfolderid01"
_MODEL_URI = f"gpt://{_FOLDER_ID}/yandexgpt/latest"


class FakeTransport:
    def __init__(
        self,
        *,
        response: S2sHttpResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._response = response
        self._error = error
        self.calls: list[S2sHttpRequest] = []

    def request(self, request: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        if self._response is None:
            raise S2sHttpTransportError("TRANSPORT_ERROR")
        return self._response


def _enabled_env(**overrides: str) -> dict[str, str]:
    env = {
        "YANDEX_LLM_ENABLED": "true",
        "YANDEX_API_KEY": _API_KEY,
        "YANDEX_FOLDER_ID": _FOLDER_ID,
    }
    env.update(overrides)
    return env


def _config(**overrides: Any) -> YandexLlmConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "api_key": _API_KEY,
        "folder_id": _FOLDER_ID,
        "model_uri": None,
        "api_base_url": DEFAULT_YANDEX_LLM_API_BASE_URL,
        "timeout_seconds": 15.0,
        "max_response_bytes": 4096,
        "temperature": 0.3,
        "max_tokens": 1024,
    }
    values.update(overrides)
    return YandexLlmConfig(**values)


def _success_payload(text: str = "Здравствуйте!") -> dict[str, Any]:
    return {
        "result": {
            "alternatives": [
                {
                    "message": {"role": "assistant", "text": text},
                    "status": "ALTERNATIVE_STATUS_FINAL",
                }
            ],
            "usage": {
                "inputTextTokens": "10",
                "completionTokens": "5",
                "totalTokens": "15",
            },
            "modelVersion": "yandexgpt/latest",
        }
    }


def _json_response(
    payload: object,
    *,
    status: int = 200,
    content_type: str = "application/json",
) -> S2sHttpResponse:
    body = b""
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return S2sHttpResponse(
        status_code=status,
        headers={"Content-Type": content_type, "Content-Length": str(len(body))},
        body=body,
    )


def _messages() -> tuple[TextGenerationMessage, ...]:
    return (
        TextGenerationMessage(role="system", text="Ты помощник салона."),
        TextGenerationMessage(role="user", text="Сколько стоит стрижка?"),
    )


def _client(transport: FakeTransport, **config_overrides: Any) -> YandexGptHttpClient:
    return YandexGptHttpClient(_config(**config_overrides), transport)


# ---------------------------------------------------------------------------
# Config / factory / disabled
# ---------------------------------------------------------------------------


def test_disabled_default_from_empty_env() -> None:
    cfg = YandexLlmConfig.from_env({})
    assert cfg.enabled is False
    assert cfg.api_key is None
    assert cfg.folder_id is None


def test_disabled_explicit_false_ignores_missing_secrets() -> None:
    cfg = YandexLlmConfig.from_env({"YANDEX_LLM_ENABLED": "false"})
    assert cfg.enabled is False
    assert build_text_generation_port({"YANDEX_LLM_ENABLED": "false"}) is None


def test_disabled_factory_makes_zero_http_calls() -> None:
    transport = FakeTransport(response=_json_response(_success_payload()))
    port = build_text_generation_port(
        {"YANDEX_LLM_ENABLED": "false"},
        transport=transport,
    )
    assert port is None
    assert transport.calls == []


def test_enabled_missing_api_key_fails_closed() -> None:
    with pytest.raises(YandexLlmConfigError) as exc_info:
        YandexLlmConfig.from_env(
            {
                "YANDEX_LLM_ENABLED": "true",
                "YANDEX_FOLDER_ID": _FOLDER_ID,
            }
        )
    assert exc_info.value.code == "YANDEX_API_KEY_REQUIRED"


def test_enabled_missing_folder_id_fails_closed() -> None:
    with pytest.raises(YandexLlmConfigError) as exc_info:
        YandexLlmConfig.from_env(
            {
                "YANDEX_LLM_ENABLED": "true",
                "YANDEX_API_KEY": _API_KEY,
            }
        )
    assert exc_info.value.code == "YANDEX_FOLDER_ID_REQUIRED"


def test_enabled_empty_secrets_fail_closed() -> None:
    with pytest.raises(YandexLlmConfigError) as exc_info:
        YandexLlmConfig.from_env(
            {
                "YANDEX_LLM_ENABLED": "true",
                "YANDEX_API_KEY": "",
                "YANDEX_FOLDER_ID": _FOLDER_ID,
            }
        )
    assert exc_info.value.code == "YANDEX_API_KEY_REQUIRED"


def test_valid_enabled_config_defaults_model_uri() -> None:
    cfg = build_yandex_llm_config(_enabled_env())
    assert cfg.enabled is True
    assert cfg.api_key == _API_KEY
    assert cfg.folder_id == _FOLDER_ID
    assert cfg.model_uri == _MODEL_URI
    assert cfg.completion_url == (
        f"{DEFAULT_YANDEX_LLM_API_BASE_URL}{COMPLETION_ROUTE_PATH}"
    )
    assert default_yandex_model_uri(_FOLDER_ID) == _MODEL_URI


def test_model_uri_override() -> None:
    override = f"gpt://{_FOLDER_ID}/yandexgpt-lite/latest"
    cfg = YandexLlmConfig.from_env(_enabled_env(YANDEX_MODEL_URI=override))
    assert cfg.model_uri == override


def test_config_repr_redacts_secrets() -> None:
    cfg = _config()
    rendered = repr(cfg)
    assert _API_KEY not in rendered
    assert _FOLDER_ID not in rendered
    assert _MODEL_URI not in rendered
    assert "Api-Key" not in rendered
    assert "api_key=<redacted>" in rendered
    assert "folder_id=<redacted>" in rendered
    assert DEFAULT_YANDEX_LLM_API_BASE_URL not in rendered


def test_invalid_enabled_flag() -> None:
    with pytest.raises(YandexLlmConfigError) as exc_info:
        YandexLlmConfig.from_env({"YANDEX_LLM_ENABLED": "yes"})
    assert exc_info.value.code == "YANDEX_LLM_ENABLED_INVALID"


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"temperature": 1.5}, "YANDEX_LLM_TEMPERATURE_INVALID"),
        ({"temperature": -0.1}, "YANDEX_LLM_TEMPERATURE_INVALID"),
        ({"max_tokens": 0}, "YANDEX_LLM_MAX_TOKENS_INVALID"),
        ({"max_tokens": 9000}, "YANDEX_LLM_MAX_TOKENS_INVALID"),
        ({"timeout_seconds": 0}, "YANDEX_LLM_TIMEOUT_INVALID"),
        ({"timeout_seconds": 121}, "YANDEX_LLM_TIMEOUT_INVALID"),
        ({"max_response_bytes": 0}, "YANDEX_LLM_MAX_RESPONSE_BYTES_INVALID"),
        (
            {"api_base_url": "https://llm.example/path"},
            "YANDEX_LLM_API_BASE_INVALID",
        ),
        ({"model_uri": "http://evil"}, "YANDEX_MODEL_URI_INVALID"),
    ],
)
def test_config_bounds(kwargs: dict[str, Any], code: str) -> None:
    with pytest.raises(YandexLlmConfigError) as exc_info:
        _config(**kwargs)
    assert exc_info.value.code == code


# ---------------------------------------------------------------------------
# HTTP request / response contract
# ---------------------------------------------------------------------------


def test_successful_generate_builds_correct_request_and_parses() -> None:
    transport = FakeTransport(response=_json_response(_success_payload("Ответ")))
    client = _client(transport)
    result = client.generate(_messages())
    assert isinstance(result, TextGenerationResult)
    assert result.text == "Ответ"
    assert len(transport.calls) == 1
    req = transport.calls[0]
    assert req.method == "POST"
    assert req.url.endswith(COMPLETION_ROUTE_PATH)
    assert req.allow_redirects is False
    assert req.headers["Authorization"] == f"Api-Key {_API_KEY}"
    assert req.headers["Content-Type"] == "application/json"
    body = json.loads(req.body.decode("utf-8"))
    assert body["modelUri"] == _MODEL_URI
    assert body["completionOptions"]["stream"] is False
    assert body["completionOptions"]["temperature"] == 0.3
    assert body["completionOptions"]["maxTokens"] == "1024"
    assert body["messages"] == [
        {"role": "system", "text": "Ты помощник салона."},
        {"role": "user", "text": "Сколько стоит стрижка?"},
    ]


def test_auth_header_uses_api_key_prefix() -> None:
    transport = FakeTransport(response=_json_response(_success_payload()))
    _client(transport).generate(_messages())
    auth = transport.calls[0].headers["Authorization"]
    assert auth.startswith("Api-Key ")
    assert auth == f"Api-Key {_API_KEY}"
    assert not auth.startswith("Bearer ")


def test_factory_enabled_returns_port() -> None:
    transport = FakeTransport(response=_json_response(_success_payload("ok")))
    port = build_text_generation_port(_enabled_env(), transport=transport)
    assert port is not None
    out = port.generate(_messages())
    assert out.text == "ok"


def test_top_level_completion_response_schema_accepted() -> None:
    payload = {
        "alternatives": [
            {
                "message": {"role": "assistant", "text": "top-level"},
                "status": "ALTERNATIVE_STATUS_FINAL",
            }
        ]
    }
    assert parse_completion_assistant_text(payload) == "top-level"


def test_empty_assistant_text_is_error() -> None:
    transport = FakeTransport(
        response=_json_response(_success_payload("   ")),
    )
    client = _client(transport)
    with pytest.raises(YandexGptHttpError) as exc_info:
        client.generate(_messages())
    assert exc_info.value.code == "EMPTY_COMPLETION"


def test_malformed_json_fails_closed() -> None:
    transport = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=b"{not-json",
        )
    )
    with pytest.raises(YandexGptHttpError) as exc_info:
        _client(transport).generate(_messages())
    assert exc_info.value.code == "RESPONSE_INVALID"


def test_unexpected_schema_fails_closed() -> None:
    transport = FakeTransport(
        response=_json_response({"ok": True, "text": "nope"}),
    )
    with pytest.raises(YandexGptHttpError) as exc_info:
        _client(transport).generate(_messages())
    assert exc_info.value.code == "RESPONSE_INVALID"


def test_non_final_status_fails_closed() -> None:
    payload = _success_payload("x")
    payload["result"]["alternatives"][0]["status"] = "ALTERNATIVE_STATUS_PARTIAL"
    transport = FakeTransport(response=_json_response(payload))
    with pytest.raises(YandexGptHttpError) as exc_info:
        _client(transport).generate(_messages())
    assert exc_info.value.code == "RESPONSE_INVALID"


@pytest.mark.parametrize("status", [400, 401, 403, 429])
def test_http_4xx_fails_closed(status: int) -> None:
    transport = FakeTransport(response=_json_response({"error": "x"}, status=status))
    with pytest.raises(YandexGptHttpError) as exc_info:
        _client(transport).generate(_messages())
    assert exc_info.value.code == "REMOTE_REJECTED"


@pytest.mark.parametrize("status", [500, 502, 503])
def test_http_5xx_fails_closed(status: int) -> None:
    transport = FakeTransport(response=_json_response({"error": "x"}, status=status))
    with pytest.raises(YandexGptHttpError) as exc_info:
        _client(transport).generate(_messages())
    assert exc_info.value.code == "REMOTE_REJECTED"


def test_timeout_fails_closed() -> None:
    transport = FakeTransport(error=S2sHttpTransportError("TIMEOUT"))
    with pytest.raises(YandexGptHttpError) as exc_info:
        _client(transport).generate(_messages())
    assert exc_info.value.code == "TIMEOUT"


def test_network_transport_error_fails_closed() -> None:
    transport = FakeTransport(error=S2sHttpTransportError("TRANSPORT_ERROR"))
    with pytest.raises(YandexGptHttpError) as exc_info:
        _client(transport).generate(_messages())
    assert exc_info.value.code == "TRANSPORT_ERROR"


def test_oversized_response_fails_closed() -> None:
    transport = FakeTransport(error=S2sHttpTransportError("RESPONSE_TOO_LARGE"))
    with pytest.raises(YandexGptHttpError) as exc_info:
        _client(transport).generate(_messages())
    assert exc_info.value.code == "RESPONSE_TOO_LARGE"


def test_non_json_content_type_fails_closed() -> None:
    transport = FakeTransport(
        response=_json_response(_success_payload(), content_type="text/plain")
    )
    with pytest.raises(YandexGptHttpError) as exc_info:
        _client(transport).generate(_messages())
    assert exc_info.value.code == "RESPONSE_INVALID"


def test_constructing_client_when_disabled_fails() -> None:
    transport = FakeTransport()
    with pytest.raises(YandexGptHttpError) as exc_info:
        YandexGptHttpClient(YandexLlmConfig(enabled=False), transport)
    assert exc_info.value.code == "DISABLED"
    assert transport.calls == []


# ---------------------------------------------------------------------------
# Secret leakage
# ---------------------------------------------------------------------------


def test_no_secret_leakage_in_errors_and_repr() -> None:
    secret = "super-secret-yandex-key-zzz"
    folder = "b1gsecretfolder99"
    cfg = _config(api_key=secret, folder_id=folder)
    transport = FakeTransport(
        response=_json_response({"error": secret, "folder": folder}, status=401)
    )
    client = YandexGptHttpClient(cfg, transport)
    with pytest.raises(YandexGptHttpError) as exc_info:
        client.generate(_messages())
    err = exc_info.value
    assert secret not in repr(err)
    assert secret not in str(err)
    assert folder not in repr(err)
    assert folder not in str(err)
    assert secret not in repr(client)
    assert secret not in repr(cfg)
    assert folder not in repr(cfg)
    assert secret not in repr(transport.calls[0])
    assert f"Api-Key {secret}" not in repr(transport.calls[0])


def test_message_and_result_repr_redact_text() -> None:
    msg = TextGenerationMessage(role="user", text="PII phone 79001234567")
    assert "79001234567" not in repr(msg)
    assert "text=<redacted>" in repr(msg)
    result = TextGenerationResult(text="секретный ответ")
    assert "секретный" not in repr(result)


def test_provider_does_not_import_into_reply_outbound_modules() -> None:
    """Safety gates stay outside the model layer; reply modules ban 'yandex'."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    banned_paths = [
        root / "app" / "services" / "outbound_arbiter.py",
        root / "app" / "services" / "reply_outbound.py",
        root / "app" / "services" / "synthetic_outbound.py",
        root / "app" / "core" / "outbound_policy.py",
        root / "app" / "core" / "mode_contract.py",
    ]
    for path in banned_paths:
        text = path.read_text(encoding="utf-8")
        assert "yandex" not in text.lower()
        assert "YandexGpt" not in text
        assert "build_text_generation_port" not in text


def test_text_generation_port_is_runtime_checkable() -> None:
    transport = FakeTransport(response=_json_response(_success_payload()))
    client = _client(transport)
    from app.core.text_generation_port import TextGenerationPort

    assert isinstance(client, TextGenerationPort)
