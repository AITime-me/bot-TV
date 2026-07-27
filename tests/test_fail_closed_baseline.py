import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BotMode, Settings
from app.core.outbound_policy import (
    OutboundAction,
    is_automatic_outbound_allowed,
)
from app.main import create_app


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_safe_defaults() -> None:
    settings = Settings.from_env({})

    assert settings.bot_mode is BotMode.OFF
    assert settings.emergency_lock is True


def test_allowed_bot_modes_are_fixed() -> None:
    assert {mode.value for mode in BotMode} == {
        "OFF",
        "HINTS",
        "DRAFT",
        "AUTO_READ",
        "AUTO_WRITE",
    }


def test_off_startup_does_not_require_integration_tokens() -> None:
    client = TestClient(create_app(Settings.from_env({})))

    assert client.get("/health/ready").status_code == 200


@pytest.mark.parametrize("bot_mode", list(BotMode))
@pytest.mark.parametrize("emergency_lock", [True, False])
def test_all_modes_deny_outbound_action(
    bot_mode: BotMode,
    emergency_lock: bool,
) -> None:
    settings = Settings(bot_mode=bot_mode, emergency_lock=emergency_lock)

    assert (
        is_automatic_outbound_allowed(settings, OutboundAction.SEND_MESSAGE)
        is False
    )


def test_unknown_action_fails_closed() -> None:
    settings = Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=False)

    assert is_automatic_outbound_allowed(settings, "UNKNOWN_ACTION") is False


def test_missing_or_invalid_settings_fail_closed() -> None:
    assert (
        is_automatic_outbound_allowed(None, OutboundAction.SEND_MESSAGE) is False
    )
    assert (
        is_automatic_outbound_allowed(object(), OutboundAction.SEND_MESSAGE)
        is False
    )


@pytest.mark.parametrize(
    "value",
    ["", "off", "AUTO", "UNKNOWN", " AUTO_WRITE", "AUTO_WRITE "],
)
def test_invalid_bot_mode_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="BOT_MODE must be one of"):
        Settings.from_env({"BOT_MODE": value})


@pytest.mark.parametrize(
    "value",
    [
        "",
        "True",
        "FALSE",
        " false ",
        "0",
        "1",
        "yes",
        "no",
        "on",
        "off",
        "maybe",
    ],
)
def test_invalid_emergency_lock_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="EMERGENCY_LOCK must be a boolean"):
        Settings.from_env({"EMERGENCY_LOCK": value})


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("false", False)],
)
def test_emergency_lock_accepts_only_canonical_values(
    value: str,
    expected: bool,
) -> None:
    assert Settings.from_env({"EMERGENCY_LOCK": value}).emergency_lock is expected


def test_health_endpoint_remains_compatible() -> None:
    client = TestClient(create_app(Settings()))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_live_endpoint_reports_process_liveness() -> None:
    client = TestClient(create_app(Settings()))

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_endpoint_reports_safe_state() -> None:
    client = TestClient(create_app(Settings()))

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "bot_mode": "OFF",
        "emergency_lock": True,
        "outbound_enabled": False,
    }


def test_ready_endpoint_keeps_auto_write_outbound_disabled() -> None:
    client = TestClient(
        create_app(
            Settings(
                bot_mode=BotMode.AUTO_WRITE,
                emergency_lock=False,
            )
        )
    )

    response = client.get("/health/ready")
    body = response.json()

    assert response.status_code == 200
    assert set(body) == {
        "status",
        "bot_mode",
        "emergency_lock",
        "outbound_enabled",
    }
    assert body == {
        "status": "ready",
        "bot_mode": "AUTO_WRITE",
        "emergency_lock": False,
        "outbound_enabled": False,
    }


def test_create_app_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_MODE", "DRAFT")
    monkeypatch.setenv("EMERGENCY_LOCK", "false")

    response = TestClient(create_app()).get("/health/ready")

    assert response.json() == {
        "status": "ready",
        "bot_mode": "DRAFT",
        "emergency_lock": False,
        "outbound_enabled": False,
    }


def test_create_app_uses_safe_defaults_without_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOT_MODE", raising=False)
    monkeypatch.delenv("EMERGENCY_LOCK", raising=False)

    response = TestClient(create_app()).get("/health/ready")

    assert response.json() == {
        "status": "ready",
        "bot_mode": "OFF",
        "emergency_lock": True,
        "outbound_enabled": False,
    }


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("BOT_MODE", "UNKNOWN", "BOT_MODE must be one of"),
        ("EMERGENCY_LOCK", "False", "EMERGENCY_LOCK must be a boolean"),
    ],
)
def test_invalid_environment_blocks_app_creation(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.delenv("BOT_MODE", raising=False)
    monkeypatch.delenv("EMERGENCY_LOCK", raising=False)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        create_app()


def _fresh_import_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("BOT_MODE", None)
    environment.pop("EMERGENCY_LOCK", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def test_uvicorn_compatible_app_imports_with_safe_defaults() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from app.main import app; "
                "paths = {route.path for route in app.routes}; "
                "assert {'/health', '/health/live', '/health/ready'} <= paths"
            ),
        ],
        cwd=REPOSITORY_ROOT,
        env=_fresh_import_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BOT_MODE", "UNKNOWN"),
        ("EMERGENCY_LOCK", "False"),
    ],
)
def test_fresh_app_import_rejects_invalid_environment(
    name: str,
    value: str,
) -> None:
    environment = _fresh_import_environment()
    environment[name] = value

    result = subprocess.run(
        [sys.executable, "-c", "from app.main import app"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0


def test_health_responses_do_not_expose_secret_values() -> None:
    fake_secrets = {
        "VK_TOKEN": "synthetic-vk-secret",
        "AMOCRM_CLIENT_SECRET": "synthetic-amocrm-secret",
        "TELEGRAM_BOT_TOKEN": "synthetic-telegram-secret",
        "YANDEX_API_KEY": "synthetic-ai-secret",
        "DATABASE_URL": (
            "postgresql+asyncpg://bot:synthetic-db-secret@127.0.0.1:5432/bot"
        ),
    }
    client = TestClient(create_app(Settings.from_env(fake_secrets)))

    responses = [
        client.get("/health").json(),
        client.get("/health/live").json(),
        client.get("/health/ready").json(),
    ]
    serialized = json.dumps(responses)

    for secret in fake_secrets.values():
        assert secret not in serialized
    assert "synthetic-db-secret" not in serialized
    assert "DATABASE_URL" not in serialized
