"""Static + compose-config contracts for production S2S wiring (CP-04)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE_COMPOSE = ROOT / "docker-compose.yml"
HOST_PROD_FIXTURE = ROOT / "tests" / "fixtures" / "compose.prod.host.minimal.yaml"
PROD_S2S_COMPOSE = ROOT / "compose.prod.s2s.yaml"
PRODUCTION_COMPOSE = ROOT / "docker-compose.production.yml"
STAGING_COMPOSE = ROOT / "docker-compose.staging.yml"
OPS_DOC = ROOT / "docs" / "ops" / "cp-04-prod-s2s-wiring.md"

PROD_NETWORK = "tvoe-vremya-production_production_internal"
PROD_BASE_URL = "http://tvoe-vremya-production-app:3000"
# Dummy only — never a real secret; length satisfies token min (32).
DUMMY_BEARER = "x" * 32 + "-dummy-prod-s2s-token-not-real"


def test_prod_s2s_overlay_exists_and_documents_stack() -> None:
    assert PROD_S2S_COMPOSE.is_file()
    text = PROD_S2S_COMPOSE.read_text(encoding="utf-8")
    assert "compose.prod.s2s.yaml" in text
    assert "compose.prod.yaml" in text
    assert "docker-compose.production.yml" in text
    assert "never hardcode" in text.lower() or "Secrets stay" in text


def test_prod_s2s_worker_only_env_and_external_network() -> None:
    doc = yaml.safe_load(PROD_S2S_COMPOSE.read_text(encoding="utf-8"))
    assert set(doc["services"]) == {"worker"}
    worker = doc["services"]["worker"]

    env = worker["environment"]
    assert env["BOOKING_ELIGIBILITY_BASE_URL"] == (
        "${BOOKING_ELIGIBILITY_BASE_URL:?BOOKING_ELIGIBILITY_BASE_URL is required}"
    )
    assert env["BOOKING_ELIGIBILITY_BEARER_TOKEN"] == (
        "${BOOKING_ELIGIBILITY_BEARER_TOKEN:?BOOKING_ELIGIBILITY_BEARER_TOKEN is required}"
    )
    assert "BOOKING_ELIGIBILITY_TIMEOUT_SECONDS" in env
    assert "BOOKING_ELIGIBILITY_MAX_RESPONSE_BYTES" in env
    assert env["BOOKING_ELIGIBILITY_MAX_RESPONSE_BYTES"].endswith("262144}")

    assert set(worker["networks"]) == {"default", PROD_NETWORK}

    networks = doc["networks"]
    assert set(networks) == {PROD_NETWORK}
    assert networks[PROD_NETWORK]["external"] is True


def test_prod_s2s_no_hardcoded_secrets_or_host_port_3100() -> None:
    text = PROD_S2S_COMPOSE.read_text(encoding="utf-8")
    # Values must be compose interpolations, not literals.
    assert re.search(r"BEARER_TOKEN:\s*\$\{", text)
    assert re.search(r"BASE_URL:\s*\$\{", text)
    assert "3100" not in text
    # No accidental committed token-looking assignments.
    for line in text.splitlines():
        if "BEARER_TOKEN" in line and not line.strip().startswith("#"):
            assert "${" in line
            assert DUMMY_BEARER not in line


def test_prod_s2s_does_not_join_oz_network_for_postgres_or_api() -> None:
    doc = yaml.safe_load(PROD_S2S_COMPOSE.read_text(encoding="utf-8"))
    assert "api" not in doc["services"]
    assert "migrate" not in doc["services"]
    assert "postgres" not in doc["services"]
    text = PROD_S2S_COMPOSE.read_text(encoding="utf-8")
    assert "bot-tv-postgres" not in text
    # Overlay comments may mention postgres DNS collision, but must not declare
    # a postgres service or alias that would recreate the DB container.
    services_block = text.split("services:", 1)[1]
    assert "aliases:" not in services_block
    assert "DATABASE_URL" not in services_block


def test_production_resource_overlay_still_resource_only() -> None:
    doc = yaml.safe_load(PRODUCTION_COMPOSE.read_text(encoding="utf-8"))
    for name, service in doc["services"].items():
        assert set(service) == {"mem_limit", "cpus", "pids_limit"}, name
    text = PRODUCTION_COMPOSE.read_text(encoding="utf-8")
    assert "compose.prod.s2s.yaml" in text
    assert PROD_NETWORK not in text
    # Resource overlay must not *map* S2S env — comments may reference the name.
    services_block = text.split("services:", 1)[1]
    assert "BOOKING_ELIGIBILITY" not in services_block


def test_staging_resource_overlay_unchanged_by_prod_s2s() -> None:
    text = STAGING_COMPOSE.read_text(encoding="utf-8")
    assert PROD_NETWORK not in text
    assert "tvoe-vremya-production-app" not in text
    assert "BOOKING_ELIGIBILITY" not in text


def test_ops_doc_lists_env_and_no_deps_migrate() -> None:
    text = OPS_DOC.read_text(encoding="utf-8")
    assert "BOT_INTERNAL_API_TOKEN=" in text
    assert f"BOOKING_ELIGIBILITY_BASE_URL={PROD_BASE_URL}" in text
    assert "BOOKING_ELIGIBILITY_BEARER_TOKEN=" in text
    assert "20260829_37_control_plane" in text
    assert "--no-deps" in text
    assert "migrate" in text
    assert "CONTROL_PLANE_POLL_SECONDS" in text
    assert "EMERGENCY_LOCK" in text
    assert "BOT_MODE" in text
    assert "bot-tv-postgres" not in text
    assert "WORKER_POLL_SECONDS" in text


def test_control_plane_migration_present_on_main_tree() -> None:
    path = ROOT / "alembic" / "versions" / "20260829_37_control_plane.py"
    assert path.is_file()
    body = path.read_text(encoding="utf-8")
    assert "control_plane_snapshots" in body
    assert 'revision: str = "20260829_37_control_plane"' in body


def test_dummy_base_url_shape_is_container_dns_not_host_bind() -> None:
    assert PROD_BASE_URL.startswith("http://tvoe-vremya-production-app:")
    assert PROD_BASE_URL.endswith(":3000")
    assert ":3100" not in PROD_BASE_URL


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
def test_docker_compose_config_production_stack_with_dummy_env() -> None:
    assert HOST_PROD_FIXTURE.is_file()
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / "dummy.env"
        env_path.write_text(
            "\n".join(
                [
                    "BOT_MODE=OFF",
                    "EMERGENCY_LOCK=true",
                    "DATABASE_URL=postgresql+asyncpg://bot:bot@127.0.0.1:5432/bot",
                    f"BOOKING_ELIGIBILITY_BASE_URL={PROD_BASE_URL}",
                    f"BOOKING_ELIGIBILITY_BEARER_TOKEN={DUMMY_BEARER}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
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
            f"compose config failed rc={completed.returncode}\n"
            f"stdout={completed.stdout[-2000:]}\n"
            f"stderr={completed.stderr[-2000:]}"
        )
        rendered = completed.stdout
        assert DUMMY_BEARER not in Path(PROD_S2S_COMPOSE).read_text(encoding="utf-8")
        # Resolved config may contain the dummy from env-file; ensure overlay
        # source file itself stays free of it (checked above) and network is set.
        assert PROD_NETWORK in rendered
        assert "BOOKING_ELIGIBILITY_BASE_URL" in rendered
        assert PROD_BASE_URL in rendered
        # Postgres must not list the external online-zapis network.
        doc = yaml.safe_load(rendered)
        postgres_nets = doc["services"]["postgres"].get("networks")
        if postgres_nets is None:
            # Default-only attach — acceptable.
            pass
        else:
            names = (
                set(postgres_nets)
                if isinstance(postgres_nets, dict)
                else set(postgres_nets)
            )
            assert PROD_NETWORK not in names
        worker_nets = doc["services"]["worker"].get("networks")
        assert worker_nets is not None
        worker_names = (
            set(worker_nets) if isinstance(worker_nets, dict) else set(worker_nets)
        )
        assert PROD_NETWORK in worker_names
        postgres = doc["services"]["postgres"]
        pg_nets = postgres.get("networks")
        if pg_nets is None:
            pass
        elif isinstance(pg_nets, dict):
            assert PROD_NETWORK not in pg_nets
            aliases: list[str] = []
            for name, conf in pg_nets.items():
                if name == PROD_NETWORK:
                    raise AssertionError("postgres joined online-zapis network")
                if isinstance(conf, dict) and conf.get("aliases"):
                    aliases.extend(conf["aliases"])
            assert "bot-tv-postgres" not in aliases
        else:
            assert PROD_NETWORK not in set(pg_nets)
        overlay = Path(PROD_S2S_COMPOSE).read_text(encoding="utf-8")
        assert "bot-tv-postgres" not in overlay
        worker_env = doc["services"]["worker"].get("environment") or {}
        if isinstance(worker_env, list):
            env_map = {}
            for item in worker_env:
                if isinstance(item, str) and "=" in item:
                    k, v = item.split("=", 1)
                    env_map[k] = v
            worker_env = env_map
        max_bytes = str(worker_env.get("BOOKING_ELIGIBILITY_MAX_RESPONSE_BYTES", ""))
        assert max_bytes == "262144"
