"""Static contracts for bot-TV staging-only Docker resource guardrails."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE_COMPOSE = ROOT / "docker-compose.yml"
STAGING_COMPOSE = ROOT / "docker-compose.staging.yml"

# Approved staging ceilings (capacity review). Production must not inherit these
# via the base compose file.
STAGING_LIMITS = {
    "api": {"mem_limit": "512m", "cpus": 0.5, "pids_limit": 256},
    "worker": {"mem_limit": "768m", "cpus": 0.75, "pids_limit": 256},
    "attachment-maintenance": {
        "mem_limit": "256m",
        "cpus": 0.25,
        "pids_limit": 128,
    },
    "postgres": {"mem_limit": "512m", "cpus": 0.5, "pids_limit": 256},
}


def _normalize_mem(value: object) -> str:
    text = str(value).strip().lower()
    if text.endswith("mi") or text.endswith("mib"):
        return text[:-2] + "m" if text.endswith("mi") else text[:-3] + "m"
    return text


def _normalize_cpus(value: object) -> float:
    return float(value)


def test_staging_overlay_declares_exact_resource_limits() -> None:
    assert STAGING_COMPOSE.is_file()
    doc = yaml.safe_load(STAGING_COMPOSE.read_text(encoding="utf-8"))
    services = doc["services"]
    assert set(services) == set(STAGING_LIMITS)

    for name, expected in STAGING_LIMITS.items():
        service = services[name]
        assert set(service) == {"mem_limit", "cpus", "pids_limit"}, (
            f"{name}: staging overlay must only declare resource ceilings, got {sorted(service)}"
        )
        assert _normalize_mem(service["mem_limit"]) == expected["mem_limit"]
        assert _normalize_cpus(service["cpus"]) == expected["cpus"]
        assert int(service["pids_limit"]) == expected["pids_limit"]


def test_base_compose_has_no_resource_limits() -> None:
    """Production/shared base must stay without staging ceilings."""
    text = BASE_COMPOSE.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    for key in ("mem_limit", "mem_reservation", "cpus", "pids_limit"):
        assert f"{key}:" not in text
    for name, service in doc["services"].items():
        assert "deploy" not in service, f"base service {name} must not set deploy.resources"
        for key in ("mem_limit", "mem_reservation", "cpus", "pids_limit"):
            assert key not in service, f"base service {name} must not set {key}"


def test_staging_overlay_documents_canonical_stack_last() -> None:
    text = STAGING_COMPOSE.read_text(encoding="utf-8")
    assert "Staging-only" in text or "staging-only" in text.lower()
    assert "Production must not use this overlay" in text
    assert "/srv/automation-data/bot-tv/stage/repo/docker-compose.yml" in text
    assert "/srv/automation-data/bot-tv/stage/config/compose.stage.yaml" in text
    assert (
        "/srv/automation-data/bot-tv/stage/config/compose.maintenance-enabled.yaml"
        in text
    )
    assert "this overlay (resource limits only)" in text
    # Overlay must stay last in the documented -f order.
    repo_idx = text.index("/srv/automation-data/bot-tv/stage/repo/docker-compose.yml")
    stage_idx = text.index(
        "/srv/automation-data/bot-tv/stage/config/compose.stage.yaml"
    )
    maint_idx = text.index(
        "/srv/automation-data/bot-tv/stage/config/compose.maintenance-enabled.yaml"
    )
    overlay_idx = text.index(
        "/srv/automation-data/bot-tv/stage/repo/docker-compose.staging.yml"
    )
    assert repo_idx < stage_idx < maint_idx < overlay_idx
