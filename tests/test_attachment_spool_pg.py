"""PostgreSQL + filesystem integration tests for attachment spool Stage 1A1."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
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
from app.services.attachment_spool_store import AttachmentSpoolStore
from tests.attachment_spool_fakes import synthetic_minimal_jpeg, synthetic_minimal_png
from tests.foundation_test_db import SecretDatabaseUrl, run_alembic_command_async
from tests.pg_harness import truncate_foundation_tables

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_PRE_ATTACHMENT_REVISION = "20260731_14_ephemeral_pii_values"
_JPEG = synthetic_minimal_jpeg()
_PNG = synthetic_minimal_png()
_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
_TTL_SECONDS = 900


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


@pytest_asyncio.fixture(autouse=True)
async def attachment_spool_row_cleanup(
    request: pytest.FixtureRequest,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    if request.node.get_closest_marker("no_foundation_row_cleanup"):
        yield
        return
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


@pytest.mark.asyncio
async def test_migration_creates_attachment_spool_table(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        exists = await session.scalar(
            text(
                "SELECT to_regclass('public.attachment_spool_objects') IS NOT NULL"
            )
        )
    assert exists is True


@pytest.mark.asyncio
async def test_store_persists_metadata_without_plaintext_or_token(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    conversation_id = uuid4()
    store = _store(session_factory, spool)
    handle = await store.store(
        _JPEG,
        conversation_id=conversation_id,
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
    )
    token = handle.reference.to_token()
    digest = handle.reference.digest()

    async with session_factory() as session:
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == digest
            )
        )
        assert row is not None
        assert row.state == "STORED"
        assert row.conversation_id == conversation_id
        assert row.kind == "IMAGE"
        assert row.detected_mime == "image/jpeg"
        assert row.plaintext_size == len(_JPEG)
        assert row.ciphertext_size == len(_JPEG) + 16
        assert len(row.ciphertext_sha256) == 32
        assert len(row.nonce) == 12
        assert row.crypto_version == 1
        assert row.expires_at > row.created_at
        columns = {c.name for c in AttachmentSpoolObject.__table__.columns}
        assert "plaintext" not in columns
        assert "raw_reference" not in columns
        assert "client_filename" not in columns
        assert "content_sha256" not in columns
        assert "storage_path" not in columns
        assert token not in repr(row)
        assert _JPEG not in (row.reference_digest, row.ciphertext_sha256, row.nonce)

    final = spool / attachment_fs.final_relpath(row.object_id)
    assert final.is_file()
    assert hashlib.sha256(final.read_bytes()).digest() == row.ciphertext_sha256
    assert _JPEG not in final.read_bytes()


@pytest.mark.asyncio
async def test_writing_commit_before_final_file(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    captured: dict[str, Any] = {}

    def _sync_fail_write(
        root: Path, object_id: Any, ciphertext: bytes, **_kwargs: Any
    ) -> None:
        captured["object_id"] = object_id
        final = root / attachment_fs.final_relpath(object_id)
        temp = root / attachment_fs.temp_relpath(object_id)
        assert not final.exists()
        assert not temp.exists()
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED")

    async def _noop_cleanup(_self: Any, _row_id: Any, _object_id: Any) -> None:
        return None

    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.write_ciphertext_atomic",
        _sync_fail_write,
    )
    monkeypatch.setattr(
        AttachmentSpoolStore,
        "_best_effort_delete_writing",
        _noop_cleanup,
    )
    store = _store(session_factory, spool)
    with pytest.raises(AttachmentError) as raised:
        await store.store(
            _PNG,
            conversation_id=uuid4(),
            kind=AttachmentKind.IMAGE,
            purpose=AttachmentPurpose.OUTBOUND_ATTACHMENT_DELIVERY,
        )
    assert raised.value.code == "ATTACHMENT_FILESYSTEM_FAILED"

    object_id = captured.get("object_id")
    assert object_id is not None
    final = spool / attachment_fs.final_relpath(object_id)
    temp = spool / attachment_fs.temp_relpath(object_id)
    assert not final.exists()
    assert not temp.exists()

    async with session_factory() as session:
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.object_id == object_id
            )
        )
        assert row is not None
        assert row.state == "WRITING"

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "DELETE FROM attachment_spool_objects "
                    "WHERE object_id = CAST(:oid AS uuid)"
                ),
                {"oid": str(object_id)},
            )


@pytest.mark.asyncio
async def test_stored_commit_failure_then_reconcile_promote(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store = _store(session_factory, spool)
    commits = {"n": 0}
    from app.db.session import session_scope as real_scope
    from contextlib import asynccontextmanager
    from collections.abc import AsyncIterator

    @asynccontextmanager
    async def _scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[Any]:
        commits["n"] += 1
        if commits["n"] == 2:
            # Run real transaction work then fail commit by raising after mark.
            async with real_scope(factory) as session:
                yield session
                raise RuntimeError("synthetic STORED commit failure")
        async with real_scope(factory) as session:
            yield session

    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        _scope,
    )
    with pytest.raises(AttachmentError) as raised:
        await store.store(
            _JPEG,
            conversation_id=uuid4(),
            kind=AttachmentKind.IMAGE,
            purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        )
    assert raised.value.code == "ATTACHMENT_STORE_FAILED"

    async with session_factory() as session:
        row = await session.scalar(select(AttachmentSpoolObject))
        assert row is not None
        assert row.state == "WRITING"
        object_id = row.object_id
        final = spool / attachment_fs.final_relpath(object_id)
        assert final.is_file()

    # Age the row past grace for reconcile.
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE attachment_spool_objects
                    SET
                        created_at = statement_timestamp() - interval '20 minutes',
                        updated_at = statement_timestamp() - interval '20 minutes'
                    WHERE object_id = :oid
                    """
                ),
                {"oid": object_id},
            )

    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        real_scope,
    )
    result = await store.reconcile(limit=100)
    assert result.promoted_to_stored == 1

    async with session_factory() as session:
        state = await session.scalar(
            select(AttachmentSpoolObject.state).where(
                AttachmentSpoolObject.object_id == object_id
            )
        )
    assert state == "STORED"


@pytest.mark.asyncio
async def test_reconcile_writing_no_file_and_temp_only(
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
    async with session_factory() as session:
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == digest
            )
        )
        assert row is not None
        object_id = row.object_id

    # Force WRITING + delete final → no files case.
    final = spool / attachment_fs.final_relpath(object_id)
    final.unlink()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE attachment_spool_objects
                    SET
                        state = 'WRITING',
                        updated_at = statement_timestamp() - interval '20 minutes'
                    WHERE object_id = :oid
                    """
                ),
                {"oid": object_id},
            )

    result = await store.reconcile(limit=100)
    assert result.deleted_writing_rows == 1
    async with session_factory() as session:
        remaining = await session.scalar(
            select(func.count()).select_from(AttachmentSpoolObject)
        )
    assert remaining == 0

    # Temp-only case.
    handle2 = await store.store(
        _PNG,
        conversation_id=uuid4(),
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.OUTBOUND_ATTACHMENT_DELIVERY,
    )
    digest2 = handle2.reference.digest()
    async with session_factory() as session:
        row2 = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == digest2
            )
        )
        assert row2 is not None
        oid2 = row2.object_id
    final2 = spool / attachment_fs.final_relpath(oid2)
    data = final2.read_bytes()
    final2.unlink()
    temp2 = spool / attachment_fs.temp_relpath(oid2)
    temp2.parent.mkdir(parents=True, exist_ok=True)
    temp2.write_bytes(data)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE attachment_spool_objects
                    SET
                        state = 'WRITING',
                        updated_at = statement_timestamp() - interval '20 minutes'
                    WHERE object_id = :oid
                    """
                ),
                {"oid": oid2},
            )
    result2 = await store.reconcile(limit=100)
    assert result2.deleted_writing_rows == 1
    assert not temp2.exists()


@pytest.mark.asyncio
async def test_reconcile_tampered_final_never_promotes(
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
    async with session_factory() as session:
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == digest
            )
        )
        assert row is not None
        object_id = row.object_id
    final = spool / attachment_fs.final_relpath(object_id)
    final.write_bytes(final.read_bytes() + b"\x00")
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE attachment_spool_objects
                    SET
                        state = 'WRITING',
                        updated_at = statement_timestamp() - interval '20 minutes'
                    WHERE object_id = :oid
                    """
                ),
                {"oid": object_id},
            )
    result = await store.reconcile(limit=100)
    assert result.promoted_to_stored == 0
    assert result.deleted_writing_rows == 1
    assert not final.exists()


@pytest.mark.asyncio
async def test_concurrent_reconcile_promotes_once(
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
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE attachment_spool_objects
                    SET
                        state = 'WRITING',
                        updated_at = statement_timestamp() - interval '20 minutes'
                    WHERE reference_digest = :digest
                    """
                ),
                {"digest": digest},
            )

    async def _run() -> int:
        local = _store(session_factory, spool)
        result = await local.reconcile(limit=100)
        return result.promoted_to_stored

    first, second = await asyncio.gather(_run(), _run())
    assert first + second == 1
    async with session_factory() as session:
        state = await session.scalar(
            select(AttachmentSpoolObject.state).where(
                AttachmentSpoolObject.reference_digest == digest
            )
        )
    assert state == "STORED"


@pytest.mark.asyncio
async def test_fresh_writing_not_reconciled(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    # Interrupt after WRITING + final write by failing STORED transition.
    from app.db.session import session_scope as real_scope
    from contextlib import asynccontextmanager
    from collections.abc import AsyncIterator

    commits = {"n": 0}

    @asynccontextmanager
    async def _scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[Any]:
        commits["n"] += 1
        if commits["n"] == 2:
            async with real_scope(factory) as session:
                yield session
                raise RuntimeError("leave WRITING")
        async with real_scope(factory) as session:
            yield session

    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        _scope,
    )
    store = _store(session_factory, spool)
    with pytest.raises(AttachmentError):
        await store.store(
            _JPEG,
            conversation_id=uuid4(),
            kind=AttachmentKind.IMAGE,
            purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        real_scope,
    )
    result = await store.reconcile(limit=100)
    assert result.promoted_to_stored == 0
    assert result.deleted_writing_rows == 0
    async with session_factory() as session:
        state = await session.scalar(select(AttachmentSpoolObject.state))
    assert state == "WRITING"


def _flatten_sql_parameters(parameters: object) -> list[object]:
    flat: list[object] = []

    def _visit(value: object) -> None:
        if value is None:
            flat.append(None)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                _visit(key)
                _visit(item)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                _visit(item)
            return
        if isinstance(value, memoryview):
            _visit(bytes(value))
            return
        if isinstance(value, bytearray):
            _visit(bytes(value))
            return
        flat.append(value)

    _visit(parameters)
    return flat


@pytest.fixture
def sql_param_probe(pg_engine: AsyncEngine) -> Iterator[tuple[list[object], list[int]]]:
    captured: list[object] = []
    statement_numbers: list[int] = []
    counter = {"value": 0}

    def _before_cursor_execute(
        _conn: Any,
        _cursor: Any,
        _statement: str,
        parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        counter["value"] += 1
        stmt_num = counter["value"]
        for param in _flatten_sql_parameters(parameters):
            captured.append(param)
            statement_numbers.append(stmt_num)

    sync_engine = pg_engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _before_cursor_execute)
    try:
        yield captured, statement_numbers
    finally:
        event.remove(sync_engine, "before_cursor_execute", _before_cursor_execute)


@pytest.mark.asyncio
async def test_store_sql_parameters_exclude_plaintext_and_token(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    sql_param_probe: tuple[list[object], list[int]],
) -> None:
    captured, _nums = sql_param_probe
    spool = tmp_path / "spool"
    spool.mkdir()
    store = _store(session_factory, spool)
    handle = await store.store(
        _JPEG,
        conversation_id=uuid4(),
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
    )
    token = handle.reference.to_token()
    token_ascii = token.encode("ascii")
    for value in captured:
        if value == _JPEG or value == token or value == token_ascii:
            raise AssertionError("sensitive value in SQL parameters")
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            if _JPEG in raw or token_ascii in raw:
                raise AssertionError("sensitive bytes in SQL parameters")
        if isinstance(value, str) and (token in value):
            raise AssertionError("token substring in SQL parameters")


@pytest.mark.asyncio
async def test_orphan_temp_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store = _store(session_factory, spool)
    object_id = uuid4()
    shard = spool / object_id.hex[:2]
    shard.mkdir(parents=True)
    temp = shard / f"{object_id}.tmp"
    temp.write_bytes(b"orphan-ciphertext")
    import os
    import time

    past = time.time() - 700
    os.utime(temp, (past, past))
    result = await store.reconcile(limit=100)
    assert result.deleted_orphan_temps == 1
    assert not temp.exists()


@pytest.mark.asyncio
async def test_reconcile_stored_missing_final_deletes_metadata(
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
    async with session_factory() as session:
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == digest
            )
        )
        assert row is not None
        object_id = row.object_id
    final = spool / attachment_fs.final_relpath(object_id)
    assert final.is_file()
    final.unlink()
    result = await store.reconcile(limit=100)
    assert result.deleted_unrecoverable_stored == 1
    async with session_factory() as session:
        remaining = await session.scalar(
            select(func.count())
            .select_from(AttachmentSpoolObject)
            .where(AttachmentSpoolObject.reference_digest == digest)
        )
    assert remaining == 0


@pytest.mark.asyncio
async def test_reconcile_stored_tampered_final_deletes_file_and_row(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store = _store(session_factory, spool)
    handle = await store.store(
        _PNG,
        conversation_id=uuid4(),
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.OUTBOUND_ATTACHMENT_DELIVERY,
    )
    digest = handle.reference.digest()
    async with session_factory() as session:
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == digest
            )
        )
        assert row is not None
        object_id = row.object_id
    final = spool / attachment_fs.final_relpath(object_id)
    final.write_bytes(final.read_bytes() + b"\x00")
    result = await store.reconcile(limit=100)
    assert result.deleted_unrecoverable_stored == 1
    assert not final.exists()
    async with session_factory() as session:
        remaining = await session.scalar(
            select(func.count())
            .select_from(AttachmentSpoolObject)
            .where(AttachmentSpoolObject.reference_digest == digest)
        )
    assert remaining == 0


@pytest.mark.asyncio
async def test_reconcile_stored_io_unavailable_preserves_row_and_file(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.attachment_types import CiphertextInspectStatus

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
    async with session_factory() as session:
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == digest
            )
        )
        assert row is not None
        object_id = row.object_id
    final = spool / attachment_fs.final_relpath(object_id)
    assert final.is_file()
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.inspect_ciphertext_file",
        lambda *_a, **_k: CiphertextInspectStatus.IO_UNAVAILABLE,
    )
    result = await store.reconcile(limit=100)
    assert result.deleted_unrecoverable_stored == 0
    assert result.io_unavailable_skipped == 1
    assert final.is_file()
    async with session_factory() as session:
        state = await session.scalar(
            select(AttachmentSpoolObject.state).where(
                AttachmentSpoolObject.reference_digest == digest
            )
        )
    assert state == "STORED"


@pytest.mark.asyncio
async def test_reconcile_writing_io_unavailable_no_promote_no_delete(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.attachment_types import CiphertextInspectStatus
    from app.db.session import session_scope as real_scope
    from contextlib import asynccontextmanager
    from collections.abc import AsyncIterator

    spool = tmp_path / "spool"
    spool.mkdir()
    commits = {"n": 0}

    @asynccontextmanager
    async def _scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[Any]:
        commits["n"] += 1
        if commits["n"] == 2:
            async with real_scope(factory) as session:
                yield session
                raise RuntimeError("leave WRITING with final")
        async with real_scope(factory) as session:
            yield session

    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        _scope,
    )
    store = _store(session_factory, spool)
    with pytest.raises(AttachmentError):
        await store.store(
            _JPEG,
            conversation_id=uuid4(),
            kind=AttachmentKind.IMAGE,
            purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        real_scope,
    )
    async with session_factory() as session:
        row = await session.scalar(select(AttachmentSpoolObject))
        assert row is not None
        assert row.state == "WRITING"
        object_id = row.object_id
        digest = row.reference_digest
    final = spool / attachment_fs.final_relpath(object_id)
    assert final.is_file()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE attachment_spool_objects
                    SET updated_at = statement_timestamp() - interval '20 minutes'
                    WHERE object_id = :oid
                    """
                ),
                {"oid": object_id},
            )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.inspect_ciphertext_file",
        lambda *_a, **_k: CiphertextInspectStatus.IO_UNAVAILABLE,
    )
    result = await store.reconcile(limit=100)
    assert result.promoted_to_stored == 0
    assert result.deleted_writing_rows == 0
    assert result.io_unavailable_skipped == 1
    assert final.is_file()
    async with session_factory() as session:
        state = await session.scalar(
            select(AttachmentSpoolObject.state).where(
                AttachmentSpoolObject.reference_digest == digest
            )
        )
    assert state == "WRITING"


@pytest.mark.asyncio
@pytest.mark.no_foundation_row_cleanup
async def test_attachment_spool_migration_downgrade_reupgrade(
    pg_database_url: SecretDatabaseUrl,
    pg_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import CheckConstraint, UniqueConstraint

    from app.db.base import Base

    await pg_engine.dispose()
    try:
        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="downgrade",
            revision=_PRE_ATTACHMENT_REVISION,
            database_url=pg_database_url,
        )
        async with session_factory() as session:
            exists = await session.scalar(
                text(
                    "SELECT to_regclass('public.attachment_spool_objects') "
                    "IS NOT NULL"
                )
            )
        assert exists is False

        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="upgrade",
            revision="head",
            database_url=pg_database_url,
        )
        async with session_factory() as session:
            exists = await session.scalar(
                text(
                    "SELECT to_regclass('public.attachment_spool_objects') "
                    "IS NOT NULL"
                )
            )
        assert exists is True

        async with pg_engine.connect() as connection:

            def _compare(sync_conn: Any) -> list[Any]:
                context = MigrationContext.configure(sync_conn)
                return compare_metadata(context, Base.metadata)

            drift = await connection.run_sync(_compare)
        assert drift == []

        checks = {
            c.name
            for table in Base.metadata.tables.values()
            if table.name == "attachment_spool_objects"
            for c in table.constraints
            if isinstance(c, CheckConstraint) and c.name
        }
        uniques = {
            c.name
            for table in Base.metadata.tables.values()
            if table.name == "attachment_spool_objects"
            for c in table.constraints
            if isinstance(c, UniqueConstraint) and c.name
        }
        assert "ck_attachment_spool_objects_ciphertext_sha256_len" in checks
        assert "uq_attachment_spool_objects_reference_digest" in uniques
        assert "uq_attachment_spool_objects_object_id" in uniques
    finally:
        await pg_engine.dispose()
        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="upgrade",
            revision="head",
            database_url=pg_database_url,
        )
        await pg_engine.dispose()
