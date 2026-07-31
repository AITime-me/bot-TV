from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models.worker_heartbeat import (
    REQUIRED_WORKER_LOOPS,
    WorkerHeartbeat,
)
from app.services.worker_health import (
    WorkerHealthReport,
    assess_worker_health,
)
from app.services.worker_runtime import (
    WorkerLoopSpec,
    WorkerRuntime,
    WorkerRuntimeFatal,
    _lease_worker_id,
    build_default_loop_specs,
)
from app.services.outbound_arbiter import OutboundArbiterDenied

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEST_DATABASE_URL = (
    "postgresql+asyncpg://bot:unit-only@127.0.0.1:5432/bot_tv_test"
)


class _FakeHeartbeatStore:
    def __init__(self) -> None:
        self.registered: list[tuple[object, str]] = []
        self.started: list[str] = []
        self.succeeded: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.failure_count = 0

    async def register(self, *, generation_id, worker_id):  # type: ignore[no-untyped-def]
        self.registered.append((generation_id, worker_id))

    async def tick_started(self, *, loop_name, generation_id):  # type: ignore[no-untyped-def]
        self.started.append(loop_name)

    async def tick_succeeded(self, *, loop_name, generation_id):  # type: ignore[no-untyped-def]
        self.succeeded.append(loop_name)

    async def tick_failed(  # type: ignore[no-untyped-def]
        self,
        *,
        loop_name,
        generation_id,
        error_code,
    ):
        self.failure_count += 1
        self.failed.append((loop_name, error_code))
        return self.failure_count


def _runtime_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": _TEST_DATABASE_URL,
        "worker_heartbeat_interval_seconds": 1,
        "worker_heartbeat_stale_seconds": 45,
    }
    values.update(overrides)
    return Settings(**values)


def _heartbeat(
    loop_name: str,
    *,
    last_succeeded_at: datetime | None,
    last_tick_started_at: datetime | None = None,
    consecutive_failures: int = 0,
    last_error_code: str | None = None,
) -> WorkerHeartbeat:
    return WorkerHeartbeat(
        loop_name=loop_name,
        generation_id=uuid4(),
        worker_id="unit-worker",
        started_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
        last_tick_started_at=last_tick_started_at,
        last_succeeded_at=last_succeeded_at,
        last_failed_at=(
            last_succeeded_at if consecutive_failures > 0 else None
        ),
        consecutive_failures=consecutive_failures,
        last_error_code=last_error_code,
        updated_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )


def test_worker_runtime_settings_are_strict_and_cross_checked() -> None:
    settings = Settings.from_env({"DATABASE_URL": _TEST_DATABASE_URL})
    settings.validate_worker_runtime()
    assert settings.worker_poll_seconds == 1
    assert settings.worker_batch_size == 100
    assert settings.worker_tick_timeout_seconds == 20
    assert settings.worker_heartbeat_interval_seconds == 10
    assert settings.worker_heartbeat_stale_seconds == 45
    assert settings.worker_max_consecutive_failures == 3

    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings().validate_worker_runtime()
    with pytest.raises(ValueError, match="too small"):
        Settings(
            database_url=_TEST_DATABASE_URL,
            handoff_expiry_poll_seconds=60,
            worker_heartbeat_stale_seconds=45,
        ).validate_worker_runtime()

    invalid = {
        "WORKER_POLL_SECONDS": "0",
        "WORKER_BATCH_SIZE": "1001",
        "WORKER_TICK_TIMEOUT_SECONDS": "4",
        "WORKER_HEARTBEAT_INTERVAL_SECONDS": "0",
        "WORKER_HEARTBEAT_STALE_SECONDS": "9",
        "WORKER_MAX_CONSECUTIVE_FAILURES": "21",
    }
    for name, value in invalid.items():
        with pytest.raises(ValueError):
            Settings.from_env({name: value})


def test_assess_worker_health_requires_every_fresh_success() -> None:
    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    healthy_rows = [
        _heartbeat(name, last_succeeded_at=now - timedelta(seconds=2))
        for name in REQUIRED_WORKER_LOOPS
    ]
    healthy = assess_worker_health(
        healthy_rows,
        checked_at=now,
        stale_after_seconds=45,
        tick_timeout_seconds=20,
    )
    assert healthy.healthy is True
    assert healthy.public_view()["healthy"] is True

    unhealthy_rows = healthy_rows[:-2]
    unhealthy_rows[0] = _heartbeat(
        REQUIRED_WORKER_LOOPS[0],
        last_succeeded_at=now - timedelta(seconds=60),
    )
    unhealthy_rows[1] = _heartbeat(
        REQUIRED_WORKER_LOOPS[1],
        last_succeeded_at=now - timedelta(seconds=5),
        consecutive_failures=1,
        last_error_code="RuntimeError",
    )
    unhealthy_rows[2] = _heartbeat(
        REQUIRED_WORKER_LOOPS[2],
        last_succeeded_at=now - timedelta(seconds=30),
        last_tick_started_at=now - timedelta(seconds=25),
    )
    report = assess_worker_health(
        unhealthy_rows,
        checked_at=now,
        stale_after_seconds=45,
        tick_timeout_seconds=20,
    )
    assert report.healthy is False
    assert report.stale_loops == (REQUIRED_WORKER_LOOPS[0],)
    assert report.failed_loops == (REQUIRED_WORKER_LOOPS[1],)
    assert report.stuck_loops == (REQUIRED_WORKER_LOOPS[2],)
    assert report.missing_loops == REQUIRED_WORKER_LOOPS[-2:]

    never_succeeded = [
        _heartbeat(
            REQUIRED_WORKER_LOOPS[0],
            last_succeeded_at=None,
            consecutive_failures=1,
            last_error_code="RuntimeError",
        )
    ]
    report = assess_worker_health(
        never_succeeded,
        checked_at=now,
        stale_after_seconds=45,
        tick_timeout_seconds=20,
    )
    assert REQUIRED_WORKER_LOOPS[0] in report.missing_loops
    assert REQUIRED_WORKER_LOOPS[0] in report.failed_loops


@pytest.mark.asyncio
async def test_runtime_registers_and_heartbeats_successful_loop() -> None:
    stop = asyncio.Event()
    store = _FakeHeartbeatStore()
    called: set[str] = set()

    def make_tick(name: str):  # type: ignore[no-untyped-def]
        async def tick() -> None:
            called.add(name)
            if called == set(REQUIRED_WORKER_LOOPS):
                stop.set()

        return tick

    runtime = WorkerRuntime(
        settings=_runtime_settings(),
        worker_id="unit-worker",
        heartbeat_store=store,  # type: ignore[arg-type]
        loops=tuple(
            WorkerLoopSpec(name, 0.001, make_tick(name))
            for name in REQUIRED_WORKER_LOOPS
        ),
    )
    await runtime.run(stop)

    assert store.registered == [(runtime.generation_id, "unit-worker")]
    assert set(store.started) == set(REQUIRED_WORKER_LOOPS)
    assert set(store.succeeded) == set(REQUIRED_WORKER_LOOPS)
    assert store.failed == []


@pytest.mark.asyncio
async def test_runtime_exits_for_supervisor_after_failure_limit() -> None:
    store = _FakeHeartbeatStore()

    async def failing_tick() -> None:
        raise RuntimeError("payload must not reach logs or heartbeat")

    async def idle_tick() -> None:
        return None

    runtime = WorkerRuntime(
        settings=_runtime_settings(worker_max_consecutive_failures=2),
        worker_id="unit-worker",
        heartbeat_store=store,  # type: ignore[arg-type]
        loops=tuple(
            WorkerLoopSpec(
                name,
                0.001,
                failing_tick if name == REQUIRED_WORKER_LOOPS[0] else idle_tick,
            )
            for name in REQUIRED_WORKER_LOOPS
        ),
    )
    with pytest.raises(WorkerRuntimeFatal, match=REQUIRED_WORKER_LOOPS[0]):
        await runtime.run(asyncio.Event())

    assert store.failure_count == 2
    assert store.failed == [
        (REQUIRED_WORKER_LOOPS[0], "RuntimeError"),
        (REQUIRED_WORKER_LOOPS[0], "RuntimeError"),
    ]


def test_default_runtime_registers_all_required_loops() -> None:
    settings = _runtime_settings()
    specs = build_default_loop_specs(
        settings=settings,
        session_factory=AsyncMock(),
        worker_id="unit-worker",
    )
    assert tuple(spec.name for spec in specs) == REQUIRED_WORKER_LOOPS
    assert next(
        spec for spec in specs if spec.name == "handoff_expiry"
    ).poll_seconds == settings.handoff_expiry_poll_seconds
    assert len(_lease_worker_id("x" * 128, "outbound")) == 128


def test_expected_outbound_fencing_is_not_a_worker_failure() -> None:
    for reason in (
        "MANAGER_OWNED",
        "HANDOFF_NOT_BOT_ACTIVE",
        "STALE_EVENT_SEQUENCE",
        "REPLY_PLAN_CANCELLED",
    ):
        assert OutboundArbiterDenied(reason).is_expected_fence_outcome is True
    for reason in (
        "SYNTHETIC_TRANSIENT",
        "UNSUPPORTED_DESTINATION",
        "CONVERSATION_MISSING",
    ):
        assert OutboundArbiterDenied(reason).is_expected_fence_outcome is False


def test_all_continuous_queue_deadlines_use_postgresql_clock() -> None:
    repositories = _REPO_ROOT / "app" / "repositories"
    for name in (
        "ingress.py",
        "reply_plans.py",
        "outbound.py",
        "amocrm_mirror.py",
    ):
        source = (repositories / name).read_text(encoding="utf-8")
        assert "datetime.now(" not in source
        assert "utcnow(" not in source
        assert "resolve_moment" in source


def test_ready_endpoint_fails_closed_for_unhealthy_worker() -> None:
    now = datetime(2026, 7, 29, tzinfo=timezone.utc)
    report = WorkerHealthReport(
        healthy=False,
        checked_at=now,
        missing_loops=("outbound",),
        stale_loops=(),
        failed_loops=(),
        stuck_loops=(),
    )
    health_service = AsyncMock()
    health_service.check.return_value = report
    app = create_app(
        Settings(database_url=_TEST_DATABASE_URL),
        worker_health_service=health_service,
    )

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["worker_health"]["missing_loops"] == ["outbound"]


def test_docker_runtime_has_real_health_restart_and_secret_exclusions() -> None:
    from tests.docker_runtime_allowlist import (
        assert_canonical_docker_runtime_allowlist,
        dockerignore_lines,
    )

    lines = dockerignore_lines(_REPO_ROOT)
    assert_canonical_docker_runtime_allowlist(lines)
    allow_rules = [line for line in lines if line.startswith("!")]

    canary_paths = (
        "app/secrets.json",
        "app/private-key.pem",
        "alembic/credentials.env",
    )
    for canary in canary_paths:
        assert f"!{canary}" not in allow_rules
        assert canary not in {rule[1:] for rule in allow_rules}

    assert "**/__pycache__/" in lines
    assert "**/*.py[cod]" in lines

    dockerfile = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER bot-tv" in dockerfile
    assert "COPY . " not in dockerfile
    assert "requirements-lock.txt" in dockerfile

    compose = yaml.safe_load(
        (_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    assert set(compose["services"]) == {"migrate", "api", "worker"}
    for service_name in ("api", "worker"):
        service = compose["services"][service_name]
        assert service["restart"] == "unless-stopped"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["healthcheck"]["test"][0:3] == ["CMD", "python", "-m"]
    assert compose["services"]["worker"]["command"] == [
        "python",
        "-m",
        "app.worker",
    ]
    assert compose["services"]["migrate"]["command"] == [
        "alembic",
        "upgrade",
        "head",
    ]
