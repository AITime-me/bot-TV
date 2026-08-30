"""S2S poll cadence + expected 429 must not fatal the worker (CP-04-POST-HARDEN)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.config import BotMode, Settings
from app.core.acquisition_source_http import AcquisitionSourceHttpError
from app.core.booking_method_http import BookingMethodHttpError
from app.core.booking_request_http import BookingRequestHttpError
from app.core.s2s_rate_limit import (
    IDLE_S2S_HEADROOM,
    OZ_BOT_INTERNAL_MAX_PER_MINUTE,
    idle_s2s_budget_ok,
    idle_s2s_requests_per_minute,
    is_expected_s2s_rate_limited,
)
from app.models.worker_heartbeat import (
    ACQUISITION_SOURCE_ANALYTICS_LOOP,
    BOOKING_METHOD_ANALYTICS_LOOP,
    CONTROL_PLANE_SNAPSHOT_LOOP,
    INGRESS_LOOP,
    OUTBOUND_LOOP,
    REQUIRED_WORKER_LOOPS,
    TEYA_REQUEST_ORCHESTRATOR_LOOP,
    TEYA_REQUEST_RECONCILIATION_LOOP,
)
from app.services.worker_runtime import (
    WorkerLoopSpec,
    WorkerRuntime,
    WorkerRuntimeFatal,
    build_default_loop_specs,
)

_TEST_DATABASE_URL = (
    "postgresql+asyncpg://bot:unit-only@127.0.0.1:5432/bot_tv_test"
)


class _FakeHeartbeatStore:
    def __init__(self) -> None:
        self.failed: list[tuple[str, str]] = []
        self.succeeded: list[str] = []
        self.failure_count = 0

    async def register(self, *, generation_id, worker_id):  # type: ignore[no-untyped-def]
        return None

    async def tick_started(self, *, loop_name, generation_id):  # type: ignore[no-untyped-def]
        return None

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


def test_default_remote_cadences_are_not_generic_one_second() -> None:
    settings = Settings(database_url=_TEST_DATABASE_URL)
    specs = build_default_loop_specs(
        settings=settings,
        session_factory=AsyncMock(),
        worker_id="unit-worker",
    )
    by_name = {spec.name: spec for spec in specs}
    assert by_name[INGRESS_LOOP].poll_seconds == 1
    assert by_name[OUTBOUND_LOOP].poll_seconds == 1
    assert by_name[TEYA_REQUEST_ORCHESTRATOR_LOOP].poll_seconds == 5
    assert by_name[TEYA_REQUEST_RECONCILIATION_LOOP].poll_seconds == 30
    assert by_name[BOOKING_METHOD_ANALYTICS_LOOP].poll_seconds == 30
    assert by_name[ACQUISITION_SOURCE_ANALYTICS_LOOP].poll_seconds == 30
    assert by_name[CONTROL_PLANE_SNAPSHOT_LOOP].poll_seconds == 30
    assert settings.worker_poll_seconds == 1
    assert settings.worker_max_consecutive_failures == 3
    assert settings.bot_mode is BotMode.OFF
    assert settings.emergency_lock is True


def test_idle_s2s_rate_budget_has_headroom_vs_oz_bucket() -> None:
    settings = Settings.from_env({})
    rpm = idle_s2s_requests_per_minute(settings)
    # 12 (teya/5s) + 0 recon idle + 2 + 2 + 4 CP = 20
    assert rpm == 20.0
    assert idle_s2s_budget_ok(settings) is True
    assert rpm * IDLE_S2S_HEADROOM <= OZ_BOT_INTERNAL_MAX_PER_MINUTE


def test_idle_budget_rejects_one_second_remote_feeds() -> None:
    one_second_rpm = 60.0 * 3 + 4.0
    assert one_second_rpm > OZ_BOT_INTERNAL_MAX_PER_MINUTE
    crowded = Settings(
        database_url=_TEST_DATABASE_URL,
        teya_request_poll_seconds=5,
        booking_method_analytics_poll_seconds=5,
        acquisition_source_analytics_poll_seconds=5,
        teya_request_reconciliation_poll_seconds=5,
        control_plane_refresh_seconds=5,
    )
    assert idle_s2s_budget_ok(crowded) is False
    with pytest.raises(ValueError, match="rate-limit budget"):
        crowded.validate_worker_runtime()


def test_remote_poll_env_rejects_one_second() -> None:
    with pytest.raises(ValueError):
        Settings.from_env({"TEYA_REQUEST_POLL_SECONDS": "1"})
    with pytest.raises(ValueError):
        Settings.from_env({"BOOKING_METHOD_ANALYTICS_POLL_SECONDS": "1"})


def test_is_expected_s2s_rate_limited() -> None:
    assert is_expected_s2s_rate_limited(BookingRequestHttpError("RATE_LIMITED"))
    assert is_expected_s2s_rate_limited(BookingMethodHttpError("RATE_LIMITED"))
    assert is_expected_s2s_rate_limited(
        AcquisitionSourceHttpError("RATE_LIMITED")
    )
    assert not is_expected_s2s_rate_limited(BookingRequestHttpError("TIMEOUT"))
    assert not is_expected_s2s_rate_limited(RuntimeError("RATE_LIMITED"))


@pytest.mark.asyncio
async def test_rate_limited_ticks_do_not_fatal_worker() -> None:
    store = _FakeHeartbeatStore()

    async def always_429() -> None:
        raise BookingRequestHttpError("RATE_LIMITED")

    async def idle() -> None:
        return None

    runtime = WorkerRuntime(
        settings=Settings(
            database_url=_TEST_DATABASE_URL,
            worker_max_consecutive_failures=3,
            worker_heartbeat_interval_seconds=1,
            worker_heartbeat_stale_seconds=45,
        ),
        worker_id="unit-worker",
        heartbeat_store=store,  # type: ignore[arg-type]
        loops=tuple(
            WorkerLoopSpec(
                name,
                0.001,
                always_429
                if name == TEYA_REQUEST_ORCHESTRATOR_LOOP
                else idle,
            )
            for name in REQUIRED_WORKER_LOOPS
        ),
        generation_id=uuid4(),
    )
    stop = asyncio.Event()

    async def run_then_stop() -> None:
        task = asyncio.create_task(runtime.run(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    await run_then_stop()
    assert store.failure_count == 0
    assert store.failed == []
    assert TEYA_REQUEST_ORCHESTRATOR_LOOP in store.succeeded


@pytest.mark.asyncio
async def test_unexpected_errors_still_hit_failure_limit() -> None:
    store = _FakeHeartbeatStore()

    async def boom() -> None:
        raise RuntimeError("not-rate-limit")

    async def idle() -> None:
        return None

    runtime = WorkerRuntime(
        settings=Settings(
            database_url=_TEST_DATABASE_URL,
            worker_max_consecutive_failures=2,
            worker_heartbeat_interval_seconds=1,
            worker_heartbeat_stale_seconds=45,
        ),
        worker_id="unit-worker",
        heartbeat_store=store,  # type: ignore[arg-type]
        loops=tuple(
            WorkerLoopSpec(
                name,
                0.001,
                boom if name == INGRESS_LOOP else idle,
            )
            for name in REQUIRED_WORKER_LOOPS
        ),
    )
    with pytest.raises(WorkerRuntimeFatal, match=INGRESS_LOOP):
        await runtime.run(asyncio.Event())
    assert store.failure_count == 2
