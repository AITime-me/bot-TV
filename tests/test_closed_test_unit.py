"""BOT-CLOSED-TEST-01A: closed-test HTTP surface unit coverage."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import BotMode, Settings
from app.core.closed_test_config import ClosedTestConfig, ClosedTestConfigError
from app.core.mode_contract import is_live_booking_s2s_read_allowed
from app.core.outbound_policy import OutboundAction, is_automatic_outbound_allowed
from app.main import create_app
from app.schemas.closed_test import ClosedTestEventCreate
from app.services.closed_test import project_safe_synthetic_result
from tests.docker_runtime_allowlist import (
    EXPECTED_DOCKER_ALLOW_RULES,
    assert_canonical_docker_runtime_allowlist,
    dockerignore_lines,
    is_included_in_docker_build_context,
)

_REPO = Path(__file__).resolve().parents[1]
_TOKEN = "c" * 32
_FAKE_DB = "postgresql+asyncpg://bot:pass@127.0.0.1:5432/bot_tv"


def _enable_env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    monkeypatch.setenv("BOT_CLOSED_TEST_ENABLED", "true")
    monkeypatch.setenv("BOT_CLOSED_TEST_TOKEN", _TOKEN)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def _settings(*, database: bool = True) -> Settings:
    env = {
        "BOT_MODE": "OFF",
        "EMERGENCY_LOCK": "true",
    }
    if database:
        env["DATABASE_URL"] = _FAKE_DB
    return Settings.from_env(env)


def test_default_disabled_config() -> None:
    config = ClosedTestConfig.from_env({})
    assert config.enabled is False
    assert config.token is None
    assert "c" * 32 not in repr(config)
    assert config.verify_token(_TOKEN) is False


def test_enabled_missing_token_fail_closed() -> None:
    with pytest.raises(ClosedTestConfigError, match="CLOSED_TEST_TOKEN_REQUIRED"):
        ClosedTestConfig.from_env({"BOT_CLOSED_TEST_ENABLED": "true"})


@pytest.mark.parametrize("token", ["", "short", "a" * 31, "has space" + "x" * 32])
def test_enabled_invalid_token_fail_closed(token: str) -> None:
    with pytest.raises(ClosedTestConfigError):
        ClosedTestConfig.from_env(
            {
                "BOT_CLOSED_TEST_ENABLED": "true",
                "BOT_CLOSED_TEST_TOKEN": token,
            }
        )


def test_enabled_invalid_bool_fail_closed() -> None:
    with pytest.raises(ClosedTestConfigError, match="CLOSED_TEST_CONFIG_INVALID"):
        ClosedTestConfig.from_env({"BOT_CLOSED_TEST_ENABLED": "yes"})


def test_valid_enabled_config() -> None:
    config = ClosedTestConfig.from_env(
        {
            "BOT_CLOSED_TEST_ENABLED": "true",
            "BOT_CLOSED_TEST_TOKEN": _TOKEN,
        }
    )
    assert config.enabled is True
    assert config.verify_token(_TOKEN) is True
    assert config.verify_token("w" * 32) is False
    assert config.verify_token(None) is False
    assert config.verify_token("") is False
    assert _TOKEN not in repr(config)


def _closed_test_paths(application) -> set[str]:
    paths: set[str] = set()

    def walk(routes: object) -> None:
        for route in routes:  # type: ignore[attr-defined]
            path = getattr(route, "path", None)
            if isinstance(path, str):
                paths.add(path)
            nested = getattr(route, "routes", None)
            if nested is not None:
                walk(nested)
            # FastAPI may wrap include_router as _IncludedRouter.
            original = getattr(route, "original_router", None)
            if original is not None and getattr(original, "routes", None) is not None:
                walk(original.routes)

    walk(application.routes)
    return paths


def test_create_app_default_route_absent() -> None:
    application = create_app(Settings())
    paths = _closed_test_paths(application)
    assert "/internal/closed-test/events" not in paths
    client = TestClient(application)
    response = client.post("/internal/closed-test/events", json={})
    assert response.status_code == 404


def test_create_app_enabled_without_database_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_env(monkeypatch)
    with pytest.raises(ClosedTestConfigError, match="CLOSED_TEST_DATABASE_REQUIRED"):
        create_app(_settings(database=False))


def test_create_app_enabled_registers_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_env(monkeypatch)
    application = create_app(_settings(database=True))
    paths = _closed_test_paths(application)
    assert "/internal/closed-test/events" in paths
    assert "/internal/closed-test/events/{event_id}" in paths
    assert "/internal/closed-test/pii-admissions" in paths
    # Reachable (auth runs) — not a bare 404 from missing registration.
    client = TestClient(application)
    response = client.post(
        "/internal/closed-test/events",
        json={"session_id": "s", "request_id": "r", "text": "hello"},
    )
    assert response.status_code == 401


def test_auth_missing_and_wrong_token_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_env(monkeypatch)
    client = TestClient(create_app(_settings(database=True)))
    body = {
        "session_id": "sess-1",
        "request_id": "req-1",
        "text": "hello-closed-test",
    }
    missing = client.post("/internal/closed-test/events", json=body)
    assert missing.status_code == 401
    assert _TOKEN not in missing.text

    wrong = client.post(
        "/internal/closed-test/events",
        json=body,
        headers={"X-Bot-Closed-Test-Token": "w" * 32},
    )
    assert wrong.status_code == 401
    assert _TOKEN not in wrong.text
    assert "w" * 32 not in wrong.text


def test_input_schema_rejects_extra_and_unsafe_ids() -> None:
    with pytest.raises(ValidationError):
        ClosedTestEventCreate(
            session_id="ok",
            request_id="ok",
            text="hello",
            phone="+10000000000",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        ClosedTestEventCreate(session_id="bad id!", request_id="ok", text="hello")
    with pytest.raises(ValidationError):
        ClosedTestEventCreate(session_id="ok", request_id="ok", text="")
    with pytest.raises(ValidationError):
        ClosedTestEventCreate(session_id="ok", request_id="ok", text="x" * 2001)


def test_input_schema_rejects_unicode_ids() -> None:
    with pytest.raises(ValidationError):
        ClosedTestEventCreate(session_id="сессия", request_id="ok", text="hello")
    with pytest.raises(ValidationError):
        ClosedTestEventCreate(session_id="ok", request_id="req-\u0400", text="hello")
    with pytest.raises(ValidationError):
        ClosedTestEventCreate(session_id="café", request_id="ok", text="hello")


def test_http_validation_redacts_raw_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_env(monkeypatch)
    client = TestClient(create_app(_settings(database=True)))
    headers = {"X-Bot-Closed-Test-Token": _TOKEN}

    control = client.post(
        "/internal/closed-test/events",
        json={
            "session_id": "sess-ctrl",
            "request_id": "req-ctrl",
            "text": "hello\x00secret-leak",
        },
        headers=headers,
    )
    assert control.status_code == 422
    assert control.json() == {"detail": "VALIDATION_ERROR"}
    assert "secret-leak" not in control.text
    assert "hello" not in control.text
    assert "input" not in control.text

    too_long_marker = "OVERLONG_MARKER_" + ("Z" * 40)
    too_long = client.post(
        "/internal/closed-test/events",
        json={
            "session_id": "sess-long",
            "request_id": "req-long",
            "text": too_long_marker + ("x" * 2000),
        },
        headers=headers,
    )
    assert too_long.status_code == 422
    assert too_long.json() == {"detail": "VALIDATION_ERROR"}
    assert too_long_marker not in too_long.text
    assert "input" not in too_long.text


def test_get_auth_missing_wrong_and_query_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_env(monkeypatch)
    client = TestClient(create_app(_settings(database=True)))
    event_id = uuid4()

    missing = client.get(f"/internal/closed-test/events/{event_id}")
    assert missing.status_code == 401
    assert _TOKEN not in missing.text

    wrong = client.get(
        f"/internal/closed-test/events/{event_id}",
        headers={"X-Bot-Closed-Test-Token": "w" * 32},
    )
    assert wrong.status_code == 401
    assert _TOKEN not in wrong.text

    # Token via query string must not authorize.
    via_query = client.get(
        f"/internal/closed-test/events/{event_id}",
        params={"token": _TOKEN, "X-Bot-Closed-Test-Token": _TOKEN},
    )
    assert via_query.status_code == 401
    assert _TOKEN not in via_query.text


def test_input_repr_redacts_text() -> None:
    body = ClosedTestEventCreate(
        session_id="sess-a",
        request_id="req-a",
        text="SECRET_TEXT_SHOULD_NOT_LEAK",
    )
    assert "SECRET_TEXT_SHOULD_NOT_LEAK" not in repr(body)
    assert "SECRET_TEXT_SHOULD_NOT_LEAK" not in str(body)


def test_test_not_in_bot_mode() -> None:
    assert "TEST" not in {mode.value for mode in BotMode}
    with pytest.raises(ValueError, match="BOT_MODE must be one of"):
        Settings.from_env({"BOT_MODE": "TEST", "EMERGENCY_LOCK": "false"})


def test_closed_test_enable_does_not_elevate_m1_live_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_env(monkeypatch)
    settings = Settings.from_env(
        {
            "BOT_MODE": "OFF",
            "EMERGENCY_LOCK": "true",
            "BOT_CLOSED_TEST_ENABLED": "true",
            "BOT_CLOSED_TEST_TOKEN": _TOKEN,
        }
    )
    assert is_live_booking_s2s_read_allowed(settings) is False
    assert ClosedTestConfig.from_env().enabled is True


def test_outbound_remains_denied_with_closed_test_enabled() -> None:
    for mode in BotMode:
        for lock in (True, False):
            settings = Settings(bot_mode=mode, emergency_lock=lock)
            assert (
                is_automatic_outbound_allowed(settings, OutboundAction.SEND_MESSAGE)
                is False
            )


def test_project_safe_synthetic_result_allowlist() -> None:
    safe = project_safe_synthetic_result(
        {
            "schema": "synthetic.outbound.v1",
            "synthetic_token": "SYNTHETIC_OK",
            "plan_type": "CLIENT_REPLY",
            "secret_token": "LEAK",
            "raw_text": "nope",
            "text": "user-facing-must-not-leak",
            "booking_action": "SERVICE_UNAVAILABLE",
            "booking_reason": "ELIGIBILITY_CLIENT_UNAVAILABLE",
            "client_message_kind": "SERVICE_TEMPORARILY_UNAVAILABLE",
        }
    )
    assert safe is not None
    assert safe["synthetic_token"] == "SYNTHETIC_OK"
    assert "secret_token" not in safe
    assert "raw_text" not in safe
    assert "text" not in safe
    assert safe.get("booking_action") == "SERVICE_UNAVAILABLE"
    assert safe.get("client_message_kind") == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert project_safe_synthetic_result({"schema": "other"}) is None


def test_docker_allowlist_includes_closed_test_runtime_files() -> None:
    lines = dockerignore_lines(_REPO)
    assert_canonical_docker_runtime_allowlist(lines)
    required = (
        "!app/core/closed_test_config.py",
        "!app/schemas/closed_test.py",
        "!app/services/closed_test.py",
        "!app/closed_test_router.py",
    )
    for rule in required:
        assert rule in EXPECTED_DOCKER_ALLOW_RULES
        assert rule in lines
        rel = rule[1:]
        assert is_included_in_docker_build_context(rel, lines) is True
        assert (_REPO / rel).is_file()
    assert (
        is_included_in_docker_build_context(
            "app/core/closed_test_config_NOT_ALLOWLISTED.py", lines
        )
        is False
    )


def test_health_ready_does_not_expose_closed_test_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_env(monkeypatch)
    client = TestClient(create_app(_settings(database=True)))
    response = client.get("/health/ready")
    # Fake DB may make ready 503; token must never appear either way.
    assert _TOKEN not in response.text
    assert "BOT_CLOSED_TEST_TOKEN" not in response.text
    if response.status_code == 200:
        assert "closed_test" not in response.json()
