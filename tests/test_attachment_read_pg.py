"""PostgreSQL integration tests for attachment spool read Stage 1A2B1."""

from __future__ import annotations

import asyncio
import base64
import secrets
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.attachment_keys import EnvAttachmentKeyProvider
from app.core.attachment_types import (
    AttachmentError,
    AttachmentKind,
    AttachmentMime,
    AttachmentPlaintext,
    AttachmentPurpose,
    AttachmentSpoolPolicy,
)
from app.models.attachment_spool import AttachmentSpoolObject
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

_IMMUTABLE_METADATA_ATTRS = (
    "object_id",
    "conversation_id",
    "kind",
    "purpose",
    "detected_mime",
    "plaintext_size",
    "ciphertext_size",
    "ciphertext_sha256",
    "nonce",
    "key_id",
    "crypto_version",
)


def _assert_immutable_metadata_equal(
    left: AttachmentSpoolObject,
    right: AttachmentSpoolObject,
) -> None:
    for attr in _IMMUTABLE_METADATA_ATTRS:
        assert getattr(left, attr) == getattr(right, attr)


@dataclass(frozen=True, slots=True)
class _ReadTaskOutcome:
    plaintext: AttachmentPlaintext | None
    error: AttachmentError | None

    @property
    def access_denied(self) -> bool:
        return self.error is not None and self.error.code == "ATTACHMENT_ACCESS_DENIED"


class _PhaseCGate:
    """Pause read Phase C at the async repository boundary."""

    def __init__(self) -> None:
        self.phase_c_reached = asyncio.Event()
        self.mutation_committed = asyncio.Event()
        self._original: Any = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.repositories import attachment_spool as spool_repo

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


async def _capture_read(
    store: AttachmentSpoolStore,
    token: Any,
) -> _ReadTaskOutcome:
    try:
        result = await store.read(token)
        return _ReadTaskOutcome(plaintext=result, error=None)
    except AttachmentError as exc:
        return _ReadTaskOutcome(plaintext=None, error=exc)


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


async def _fetch_statement_timestamp(
    session_factory: async_sessionmaker[AsyncSession],
) -> datetime:
    async with session_factory() as session:
        now = await session.scalar(select(func.statement_timestamp()))
    assert now is not None
    return now


async def _await_phase_c_or_fail(
    gate: _PhaseCGate,
    read_task: asyncio.Task[_ReadTaskOutcome],
) -> None:
    try:
        await asyncio.wait_for(
            gate.phase_c_reached.wait(),
            timeout=_DEADLOCK_GUARD_SECONDS,
        )
    except TimeoutError:
        read_task.cancel()
        await asyncio.gather(read_task, return_exceptions=True)
        raise AssertionError("read did not reach Phase C gate in time") from None


async def _finish_read_task(
    read_task: asyncio.Task[_ReadTaskOutcome],
) -> _ReadTaskOutcome:
    try:
        return await asyncio.wait_for(read_task, timeout=_DEADLOCK_GUARD_SECONDS)
    except TimeoutError:
        read_task.cancel()
        await asyncio.gather(read_task, return_exceptions=True)
        raise AssertionError("read task did not finish in time") from None


async def _cancel_read_task_if_needed(
    read_task: asyncio.Task[_ReadTaskOutcome] | None,
) -> None:
    if read_task is None or read_task.done():
        return
    read_task.cancel()
    await asyncio.gather(read_task, return_exceptions=True)


@pytest_asyncio.fixture(autouse=True)
async def attachment_read_row_cleanup(
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
async def test_read_active_lease_returns_plaintext_and_mime(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle, lease = await _stored_and_leased(session_factory, spool)
    result = await store.read(lease.token)
    assert isinstance(result, AttachmentPlaintext)
    assert result.data == _JPEG
    assert result.mime is AttachmentMime.IMAGE_JPEG
    assert handle.reference.to_token() not in repr(result)
    assert lease.token.to_token() not in repr(result)


@pytest.mark.asyncio
async def test_read_leaves_lease_and_object_expiry_unchanged(
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
    await store.read(lease.token)
    after = await _row_by_reference_digest(
        session_factory,
        reference_digest=handle.reference.digest(),
    )
    assert before is not None and after is not None
    assert after.state == "LEASED"
    assert after.lease_token_digest == before.lease_token_digest
    assert after.leased_at == before.leased_at
    assert after.lease_expires_at == before.lease_expires_at
    assert after.expires_at == before.expires_at


@pytest.mark.asyncio
async def test_read_vs_release_serialization(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle, lease = await _stored_and_leased(session_factory, spool)
    gate = _PhaseCGate()
    gate.install(monkeypatch)
    read_task: asyncio.Task[_ReadTaskOutcome] | None = None
    try:
        read_task = asyncio.create_task(_capture_read(store, lease.token))
        await _await_phase_c_or_fail(gate, read_task)
        await store.release(lease.token)
        gate.resume()
        outcome = await _finish_read_task(read_task)
        assert outcome.plaintext is None
        assert outcome.access_denied
        row = await _row_by_reference_digest(
            session_factory,
            reference_digest=handle.reference.digest(),
        )
        assert row is not None
        assert row.state == "STORED"
        assert row.lease_token_digest is None
        assert row.leased_at is None
        assert row.lease_expires_at is None
    finally:
        gate.resume()
        await _cancel_read_task_if_needed(read_task)


@pytest.mark.asyncio
async def test_read_vs_reclaim_serialization(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle, lease = await _stored_and_leased(session_factory, spool)
    gate = _PhaseCGate()
    gate.install(monkeypatch)
    read_task: asyncio.Task[_ReadTaskOutcome] | None = None
    try:
        read_task = asyncio.create_task(_capture_read(store, lease.token))
        await _await_phase_c_or_fail(gate, read_task)
        await _expire_lease_by_reference_digest(
            session_factory,
            reference_digest=handle.reference.digest(),
        )
        reclaim_result = await store.reclaim_expired_leases(limit=10)
        gate.resume()
        outcome = await _finish_read_task(read_task)
        assert outcome.plaintext is None
        assert outcome.access_denied
        assert reclaim_result.reclaimed == 1
        assert reclaim_result.skipped == 0
        row = await _row_by_reference_digest(
            session_factory,
            reference_digest=handle.reference.digest(),
        )
        assert row is not None
        assert row.state == "STORED"
        assert row.lease_token_digest is None
        assert row.leased_at is None
        assert row.lease_expires_at is None
    finally:
        gate.resume()
        await _cancel_read_task_if_needed(read_task)


@pytest.mark.asyncio
async def test_read_phase_c_lease_expiry_denied(
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
    gate = _PhaseCGate()
    gate.install(monkeypatch)
    read_task: asyncio.Task[_ReadTaskOutcome] | None = None
    try:
        read_task = asyncio.create_task(_capture_read(store, lease.token))
        await _await_phase_c_or_fail(gate, read_task)
        await _expire_lease_by_reference_digest(
            session_factory,
            reference_digest=handle.reference.digest(),
        )
        expired = await _row_by_reference_digest(
            session_factory,
            reference_digest=handle.reference.digest(),
        )
        assert expired is not None
        assert expired.state == "LEASED"
        assert expired.lease_token_digest == before.lease_token_digest
        assert expired.leased_at is not None and before.leased_at is not None
        assert expired.leased_at < before.leased_at
        assert expired.lease_expires_at is not None and before.lease_expires_at is not None
        assert expired.lease_expires_at != before.lease_expires_at
        assert expired.lease_expires_at > expired.leased_at
        pg_now = await _fetch_statement_timestamp(session_factory)
        assert expired.lease_expires_at < pg_now
        assert expired.expires_at == before.expires_at
        _assert_immutable_metadata_equal(expired, before)
        gate.resume()
        outcome = await _finish_read_task(read_task)
        assert outcome.plaintext is None
        assert outcome.access_denied
        after = await _row_by_reference_digest(
            session_factory,
            reference_digest=handle.reference.digest(),
        )
        assert after is not None
        assert after.state == expired.state == "LEASED"
        assert after.lease_token_digest == expired.lease_token_digest
        assert after.lease_token_digest is not None
        assert after.leased_at == expired.leased_at
        assert after.leased_at is not None
        assert after.lease_expires_at == expired.lease_expires_at
        assert after.lease_expires_at is not None
        assert after.expires_at == expired.expires_at
        _assert_immutable_metadata_equal(after, expired)
    finally:
        gate.resume()
        await _cancel_read_task_if_needed(read_task)


@pytest.mark.asyncio
async def test_read_no_raw_token_in_sql_binds(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    sql_bind_probe: list[object],
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, _handle, lease = await _stored_and_leased(session_factory, spool)
    sql_bind_probe.clear()
    await store.read(lease.token)
    rendered = repr(sql_bind_probe)
    assert lease.token.to_token() not in rendered
