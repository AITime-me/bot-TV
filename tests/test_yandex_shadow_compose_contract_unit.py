"""Compose env contract for Yandex shadow draft (worker-only, default-off)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.docker_runtime_allowlist import assert_canonical_docker_runtime_allowlist

ROOT = Path(__file__).resolve().parents[1]
BASE_COMPOSE = ROOT / "docker-compose.yml"
HOST_PROD_FIXTURE = ROOT / "tests" / "fixtures" / "compose.prod.host.minimal.yaml"
PROD_S2S_COMPOSE = ROOT / "compose.prod.s2s.yaml"
PRODUCTION_COMPOSE = ROOT / "docker-compose.production.yml"

# Dummy host values only — never real credentials. Length/shape satisfy parsers.
_DUMMY_API_KEY = "y" * 32 + "-dummy-yandex-api-key-not-real"
_DUMMY_FOLDER_ID = "b1gdummyfolderid00000000000000001"


def _service_env_map(service: dict[str, Any]) -> dict[str, str]:
    raw = service.get("environment") or {}
    if isinstance(raw, dict):
        return {str(k): "" if v is None else str(v) for k, v in raw.items()}
    env_map: dict[str, str] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                env_map[key] = value
            elif isinstance(item, dict):
                for key, value in item.items():
                    env_map[str(key)] = "" if value is None else str(value)
    return env_map


def _compose_config(env_path: Path) -> dict[str, Any]:
    cmd = [
        "docker",
        "compose",
        "--env-file",
        str(env_path),
        "-f",
        str(BASE_COMPOSE),
        "-f",
        str(HOST_PROD_FIXTURE),
        "-f",
        str(PROD_S2S_COMPOSE),
        "-f",
        str(PRODUCTION_COMPOSE),
        "config",
    ]
    completed = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env={**os.environ, "COMPOSE_ANSI": "never"},
    )
    assert completed.returncode == 0, (
        f"compose config failed rc={completed.returncode} "
        f"stderr_len={len(completed.stderr)}"
    )
    # Never assert on raw stdout that may contain interpolated secrets.
    return yaml.safe_load(completed.stdout)


def test_compose_yandex_anchor_worker_only_static() -> None:
    text = BASE_COMPOSE.read_text(encoding="utf-8")
    compose = yaml.safe_load(text)

    worker_env = compose["services"]["worker"]["environment"]
    api_env = compose["services"]["api"]["environment"]
    migrate_env = compose["services"]["migrate"]["environment"]

    assert worker_env["YANDEX_LLM_ENABLED"] == "${YANDEX_LLM_ENABLED:-false}"
    assert worker_env["YANDEX_SHADOW_DRAFT_ENABLED"] == (
        "${YANDEX_SHADOW_DRAFT_ENABLED:-false}"
    )
    assert worker_env["YANDEX_SHADOW_ALLOW_UNDER_EMERGENCY_LOCK"] == (
        "${YANDEX_SHADOW_ALLOW_UNDER_EMERGENCY_LOCK:-false}"
    )
    assert worker_env["YANDEX_API_KEY"] == "${YANDEX_API_KEY:-}"
    assert worker_env["YANDEX_FOLDER_ID"] == "${YANDEX_FOLDER_ID:-}"
    assert worker_env["YANDEX_MODEL_URI"] == "${YANDEX_MODEL_URI:-}"
    assert worker_env["YANDEX_LLM_API_BASE_URL"] == (
        "${YANDEX_LLM_API_BASE_URL:-https://llm.api.cloud.yandex.net}"
    )
    assert worker_env["YANDEX_LLM_TIMEOUT_SECONDS"] == (
        "${YANDEX_LLM_TIMEOUT_SECONDS:-15.0}"
    )
    assert worker_env["YANDEX_LLM_MAX_RESPONSE_BYTES"] == (
        "${YANDEX_LLM_MAX_RESPONSE_BYTES:-65536}"
    )
    assert worker_env["YANDEX_LLM_TEMPERATURE"] == (
        "${YANDEX_LLM_TEMPERATURE:-0.3}"
    )
    assert worker_env["YANDEX_LLM_MAX_TOKENS"] == (
        "${YANDEX_LLM_MAX_TOKENS:-1024}"
    )

    for key in (
        "YANDEX_LLM_ENABLED",
        "YANDEX_API_KEY",
        "YANDEX_FOLDER_ID",
        "YANDEX_SHADOW_DRAFT_ENABLED",
        "YANDEX_SHADOW_ALLOW_UNDER_EMERGENCY_LOCK",
    ):
        assert key not in api_env
        assert key not in migrate_env

    # No hardcoded secret material in compose source.
    assert _DUMMY_API_KEY not in text
    assert "sk-" not in text
    assert "Api-Key " not in text


def test_docker_runtime_allowlist_still_canonical() -> None:
    assert_canonical_docker_runtime_allowlist()


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
def test_compose_config_default_off_yandex_worker_flags() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / "prod-like.env"
        # Production-like: Yandex vars ABSENT from host env.
        env_path.write_text(
            "\n".join(
                [
                    "BOT_MODE=OFF",
                    "EMERGENCY_LOCK=true",
                    "DATABASE_URL=postgresql+asyncpg://bot:bot@127.0.0.1:5432/bot",
                    "BOOKING_ELIGIBILITY_BASE_URL=http://tvoe-vremya-production-app:3000",
                    "BOOKING_ELIGIBILITY_BEARER_TOKEN=" + ("x" * 32 + "-dummy"),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        doc = _compose_config(env_path)
        worker_env = _service_env_map(doc["services"]["worker"])
        api_env = _service_env_map(doc["services"]["api"])

        assert worker_env.get("YANDEX_LLM_ENABLED") == "false"
        assert worker_env.get("YANDEX_SHADOW_DRAFT_ENABLED") == "false"
        assert worker_env.get("YANDEX_SHADOW_ALLOW_UNDER_EMERGENCY_LOCK") == "false"
        # Credentials empty/absent-safe under default-off.
        assert worker_env.get("YANDEX_API_KEY", "") == ""
        assert worker_env.get("YANDEX_FOLDER_ID", "") == ""
        # Optional defaults preserved (not empty-broken).
        assert worker_env.get("YANDEX_LLM_API_BASE_URL") == (
            "https://llm.api.cloud.yandex.net"
        )
        assert worker_env.get("YANDEX_LLM_TIMEOUT_SECONDS") == "15.0"
        assert worker_env.get("YANDEX_LLM_MAX_RESPONSE_BYTES") == "65536"
        assert worker_env.get("YANDEX_LLM_TEMPERATURE") == "0.3"
        assert worker_env.get("YANDEX_LLM_MAX_TOKENS") == "1024"

        assert "YANDEX_API_KEY" not in api_env
        assert "YANDEX_FOLDER_ID" not in api_env
        assert "YANDEX_LLM_ENABLED" not in api_env


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
def test_compose_config_enabled_yandex_worker_receives_supplied_env() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / "enabled.env"
        env_path.write_text(
            "\n".join(
                [
                    "BOT_MODE=OFF",
                    "EMERGENCY_LOCK=true",
                    "DATABASE_URL=postgresql+asyncpg://bot:bot@127.0.0.1:5432/bot",
                    "BOOKING_ELIGIBILITY_BASE_URL=http://tvoe-vremya-production-app:3000",
                    "BOOKING_ELIGIBILITY_BEARER_TOKEN=" + ("x" * 32 + "-dummy"),
                    "YANDEX_LLM_ENABLED=true",
                    f"YANDEX_API_KEY={_DUMMY_API_KEY}",
                    f"YANDEX_FOLDER_ID={_DUMMY_FOLDER_ID}",
                    "YANDEX_SHADOW_DRAFT_ENABLED=true",
                    "YANDEX_SHADOW_ALLOW_UNDER_EMERGENCY_LOCK=true",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        doc = _compose_config(env_path)
        worker_env = _service_env_map(doc["services"]["worker"])
        api_env = _service_env_map(doc["services"]["api"])

        assert worker_env.get("YANDEX_LLM_ENABLED") == "true"
        assert worker_env.get("YANDEX_SHADOW_DRAFT_ENABLED") == "true"
        assert worker_env.get("YANDEX_SHADOW_ALLOW_UNDER_EMERGENCY_LOCK") == "true"
        # Equality checks only — failure messages must not be custom-printed.
        assert worker_env.get("YANDEX_API_KEY") == _DUMMY_API_KEY
        assert worker_env.get("YANDEX_FOLDER_ID") == _DUMMY_FOLDER_ID

        assert "YANDEX_API_KEY" not in api_env
        assert "YANDEX_FOLDER_ID" not in api_env
        assert api_env.get("YANDEX_LLM_ENABLED") is None

        # Compose source stays free of the dummy secret.
        assert _DUMMY_API_KEY not in BASE_COMPOSE.read_text(encoding="utf-8")
