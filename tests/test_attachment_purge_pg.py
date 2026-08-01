"""PostgreSQL integration tests for attachment spool expiry purge Stage 1A2B3.

These tests require PostgreSQL fixtures; do not run without an isolated PG harness.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import attachment_fs
from app.core.attachment_keys import EnvAttachmentKeyProvider
from app.core.attachment_types import (
    AttachmentError,
    AttachmentKind,
    AttachmentPlaintext,
    AttachmentPurpose,
    AttachmentSpoolPolicy,
)
from app.models.attachment_spool import AttachmentSpoolObject
from app.repositories import attachment_spool as spool_repo
from app.services.attachment_spool_store import AttachmentSpoolStore
from tests.attachment_spool_fakes import synthetic_minimal_jpeg
from tests.pg_harness import truncate_foundation_tables

_JPEG = synthetic_minimal_jpeg()
_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
_TTL_SECONDS = 900
_DEADLOCK_GUARD_SECONDS = 10.0

_EXPIRE_OBJECT_BY_DIGEST_SQL = """
UPDATE attachment_spool_objects
SET
    created_at = statement_timestamp() - interval '902 seconds',
    updated_at = statement_timestamp() - interval '902 seconds',
    expires_at = statement_timestamp() - interval '1 second'
WHERE reference_digest = :digest
"""

_EXPIRE_LEASE_BY_DIGEST_SQL = """
UPDATE attachment_spool_objects
SET
    leased_at = statement_timestamp() - interval '301 seconds',
    lease_expires_at = statement_timestamp() - interval '1 second'
WHERE reference_digest = :digest
"""

_FORCE_DELETE_PENDING_CLEAR_LEASE_SQL = """
UPDATE attachment_spool_objects
SET
    state = 'DELETE_PENDING',
    updated_at = statement_timestamp(),
    lease_token_digest = NULL,
    leased_at = NULL,
    lease_expires_at = NULL
WHERE reference_digest = :digest
"""


class _FinalizeGate:
    """Block only the first shared-finalizer call (ack/purge crash-window tests)."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.resume = asyncio.Event()
        self.call_count = 0
        self._original = AttachmentSpoolStore._finalize_delete_pending

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reached = self.reached
        resume = self.resume
        original = self._original
        gate = self

        async def _gated(
            self: AttachmentSpoolStore, snapshot: Any
        ) -> Any:
            gate.call_count += 1
            if gate.call_count == 1:
                reached.set()
                await resume.wait()
            return await original(self, snapshot)

        monkeypatch.setattr(
            AttachmentSpoolStore,
            "_finalize_delete_pending",
            _gated,
        )

    def unblock(self) -> None:
        self.resume.set()


class _PhaseCGate:
    """Pause read Phase C at select_for_update_by_id (after Phase A commit)."""

    def __init__(self) -> None:
        self.phase_c_reached = asyncio.Event()
        self.mutation_committed = asyncio.Event()
        self._original: Any = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._original = spool_repo.select_for_update_by_id
        phase_c_reached = self.phase_c_reached
        mutation_committed = self.mutation_committed
        original = self._original

        async def _gated_select_for_update_by_id(*args: Any, **kwargs: Any) -> Any:
            phase_c_reached.set()
            await mutation_committed.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(
            "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
            _gated_select_for_update_by_id,
        )

    def resume(self) -> None:
        self.mutation_committed.set()


@dataclass(frozen=True, slots=True)
class _ReadTaskOutcome:
    plaintext: AttachmentPlaintext | None
    error: AttachmentError | None


class _SelectHoldGate:
    """Hold first purge selector result while locks remain (SKIP LOCKED proof)."""

    def __init__(self) -> None:
        self.first_locked = asyncio.Event()
        self.release_first = asyncio.Event()
        self.call_count = 0
        self._original = spool_repo.select_expired_for_purge

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gate = self
        original = self._original

        async def _gated(session: Any, *, limit: int) -> Any:
            gate.call_count += 1
            rows = await original(session, limit=limit)
            if gate.call_count == 1:
                gate.first_locked.set()
                await gate.release_first.wait()
            return rows

        monkeypatch.setattr(
            "app.services.attachment_spool_store.spool_repo.select_expired_for_purge",
            _gated,
        )

    def release(self) -> None:
        self.release_first.set()


async def _capture_read(
    store: AttachmentSpoolStore,
    token: Any,
) -> _ReadTaskOutcome:
    try:
        result = await store.read(token)
        return _ReadTaskOutcome(plaintext=result, error=None)
    except AttachmentError as exc:
        return _ReadTaskOutcome(plaintext=None, error=exc)


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


async def _expire_lease(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reference_digest: bytes,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_EXPIRE_LEASE_BY_DIGEST_SQL),
                {"digest": reference_digest},
            )


@pytest_asyncio.fixture(autouse=True)
async def attachment_purge_row_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


@pytest.mark.asyncio
async def test_purge_expired_stored_removes_file_and_row(
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
    await _expire_object(
        session_factory, reference_digest=handle.reference.digest()
    )
    before = await _row_by_digest(
        session_factory, reference_digest=handle.reference.digest()
    )
    assert before is not None
    final = spool / attachment_fs.final_relpath(before.object_id)
    assert final.is_file()
    result = await store.purge_expired(limit=10)
    assert result.transitioned_stored == 1
    assert result.transitioned_leased == 0
    assert result.deleted == 1
    assert not final.exists()
    after = await _row_by_digest(
        session_factory, reference_digest=handle.reference.digest()
    )
    assert after is None


@pytest.mark.asyncio
async def test_purge_unexpired_stored_untouched(
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
    result = await store.purge_expired(limit=10)
    assert result.transitioned_stored == 0
    assert result.deleted == 0
    row = await _row_by_digest(
        session_factory, reference_digest=handle.reference.digest()
    )
    assert row is not None
    assert row.state == "STORED"


@pytest.mark.asyncio
async def test_purge_dual_expired_leased_clears_lease_and_deletes(
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
    lease = await store.acquire(handle.reference)
    await _expire_object(
        session_factory, reference_digest=handle.reference.digest()
    )
    await _expire_lease(
        session_factory, reference_digest=handle.reference.digest()
    )
    result = await store.purge_expired(limit=10)
    assert result.transitioned_leased == 1
    assert result.deleted == 1
    with pytest.raises(AttachmentError) as raised:
        await store.acknowledge(lease.token)
    assert raised.value.code == "ATTACHMENT_ACCESS_DENIED"
    after = await _row_by_digest(
        session_factory, reference_digest=handle.reference.digest()
    )
    assert after is None


@pytest.mark.asyncio
async def test_purge_expired_object_active_lease_untouched(
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
    lease = await store.acquire(handle.reference)
    await _expire_object(
        session_factory, reference_digest=handle.reference.digest()
    )
    result = await store.purge_expired(limit=10)
    assert result.transitioned_leased == 0
    assert result.deleted == 0
    row = await _row_by_digest(
        session_factory, reference_digest=handle.reference.digest()
    )
    assert row is not None
    assert row.state == "LEASED"
    assert row.lease_token_digest is not None
    await store.release(lease.token)


@pytest.mark.asyncio
async def test_purge_active_object_expired_lease_untouched(
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
    await store.acquire(handle.reference)
    await _expire_lease(
        session_factory, reference_digest=handle.reference.digest()
    )
    result = await store.purge_expired(limit=10)
    assert result.transitioned_leased == 0
    assert result.deleted == 0
    row = await _row_by_digest(
        session_factory, reference_digest=handle.reference.digest()
    )
    assert row is not None
    assert row.state == "LEASED"
    reclaim = await store.reclaim_expired_leases(limit=10)
    assert reclaim.reclaimed == 1


@pytest.mark.asyncio
async def test_purge_does_not_select_writing_or_delete_pending(
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
    await _expire_object(
        session_factory, reference_digest=handle.reference.digest()
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_FORCE_DELETE_PENDING_CLEAR_LEASE_SQL),
                {"digest": handle.reference.digest()},
            )
    result = await store.purge_expired(limit=10)
    assert result.transitioned_stored == 0
    assert result.transitioned_leased == 0
    assert result.deleted == 0
    row = await _row_by_digest(
        session_factory, reference_digest=handle.reference.digest()
    )
    assert row is not None
    assert row.state == "DELETE_PENDING"


@pytest.mark.asyncio
async def test_purge_lease_cleared_before_finalize_window(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    lease = await store.acquire(handle.reference)
    await _expire_object(
        session_factory, reference_digest=handle.reference.digest()
    )
    await _expire_lease(
        session_factory, reference_digest=handle.reference.digest()
    )
    gate = _FinalizeGate()
    gate.install(monkeypatch)
    task = asyncio.create_task(store.purge_expired(limit=10))
    try:
        await asyncio.wait_for(gate.reached.wait(), timeout=_DEADLOCK_GUARD_SECONDS)
        pending = await _row_by_digest(
            session_factory, reference_digest=handle.reference.digest()
        )
        assert pending is not None
        assert pending.state == "DELETE_PENDING"
        assert pending.lease_token_digest is None
        assert pending.leased_at is None
        assert pending.lease_expires_at is None
        with pytest.raises(AttachmentError) as raised:
            await store.acknowledge(lease.token)
        assert raised.value.code == "ATTACHMENT_ACCESS_DENIED"
        gate.unblock()
        result = await asyncio.wait_for(task, timeout=_DEADLOCK_GUARD_SECONDS)
        assert result.deleted == 1
    finally:
        gate.unblock()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_purge_origin_null_lease_dp_reconcile_completes(
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
    row = await _row_by_digest(
        session_factory, reference_digest=handle.reference.digest()
    )
    assert row is not None
    final = spool / attachment_fs.final_relpath(row.object_id)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_FORCE_DELETE_PENDING_CLEAR_LEASE_SQL),
                {"digest": handle.reference.digest()},
            )
    pending = await _row_by_digest(
        session_factory, reference_digest=handle.reference.digest()
    )
    assert pending is not None
    assert pending.state == "DELETE_PENDING"
    assert pending.lease_token_digest is None
    result = await store.reconcile(limit=10)
    assert result.deleted_delete_pending == 1
    assert not final.exists()
    after = await _row_by_digest(
        session_factory, reference_digest=handle.reference.digest()
    )
    assert after is None


@pytest.mark.asyncio
async def test_purge_crash_window_reconcile_completes(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    await _expire_object(
        session_factory, reference_digest=handle.reference.digest()
    )
    before = await _row_by_digest(
        session_factory, reference_digest=handle.reference.digest()
    )
    assert before is not None
    final = spool / attachment_fs.final_relpath(before.object_id)
    gate = _FinalizeGate()
    gate.install(monkeypatch)
    task = asyncio.create_task(store.purge_expired(limit=10))
    try:
        await asyncio.wait_for(gate.reached.wait(), timeout=_DEADLOCK_GUARD_SECONDS)
        assert final.is_file()
        pending = await _row_by_digest(
            session_factory, reference_digest=handle.reference.digest()
        )
        assert pending is not None
        assert pending.state == "DELETE_PENDING"
        reconcile = await store.reconcile(limit=10)
        assert reconcile.deleted_delete_pending == 1
        assert not final.exists()
        assert gate.call_count == 2
        gate.unblock()
        result = await asyncio.wait_for(task, timeout=_DEADLOCK_GUARD_SECONDS)
        assert result.transitioned_stored == 1
        # purge's gated call #1 resumes into already_gone after reconcile deleted
        assert result.deleted == 0
        assert result.skipped == 1
        after = await _row_by_digest(
            session_factory, reference_digest=handle.reference.digest()
        )
        assert after is None
    finally:
        gate.unblock()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_purge_phase_c_store_failed_retains_dp(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    await _expire_object(
        session_factory, reference_digest=handle.reference.digest()
    )
    before = await _row_by_digest(
        session_factory, reference_digest=handle.reference.digest()
    )
    assert before is not None
    final = spool / attachment_fs.final_relpath(before.object_id)
    original = AttachmentSpoolStore._finalize_delete_pending

    async def _fail_phase_c(
        self: AttachmentSpoolStore, snapshot: Any
    ) -> str:
        attachment_fs.unlink_final(self._policy.spool_root, snapshot.object_id)
        return "store_failed"

    monkeypatch.setattr(
        AttachmentSpoolStore,
        "_finalize_delete_pending",
        _fail_phase_c,
    )
    with pytest.raises(AttachmentError) as raised:
        await store.purge_expired(limit=10)
    assert raised.value.code == "ATTACHMENT_RECONCILE_FAILED"
    assert str(raised.value) == "ATTACHMENT_RECONCILE_FAILED"
    assert raised.value.__cause__ is None
    assert not final.exists()
    pending = await _row_by_digest(
        session_factory, reference_digest=handle.reference.digest()
    )
    assert pending is not None
    assert pending.state == "DELETE_PENDING"
    monkeypatch.setattr(
        AttachmentSpoolStore,
        "_finalize_delete_pending",
        original,
    )
    reconcile = await store.reconcile(limit=10)
    assert reconcile.deleted_delete_pending == 1
    after = await _row_by_digest(
        session_factory, reference_digest=handle.reference.digest()
    )
    assert after is None


@pytest.mark.asyncio
async def test_purge_batch_limit_is_global(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store = _store(session_factory, spool)
    digests: list[bytes] = []
    for _ in range(3):
        handle = await store.store(
            _JPEG,
            conversation_id=uuid4(),
            kind=AttachmentKind.IMAGE,
            purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        )
        digests.append(handle.reference.digest())
        await _expire_object(session_factory, reference_digest=digests[-1])
    result = await store.purge_expired(limit=2)
    assert result.transitioned_stored == 2
    assert result.deleted == 2
    remaining = 0
    for digest in digests:
        row = await _row_by_digest(session_factory, reference_digest=digest)
        if row is not None:
            remaining += 1
    assert remaining == 1


@pytest.mark.asyncio
async def test_purge_vs_acquire_expired_stored(
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
    await _expire_object(
        session_factory, reference_digest=handle.reference.digest()
    )
    with pytest.raises(AttachmentError) as raised:
        await store.acquire(handle.reference)
    assert raised.value.code == "ATTACHMENT_ACCESS_DENIED"
    result = await store.purge_expired(limit=10)
    assert result.deleted == 1


@pytest.mark.asyncio
async def test_purge_file_already_missing(
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
    await _expire_object(
        session_factory, reference_digest=handle.reference.digest()
    )
    before = await _row_by_digest(
        session_factory, reference_digest=handle.reference.digest()
    )
    assert before is not None
    final = spool / attachment_fs.final_relpath(before.object_id)
    final.unlink()
    result = await store.purge_expired(limit=10)
    assert result.transitioned_stored == 1
    assert result.deleted == 1
    after = await _row_by_digest(
        session_factory, reference_digest=handle.reference.digest()
    )
    assert after is None


@pytest.mark.asyncio
async def test_read_phase_a_then_purge_phase_c_denies(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase A succeeds on active lease; purge to DP; Phase C denies."""
    spool = tmp_path / "spool"
    spool.mkdir()
    store = _store(session_factory, spool)
    handle = await store.store(
        _JPEG,
        conversation_id=uuid4(),
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
    )
    lease = await store.acquire(handle.reference)
    phase_c = _PhaseCGate()
    phase_c.install(monkeypatch)
    finalize = _FinalizeGate()
    finalize.install(monkeypatch)
    read_task = asyncio.create_task(_capture_read(store, lease.token))
    purge_task: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(
            phase_c.phase_c_reached.wait(),
            timeout=_DEADLOCK_GUARD_SECONDS,
        )
        assert not read_task.done()
        mid = await _row_by_digest(
            session_factory, reference_digest=handle.reference.digest()
        )
        assert mid is not None
        assert mid.state == "LEASED"
        assert mid.lease_token_digest is not None
        await _expire_object(
            session_factory, reference_digest=handle.reference.digest()
        )
        await _expire_lease(
            session_factory, reference_digest=handle.reference.digest()
        )
        purge_task = asyncio.create_task(store.purge_expired(limit=10))
        await asyncio.wait_for(
            finalize.reached.wait(),
            timeout=_DEADLOCK_GUARD_SECONDS,
        )
        pending = await _row_by_digest(
            session_factory, reference_digest=handle.reference.digest()
        )
        assert pending is not None
        assert pending.state == "DELETE_PENDING"
        assert pending.lease_token_digest is None
        assert pending.leased_at is None
        assert pending.lease_expires_at is None
        phase_c.resume()
        outcome = await asyncio.wait_for(
            read_task, timeout=_DEADLOCK_GUARD_SECONDS
        )
        assert outcome.plaintext is None
        assert outcome.error is not None
        assert outcome.error.code == "ATTACHMENT_ACCESS_DENIED"
        assert str(outcome.error) == "ATTACHMENT_ACCESS_DENIED"
        finalize.unblock()
        purge_result = await asyncio.wait_for(
            purge_task, timeout=_DEADLOCK_GUARD_SECONDS
        )
        assert purge_result.transitioned_leased == 1
        assert purge_result.deleted == 1
        after = await _row_by_digest(
            session_factory, reference_digest=handle.reference.digest()
        )
        assert after is None
    finally:
        phase_c.resume()
        finalize.unblock()
        if not read_task.done():
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)
        if purge_task is not None and not purge_task.done():
            purge_task.cancel()
            await asyncio.gather(purge_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_dual_concurrent_purge_skip_locked(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store = _store(session_factory, spool)
    digests: list[bytes] = []
    object_ids: list[UUID] = []
    for i in range(4):
        handle = await store.store(
            _JPEG,
            conversation_id=uuid4(),
            kind=AttachmentKind.IMAGE,
            purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        )
        digests.append(handle.reference.digest())
        row = await _row_by_digest(
            session_factory, reference_digest=digests[-1]
        )
        assert row is not None
        object_ids.append(row.object_id)
        if i % 2 == 1:
            await store.acquire(handle.reference)
            await _expire_object(
                session_factory, reference_digest=digests[-1]
            )
            await _expire_lease(
                session_factory, reference_digest=digests[-1]
            )
        else:
            await _expire_object(
                session_factory, reference_digest=digests[-1]
            )
    # Control: unexpired STORED must remain.
    control = await store.store(
        _JPEG,
        conversation_id=uuid4(),
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
    )
    hold = _SelectHoldGate()
    hold.install(monkeypatch)
    task_a = asyncio.create_task(store.purge_expired(limit=10))
    task_b: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(
            hold.first_locked.wait(),
            timeout=_DEADLOCK_GUARD_SECONDS,
        )
        task_b = asyncio.create_task(store.purge_expired(limit=10))
        result_b = await asyncio.wait_for(
            task_b, timeout=_DEADLOCK_GUARD_SECONDS
        )
        hold.release()
        result_a = await asyncio.wait_for(
            task_a, timeout=_DEADLOCK_GUARD_SECONDS
        )
    finally:
        hold.release()
        for task in (task_a, task_b):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    total_transitioned = (
        result_a.transitioned_stored
        + result_a.transitioned_leased
        + result_b.transitioned_stored
        + result_b.transitioned_leased
    )
    total_deleted = result_a.deleted + result_b.deleted
    assert total_transitioned == 4
    assert total_deleted == 4
    assert hold.call_count >= 2
    for digest in digests:
        assert (
            await _row_by_digest(session_factory, reference_digest=digest)
        ) is None
    for object_id in object_ids:
        assert not (spool / attachment_fs.final_relpath(object_id)).exists()
    control_row = await _row_by_digest(
        session_factory, reference_digest=control.reference.digest()
    )
    assert control_row is not None
    assert control_row.state == "STORED"


@pytest.mark.asyncio
async def test_purge_partial_progress_store_failed_then_reconcile(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store = _store(session_factory, spool)
    digests: list[bytes] = []
    object_ids: list[UUID] = []
    for _ in range(3):
        handle = await store.store(
            _JPEG,
            conversation_id=uuid4(),
            kind=AttachmentKind.IMAGE,
            purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        )
        digests.append(handle.reference.digest())
        row = await _row_by_digest(
            session_factory, reference_digest=digests[-1]
        )
        assert row is not None
        object_ids.append(row.object_id)
        await _expire_object(
            session_factory, reference_digest=digests[-1]
        )

    finalize_object_ids: list[UUID] = []
    allow_delete = True
    original_finalize = AttachmentSpoolStore._finalize_delete_pending
    original_delete = spool_repo.delete_by_id

    async def _delete_gate(session: Any, *, row_id: UUID) -> None:
        if not allow_delete:
            raise RuntimeError("synthetic phase-c failure")
        await original_delete(session, row_id=row_id)

    async def _finalize_wrap(
        self: AttachmentSpoolStore, snapshot: Any
    ) -> Any:
        finalize_object_ids.append(snapshot.object_id)
        if len(finalize_object_ids) == 1:
            return await original_finalize(self, snapshot)
        if len(finalize_object_ids) == 2:
            nonlocal allow_delete
            allow_delete = False
            return await original_finalize(self, snapshot)
        raise AssertionError("third finalizer call must not run")

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id",
        _delete_gate,
    )
    monkeypatch.setattr(
        AttachmentSpoolStore,
        "_finalize_delete_pending",
        _finalize_wrap,
    )

    with pytest.raises(AttachmentError) as raised:
        await store.purge_expired(limit=10)
    assert raised.value.code == "ATTACHMENT_RECONCILE_FAILED"
    assert str(raised.value) == "ATTACHMENT_RECONCILE_FAILED"
    assert raised.value.__cause__ is None
    assert len(finalize_object_ids) == 2

    first_oid, second_oid = finalize_object_ids[0], finalize_object_ids[1]
    third_oid = next(
        oid for oid in object_ids if oid not in (first_oid, second_oid)
    )
    id_to_digest = {
        oid: digest for digest, oid in zip(digests, object_ids, strict=True)
    }

    assert (
        await _row_by_digest(
            session_factory, reference_digest=id_to_digest[first_oid]
        )
    ) is None
    assert not (spool / attachment_fs.final_relpath(first_oid)).exists()

    second_row = await _row_by_digest(
        session_factory, reference_digest=id_to_digest[second_oid]
    )
    assert second_row is not None
    assert second_row.state == "DELETE_PENDING"
    assert second_row.lease_token_digest is None
    assert not (spool / attachment_fs.final_relpath(second_oid)).exists()

    third_row = await _row_by_digest(
        session_factory, reference_digest=id_to_digest[third_oid]
    )
    assert third_row is not None
    assert third_row.state == "DELETE_PENDING"
    assert (spool / attachment_fs.final_relpath(third_oid)).is_file()

    # Restore real delete for reconcile recovery.
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id",
        original_delete,
    )
    monkeypatch.setattr(
        AttachmentSpoolStore,
        "_finalize_delete_pending",
        original_finalize,
    )
    reconcile = await store.reconcile(limit=10)
    assert reconcile.deleted_delete_pending == 2
    for digest in digests:
        assert (
            await _row_by_digest(session_factory, reference_digest=digest)
        ) is None
    for oid in object_ids:
        assert not (spool / attachment_fs.final_relpath(oid)).exists()
