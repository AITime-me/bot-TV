"""PostgreSQL integration tests for encrypted ephemeral PII store."""

from __future__ import annotations

import asyncio
import base64
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.ephemeral_pii_types import (
    EphemeralPiiError,
    EphemeralPiiKind,
    EphemeralPiiPurpose,
    EphemeralPiiReference,
    EphemeralPiiTtlPolicy,
)
from app.core.ephemeral_pii_keys import EnvEphemeralPiiKeyProvider
from app.db.session import session_scope
from app.models.ephemeral_pii import EphemeralPiiValue
from app.services.ephemeral_pii_store import EphemeralPiiStore
from tests.foundation_test_db import SecretDatabaseUrl, run_alembic_command_async
from tests.pg_harness import truncate_foundation_tables

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_PRE_EPHEMERAL_REVISION = "20260729_13_handoff_quarantine"

_SYNTHETIC_PLAINTEXT = "SYNTHETIC_PHONE_VALUE_FOR_TEST_ONLY"
_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
_TTL_SECONDS = 900


def _store(
    session_factory: async_sessionmaker[AsyncSession],
) -> EphemeralPiiStore:
    return EphemeralPiiStore(
        session_factory=session_factory,
        key_provider=EnvEphemeralPiiKeyProvider(
            {
                "EPHEMERAL_PII_ACTIVE_KEY_ID": "TESTK1",
                "EPHEMERAL_PII_KEY_TESTK1": _KEY_B64,
            }
        ),
        ttl_policy=EphemeralPiiTtlPolicy(_TTL_SECONDS),
    )


@pytest_asyncio.fixture(autouse=True)
async def ephemeral_pii_row_cleanup(
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
async def test_migration_creates_ephemeral_pii_table(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        exists = await session.scalar(
            text("SELECT to_regclass('public.ephemeral_pii_values') IS NOT NULL")
        )
    assert exists is True


@pytest.mark.asyncio
async def test_store_persists_ciphertext_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    token = handle.reference.to_token()
    digest = handle.reference.digest()

    async with session_factory() as session:
        row = await session.scalar(
            select(EphemeralPiiValue).where(
                EphemeralPiiValue.reference_digest == digest
            )
        )
        assert row is not None
        assert row.conversation_id == conversation_id
        assert row.pii_kind == "PHONE"
        assert row.allowed_purpose == "BOOKING_PHONE_WRITE"
        assert len(row.reference_digest) == 32
        assert len(row.nonce) == 12
        assert len(row.ciphertext) >= 16
        assert row.crypto_version == 1
        assert row.expires_at > row.created_at
        delta = row.expires_at - row.created_at
        assert 890 <= delta.total_seconds() <= 910
        assert _SYNTHETIC_PLAINTEXT not in repr(row)
        assert token not in repr(row)
        columns = {column.name for column in EphemeralPiiValue.__table__.columns}
        assert "plaintext" not in columns
        assert "raw_reference" not in columns
        assert "reference_token" not in columns


@pytest.mark.asyncio
async def test_consume_once_deletes_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    plaintext = await store.consume_once(
        handle.reference,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    assert plaintext == _SYNTHETIC_PLAINTEXT

    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(EphemeralPiiValue)
            .where(
                EphemeralPiiValue.reference_digest == handle.reference.digest()
            )
        )
    assert count == 0

    with pytest.raises(EphemeralPiiError) as raised:
        await store.consume_once(
            handle.reference,
            conversation_id=conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    assert raised.value.code == "EPHEMERAL_PII_ACCESS_DENIED"


@pytest.mark.asyncio
async def test_consume_rejects_wrong_binding(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    with pytest.raises(EphemeralPiiError) as raised:
        await store.consume_once(
            handle.reference,
            conversation_id=uuid4(),
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    assert raised.value.code == "EPHEMERAL_PII_ACCESS_DENIED"


@pytest.mark.asyncio
async def test_expired_row_cannot_be_consumed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE ephemeral_pii_values
                    SET
                        created_at = statement_timestamp() - interval '2 hours',
                        expires_at = statement_timestamp() - interval '1 second'
                    WHERE reference_digest = :digest
                    """
                ),
                {"digest": handle.reference.digest()},
            )

    with pytest.raises(EphemeralPiiError) as raised:
        await store.consume_once(
            handle.reference,
            conversation_id=conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    assert raised.value.code == "EPHEMERAL_PII_ACCESS_DENIED"


@pytest.mark.asyncio
async def test_delete_requires_full_binding(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    await store.delete(
        handle.reference,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    with pytest.raises(EphemeralPiiError) as raised:
        await store.delete(
            handle.reference,
            conversation_id=conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    assert raised.value.code == "EPHEMERAL_PII_ACCESS_DENIED"


@pytest.mark.asyncio
async def test_purge_removes_only_expired_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE ephemeral_pii_values
                    SET
                        created_at = statement_timestamp() - interval '2 hours',
                        expires_at = statement_timestamp() - interval '1 second'
                    WHERE reference_digest = :digest
                    """
                ),
                {"digest": handle.reference.digest()},
            )

    deleted = await store.purge_expired(limit=100)
    assert deleted == 1

    async with session_factory() as session:
        remaining = await session.scalar(select(func.count()).select_from(EphemeralPiiValue))
    assert remaining == 0

    assert await store.purge_expired(limit=100) == 0


@pytest.mark.asyncio
async def test_concurrent_consume_allows_single_plaintext(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )

    async def _consume() -> str | EphemeralPiiError:
        local_store = _store(session_factory)
        try:
            return await local_store.consume_once(
                handle.reference,
                conversation_id=conversation_id,
                kind=EphemeralPiiKind.PHONE,
                purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
            )
        except EphemeralPiiError as exc:
            return exc

    first, second = await asyncio.gather(_consume(), _consume())
    successes = [item for item in (first, second) if item == _SYNTHETIC_PLAINTEXT]
    denials = [
        item
        for item in (first, second)
        if isinstance(item, EphemeralPiiError)
        and item.code == "EPHEMERAL_PII_ACCESS_DENIED"
    ]
    assert len(successes) == 1
    assert len(denials) == 1

    async with session_factory() as session:
        remaining = await session.scalar(
            select(func.count())
            .select_from(EphemeralPiiValue)
            .where(
                EphemeralPiiValue.reference_digest == handle.reference.digest()
            )
        )
    assert remaining == 0


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
        tobytes = getattr(value, "tobytes", None)
        if tobytes is not None and callable(tobytes) and type(value) is not bytes:
            try:
                _visit(tobytes())
                return
            except Exception:
                pass
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


def _assert_no_sensitive_sql_params(
    captured: list[object],
    statement_numbers: list[int],
    *,
    plaintext: str,
    token: str,
) -> None:
    plaintext_utf8 = plaintext.encode("utf-8")
    token_ascii = token.encode("ascii")
    for index, value in enumerate(captured):
        stmt_num = statement_numbers[index] if index < len(statement_numbers) else 0
        if value in (plaintext, plaintext_utf8):
            raise AssertionError(
                f"synthetic plaintext in SQL parameters at statement {stmt_num}"
            )
        if value in (token, token_ascii):
            raise AssertionError(
                f"raw reference token in SQL parameters at statement {stmt_num}"
            )
        if isinstance(value, str):
            if plaintext in value:
                raise AssertionError(
                    f"synthetic plaintext substring in SQL str at statement {stmt_num}"
                )
            if token in value:
                raise AssertionError(
                    f"raw reference token substring in SQL str at statement {stmt_num}"
                )
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            if plaintext_utf8 in raw:
                raise AssertionError(
                    f"synthetic plaintext bytes in SQL parameters at statement {stmt_num}"
                )
            if token_ascii in raw:
                raise AssertionError(
                    f"raw reference token bytes in SQL parameters at statement {stmt_num}"
                )


@pytest.mark.asyncio
async def test_store_sql_parameters_exclude_plaintext_and_token(
    session_factory: async_sessionmaker[AsyncSession],
    sql_param_probe: tuple[list[object], list[int]],
) -> None:
    captured, statement_numbers = sql_param_probe
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    token = handle.reference.to_token()
    _assert_no_sensitive_sql_params(
        captured,
        statement_numbers,
        plaintext=_SYNTHETIC_PLAINTEXT,
        token=token,
    )


@pytest.mark.asyncio
async def test_consume_denies_at_exact_expiry_boundary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE ephemeral_pii_values
                    SET
                        created_at = statement_timestamp() - interval '1 hour',
                        expires_at = statement_timestamp()
                    WHERE reference_digest = :digest
                    """
                ),
                {"digest": handle.reference.digest()},
            )
    with pytest.raises(EphemeralPiiError) as raised:
        await store.consume_once(
            handle.reference,
            conversation_id=conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    assert raised.value.code == "EPHEMERAL_PII_ACCESS_DENIED"


@pytest.mark.asyncio
async def test_consume_denies_one_millisecond_past_expiry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE ephemeral_pii_values
                    SET
                        created_at = statement_timestamp() - interval '1 hour',
                        expires_at = statement_timestamp() - interval '1 millisecond'
                    WHERE reference_digest = :digest
                    """
                ),
                {"digest": handle.reference.digest()},
            )
    with pytest.raises(EphemeralPiiError) as raised:
        await store.consume_once(
            handle.reference,
            conversation_id=conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    assert raised.value.code == "EPHEMERAL_PII_ACCESS_DENIED"


@pytest.mark.asyncio
async def test_consume_allows_five_second_future_expiry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE ephemeral_pii_values
                    SET
                        created_at = statement_timestamp(),
                        expires_at = statement_timestamp() + interval '5 seconds'
                    WHERE reference_digest = :digest
                    """
                ),
                {"digest": handle.reference.digest()},
            )
    plaintext = await store.consume_once(
        handle.reference,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    assert plaintext == _SYNTHETIC_PLAINTEXT


@pytest.mark.asyncio
async def test_schema_rejects_invalid_reference_digest_length(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO ephemeral_pii_values (
                            id, reference_digest, conversation_id, pii_kind,
                            allowed_purpose, ciphertext, nonce, key_id,
                            crypto_version, created_at, expires_at
                        ) VALUES (
                            :id, :digest, :conversation_id, 'PHONE',
                            'BOOKING_PHONE_WRITE', :ciphertext, :nonce, 'TESTK1',
                            1, statement_timestamp(),
                            statement_timestamp() + interval '5 seconds'
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "digest": b"short",
                        "conversation_id": uuid4(),
                        "ciphertext": b"x" * 16,
                        "nonce": b"y" * 12,
                    },
                )


@pytest.mark.asyncio
async def test_tampered_ciphertext_denied_and_row_persists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    digest = handle.reference.digest()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE ephemeral_pii_values
                    SET ciphertext = decode(repeat('ab', 16), 'hex')
                    WHERE reference_digest = :digest
                    """
                ),
                {"digest": digest},
            )
    with pytest.raises(EphemeralPiiError) as raised:
        await store.consume_once(
            handle.reference,
            conversation_id=conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    assert raised.value.code == "EPHEMERAL_PII_ACCESS_DENIED"
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(EphemeralPiiValue)
            .where(EphemeralPiiValue.reference_digest == digest)
        )
    assert count == 1


@pytest.mark.asyncio
async def test_wrong_purpose_binding_denied(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    with pytest.raises(EphemeralPiiError) as raised:
        await store.consume_once(
            handle.reference,
            conversation_id=conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.AMOCRM_CONTACT_SYNC,
        )
    assert raised.value.code == "EPHEMERAL_PII_ACCESS_DENIED"


@pytest.mark.asyncio
async def test_expired_consume_vs_purge_no_plaintext(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE ephemeral_pii_values
                    SET
                        created_at = statement_timestamp() - interval '2 hours',
                        expires_at = statement_timestamp() - interval '1 second'
                    WHERE reference_digest = :digest
                    """
                ),
                {"digest": handle.reference.digest()},
            )

    async def _consume() -> object:
        try:
            return await store.consume_once(
                handle.reference,
                conversation_id=conversation_id,
                kind=EphemeralPiiKind.PHONE,
                purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
            )
        except EphemeralPiiError as exc:
            return exc

    consume_result, purge_count = await asyncio.gather(
        _consume(),
        store.purge_expired(limit=10),
    )
    assert isinstance(consume_result, EphemeralPiiError)
    assert consume_result.code == "EPHEMERAL_PII_ACCESS_DENIED"
    assert purge_count in (0, 1)
    assert _SYNTHETIC_PLAINTEXT not in (consume_result,)


@pytest.mark.asyncio
async def test_purge_skips_unexpired_row_locked_by_other_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    digest = handle.reference.digest()
    lock_acquired = asyncio.Event()
    release_lock = asyncio.Event()

    async def _hold_lock() -> None:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        SELECT id FROM ephemeral_pii_values
                        WHERE reference_digest = :digest
                        FOR UPDATE
                        """
                    ),
                    {"digest": digest},
                )
                lock_acquired.set()
                await release_lock.wait()

    holder = asyncio.create_task(_hold_lock())
    await lock_acquired.wait()
    deleted = await store.purge_expired(limit=10)
    release_lock.set()
    await holder
    assert deleted == 0


@pytest.mark.asyncio
async def test_orm_repr_safe_loaded_and_transient() -> None:
    loaded = EphemeralPiiValue()
    rendered = f"{loaded!r}"
    assert "ciphertext=<redacted>" in rendered
    assert "nonce=<redacted>" in rendered
    assert "reference_digest=<redacted>" in rendered
    assert "key_id=<redacted>" in rendered


@pytest.mark.asyncio
async def test_consume_denies_after_expiry_during_lock_wait(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Post-lock expiry check must deny consume after TTL during FOR UPDATE wait."""
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    digest = handle.reference.digest()
    lock_acquired = asyncio.Event()
    consume_started = asyncio.Event()
    release_lock = asyncio.Event()

    async def _hold_lock() -> None:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        SELECT id FROM ephemeral_pii_values
                        WHERE reference_digest = :digest
                        FOR UPDATE
                        """
                    ),
                    {"digest": digest},
                )
                await session.execute(
                    text(
                        """
                        UPDATE ephemeral_pii_values
                        SET
                            created_at = statement_timestamp(),
                            expires_at = statement_timestamp()
                                + interval '500 milliseconds'
                        WHERE reference_digest = :digest
                        """
                    ),
                    {"digest": digest},
                )
                lock_acquired.set()
                await release_lock.wait()

    async def _consume() -> EphemeralPiiError | str:
        consume_started.set()
        try:
            return await store.consume_once(
                handle.reference,
                conversation_id=conversation_id,
                kind=EphemeralPiiKind.PHONE,
                purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
            )
        except EphemeralPiiError as exc:
            return exc

    holder = asyncio.create_task(_hold_lock())
    await lock_acquired.wait()
    consume_task = asyncio.create_task(_consume())
    await consume_started.wait()
    assert not consume_task.done()

    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("SELECT pg_sleep(0.7)"))

    release_lock.set()
    consume_result = await consume_task
    await holder

    assert isinstance(consume_result, EphemeralPiiError)
    assert consume_result.code == "EPHEMERAL_PII_ACCESS_DENIED"
    assert consume_result is not _SYNTHETIC_PLAINTEXT

    async with session_factory() as session:
        remaining = await session.scalar(
            select(func.count())
            .select_from(EphemeralPiiValue)
            .where(EphemeralPiiValue.reference_digest == digest)
        )
    assert remaining == 1

    purged = await store.purge_expired(limit=10)
    assert purged == 1
    async with session_factory() as session:
        final_count = await session.scalar(
            select(func.count()).select_from(EphemeralPiiValue)
        )
    assert final_count == 0


@pytest.mark.asyncio
async def test_tampered_nonce_denied_and_row_persists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    digest = handle.reference.digest()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE ephemeral_pii_values
                    SET nonce = decode(repeat('cd', 12), 'hex')
                    WHERE reference_digest = :digest
                    """
                ),
                {"digest": digest},
            )
    with pytest.raises(EphemeralPiiError) as raised:
        await store.consume_once(
            handle.reference,
            conversation_id=conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    assert raised.value.code == "EPHEMERAL_PII_ACCESS_DENIED"
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(EphemeralPiiValue)
            .where(EphemeralPiiValue.reference_digest == digest)
        )
    assert count == 1


@pytest.mark.asyncio
async def test_missing_key_id_denied_and_row_persists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    digest = handle.reference.digest()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE ephemeral_pii_values
                    SET key_id = 'MISSINGK2'
                    WHERE reference_digest = :digest
                    """
                ),
                {"digest": digest},
            )
    with pytest.raises(EphemeralPiiError) as raised:
        await store.consume_once(
            handle.reference,
            conversation_id=conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    assert raised.value.code == "EPHEMERAL_PII_ACCESS_DENIED"
    assert "MISSINGK2" not in str(raised.value)
    assert "MISSINGK2" not in repr(raised.value)
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(EphemeralPiiValue)
            .where(EphemeralPiiValue.reference_digest == digest)
        )
    assert count == 1


async def _expire_row(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    digest: bytes,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE ephemeral_pii_values
                    SET
                        created_at = statement_timestamp() - interval '2 hours',
                        expires_at = statement_timestamp() - interval '1 second'
                    WHERE reference_digest = :digest
                    """
                ),
                {"digest": digest},
            )


@pytest.mark.asyncio
async def test_concurrent_purge_counts_each_row_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = _store(session_factory)
    digests: list[bytes] = []
    for _ in range(5):
        handle = await store.store(
            _SYNTHETIC_PLAINTEXT,
            conversation_id=uuid4(),
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
        digests.append(handle.reference.digest())
        await _expire_row(session_factory, digest=digests[-1])

    async def _purge() -> int:
        return await _store(session_factory).purge_expired(limit=3)

    first, second = await asyncio.gather(_purge(), _purge())
    assert first + second == 5
    assert first <= 3
    assert second <= 3

    async with session_factory() as session:
        remaining = await session.scalar(select(func.count()).select_from(EphemeralPiiValue))
    assert remaining == 0


@pytest.mark.asyncio
async def test_rollback_before_commit_leaves_row_recoverable_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.repositories import ephemeral_pii as ephemeral_pii_repo

    conversation_id = uuid4()
    store = _store(session_factory)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    digest = handle.reference.digest()

    async with session_factory() as session:
        trans = await session.begin()
        row = await ephemeral_pii_repo.select_for_consume(
            session,
            reference_digest=digest,
        )
        assert row is not None
        await ephemeral_pii_repo.delete_locked_row(session, row_id=row.id)
        await trans.rollback()

    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(EphemeralPiiValue)
            .where(EphemeralPiiValue.reference_digest == digest)
        )
    assert count == 1

    plaintext = await store.consume_once(
        handle.reference,
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    assert plaintext == _SYNTHETIC_PLAINTEXT

    async with session_factory() as session:
        remaining = await session.scalar(
            select(func.count())
            .select_from(EphemeralPiiValue)
            .where(EphemeralPiiValue.reference_digest == digest)
        )
    assert remaining == 0


@pytest.mark.asyncio
@pytest.mark.no_foundation_row_cleanup
async def test_ephemeral_pii_migration_downgrade_reupgrade(
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
            revision=_PRE_EPHEMERAL_REVISION,
            database_url=pg_database_url,
        )
        async with session_factory() as session:
            exists = await session.scalar(
                text("SELECT to_regclass('public.ephemeral_pii_values') IS NOT NULL")
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
                text("SELECT to_regclass('public.ephemeral_pii_values') IS NOT NULL")
            )
        assert exists is True

        async with pg_engine.connect() as connection:

            def _compare(sync_conn: Any) -> list[Any]:
                context = MigrationContext.configure(sync_conn)
                return compare_metadata(context, Base.metadata)

            drift = await connection.run_sync(_compare)
        assert drift == []

        ephemeral_checks = {
            c.name
            for table in Base.metadata.tables.values()
            if table.name == "ephemeral_pii_values"
            for c in table.constraints
            if isinstance(c, CheckConstraint) and c.name
        }
        ephemeral_uniques = {
            c.name
            for table in Base.metadata.tables.values()
            if table.name == "ephemeral_pii_values"
            for c in table.constraints
            if isinstance(c, UniqueConstraint) and c.name
        }
        assert "ck_ephemeral_pii_values_reference_digest_len" in ephemeral_checks
        assert "uq_ephemeral_pii_values_reference_digest" in ephemeral_uniques
    finally:
        await pg_engine.dispose()
        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="upgrade",
            revision="head",
            database_url=pg_database_url,
        )
        await pg_engine.dispose()
