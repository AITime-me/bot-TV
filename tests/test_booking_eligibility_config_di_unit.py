"""Unit tests for CURSOR-16 booking eligibility Settings, factory, and DI."""

from __future__ import annotations

import http.client
import logging
import socket
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import BotMode, Settings
from app.core.booking_eligibility_factory import build_booking_eligibility_client
from app.core.booking_eligibility_http import (
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    BookingEligibilityHttpClient,
)
from app.core.s2s_http_stdlib import S2sHttpStdlibTransport
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
)
from app.main import create_app

_VALID_URL = "https://eligibility.example"
_SECRET_URL = "https://internal-s2s.prod.example"
_VALID_TOKEN = "a" * 32
_SECRET_TOKEN = "secret-token-value-must-not-leak!!"


def _full_env(**overrides: str) -> dict[str, str]:
    env = {
        "BOOKING_ELIGIBILITY_BASE_URL": _VALID_URL,
        "BOOKING_ELIGIBILITY_BEARER_TOKEN": _VALID_TOKEN,
    }
    env.update(overrides)
    return env


def _assert_no_eligibility_secrets(text: str, *, url: str, token: str) -> None:
    assert url not in text
    assert token not in text
    assert "Authorization" not in text


class _FakeTransport:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, request: S2sHttpRequest) -> S2sHttpResponse:
        self.calls += 1
        raise AssertionError("network must not be called")


class _RecordingFakeClient:
    """Stand-in injected into create_app; not a real HTTP client."""

    marker = "fake-eligibility-client"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_eligibility_absent_by_default() -> None:
    settings = Settings.from_env({})
    assert settings.booking_eligibility_base_url is None
    assert settings.booking_eligibility_bearer_token is None
    assert settings.booking_eligibility_timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    assert (
        settings.booking_eligibility_max_response_bytes
        == DEFAULT_MAX_RESPONSE_BYTES
    )
    assert settings.bot_mode is BotMode.OFF
    assert settings.emergency_lock is True


def test_settings_eligibility_valid_full_config() -> None:
    settings = Settings.from_env(
        _full_env(
            BOOKING_ELIGIBILITY_TIMEOUT_SECONDS="7.5",
            BOOKING_ELIGIBILITY_MAX_RESPONSE_BYTES="4096",
        )
    )
    assert settings.booking_eligibility_base_url == _VALID_URL
    assert settings.booking_eligibility_bearer_token == _VALID_TOKEN
    assert settings.booking_eligibility_timeout_seconds == 7.5
    assert settings.booking_eligibility_max_response_bytes == 4096


def test_settings_eligibility_empty_strings_are_absent() -> None:
    settings = Settings.from_env(
        {
            "BOOKING_ELIGIBILITY_BASE_URL": "",
            "BOOKING_ELIGIBILITY_BEARER_TOKEN": "",
        }
    )
    assert settings.booking_eligibility_base_url is None
    assert settings.booking_eligibility_bearer_token is None
    assert build_booking_eligibility_client(settings) is None


def test_settings_eligibility_timeout_and_max_bytes_without_pair() -> None:
    settings = Settings.from_env(
        {
            "BOOKING_ELIGIBILITY_TIMEOUT_SECONDS": "3.5",
            "BOOKING_ELIGIBILITY_MAX_RESPONSE_BYTES": "4096",
        }
    )
    assert settings.booking_eligibility_base_url is None
    assert settings.booking_eligibility_bearer_token is None
    assert settings.booking_eligibility_timeout_seconds == 3.5
    assert settings.booking_eligibility_max_response_bytes == 4096
    assert build_booking_eligibility_client(settings) is None


def test_settings_eligibility_url_without_token_fails() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        Settings.from_env({"BOOKING_ELIGIBILITY_BASE_URL": _VALID_URL})


def test_settings_eligibility_token_without_url_fails() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        Settings.from_env({"BOOKING_ELIGIBILITY_BEARER_TOKEN": _VALID_TOKEN})


def test_settings_incomplete_exception_never_shows_url_or_token() -> None:
    with pytest.raises(ValueError) as raised_url_only:
        Settings.from_env({"BOOKING_ELIGIBILITY_BASE_URL": _SECRET_URL})
    url_text = f"{raised_url_only.value!s}{raised_url_only.value!r}"
    assert _SECRET_URL not in url_text
    assert "incomplete" in str(raised_url_only.value)

    with pytest.raises(ValueError) as raised_token_only:
        Settings.from_env({"BOOKING_ELIGIBILITY_BEARER_TOKEN": _SECRET_TOKEN})
    token_text = f"{raised_token_only.value!s}{raised_token_only.value!r}"
    assert _SECRET_TOKEN not in token_text
    assert "incomplete" in str(raised_token_only.value)


def test_settings_eligibility_invalid_url_fails() -> None:
    with pytest.raises(ValueError, match="invalid"):
        Settings.from_env(
            _full_env(BOOKING_ELIGIBILITY_BASE_URL="ftp://eligibility.example")
        )


def test_settings_eligibility_short_token_fails() -> None:
    with pytest.raises(ValueError, match="invalid"):
        Settings.from_env(_full_env(BOOKING_ELIGIBILITY_BEARER_TOKEN="short"))


@pytest.mark.parametrize(
    "token",
    [
        "a" * 31 + " ",
        "a" * 16 + " " + "b" * 16,
        "a" * 31 + "\n",
        "a" * 31 + "\x00",
    ],
)
def test_settings_eligibility_token_whitespace_or_control_fails(token: str) -> None:
    with pytest.raises(ValueError, match="invalid"):
        Settings.from_env(_full_env(BOOKING_ELIGIBILITY_BEARER_TOKEN=token))


@pytest.mark.parametrize(
    "value",
    ["0", "-1", "nan", "inf", "-inf", "121", "true", "false", "True"],
)
def test_settings_eligibility_invalid_timeout(value: str) -> None:
    with pytest.raises(ValueError):
        Settings.from_env(_full_env(BOOKING_ELIGIBILITY_TIMEOUT_SECONDS=value))


@pytest.mark.parametrize(
    "value",
    ["0", "-1", "1000001", "true", "false", "1.5", "nan"],
)
def test_settings_eligibility_invalid_max_response_bytes(value: str) -> None:
    with pytest.raises(ValueError):
        Settings.from_env(
            _full_env(BOOKING_ELIGIBILITY_MAX_RESPONSE_BYTES=value)
        )


def test_settings_eligibility_bool_rejected_for_numeric_fields() -> None:
    with pytest.raises(ValueError):
        Settings(
            booking_eligibility_timeout_seconds=True,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        Settings(
            booking_eligibility_max_response_bytes=True,  # type: ignore[arg-type]
        )


def test_settings_repr_never_shows_url_or_bearer_token() -> None:
    settings = Settings.from_env(
        _full_env(
            BOOKING_ELIGIBILITY_BASE_URL=_SECRET_URL,
            BOOKING_ELIGIBILITY_BEARER_TOKEN=_SECRET_TOKEN,
        )
    )
    rendered = repr(settings)
    _assert_no_eligibility_secrets(rendered, url=_SECRET_URL, token=_SECRET_TOKEN)
    assert "booking_eligibility_base_url=<redacted>" in rendered
    assert "booking_eligibility_bearer_token=<redacted>" in rendered
    assert settings.booking_eligibility_base_url == _SECRET_URL
    assert settings.booking_eligibility_bearer_token == _SECRET_TOKEN


def test_settings_exception_never_shows_url_or_bearer_token() -> None:
    with pytest.raises(ValueError) as raised:
        Settings.from_env(
            {
                "BOOKING_ELIGIBILITY_BASE_URL": _SECRET_URL + "/with-path",
                "BOOKING_ELIGIBILITY_BEARER_TOKEN": _SECRET_TOKEN,
            }
        )
    text = f"{raised.value!s}{raised.value!r}"
    _assert_no_eligibility_secrets(text, url=_SECRET_URL, token=_SECRET_TOKEN)
    assert "invalid" in str(raised.value)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_absent_config_returns_none() -> None:
    assert build_booking_eligibility_client(Settings()) is None


def test_factory_valid_config_builds_client() -> None:
    settings = Settings.from_env(_full_env())
    client = build_booking_eligibility_client(settings)
    assert isinstance(client, BookingEligibilityHttpClient)


def test_factory_uses_stdlib_transport_by_default() -> None:
    settings = Settings.from_env(_full_env())
    client = build_booking_eligibility_client(settings)
    assert isinstance(client, BookingEligibilityHttpClient)
    transport = client._transport  # noqa: SLF001 — assertion of wiring
    assert isinstance(transport, S2sHttpStdlibTransport)


def test_factory_uses_injected_fake_transport() -> None:
    settings = Settings.from_env(_full_env())
    fake = _FakeTransport()
    client = build_booking_eligibility_client(settings, transport=fake)
    assert client._transport is fake  # noqa: SLF001


def test_factory_construction_makes_no_network_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _deny_socket(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("socket must not be opened")

    def _deny_https(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("HTTPSConnection must not be created")

    def _deny_http(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("HTTPConnection must not be created")

    monkeypatch.setattr(socket, "socket", _deny_socket)
    monkeypatch.setattr(http.client, "HTTPSConnection", _deny_https)
    monkeypatch.setattr(http.client, "HTTPConnection", _deny_http)

    settings = Settings.from_env(_full_env())
    client = build_booking_eligibility_client(settings)
    assert isinstance(client, BookingEligibilityHttpClient)


def test_factory_partial_config_fails_closed() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        Settings(
            booking_eligibility_base_url=_VALID_URL,
            booking_eligibility_bearer_token=None,
        )


def test_factory_invalid_config_fails_closed_without_secrets() -> None:
    with pytest.raises(ValueError) as raised:
        Settings(
            booking_eligibility_base_url="ftp://bad.example",
            booking_eligibility_bearer_token=_SECRET_TOKEN,
        )
    text = f"{raised.value!s}{raised.value!r}"
    _assert_no_eligibility_secrets(
        text, url="ftp://bad.example", token=_SECRET_TOKEN
    )
    assert "invalid" in str(raised.value)


def test_factory_secrets_absent_from_client_repr() -> None:
    settings = Settings.from_env(
        _full_env(
            BOOKING_ELIGIBILITY_BASE_URL=_SECRET_URL,
            BOOKING_ELIGIBILITY_BEARER_TOKEN=_SECRET_TOKEN,
        )
    )
    client = build_booking_eligibility_client(settings)
    assert client is not None
    config_repr = repr(client._config)  # noqa: SLF001
    text = f"{client!r}{client!s}{config_repr}"
    _assert_no_eligibility_secrets(text, url=_SECRET_URL, token=_SECRET_TOKEN)
    assert "base_url=<redacted>" in config_repr
    assert "bearer_token=<redacted>" in config_repr


def test_factory_does_not_log_url_or_token(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        settings = Settings.from_env(
            _full_env(
                BOOKING_ELIGIBILITY_BASE_URL=_SECRET_URL,
                BOOKING_ELIGIBILITY_BEARER_TOKEN=_SECRET_TOKEN,
            )
        )
        build_booking_eligibility_client(settings)
        _ = repr(settings)
    joined = "\n".join(
        f"{record.getMessage()}{record.exc_text or ''}"
        for record in caplog.records
    )
    _assert_no_eligibility_secrets(joined, url=_SECRET_URL, token=_SECRET_TOKEN)


# ---------------------------------------------------------------------------
# Startup DI
# ---------------------------------------------------------------------------


def test_create_app_auto_builds_eligibility_client() -> None:
    settings = Settings.from_env(_full_env())
    application = create_app(settings)
    client = application.state.booking_eligibility_client
    assert isinstance(client, BookingEligibilityHttpClient)
    assert isinstance(client._transport, S2sHttpStdlibTransport)  # noqa: SLF001


def test_create_app_absent_config_sets_none() -> None:
    application = create_app(Settings())
    assert application.state.booking_eligibility_client is None


def test_create_app_partial_config_fails_closed_via_settings() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        create_app(
            Settings(
                booking_eligibility_base_url=_VALID_URL,
                booking_eligibility_bearer_token=None,
            )
        )


def test_create_app_partial_config_fails_closed_in_factory_path() -> None:
    """Bypass Settings validation to exercise create_app → factory fail-closed."""

    settings = Settings.from_env(_full_env())
    object.__setattr__(settings, "booking_eligibility_bearer_token", None)
    with pytest.raises(ValueError, match="incomplete"):
        create_app(settings)
    assert settings.booking_eligibility_base_url == _VALID_URL
    assert settings.booking_eligibility_bearer_token is None


def test_create_app_explicit_fake_client_skips_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"build": 0}

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        called["build"] += 1
        raise AssertionError("auto-factory must not run")

    monkeypatch.setattr(
        "app.main.build_booking_eligibility_client",
        _boom,
    )
    fake = _RecordingFakeClient()
    application = create_app(
        Settings.from_env(_full_env()),
        booking_eligibility_client=fake,  # type: ignore[arg-type]
    )
    assert application.state.booking_eligibility_client is fake
    assert called["build"] == 0


def test_create_app_explicit_none_skips_auto_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"build": 0}

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        called["build"] += 1
        raise AssertionError("auto-factory must not run for explicit None")

    monkeypatch.setattr(
        "app.main.build_booking_eligibility_client",
        _boom,
    )
    application = create_app(
        Settings.from_env(_full_env()),
        booking_eligibility_client=None,
    )
    assert application.state.booking_eligibility_client is None
    assert called["build"] == 0


def test_create_app_makes_no_http_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _deny(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("network must not be used during create_app")

    monkeypatch.setattr(socket, "socket", _deny)
    monkeypatch.setattr(http.client, "HTTPSConnection", _deny)
    monkeypatch.setattr(http.client, "HTTPConnection", _deny)

    application = create_app(Settings.from_env(_full_env()))
    assert isinstance(
        application.state.booking_eligibility_client,
        BookingEligibilityHttpClient,
    )


def test_create_app_health_endpoints_unchanged() -> None:
    client = TestClient(create_app(Settings.from_env(_full_env())))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/health/live").json() == {"status": "ok"}
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    payload = ready.json()
    assert payload["status"] == "ready"
    assert payload["bot_mode"] == "OFF"
    assert payload["emergency_lock"] is True
    assert "booking_eligibility" not in payload
    assert "eligibility" not in payload


def test_create_app_preserves_fail_closed_mode_defaults() -> None:
    settings = Settings.from_env(_full_env())
    assert settings.bot_mode is BotMode.OFF
    assert settings.emergency_lock is True
    application = create_app(settings)
    ready = TestClient(application).get("/health/ready").json()
    assert ready["bot_mode"] == "OFF"
    assert ready["emergency_lock"] is True
