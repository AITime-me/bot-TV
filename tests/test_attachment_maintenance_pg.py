"""PostgreSQL composition tests for AttachmentMaintenanceRunner (CURSOR-13 Stage 2B).

Proves runner → real AttachmentSpoolStore → disposable PostgreSQL + tmp spool.
Does not re-cover domain purge/reconcile edge cases from spool/purge PG suites.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import attachment_fs
from app.core.attachment_keys import EnvAttachmentKeyProvider
from app.core.attachment_maintenance_types import (
    AttachmentMaintenanceConfig,
    AttachmentMaintenanceCycleStatus,
)
from app.core.attachment_types import (
    AttachmentKind,
    AttachmentPurpose,
    AttachmentSpoolPolicy,
)
from app.db.session import create_engine, create_session_factory
from app.models.attachment_spool import AttachmentSpoolObject
from app.services.attachment_maintenance import AttachmentMaintenanceRunner
from app.services.attachment_spool_store import AttachmentSpoolStore
from tests.attachment_spool_fakes import synthetic_minimal_jpeg
from tests.foundation_test_db import SecretDatabaseUrl
from tests.pg_harness import truncate_foundation_tables

_JPEG = synthetic_minimal_jpeg()
_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
_TTL_SECONDS = 900
_RUN_FOREVER_GUARD_SECONDS = 10.0

_AGE_WRITING_SQL = """
UPDATE attachment_spool_objects
SET
    state = 'WRITING',
    updated_at = statement_timestamp() - interval '20 minutes'
WHERE reference_digest = :digest
"""

_EXPIRE_OBJECT_BY_DIGEST_SQL = """
UPDATE attachment_spool_objects
SET
    created_at = statement_timestamp() - interval '902 seconds',
    updated_at = statement_timestamp() - interval '902 seconds',
    expires_at = statement_timestamp() - interval '1 second'
WHERE reference_digest = :digest
"""


def _store(
    session_factory: async_sessionmaker[AsyncSession],
    spool_root: Path,
) -> AttachmentSpoolStore:
    return AttachmentSpoolStore(
        session_factory=session_factory,
        key_provider=EnvAttachmentKeyProvider(
            {
                "ATTACHMENT_SPOOL_ACTIVE_KEY_ID": "ATTK1",
                "ATTACHMENT_SPOOL_KEY_ATTK1": _KEY_B64,
            }
        ),
        policy=AttachmentSpoolPolicy(spool_root, _TTL_SECONDS),
    )


def _runner(
    store: AttachmentSpoolStore,
    *,
    reconcile_limit: int = 100,
    purge_limit: int = 100,
    interval_seconds: int = 60,
    initial_delay_seconds: int = 0,
    _waiter=None,
) -> AttachmentMaintenanceRunner:
    return AttachmentMaintenanceRunner(
        store=store,
        config=AttachmentMaintenanceConfig(
            interval_seconds=interval_seconds,
            reconcile_limit=reconcile_limit,
            purge_limit=purge_limit,
            initial_delay_seconds=initial_delay_seconds,
        ),
        _waiter=_waiter,
    )


async def _row_by_digest(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reference_digest: bytes,
) -> AttachmentSpoolObject | None:
    async with session_factory() as session:
        return await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == reference_digest
            )
        )


async def _force_stale_writing_keep_final(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reference_digest: bytes,
) -> None:
    """STORED→aged WRITING with final ciphertext retained (promote candidate)."""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_AGE_WRITING_SQL),
                {"digest": reference_digest},
            )


async def _expire_object(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reference_digest: bytes,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_EXPIRE_OBJECT_BY_DIGEST_SQL),
                {"digest": reference_digest},
            )


async def _count_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    async with session_factory() as session:
        value = await session.scalar(
            select(func.count()).select_from(AttachmentSpoolObject)
        )
        return int(value or 0)


async def _count_state(
    session_factory: async_sessionmaker[AsyncSession],
    state: str,
) -> int:
    async with session_factory() as session:
        value = await session.scalar(
            select(func.count())
            .select_from(AttachmentSpoolObject)
            .where(AttachmentSpoolObject.state == state)
        )
        return int(value or 0)


@pytest_asyncio.fixture(autouse=True)
async def attachment_maintenance_row_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


@pytest.mark.asyncio
async def test_run_once_reconciles_stale_writing_then_purges_expired(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Exact reconcile contract: aged WRITING with valid final → promote to STORED.

    Same promote path as test_concurrent_reconcile_promotes_once.
    """
    spool = tmp_path / "spool"
    spool.mkdir()
    store = _store(session_factory, spool)

    writing_handle = await store.store(
        _JPEG,
        conversation_id=uuid4(),
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
    )
    writing_digest = writing_handle.reference.digest()
    await _force_stale_writing_keep_final(
        session_factory, reference_digest=writing_digest
    )
    writing_row = await _row_by_digest(
        session_factory, reference_digest=writing_digest
    )
    assert writing_row is not None
    assert writing_row.state == "WRITING"
    writing_final = spool / attachment_fs.final_relpath(writing_row.object_id)
    assert writing_final.is_file()

    purge_handle = await store.store(
        _JPEG,
        conversation_id=uuid4(),
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
    )
    purge_digest = purge_handle.reference.digest()
    await _expire_object(session_factory, reference_digest=purge_digest)
    purge_row = await _row_by_digest(session_factory, reference_digest=purge_digest)
    assert purge_row is not None
    purge_final = spool / attachment_fs.final_relpath(purge_row.object_id)
    assert purge_final.is_file()

    result = await _runner(store).run_once()

    assert result.status is AttachmentMaintenanceCycleStatus.SUCCESS
    assert result.reconcile is not None
    assert result.purge is not None
    assert result.reconcile.promoted_to_stored == 1
    assert result.purge.transitioned_stored == 1
    assert result.purge.deleted == 1

    after_writing = await _row_by_digest(
        session_factory, reference_digest=writing_digest
    )
    assert after_writing is not None
    assert after_writing.state == "STORED"
    assert writing_final.is_file()

    assert await _row_by_digest(session_factory, reference_digest=purge_digest) is None
    assert not purge_final.exists()


@pytest.mark.asyncio
async def test_run_once_preserves_unexpired_object_and_active_lease(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store = _store(session_factory, spool)

    fresh_handle = await store.store(
        _JPEG,
        conversation_id=uuid4(),
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
    )
    fresh_digest = fresh_handle.reference.digest()

    leased_handle = await store.store(
        _JPEG,
        conversation_id=uuid4(),
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
    )
    lease = await store.acquire(leased_handle.reference)
    leased_digest = leased_handle.reference.digest()
    await _expire_object(session_factory, reference_digest=leased_digest)

    before_leased = await _row_by_digest(
        session_factory, reference_digest=leased_digest
    )
    assert before_leased is not None
    assert before_leased.state == "LEASED"
    lease_digest = before_leased.lease_token_digest
    assert lease_digest is not None
    fresh_row = await _row_by_digest(session_factory, reference_digest=fresh_digest)
    assert fresh_row is not None
    fresh_final = spool / attachment_fs.final_relpath(fresh_row.object_id)
    leased_final = spool / attachment_fs.final_relpath(before_leased.object_id)
    assert fresh_final.is_file()
    assert leased_final.is_file()

    result = await _runner(store).run_once()
    assert result.status is AttachmentMaintenanceCycleStatus.SUCCESS
    assert result.reconcile is not None
    assert result.purge is not None
    assert result.purge.transitioned_stored == 0
    assert result.purge.transitioned_leased == 0
    assert result.purge.deleted == 0

    after_fresh = await _row_by_digest(session_factory, reference_digest=fresh_digest)
    after_leased = await _row_by_digest(
        session_factory, reference_digest=leased_digest
    )
    assert after_fresh is not None
    assert after_fresh.state == "STORED"
    assert after_leased is not None
    assert after_leased.state == "LEASED"
    assert after_leased.lease_token_digest == lease_digest
    assert fresh_final.is_file()
    assert leased_final.is_file()
    await store.release(lease.token)


@pytest.mark.asyncio
async def test_run_once_respects_reconcile_and_purge_batch_limits(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store = _store(session_factory, spool)

    writing_digests: list[bytes] = []
    for _ in range(2):
        handle = await store.store(
            _JPEG,
            conversation_id=uuid4(),
            kind=AttachmentKind.IMAGE,
            purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        )
        digest = handle.reference.digest()
        await _force_stale_writing_keep_final(
            session_factory, reference_digest=digest
        )
        writing_digests.append(digest)

    purge_digests: list[bytes] = []
    for _ in range(2):
        handle = await store.store(
            _JPEG,
            conversation_id=uuid4(),
            kind=AttachmentKind.IMAGE,
            purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        )
        digest = handle.reference.digest()
        await _expire_object(session_factory, reference_digest=digest)
        purge_digests.append(digest)

    assert await _count_state(session_factory, "WRITING") == 2
    assert await _count_state(session_factory, "STORED") == 2

    result = await _runner(store, reconcile_limit=1, purge_limit=1).run_once()
    assert result.status is AttachmentMaintenanceCycleStatus.SUCCESS
    assert result.reconcile is not None
    assert result.purge is not None
    assert result.reconcile.promoted_to_stored == 1
    assert result.purge.transitioned_stored == 1
    assert result.purge.deleted == 1

    # 1 WRITING left + 1 promoted STORED + 1 remaining expired STORED = 3 rows.
    assert await _count_state(session_factory, "WRITING") == 1
    assert await _count_state(session_factory, "STORED") == 2
    assert await _count_rows(session_factory) == 3

    remaining_writing = 0
    for digest in writing_digests:
        row = await _row_by_digest(session_factory, reference_digest=digest)
        if row is not None and row.state == "WRITING":
            remaining_writing += 1
    assert remaining_writing == 1

    remaining_expired = 0
    for digest in purge_digests:
        if await _row_by_digest(session_factory, reference_digest=digest) is not None:
            remaining_expired += 1
    assert remaining_expired == 1


@pytest.mark.asyncio
async def test_run_once_second_cycle_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store = _store(session_factory, spool)
    handle = await store.store(
        _JPEG,
        conversation_id=uuid4(),
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
    )
    digest = handle.reference.digest()
    await _expire_object(session_factory, reference_digest=digest)
    row = await _row_by_digest(session_factory, reference_digest=digest)
    assert row is not None
    final = spool / attachment_fs.final_relpath(row.object_id)
    assert final.is_file()

    runner = _runner(store)
    first = await runner.run_once()
    assert first.status is AttachmentMaintenanceCycleStatus.SUCCESS
    assert first.purge is not None
    assert first.purge.deleted == 1
    assert await _row_by_digest(session_factory, reference_digest=digest) is None
    assert not final.exists()

    second = await runner.run_once()
    assert second.status is AttachmentMaintenanceCycleStatus.SUCCESS
    assert second.reconcile is not None
    assert second.purge is not None
    assert second.reconcile.promoted_to_stored == 0
    assert second.reconcile.deleted_writing_rows == 0
    assert second.reconcile.deleted_orphan_temps == 0
    assert second.reconcile.deleted_orphan_finals == 0
    assert second.reconcile.deleted_unrecoverable_stored == 0
    assert second.reconcile.deleted_delete_pending == 0
    assert second.purge.transitioned_stored == 0
    assert second.purge.transitioned_leased == 0
    assert second.purge.deleted == 0
    assert await _count_rows(session_factory) == 0
    assert not final.exists()


@pytest.mark.asyncio
async def test_run_forever_executes_real_cycle_then_stops(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store = _store(session_factory, spool)
    handle = await store.store(
        _JPEG,
        conversation_id=uuid4(),
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
    )
    digest = handle.reference.digest()
    await _expire_object(session_factory, reference_digest=digest)
    row = await _row_by_digest(session_factory, reference_digest=digest)
    assert row is not None
    final = spool / attachment_fs.final_relpath(row.object_id)
    assert final.is_file()

    cycles = {"n": 0}

    async def _stop_after_cycle(
        *,
        stop_event: asyncio.Event,
        delay_seconds: int,
    ) -> bool:
        assert delay_seconds >= 1
        stop_event.set()
        return True

    runner = _runner(store, interval_seconds=1, _waiter=_stop_after_cycle)
    original = runner.run_once

    async def _counting_run_once():
        cycles["n"] += 1
        return await original()

    runner.run_once = _counting_run_once  # type: ignore[method-assign]

    stop_event = asyncio.Event()
    await asyncio.wait_for(
        runner.run_forever(stop_event=stop_event),
        timeout=_RUN_FOREVER_GUARD_SECONDS,
    )

    assert cycles["n"] == 1
    assert await _row_by_digest(session_factory, reference_digest=digest) is None
    assert not final.exists()
    assert runner.status.loop_running is False


@pytest.mark.asyncio
async def test_maintenance_composition_constructs_with_real_postgres(
    pg_database_url: SecretDatabaseUrl,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Entrypoint-like graph using safe pg_database_url (not process DATABASE_URL)."""
    spool = tmp_path / "spool"
    spool.mkdir()

    seed_store = _store(session_factory, spool)
    handle = await seed_store.store(
        _JPEG,
        conversation_id=uuid4(),
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
    )
    digest = handle.reference.digest()
    await _expire_object(session_factory, reference_digest=digest)
    row = await _row_by_digest(session_factory, reference_digest=digest)
    assert row is not None
    final = spool / attachment_fs.final_relpath(row.object_id)
    assert final.is_file()

    settings = Settings(database_url=pg_database_url.reveal())
    engine = create_engine(settings)
    try:
        composed_factory = create_session_factory(engine)
        store = _store(composed_factory, spool)
        result = await _runner(store).run_once()
    finally:
        await engine.dispose()

    assert result.status is AttachmentMaintenanceCycleStatus.SUCCESS
    assert result.purge is not None
    assert result.purge.deleted == 1
    assert await _row_by_digest(session_factory, reference_digest=digest) is None
    assert not final.exists()
