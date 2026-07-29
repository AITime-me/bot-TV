from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.config import Settings
from app.db.session import session_scope
from app.db.worker_lock import (
    WorkerAlreadyRunningError,
    worker_singleton_lock,
)
from app.models.worker_heartbeat import (
    REQUIRED_WORKER_LOOPS,
    WorkerHeartbeat,
)
from app.main import create_app
from app.repositories import worker_heartbeats as heartbeat_repo
from app.repositories.worker_heartbeats import StaleWorkerGenerationError
from app.services.worker_health import WorkerHealthService
from app.services.worker_runtime import (
    WorkerHeartbeatStore,
    WorkerRuntime,
    build_default_loop_specs,
)
from tests.pg_harness import truncate_foundation_tables

_RUNTIME_ONLY_URL = (
    "postgresql+asyncpg://bot:not-used@127.0.0.1:5432/bot_tv_test"
)


@pytest_asyncio.fixture(autouse=True)
async def worker_runtime_row_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


@pytest.mark.asyncio
async def test_generation_registration_and_heartbeat_health(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = WorkerHeartbeatStore(session_factory)
    generation = uuid4()
    await store.register(generation_id=generation, worker_id="pg-worker")

    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(WorkerHeartbeat).order_by(
                        WorkerHeartbeat.loop_name
                    )
                )
            ).all()
        )
    assert {row.loop_name for row in rows} == set(REQUIRED_WORKER_LOOPS)
    assert {row.generation_id for row in rows} == {generation}
    assert all(row.last_succeeded_at is None for row in rows)

    for loop_name in REQUIRED_WORKER_LOOPS:
        await store.tick_started(
            loop_name=loop_name,
            generation_id=generation,
        )
        await store.tick_succeeded(
            loop_name=loop_name,
            generation_id=generation,
        )

    service = WorkerHealthService(
        session_factory,
        stale_after_seconds=45,
        tick_timeout_seconds=20,
    )
    assert (await service.check()).healthy is True
    app = create_app(
        Settings(database_url=_RUNTIME_ONLY_URL),
        worker_health_service=service,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (await client.get("/health/ready")).status_code == 200

    failures = await store.tick_failed(
        loop_name=REQUIRED_WORKER_LOOPS[0],
        generation_id=generation,
        error_code="SyntheticFailure",
    )
    assert failures == 1
    failed = await service.check()
    assert failed.healthy is False
    assert failed.failed_loops == (REQUIRED_WORKER_LOOPS[0],)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (await client.get("/health/ready")).status_code == 503

    await store.tick_succeeded(
        loop_name=REQUIRED_WORKER_LOOPS[0],
        generation_id=generation,
    )
    assert (await service.check()).healthy is True


@pytest.mark.asyncio
async def test_new_generation_fences_previous_worker(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = WorkerHeartbeatStore(session_factory)
    first = uuid4()
    second = uuid4()
    await store.register(generation_id=first, worker_id="worker-first")
    await store.tick_started(
        loop_name=REQUIRED_WORKER_LOOPS[0],
        generation_id=first,
    )
    await store.register(generation_id=second, worker_id="worker-second")

    with pytest.raises(StaleWorkerGenerationError):
        await store.tick_succeeded(
            loop_name=REQUIRED_WORKER_LOOPS[0],
            generation_id=first,
        )

    await store.tick_started(
        loop_name=REQUIRED_WORKER_LOOPS[0],
        generation_id=second,
    )
    await store.tick_succeeded(
        loop_name=REQUIRED_WORKER_LOOPS[0],
        generation_id=second,
    )
    async with session_factory() as session:
        row = await session.get(WorkerHeartbeat, REQUIRED_WORKER_LOOPS[0])
    assert row is not None
    assert row.generation_id == second
    assert row.worker_id == "worker-second"


@pytest.mark.asyncio
async def test_worker_runtime_advisory_lock_is_singleton_and_recoverable(
    pg_engine: AsyncEngine,
) -> None:
    async with worker_singleton_lock(pg_engine):
        with pytest.raises(WorkerAlreadyRunningError):
            async with worker_singleton_lock(pg_engine):
                pass

    async with worker_singleton_lock(pg_engine):
        pass


@pytest.mark.asyncio
async def test_worker_heartbeat_constraints_reject_impossible_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO worker_heartbeats (
                            loop_name,
                            generation_id,
                            worker_id,
                            consecutive_failures,
                            last_error_code
                        )
                        VALUES (
                            'invented_loop',
                            gen_random_uuid(),
                            'bad-worker',
                            0,
                            'IMPOSSIBLE'
                        )
                        """
                    )
                )


@pytest.mark.asyncio
async def test_continuous_runtime_starts_all_loops_and_stops_cleanly(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        database_url=_RUNTIME_ONLY_URL,
        worker_poll_seconds=1,
        worker_batch_size=5,
        worker_tick_timeout_seconds=20,
        worker_heartbeat_interval_seconds=1,
        worker_heartbeat_stale_seconds=45,
        worker_max_consecutive_failures=3,
    )
    runtime = WorkerRuntime(
        settings=settings,
        worker_id="pg-runtime",
        heartbeat_store=WorkerHeartbeatStore(session_factory),
        loops=build_default_loop_specs(
            settings=settings,
            session_factory=session_factory,
            worker_id="pg-runtime",
        ),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runtime.run(stop))
    health = WorkerHealthService(
        session_factory,
        stale_after_seconds=45,
        tick_timeout_seconds=20,
    )
    try:
        report = None
        for _ in range(100):
            await asyncio.sleep(0.05)
            report = await health.check()
            if report.healthy:
                break
        assert report is not None
        assert report.healthy is True
        assert task.done() is False
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    async with session_scope(session_factory) as session:
        rows = await heartbeat_repo.list_required(session)
    assert {row.generation_id for row in rows} == {runtime.generation_id}
    assert all(row.last_succeeded_at is not None for row in rows)
