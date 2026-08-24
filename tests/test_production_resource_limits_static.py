"""Static contracts for bot-TV production-only Docker resource guardrails."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE_COMPOSE = ROOT / "docker-compose.yml"
STAGING_COMPOSE = ROOT / "docker-compose.staging.yml"
PRODUCTION_COMPOSE = ROOT / "docker-compose.production.yml"

# Approved production ceilings (capacity review). Must not leak into staging
# overlay or the shared base compose file.
PRODUCTION_LIMITS = {
    "api": {"mem_limit": "768m", "cpus": 1.0, "pids_limit": 256},
    "worker": {"mem_limit": "1g", "cpus": 1.0, "pids_limit": 256},
    "postgres": {"mem_limit": "768m", "cpus": 0.75, "pids_limit": 256},
}


def _normalize_mem(value: object) -> str:
    text = str(value).strip().lower()
    if text.endswith("gib"):
        return text[:-3] + "g"
    if text.endswith("gi"):
        return text[:-2] + "g"
    if text.endswith("mib"):
        return text[:-3] + "m"
    if text.endswith("mi"):
        return text[:-2] + "m"
    return text


def _normalize_cpus(value: object) -> float:
    return float(value)


def test_production_overlay_declares_exact_resource_limits() -> None:
    assert PRODUCTION_COMPOSE.is_file()
    doc = yaml.safe_load(PRODUCTION_COMPOSE.read_text(encoding="utf-8"))
    services = doc["services"]
    assert set(services) == set(PRODUCTION_LIMITS)

    for name, expected in PRODUCTION_LIMITS.items():
        service = services[name]
        assert set(service) == {"mem_limit", "cpus", "pids_limit"}, (
            f"{name}: production overlay must only declare resource ceilings, "
            f"got {sorted(service)}"
        )
        assert _normalize_mem(service["mem_limit"]) == expected["mem_limit"]
        assert _normalize_cpus(service["cpus"]) == expected["cpus"]
        assert int(service["pids_limit"]) == expected["pids_limit"]


def test_base_compose_has_no_resource_limits() -> None:
    """Shared base must stay without production/staging ceilings."""
    text = BASE_COMPOSE.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    for key in ("mem_limit", "mem_reservation", "cpus", "pids_limit"):
        assert f"{key}:" not in text
    for name, service in doc["services"].items():
        assert "deploy" not in service, f"base service {name} must not set deploy.resources"
        for key in ("mem_limit", "mem_reservation", "cpus", "pids_limit"):
            assert key not in service, f"base service {name} must not set {key}"


def test_staging_overlay_unchanged_by_production_limits() -> None:
    """Production ceilings must not rewrite the staging overlay."""
    text = STAGING_COMPOSE.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    staging = doc["services"]
    assert staging["api"]["mem_limit"] == "512m"
    assert staging["worker"]["mem_limit"] == "768m"
    assert staging["postgres"]["mem_limit"] == "512m"
    assert "Production must not use this overlay" in text


def test_production_overlay_documents_canonical_stack_last() -> None:
    text = PRODUCTION_COMPOSE.read_text(encoding="utf-8")
    assert "Production-only" in text or "production-only" in text.lower()
    assert "Staging must not use this overlay" in text
    assert "/srv/automation-data/bot-tv/prod/repo/docker-compose.yml" in text
    assert "/srv/automation-data/bot-tv/prod/config/compose.prod.yaml" in text
    assert "/srv/automation-data/bot-tv/prod/config/.env" in text
    assert "this overlay (resource limits only)" in text
    assert "-p tv_bot_prod" in text
    assert "--env-file" in text
    # Overlay must stay last in the documented -f order.
    repo_idx = text.index("/srv/automation-data/bot-tv/prod/repo/docker-compose.yml")
    prod_override_idx = text.index(
        "/srv/automation-data/bot-tv/prod/config/compose.prod.yaml"
    )
    overlay_idx = text.index(
        "/srv/automation-data/bot-tv/prod/repo/docker-compose.production.yml"
    )
    assert repo_idx < prod_override_idx < overlay_idx
