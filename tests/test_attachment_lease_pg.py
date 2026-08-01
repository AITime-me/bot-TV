"""PostgreSQL integration tests for attachment spool lease Stage 1A2A."""

from __future__ import annotations

import asyncio
import base64
import secrets
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.attachment_keys import EnvAttachmentKeyProvider
from app.core.attachment_types import (
    LEASE_TTL_SECONDS,
    AttachmentError,
    AttachmentKind,
    AttachmentLeaseToken,
    AttachmentPurpose,
    AttachmentSpoolPolicy,
)
from app.models.attachment_spool import AttachmentSpoolObject
from app.services.attachment_spool_store import AttachmentSpoolStore
from tests.attachment_spool_fakes import synthetic_minimal_jpeg
from tests.foundation_test_db import SecretDatabaseUrl, run_alembic_command_async
from tests.pg_harness import truncate_foundation_tables

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_PRE_LEASE_REVISION = "20260801_15_attachment_spool"
_JPEG = synthetic_minimal_jpeg()
_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
_TTL_SECONDS = 900
_UTC = timezone.utc

# Structurally valid expired LEASED timestamps: leased_at < lease_expires_at <= now.
_EXPIRE_LEASE_BY_DIGEST_SQL = """
UPDATE attachment_spool_objects
SET
    leased_at = statement_timestamp() - interval '301 seconds',
    lease_expires_at = statement_timestamp() - interval '1 second'
WHERE reference_digest = :digest
"""

_EXPIRE_ALL_LEASED_SQL = """
UPDATE attachment_spool_objects
SET
    leased_at = statement_timestamp() - interval '301 seconds',
    lease_expires_at = statement_timestamp() - interval '1 second'
WHERE state = 'LEASED'
"""

# Shift lifecycle timestamps together so expires_at > created_at while object is expired.
_EXPIRE_OBJECT_BY_DIGEST_SQL = """
UPDATE attachment_spool_objects
SET
    created_at = statement_timestamp() - interval '902 seconds',
    updated_at = statement_timestamp() - interval '902 seconds',
    expires_at = statement_timestamp() - interval '1 second'
WHERE reference_digest = :digest
"""

_INVALID_LEASE_ORDER_SQL = """
UPDATE attachment_spool_objects
SET
    leased_at = statement_timestamp(),
    lease_expires_at = statement_timestamp() - interval '1 second'
WHERE reference_digest = :digest
"""

_INSTALL_LEASED_DIGEST_SQL = """
UPDATE attachment_spool_objects
SET
    state = 'LEASED',
    lease_token_digest = :lease_digest,
    leased_at = statement_timestamp(),
    lease_expires_at = statement_timestamp() + interval '5 minutes'
WHERE reference_digest = :reference_digest
"""

_STAGE_1A1_INDEXES = frozenset(
    {
        "ix_attachment_spool_objects_expires_at",
        "ix_attachment_spool_objects_state_updated_at",
    }
)

_STAGE_1A2_INDEXES = frozenset(
    {
        "uq_attachment_spool_objects_lease_token_digest",
        "ix_attachment_spool_objects_leased_expires_at",
        "ix_attachment_spool_objects_object_expiry_purge",
    }
)

_STAGE_1A2_LEASE_COLUMNS = frozenset(
    {
        "lease_token_digest",
        "leased_at",
        "lease_expires_at",
    }
)

_STAGE_1A2_LEASE_CHECKS = frozenset(
    {
        "ck_attachment_spool_objects_lease_digest_len",
        "ck_attachment_spool_objects_lease_fields_all_or_none",
        "ck_attachment_spool_objects_state_lease",
    }
)


def _deterministic_lease_token(fill_byte: int) -> AttachmentLeaseToken:
    return AttachmentLeaseToken(bytes([fill_byte]) * 32)


def _lease_token_factory(
    sequence: list[AttachmentLeaseToken],
    *,
    attempts: list[AttachmentLeaseToken] | None = None,
) -> Any:
    index = {"value": 0}

    def _generate() -> AttachmentLeaseToken:
        if index["value"] >= len(sequence):
            raise RuntimeError("deterministic lease token sequence exhausted")
        token = sequence[index["value"]]
        index["value"] += 1
        if attempts is not None:
            attempts.append(token)
        return token

    return _generate


async def _install_leased_digest(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reference_digest: bytes,
    lease_digest: bytes,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_INSTALL_LEASED_DIGEST_SQL),
                {
                    "lease_digest": lease_digest,
                    "reference_digest": reference_digest,
                },
            )


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


def _assert_no_sensitive_values(
    values: list[object],
    *,
    forbidden_tokens: list[AttachmentLeaseToken],
) -> None:
    rendered = repr(values)
    for token in forbidden_tokens:
        assert token.to_token() not in rendered


async def _fetch_row_by_reference(
    session_factory: async_sessionmaker[AsyncSession],
    reference_digest: bytes,
) -> AttachmentSpoolObject:
    async with session_factory() as session:
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == reference_digest
            )
        )
    assert row is not None
    return row


async def _assert_stage_1a1_schema(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        columns = {
            row[0]
            for row in (
                await session.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'attachment_spool_objects'
                        """
                    )
                )
            ).all()
        }
        for name in _STAGE_1A2_LEASE_COLUMNS:
            assert name not in columns

        indexes = {
            row[0]
            for row in (
                await session.execute(
                    text(
                        """
                        SELECT indexname
                        FROM pg_indexes
                        WHERE schemaname = 'public'
                          AND tablename = 'attachment_spool_objects'
                        """
                    )
                )
            ).all()
        }
        for name in _STAGE_1A2_INDEXES:
            assert name not in indexes
        for name in _STAGE_1A1_INDEXES:
            assert name in indexes

        checks = {
            row[0]: row[1]
            for row in (
                await session.execute(
                    text(
                        """
                        SELECT c.conname, pg_get_constraintdef(c.oid)
                        FROM pg_catalog.pg_constraint c
                        JOIN pg_catalog.pg_class t ON t.oid = c.conrelid
                        JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
                        WHERE n.nspname = 'public'
                          AND t.relname = 'attachment_spool_objects'
                          AND c.contype = 'c'
                        """
                    )
                )
            ).all()
        }
        for name in _STAGE_1A2_LEASE_CHECKS:
            assert name not in checks

        assert "ck_attachment_spool_objects_state" in checks
        state_check = checks["ck_attachment_spool_objects_state"]
        assert "LEASED" not in state_check
        assert "DELETE_PENDING" not in state_check
        assert "WRITING" in state_check
        assert "STORED" in state_check

        version = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert version == _PRE_LEASE_REVISION


async def _fetch_state_by_reference_digest_sql(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reference_digest: bytes,
) -> str:
    async with session_factory() as session:
        state = await session.scalar(
            text(
                """
                SELECT state
                FROM attachment_spool_objects
                WHERE reference_digest = :digest
                """
            ),
            {"digest": reference_digest},
        )
    assert state is not None
    return str(state)


async def _delete_row_by_reference_digest_sql(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reference_digest: bytes,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    DELETE FROM attachment_spool_objects
                    WHERE reference_digest = :digest
                    """
                ),
                {"digest": reference_digest},
            )


async def _assert_stage_1a1_rejects_expanded_states(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reference_digest: bytes,
) -> None:
    assert (
        await _fetch_state_by_reference_digest_sql(
            session_factory,
            reference_digest=reference_digest,
        )
        == "STORED"
    )

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE attachment_spool_objects
                        SET state = 'LEASED'
                        WHERE reference_digest = :digest
                        """
                    ),
                    {"digest": reference_digest},
                )

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE attachment_spool_objects
                        SET state = 'DELETE_PENDING'
                        WHERE reference_digest = :digest
                        """
                    ),
                    {"digest": reference_digest},
                )

    assert (
        await _fetch_state_by_reference_digest_sql(
            session_factory,
            reference_digest=reference_digest,
        )
        == "STORED"
    )

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE attachment_spool_objects
                    SET state = 'WRITING'
                    WHERE reference_digest = :digest
                    """
                ),
                {"digest": reference_digest},
            )
    assert (
        await _fetch_state_by_reference_digest_sql(
            session_factory,
            reference_digest=reference_digest,
        )
        == "WRITING"
    )

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE attachment_spool_objects
                    SET state = 'STORED'
                    WHERE reference_digest = :digest
                    """
                ),
                {"digest": reference_digest},
            )
    assert (
        await _fetch_state_by_reference_digest_sql(
            session_factory,
            reference_digest=reference_digest,
        )
        == "STORED"
    )


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
async def attachment_lease_row_cleanup(
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


async def _stored_handle(
    session_factory: async_sessionmaker[AsyncSession],
    spool_root: Path,
) -> tuple[AttachmentSpoolStore, Any]:
    store = _store(session_factory, spool_root)
    handle = await store.store(
        _JPEG,
        conversation_id=uuid4(),
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
    )
    return store, handle


@pytest.mark.asyncio
async def test_existing_stored_rows_have_null_lease_fields(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    _store_obj, handle = await _stored_handle(session_factory, spool)
    async with session_factory() as session:
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == handle.reference.digest()
            )
        )
    assert row is not None
    assert row.state == "STORED"
    assert row.lease_token_digest is None
    assert row.leased_at is None
    assert row.lease_expires_at is None


@pytest.mark.asyncio
async def test_schema_state_and_lease_checks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        checks = {
            row[0]
            for row in (
                await session.execute(
                    text(
                        """
                        SELECT c.conname
                        FROM pg_catalog.pg_constraint c
                        JOIN pg_catalog.pg_class t ON t.oid = c.conrelid
                        JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
                        WHERE n.nspname = 'public'
                          AND t.relname = 'attachment_spool_objects'
                          AND c.contype = 'c'
                        """
                    )
                )
            ).all()
        }
    for name in (
        "ck_attachment_spool_objects_state",
        "ck_attachment_spool_objects_lease_digest_len",
        "ck_attachment_spool_objects_lease_fields_all_or_none",
        "ck_attachment_spool_objects_state_lease",
    ):
        assert name in checks


@pytest.mark.asyncio
async def test_schema_rejects_partial_lease_fields(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    _store_obj, handle = await _stored_handle(session_factory, spool)
    digest = handle.reference.digest()
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE attachment_spool_objects
                        SET
                            state = 'DELETE_PENDING',
                            lease_token_digest = :digest,
                            leased_at = NULL,
                            lease_expires_at = NULL
                        WHERE reference_digest = :ref_digest
                        """
                    ),
                    {"digest": secrets.token_bytes(32), "ref_digest": digest},
                )


@pytest.mark.asyncio
async def test_delete_pending_accepts_null_or_full_lease_fields(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    _store_obj, handle = await _stored_handle(session_factory, spool)
    digest = handle.reference.digest()
    lease_digest = secrets.token_bytes(32)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE attachment_spool_objects
                    SET state = 'DELETE_PENDING'
                    WHERE reference_digest = :ref_digest
                    """
                ),
                {"ref_digest": digest},
            )
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE attachment_spool_objects
                    SET state = 'STORED'
                    WHERE reference_digest = :ref_digest
                    """
                ),
                {"ref_digest": digest},
            )
            await session.execute(
                text(
                    """
                    UPDATE attachment_spool_objects
                    SET
                        state = 'DELETE_PENDING',
                        lease_token_digest = :lease_digest,
                        leased_at = statement_timestamp(),
                        lease_expires_at = statement_timestamp() - interval '1 second'
                    WHERE reference_digest = :ref_digest
                    """
                ),
                {"lease_digest": lease_digest, "ref_digest": digest},
            )


@pytest.mark.asyncio
async def test_unique_partial_lease_digest_index(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, first = await _stored_handle(session_factory, spool)
    store2, second = await _stored_handle(session_factory, spool)
    lease_digest = secrets.token_bytes(32)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE attachment_spool_objects
                    SET
                        state = 'LEASED',
                        lease_token_digest = :digest,
                        leased_at = statement_timestamp(),
                        lease_expires_at = statement_timestamp() + interval '5 minutes'
                    WHERE reference_digest = :ref_digest
                    """
                ),
                {"digest": lease_digest, "ref_digest": first.reference.digest()},
            )
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE attachment_spool_objects
                        SET
                            state = 'LEASED',
                            lease_token_digest = :digest,
                            leased_at = statement_timestamp(),
                            lease_expires_at = statement_timestamp() + interval '5 minutes'
                        WHERE reference_digest = :ref_digest
                        """
                    ),
                    {
                        "digest": lease_digest,
                        "ref_digest": second.reference.digest(),
                    },
                )
    assert store2 is not None


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
        captured.extend(_flatten_sql_parameters(parameters))

    sync_engine = pg_engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _before_cursor_execute)
    try:
        yield captured
    finally:
        event.remove(sync_engine, "before_cursor_execute", _before_cursor_execute)


@pytest.mark.asyncio
async def test_acquire_pg_one_digest_collision_retries_via_savepoint(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sql_statement_probe: list[str],
    sql_bind_probe: list[object],
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    _store_obj, occupant = await _stored_handle(session_factory, spool)
    store, target = await _stored_handle(session_factory, spool)
    token_a = _deterministic_lease_token(0x41)
    token_b = _deterministic_lease_token(0x42)
    await _install_leased_digest(
        session_factory,
        reference_digest=occupant.reference.digest(),
        lease_digest=token_a.digest(),
    )
    attempts: list[AttachmentLeaseToken] = []
    monkeypatch.setattr(
        AttachmentLeaseToken,
        "generate",
        _lease_token_factory([token_a, token_b], attempts=attempts),
    )
    sql_statement_probe.clear()
    sql_bind_probe.clear()

    lease = await store.acquire(target.reference)

    assert len(attempts) == 2
    assert lease.token == token_b
    row = await _fetch_row_by_reference(
        session_factory, target.reference.digest()
    )
    assert row.state == "LEASED"
    assert row.lease_token_digest == token_b.digest()
    assert row.lease_token_digest != token_a.digest()
    assert row.leased_at is not None
    assert row.lease_expires_at is not None
    assert row.lease_expires_at > row.leased_at
    acquire_sql = " ".join(sql_statement_probe).lower()
    assert "savepoint" in acquire_sql
    _assert_no_sensitive_values(sql_bind_probe, forbidden_tokens=[token_a, token_b])


@pytest.mark.asyncio
async def test_acquire_pg_three_digest_collisions_fail_closed(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sql_bind_probe: list[object],
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    _store_a, occupant_a = await _stored_handle(session_factory, spool)
    _store_b, occupant_b = await _stored_handle(session_factory, spool)
    _store_c, occupant_c = await _stored_handle(session_factory, spool)
    store, target = await _stored_handle(session_factory, spool)
    token_a = _deterministic_lease_token(0x41)
    token_b = _deterministic_lease_token(0x42)
    token_c = _deterministic_lease_token(0x43)
    await _install_leased_digest(
        session_factory,
        reference_digest=occupant_a.reference.digest(),
        lease_digest=token_a.digest(),
    )
    await _install_leased_digest(
        session_factory,
        reference_digest=occupant_b.reference.digest(),
        lease_digest=token_b.digest(),
    )
    await _install_leased_digest(
        session_factory,
        reference_digest=occupant_c.reference.digest(),
        lease_digest=token_c.digest(),
    )
    attempts: list[AttachmentLeaseToken] = []
    monkeypatch.setattr(
        AttachmentLeaseToken,
        "generate",
        _lease_token_factory([token_a, token_b, token_c], attempts=attempts),
    )
    sql_bind_probe.clear()

    with pytest.raises(AttachmentError) as raised:
        await store.acquire(target.reference)

    assert raised.value.code == "ATTACHMENT_STORE_FAILED"
    assert str(raised.value) == "ATTACHMENT_STORE_FAILED"
    error_blob = str(raised.value) + repr(raised.value)
    for forbidden in (
        token_a.to_token(),
        token_b.to_token(),
        token_c.to_token(),
        "LEASED",
        "STORED",
        "uq_attachment_spool_objects_lease_token_digest",
        "23505",
    ):
        assert forbidden not in error_blob

    assert len(attempts) == 3
    row = await _fetch_row_by_reference(
        session_factory, target.reference.digest()
    )
    assert row.state == "STORED"
    assert row.lease_token_digest is None
    assert row.leased_at is None
    assert row.lease_expires_at is None
    _assert_no_sensitive_values(
        sql_bind_probe,
        forbidden_tokens=[token_a, token_b, token_c],
    )


@pytest.mark.asyncio
async def test_schema_rejects_invalid_lease_digest_lengths(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    _store_obj, handle = await _stored_handle(session_factory, spool)
    reference_digest = handle.reference.digest()
    valid_digest = secrets.token_bytes(32)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_INSTALL_LEASED_DIGEST_SQL),
                {
                    "lease_digest": valid_digest,
                    "reference_digest": reference_digest,
                },
            )
        for bad_digest in (secrets.token_bytes(31), secrets.token_bytes(33), b""):
            with pytest.raises(IntegrityError):
                async with session.begin():
                    await session.execute(
                        text(
                            """
                            UPDATE attachment_spool_objects
                            SET lease_token_digest = :digest
                            WHERE reference_digest = :ref_digest
                            """
                        ),
                        {"digest": bad_digest, "ref_digest": reference_digest},
                    )
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE attachment_spool_objects
                    SET lease_token_digest = :digest
                    WHERE reference_digest = :ref_digest
                    """
                ),
                {
                    "digest": secrets.token_bytes(32),
                    "ref_digest": reference_digest,
                },
            )
        row = await session.scalar(
            select(AttachmentSpoolObject.lease_token_digest).where(
                AttachmentSpoolObject.reference_digest == reference_digest
            )
        )
    assert row is not None
    assert len(row) == 32


@pytest.mark.asyncio
async def test_acquire_release_and_reclaim_flow(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle = await _stored_handle(session_factory, spool)
    lease = await store.acquire(handle.reference)
    token = lease.token.to_token()
    async with session_factory() as session:
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == handle.reference.digest()
            )
        )
    assert row is not None
    assert row.state == "LEASED"
    assert row.lease_token_digest is not None
    assert token not in repr(row)
    assert handle.reference.to_token() not in repr(row)
    await store.release(lease.token)
    async with session_factory() as session:
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == handle.reference.digest()
            )
        )
    assert row is not None
    assert row.state == "STORED"
    assert row.lease_token_digest is None


@pytest.mark.asyncio
async def test_concurrent_acquire_has_single_winner(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle = await _stored_handle(session_factory, spool)
    reference_digest = handle.reference.digest()

    async def _acquire() -> Any:
        local = _store(session_factory, spool)
        try:
            return await local.acquire(handle.reference)
        except AttachmentError as exc:
            return exc

    first, second = await asyncio.gather(_acquire(), _acquire())
    successes = [item for item in (first, second) if not isinstance(item, AttachmentError)]
    denials = [
        item
        for item in (first, second)
        if isinstance(item, AttachmentError)
        and item.code == "ATTACHMENT_ACCESS_DENIED"
    ]
    assert len(successes) == 1
    assert len(denials) == 1

    winner = successes[0]

    async with session_factory() as session:
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == reference_digest
            )
        )
        row_count = await session.scalar(
            select(func.count())
            .select_from(AttachmentSpoolObject)
            .where(AttachmentSpoolObject.reference_digest == reference_digest)
        )
        pg_now = await session.scalar(select(func.statement_timestamp()))
    assert row is not None
    assert row_count == 1
    assert row.state == "LEASED"
    assert row.lease_token_digest is not None
    assert row.leased_at is not None
    assert row.lease_expires_at is not None
    assert row.lease_token_digest == winner.token.digest()
    assert (row.lease_expires_at - row.leased_at) == timedelta(
        seconds=LEASE_TTL_SECONDS
    )
    assert row.lease_expires_at > pg_now - timedelta(seconds=5)


@pytest.mark.asyncio
async def test_expired_lease_reclaimed_and_reacquired(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle = await _stored_handle(session_factory, spool)
    first = await store.acquire(handle.reference)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_EXPIRE_LEASE_BY_DIGEST_SQL),
                {"digest": handle.reference.digest()},
            )
    second = await store.acquire(handle.reference)
    assert second.token != first.token
    async with session_factory() as session:
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == handle.reference.digest()
            )
        )
    assert row is not None
    assert row.state == "LEASED"
    assert row.lease_token_digest == second.token.digest()


@pytest.mark.asyncio
async def test_schema_accepts_expired_object_fixture(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    _store_obj, handle = await _stored_handle(session_factory, spool)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_EXPIRE_OBJECT_BY_DIGEST_SQL),
                {"digest": handle.reference.digest()},
            )
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == handle.reference.digest()
            )
        )
        pg_now = await session.scalar(select(func.statement_timestamp()))
    assert row is not None
    assert row.expires_at > row.created_at
    assert row.updated_at >= row.created_at
    assert row.expires_at <= pg_now


@pytest.mark.asyncio
async def test_expired_object_acquire_denied(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle = await _stored_handle(session_factory, spool)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_EXPIRE_OBJECT_BY_DIGEST_SQL),
                {"digest": handle.reference.digest()},
            )
    with pytest.raises(AttachmentError) as raised:
        await store.acquire(handle.reference)
    assert raised.value.code == "ATTACHMENT_ACCESS_DENIED"


@pytest.mark.asyncio
async def test_release_allows_object_expired_but_active_lease(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle = await _stored_handle(session_factory, spool)
    lease = await store.acquire(handle.reference)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_EXPIRE_OBJECT_BY_DIGEST_SQL),
                {"digest": handle.reference.digest()},
            )
            expired_at = await session.scalar(
                select(AttachmentSpoolObject.expires_at).where(
                    AttachmentSpoolObject.reference_digest == handle.reference.digest()
                )
            )
    await store.release(lease.token)
    async with session_factory() as session:
        after = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == handle.reference.digest()
            )
        )
        pg_now = await session.scalar(select(func.statement_timestamp()))
    assert after is not None
    assert after.state == "STORED"
    assert expired_at is not None
    assert after.expires_at == expired_at
    assert after.expires_at < pg_now


@pytest.mark.asyncio
async def test_repeated_and_expired_release_denied(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle = await _stored_handle(session_factory, spool)
    lease = await store.acquire(handle.reference)
    await store.release(lease.token)
    with pytest.raises(AttachmentError):
        await store.release(lease.token)
    lease2 = await store.acquire(handle.reference)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_EXPIRE_LEASE_BY_DIGEST_SQL),
                {"digest": handle.reference.digest()},
            )
    with pytest.raises(AttachmentError):
        await store.release(lease2.token)


@pytest.mark.asyncio
async def test_reclaim_expired_leases_skip_locked(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, first = await _stored_handle(session_factory, spool)
    _store_obj, second = await _stored_handle(session_factory, spool)
    await store.acquire(first.reference)
    await store.acquire(second.reference)
    locked_digest = first.reference.digest()
    unlocked_digest = second.reference.digest()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text(_EXPIRE_ALL_LEASED_SQL))

    lock_ready = asyncio.Event()
    reclaim_finished = asyncio.Event()
    reclaim_result: dict[str, Any] = {}

    async def _hold_row_lock() -> None:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        SELECT id
                        FROM attachment_spool_objects
                        WHERE reference_digest = :digest
                        FOR UPDATE
                        """
                    ),
                    {"digest": locked_digest},
                )
                lock_ready.set()
                await reclaim_finished.wait()

    async def _run_reclaim() -> None:
        await lock_ready.wait()
        reclaim_result["value"] = await store.reclaim_expired_leases(limit=10)
        reclaim_finished.set()

    await asyncio.gather(_hold_row_lock(), _run_reclaim())

    result = reclaim_result["value"]
    assert result.reclaimed == 1
    assert result.skipped == 0

    async with session_factory() as session:
        locked_state = await session.scalar(
            select(AttachmentSpoolObject.state).where(
                AttachmentSpoolObject.reference_digest == locked_digest
            )
        )
        unlocked_state = await session.scalar(
            select(AttachmentSpoolObject.state).where(
                AttachmentSpoolObject.reference_digest == unlocked_digest
            )
        )
    assert locked_state == "LEASED"
    assert unlocked_state == "STORED"

    follow_up = await store.reclaim_expired_leases(limit=10)
    assert follow_up.reclaimed == 1
    assert follow_up.skipped == 0

    async with session_factory() as session:
        final_locked_state = await session.scalar(
            select(AttachmentSpoolObject.state).where(
                AttachmentSpoolObject.reference_digest == locked_digest
            )
        )
    assert final_locked_state == "STORED"


@pytest.fixture
def sql_statement_probe(pg_engine: AsyncEngine) -> Iterator[list[str]]:
    captured: list[str] = []

    def _before_cursor_execute(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        captured.append(statement)

    sync_engine = pg_engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _before_cursor_execute)
    try:
        yield captured
    finally:
        event.remove(sync_engine, "before_cursor_execute", _before_cursor_execute)


@pytest.mark.asyncio
async def test_schema_accepts_structurally_valid_expired_lease(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle = await _stored_handle(session_factory, spool)
    await store.acquire(handle.reference)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_EXPIRE_LEASE_BY_DIGEST_SQL),
                {"digest": handle.reference.digest()},
            )
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == handle.reference.digest()
            )
        )
        pg_now = await session.scalar(select(func.statement_timestamp()))
    assert row is not None
    assert row.state == "LEASED"
    assert row.leased_at is not None
    assert row.lease_expires_at is not None
    assert row.lease_expires_at > row.leased_at
    assert row.lease_expires_at <= pg_now


@pytest.mark.asyncio
async def test_schema_rejects_lease_expires_at_not_after_leased_at(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    _store_obj, handle = await _stored_handle(session_factory, spool)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE attachment_spool_objects
                    SET
                        state = 'LEASED',
                        lease_token_digest = :digest,
                        leased_at = statement_timestamp(),
                        lease_expires_at = statement_timestamp() + interval '5 minutes'
                    WHERE reference_digest = :ref_digest
                    """
                ),
                {
                    "digest": secrets.token_bytes(32),
                    "ref_digest": handle.reference.digest(),
                },
            )
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    text(_INVALID_LEASE_ORDER_SQL),
                    {"digest": handle.reference.digest()},
                )


@pytest.mark.asyncio
async def test_acquire_uses_statement_timestamp_not_wall_clock(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    sql_statement_probe: list[str],
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle = await _stored_handle(session_factory, spool)
    sql_statement_probe.clear()
    lease = await store.acquire(handle.reference)
    acquire_statements = " ".join(sql_statement_probe).lower()
    assert "statement_timestamp" in acquire_statements
    assert "make_interval" in acquire_statements
    assert handle.reference.to_token() not in acquire_statements

    async with session_factory() as session:
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == handle.reference.digest()
            )
        )
        pg_now = await session.scalar(select(func.statement_timestamp()))
    assert row is not None
    assert row.leased_at is not None
    assert row.lease_expires_at is not None
    assert row.leased_at.tzinfo is not None
    assert row.lease_expires_at.tzinfo is not None
    delta = row.lease_expires_at - row.leased_at
    assert delta == timedelta(seconds=LEASE_TTL_SECONDS)
    assert lease.lease_expires_at == row.lease_expires_at
    leased_age = abs((pg_now - row.leased_at).total_seconds())
    assert leased_age < 5


@pytest.mark.asyncio
async def test_transaction_rollback_restores_state(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle = await _stored_handle(session_factory, spool)
    original_apply = __import__(
        "app.repositories.attachment_spool", fromlist=["apply_lease"]
    ).apply_lease

    async def _fail_apply(session: AsyncSession, **kwargs: Any) -> Any:
        row = await original_apply(session, **kwargs)
        raise RuntimeError("synthetic rollback")

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.apply_lease",
        _fail_apply,
    )
    with pytest.raises(AttachmentError):
        await store.acquire(handle.reference)
    async with session_factory() as session:
        state = await session.scalar(
            select(AttachmentSpoolObject.state).where(
                AttachmentSpoolObject.reference_digest == handle.reference.digest()
            )
        )
    assert state == "STORED"


@pytest.mark.asyncio
@pytest.mark.no_foundation_row_cleanup
async def test_lease_migration_upgrade_from_stage_1a1(
    pg_database_url: SecretDatabaseUrl,
    pg_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await pg_engine.dispose()
    try:
        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="downgrade",
            revision=_PRE_LEASE_REVISION,
            database_url=pg_database_url,
        )
        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="upgrade",
            revision="head",
            database_url=pg_database_url,
        )
        async with session_factory() as session:
            cols = await session.scalar(
                text(
                    """
                    SELECT COUNT(*)
                    FROM information_schema.columns
                    WHERE table_name = 'attachment_spool_objects'
                      AND column_name IN (
                        'lease_token_digest', 'leased_at', 'lease_expires_at'
                      )
                    """
                )
            )
        assert cols == 3
    finally:
        await pg_engine.dispose()
        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="upgrade",
            revision="head",
            database_url=pg_database_url,
        )
        await pg_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.no_foundation_row_cleanup
async def test_lease_migration_downgrade_safe_when_no_live_rows(
    pg_database_url: SecretDatabaseUrl,
    pg_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    _store_obj, handle = await _stored_handle(session_factory, spool)
    reference_digest = handle.reference.digest()

    await pg_engine.dispose()
    try:
        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="downgrade",
            revision=_PRE_LEASE_REVISION,
            database_url=pg_database_url,
        )
        await _assert_stage_1a1_schema(session_factory)
        await _assert_stage_1a1_rejects_expanded_states(
            session_factory,
            reference_digest=reference_digest,
        )
    finally:
        await pg_engine.dispose()
        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="upgrade",
            revision="head",
            database_url=pg_database_url,
        )
        await pg_engine.dispose()
        await _delete_row_by_reference_digest_sql(
            session_factory,
            reference_digest=reference_digest,
        )


@pytest.mark.asyncio
@pytest.mark.no_foundation_row_cleanup
async def test_lease_migration_downgrade_refuses_leased_rows(
    pg_database_url: SecretDatabaseUrl,
    pg_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle = await _stored_handle(session_factory, spool)
    await store.acquire(handle.reference)
    await pg_engine.dispose()
    try:
        with pytest.raises(RuntimeError):
            await run_alembic_command_async(
                alembic_ini=_ALEMBIC_INI,
                command_name="downgrade",
                revision=_PRE_LEASE_REVISION,
                database_url=pg_database_url,
            )
    finally:
        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="upgrade",
            revision="head",
            database_url=pg_database_url,
        )
        await pg_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.no_foundation_row_cleanup
async def test_lease_migration_downgrade_refuses_delete_pending_rows(
    pg_database_url: SecretDatabaseUrl,
    pg_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    _store_obj, handle = await _stored_handle(session_factory, spool)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE attachment_spool_objects
                    SET state = 'DELETE_PENDING'
                    WHERE reference_digest = :digest
                    """
                ),
                {"digest": handle.reference.digest()},
            )
    await pg_engine.dispose()
    try:
        with pytest.raises(RuntimeError):
            await run_alembic_command_async(
                alembic_ini=_ALEMBIC_INI,
                command_name="downgrade",
                revision=_PRE_LEASE_REVISION,
                database_url=pg_database_url,
            )
    finally:
        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="upgrade",
            revision="head",
            database_url=pg_database_url,
        )
        await pg_engine.dispose()


@pytest.mark.asyncio
async def test_lease_ttl_matches_policy_constant(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    store, handle = await _stored_handle(session_factory, spool)
    lease = await store.acquire(handle.reference)
    async with session_factory() as session:
        row = await session.scalar(
            select(AttachmentSpoolObject).where(
                AttachmentSpoolObject.reference_digest == handle.reference.digest()
            )
        )
    assert row is not None
    assert row.leased_at is not None
    assert row.lease_expires_at is not None
    delta = row.lease_expires_at - row.leased_at
    assert abs(delta.total_seconds() - LEASE_TTL_SECONDS) < 2
    assert lease.lease_expires_at == row.lease_expires_at
