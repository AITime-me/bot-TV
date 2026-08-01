"""PostgreSQL integration tests for attachment spool acknowledge Stage 1A2B2."""

from __future__ import annotations

import asyncio
import base64
import secrets
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core import attachment_fs
from app.core.attachment_keys import EnvAttachmentKeyProvider
from app.core.attachment_types import (
    AttachmentError,
    AttachmentKind,
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

_EXPIRE_LEASE_BY_DIGEST_SQL = """
UPDATE attachment_spool_objects
SET
    leased_at = statement_timestamp() - interval '301 seconds',
    lease_expires_at = statement_timestamp() - interval '1 second'
WHERE reference_digest = :digest
"""

_FORCE_DELETE_PENDING_SQL = """
UPDATE attachment_spool_objects
SET state = 'DELETE_PENDING', updated_at = statement_timestamp()
WHERE reference_digest = :digest
"""

_EXPIRE_LEASE_IN_LOCKED_SESSION_SQL = """
UPDATE attachment_spool_objects
SET
    leased_at = statement_timestamp() - interval '2 seconds',
    lease_expires_at = statement_timestamp() - interval '1 second'
WHERE id = :row_id
"""

_PROBE_LEASE_IN_LOCKED_SESSION_SQL = """
SELECT
    leased_at,
    lease_expires_at,
    statement_timestamp() AS pg_now
FROM attachment_spool_objects
WHERE id = :row_id
"""


@dataclass(frozen=True, slots=True)
class _ExpiryTransitionProbe:
    observed_leased_at: datetime
    observed_lease_expires_at: datetime
    observed_pg_now: datetime
    production_transition_result: Any


@dataclass(frozen=True, slots=True)
class _AckTaskOutcome:
    error: AttachmentError | None

    @property
    def access_denied(self) -> bool:
        return self.error is not None and self.error.code == "ATTACHMENT_ACCESS_DENIED"


class _SqlExpiryTransitionGate:
    """Expire lease inside the locked Phase A session, then run real transition."""

    def __init__(self) -> None:
        self.transition_reached = asyncio.Event()
        self.continue_transition = asyncio.Event()
        self.production_calls: list[tuple[UUID, bytes]] = []
        self.last_probe: _ExpiryTransitionProbe | None = None
        self._original = spool_repo.transition_leased_to_delete_pending

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reached = self.transition_reached
        continue_ev = self.continue_transition
        calls = self.production_calls
        original = self._original
        gate = self

        async def _wrapper(
            session: AsyncSession,
            *,
            row_id: UUID,
            lease_token_digest: bytes,
        ) -> Any:
            reached.set()
            await asyncio.wait_for(
                continue_ev.wait(),
                timeout=_DEADLOCK_GUARD_SECONDS,
            )
            await session.execute(
                text(_EXPIRE_LEASE_IN_LOCKED_SESSION_SQL),
                {"row_id": row_id},
            )
            probe_row = (
                await session.execute(
                    text(_PROBE_LEASE_IN_LOCKED_SESSION_SQL),
                    {"row_id": row_id},
                )
            ).one()
            observed_leased_at = probe_row.leased_at
            observed_lease_expires_at = probe_row.lease_expires_at
            observed_pg_now = probe_row.pg_now
            assert observed_leased_at is not None
            assert observed_lease_expires_at is not None
            assert observed_pg_now is not None
            calls.append((row_id, lease_token_digest))
            transition_result = await original(
                session,
                row_id=row_id,
                lease_token_digest=lease_token_digest,
            )
            gate.last_probe = _ExpiryTransitionProbe(
                observed_leased_at=observed_leased_at,
                observed_lease_expires_at=observed_lease_expires_at,
                observed_pg_now=observed_pg_now,
                production_transition_result=transition_result,
            )
            return transition_result

        monkeypatch.setattr(
            "app.services.attachment_spool_store.spool_repo.transition_leased_to_delete_pending",
            _wrapper,
        )

    def unblock(self) -> None:
        self.continue_transition.set()


class _FinalizeGate:
    """Pause acknowledge after Phase A commit, before shared finalizer."""

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


async def _stored_and_leased(
    session_factory: async_sessionmaker[AsyncSession],
    spool: Path,
) -> tuple[AttachmentSpoolStore, Any, Any]:
    store = _store(session_factory, spool)
    handle = await store.store(
        _JPEG,
        conversation_id=uuid4(),
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
    )
    lease = await store.acquire(handle.reference)
    return store, handle, lease


async def _row_by_reference_digest(
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


async def _expire_lease_by_reference_digest(
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


async def _capture_ack(
    store: AttachmentSpoolStore,
    token: Any,
) -> _AckTaskOutcome:
    try:
        await store.acknowledge(token)
        return _AckTaskOutcome(error=None)
    except AttachmentError as exc:
        return _AckTaskOutcome(error=exc)


@pytest_asyncio.fixture(autouse=True)
async def attachment_ack_row_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


@pytest.fixture
def sql_bind_probe(pg_engine: AsyncEngine) -> Iterator[list[object]]:
    captured: list[object] = []

    def _before_cursor_execute(
        _conn: Any,
        _cursor: Any,
        _statement: str,
        parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if parameters is not None:
            captured.append(parameters)

    sync_engine = pg_engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _before_cursor_execute)
    try:
        yield captured
    finally:
        event.remove(sync_engine, "before_cursor_execute", _before_cursor_execute)


@pytest.mark.asyncio
async def test_ack_active_leased_removes_file_and_row(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle, lease = await _stored_and_leased(session_factory, spool)
    before = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert before is not None
    final = spool / attachment_fs.final_relpath(before.object_id)
    assert final.is_file()
    await store.acknowledge(lease.token)
    assert not final.exists()
    after = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert after is None


@pytest.mark.asyncio
async def test_ack_stranded_delete_pending_retry_same_token(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle, lease = await _stored_and_leased(session_factory, spool)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_FORCE_DELETE_PENDING_SQL),
                {"digest": handle.reference.digest()},
            )
    row = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert row is not None
    assert row.state == "DELETE_PENDING"
    assert row.lease_token_digest is not None
    assert row.leased_at is not None
    assert row.lease_expires_at is not None
    await store.acknowledge(lease.token)
    after = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert after is None


@pytest.mark.asyncio
async def test_ack_after_full_delete_denied(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle, lease = await _stored_and_leased(session_factory, spool)
    await store.acknowledge(lease.token)
    with pytest.raises(AttachmentError) as raised:
        await store.acknowledge(lease.token)
    assert raised.value.code == "ATTACHMENT_ACCESS_DENIED"


@pytest.mark.asyncio
async def test_ack_transition_committed_reconcile_completes(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle, lease = await _stored_and_leased(session_factory, spool)
    before = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert before is not None
    final = spool / attachment_fs.final_relpath(before.object_id)
    gate = _FinalizeGate()
    gate.install(monkeypatch)
    task = asyncio.create_task(_capture_ack(store, lease.token))
    try:
        await asyncio.wait_for(gate.reached.wait(), timeout=_DEADLOCK_GUARD_SECONDS)
        pending = await _row_by_reference_digest(
            session_factory,
            reference_digest=handle.reference.digest(),
        )
        assert pending is not None
        assert pending.state == "DELETE_PENDING"
        assert pending.lease_token_digest == before.lease_token_digest
        assert pending.leased_at == before.leased_at
        assert pending.lease_expires_at == before.lease_expires_at
        assert final.is_file()
        assert not task.done()
        assert gate.call_count == 1
        result = await store.reconcile(limit=10)
        assert result.deleted_delete_pending == 1
        assert result.promoted_to_stored == 0
        assert result.deleted_writing_rows == 0
        assert result.deleted_orphan_temps == 0
        assert result.deleted_orphan_finals == 0
        assert result.deleted_unrecoverable_stored == 0
        assert result.unsafe_skipped == 0
        assert result.io_unavailable_skipped == 0
        assert not final.exists()
        row = await _row_by_reference_digest(
            session_factory,
            reference_digest=handle.reference.digest(),
        )
        assert row is None
        assert gate.call_count == 2
        gate.unblock()
        outcome = await asyncio.wait_for(task, timeout=_DEADLOCK_GUARD_SECONDS)
        assert outcome.error is None
        row = await _row_by_reference_digest(
            session_factory,
            reference_digest=handle.reference.digest(),
        )
        assert row is None
        assert not final.exists()
        assert gate.call_count == 2
    finally:
        gate.unblock()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_reconcile_delete_pending_missing_file_deletes_row(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle, lease = await _stored_and_leased(session_factory, spool)
    row = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert row is not None
    final = spool / attachment_fs.final_relpath(row.object_id)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_FORCE_DELETE_PENDING_SQL),
                {"digest": handle.reference.digest()},
            )
    final.unlink(missing_ok=True)
    result = await store.reconcile(limit=10)
    assert result.deleted_delete_pending == 1
    after = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert after is None


@pytest.mark.asyncio
async def test_reconcile_never_deletes_active_leased(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle, lease = await _stored_and_leased(session_factory, spool)
    result = await store.reconcile(limit=10)
    assert result.deleted_delete_pending == 0
    row = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert row is not None
    assert row.state == "LEASED"
    assert row.lease_token_digest is not None
    final = spool / attachment_fs.final_relpath(row.object_id)
    assert final.is_file()
    assert lease.token.to_token() not in repr(result)


@pytest.mark.asyncio
async def test_ack_concurrent_same_token(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle, lease = await _stored_and_leased(session_factory, spool)
    row = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert row is not None
    final = spool / attachment_fs.final_relpath(row.object_id)

    async def _run() -> _AckTaskOutcome:
        return await _capture_ack(store, lease.token)

    first, second = await asyncio.gather(_run(), _run())
    errors = [o.error for o in (first, second) if o.error is not None]
    if errors:
        assert len(errors) == 1
        assert errors[0] is not None
        assert errors[0].code == "ATTACHMENT_ACCESS_DENIED"
        assert str(errors[0]) == "ATTACHMENT_ACCESS_DENIED"
        assert errors[0].__cause__ is None
    row = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert row is None
    assert not final.exists()


@pytest.mark.asyncio
async def test_ack_vs_release_after_phase_a_commit(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle, lease = await _stored_and_leased(session_factory, spool)
    before = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert before is not None
    final = spool / attachment_fs.final_relpath(before.object_id)
    gate = _FinalizeGate()
    gate.install(monkeypatch)
    ack_task = asyncio.create_task(_capture_ack(store, lease.token))
    try:
        await asyncio.wait_for(gate.reached.wait(), timeout=_DEADLOCK_GUARD_SECONDS)
        pending = await _row_by_reference_digest(
            session_factory,
            reference_digest=handle.reference.digest(),
        )
        assert pending is not None
        assert pending.state == "DELETE_PENDING"
        with pytest.raises(AttachmentError) as raised:
            await store.release(lease.token)
        assert raised.value.code == "ATTACHMENT_ACCESS_DENIED"
        assert str(raised.value) == "ATTACHMENT_ACCESS_DENIED"
        still_pending = await _row_by_reference_digest(
            session_factory,
            reference_digest=handle.reference.digest(),
        )
        assert still_pending is not None
        assert still_pending.state == "DELETE_PENDING"
        gate.unblock()
        outcome = await asyncio.wait_for(ack_task, timeout=_DEADLOCK_GUARD_SECONDS)
        assert outcome.error is None
        assert not final.exists()
        after = await _row_by_reference_digest(
            session_factory,
            reference_digest=handle.reference.digest(),
        )
        assert after is None
    finally:
        gate.unblock()
        if not ack_task.done():
            ack_task.cancel()
            await asyncio.gather(ack_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_ack_vs_reclaim_after_phase_a_commit(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle, lease = await _stored_and_leased(session_factory, spool)
    before = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert before is not None
    final = spool / attachment_fs.final_relpath(before.object_id)
    gate = _FinalizeGate()
    gate.install(monkeypatch)
    ack_task = asyncio.create_task(_capture_ack(store, lease.token))
    try:
        await asyncio.wait_for(gate.reached.wait(), timeout=_DEADLOCK_GUARD_SECONDS)
        reclaim = await store.reclaim_expired_leases(limit=10)
        assert reclaim.reclaimed == 0
        assert reclaim.skipped == 0
        pending = await _row_by_reference_digest(
            session_factory,
            reference_digest=handle.reference.digest(),
        )
        assert pending is not None
        assert pending.state == "DELETE_PENDING"
        gate.unblock()
        outcome = await asyncio.wait_for(ack_task, timeout=_DEADLOCK_GUARD_SECONDS)
        assert outcome.error is None
        assert not final.exists()
        after = await _row_by_reference_digest(
            session_factory,
            reference_digest=handle.reference.digest(),
        )
        assert after is None
    finally:
        gate.unblock()
        if not ack_task.done():
            ack_task.cancel()
            await asyncio.gather(ack_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_ack_denied_after_reclaim_sequence(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle, lease = await _stored_and_leased(session_factory, spool)
    before = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert before is not None
    final = spool / attachment_fs.final_relpath(before.object_id)
    assert final.is_file()
    await _expire_lease_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    reclaim = await store.reclaim_expired_leases(limit=10)
    assert reclaim.reclaimed == 1
    stored = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert stored is not None
    assert stored.state == "STORED"
    assert stored.lease_token_digest is None
    assert stored.leased_at is None
    assert stored.lease_expires_at is None
    with pytest.raises(AttachmentError) as raised:
        await store.acknowledge(lease.token)
    assert raised.value.code == "ATTACHMENT_ACCESS_DENIED"
    assert str(raised.value) == "ATTACHMENT_ACCESS_DENIED"
    assert raised.value.__cause__ is None
    assert final.is_file()
    after = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert after is not None
    assert after.state == "STORED"
    assert after.lease_token_digest is None
    assert after.leased_at is None
    assert after.lease_expires_at is None


@pytest.mark.asyncio
async def test_ack_conditional_update_expired_lease_denied(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle, lease = await _stored_and_leased(session_factory, spool)
    before = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert before is not None
    final = spool / attachment_fs.final_relpath(before.object_id)
    unlink_calls: list[UUID] = []

    def _track_unlink(_root: Path, object_id: UUID) -> Any:
        unlink_calls.append(object_id)
        return attachment_fs.unlink_final(_root, object_id)

    gate = _SqlExpiryTransitionGate()
    gate.install(monkeypatch)
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.unlink_final",
        _track_unlink,
    )
    ack_task = asyncio.create_task(_capture_ack(store, lease.token))
    try:
        await asyncio.wait_for(
            gate.transition_reached.wait(),
            timeout=_DEADLOCK_GUARD_SECONDS,
        )
        gate.unblock()
        outcome = await asyncio.wait_for(ack_task, timeout=_DEADLOCK_GUARD_SECONDS)
        assert outcome.access_denied
        assert outcome.error is not None
        assert str(outcome.error) == "ATTACHMENT_ACCESS_DENIED"
        assert outcome.error.__cause__ is None
        assert len(gate.production_calls) == 1
        assert unlink_calls == []
        probe = gate.last_probe
        assert probe is not None
        assert probe.observed_leased_at < probe.observed_lease_expires_at
        assert probe.observed_lease_expires_at < probe.observed_pg_now
        assert probe.production_transition_result is None
        assert final.is_file()
        after = await _row_by_reference_digest(
            session_factory,
            reference_digest=handle.reference.digest(),
        )
        assert after is not None
        assert after.state == "LEASED"
        assert after.lease_token_digest == before.lease_token_digest
        assert after.leased_at == before.leased_at
        assert after.lease_expires_at == before.lease_expires_at
    finally:
        gate.unblock()
        if not ack_task.done():
            ack_task.cancel()
            await asyncio.gather(ack_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_ack_no_raw_token_in_sql_binds(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    sql_bind_probe: list[object],
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, _handle, lease = await _stored_and_leased(session_factory, spool)
    sql_bind_probe.clear()
    await store.acknowledge(lease.token)
    rendered = repr(sql_bind_probe)
    assert lease.token.to_token() not in rendered


@pytest.mark.asyncio
async def test_ack_lease_tuple_preserved_in_delete_pending(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle, lease = await _stored_and_leased(session_factory, spool)
    before = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert before is not None
    gate = _FinalizeGate()
    gate.install(monkeypatch)
    task = asyncio.create_task(_capture_ack(store, lease.token))
    try:
        await asyncio.wait_for(gate.reached.wait(), timeout=_DEADLOCK_GUARD_SECONDS)
        pending = await _row_by_reference_digest(
            session_factory,
            reference_digest=handle.reference.digest(),
        )
        assert pending is not None
        assert pending.state == "DELETE_PENDING"
        assert pending.lease_token_digest == before.lease_token_digest
        assert pending.leased_at == before.leased_at
        assert pending.lease_expires_at == before.lease_expires_at
        gate.unblock()
        await asyncio.wait_for(task, timeout=_DEADLOCK_GUARD_SECONDS)
    finally:
        gate.unblock()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
