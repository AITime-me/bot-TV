"""Unit orchestration tests for attachment spool lease Stage 1A2A."""

from __future__ import annotations

import secrets
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.attachment_types import (
    LEASE_TOKEN_LENGTH,
    LEASE_TOKEN_RAW_BYTES,
    LEASE_TTL_SECONDS,
    MAX_LEASE_TOKEN_COLLISION_RETRIES,
    REFERENCE_DIGEST_BYTES,
    AttachmentError,
    AttachmentKind,
    AttachmentLeaseHandle,
    AttachmentLeaseReclaimResult,
    AttachmentLeaseToken,
    AttachmentPurpose,
    AttachmentReference,
    AttachmentSpoolPolicy,
)
from app.repositories.attachment_spool import AttachmentSpoolRow
from app.services.attachment_spool_store import (
    AttachmentSpoolStore,
    _is_lease_digest_unique_collision,
    _structured_pg_violation_fields,
)
from tests.attachment_spool_fakes import TxnTracker, make_observing_session_scope

_UTC = timezone.utc
_NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=_UTC)
_FUTURE = _NOW + timedelta(hours=1)
_PAST = _NOW - timedelta(hours=1)


async def _pg_now(*_a: Any, **_k: Any) -> datetime:
    return _NOW


async def _return_row(row: AttachmentSpoolRow) -> AttachmentSpoolRow:
    return row


def _assert_safe_error(exc: AttachmentError, code: str, *forbidden: str) -> None:
    assert exc.code == code
    assert str(exc) == code
    assert exc.__cause__ is None
    blob = str(exc) + repr(exc) + "".join(traceback.format_exception(exc))
    for item in forbidden:
        if item:
            assert item not in blob


def _store(root: Any) -> AttachmentSpoolStore:
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return AttachmentSpoolStore(
        session_factory=object(),  # type: ignore[arg-type]
        key_provider=object(),  # type: ignore[arg-type]
        policy=AttachmentSpoolPolicy(root, 900),
    )


def _row(
    *,
    state: str = "STORED",
    expires_at: datetime | None = _FUTURE,
    lease_token_digest: bytes | None = None,
    leased_at: datetime | None = None,
    lease_expires_at: datetime | None = None,
) -> AttachmentSpoolRow:
    return AttachmentSpoolRow(
        id=uuid4(),
        object_id=uuid4(),
        conversation_id=uuid4(),
        kind="IMAGE",
        purpose="INBOUND_ATTACHMENT_RELAY",
        detected_mime="image/jpeg",
        plaintext_size=32,
        ciphertext_size=48,
        ciphertext_sha256=secrets.token_bytes(32),
        nonce=secrets.token_bytes(12),
        key_id="ATTK1",
        crypto_version=1,
        state=state,
        reference_digest=secrets.token_bytes(32),
        expires_at=expires_at,
        lease_token_digest=lease_token_digest,
        leased_at=leased_at,
        lease_expires_at=lease_expires_at,
    )


def test_lease_token_generate_parse_digest() -> None:
    token = AttachmentLeaseToken.generate()
    rendered = token.to_token()
    assert len(rendered) == LEASE_TOKEN_LENGTH
    assert rendered.endswith("=")
    assert "+" not in rendered
    assert "/" not in rendered
    assert AttachmentLeaseToken.parse(rendered) == token
    digest = token.digest()
    assert type(digest) is bytes
    assert len(digest) == REFERENCE_DIGEST_BYTES
    assert len(secrets.token_bytes(LEASE_TOKEN_RAW_BYTES)) == LEASE_TOKEN_RAW_BYTES
    blob = f"{token!r}{token!s}{token}"
    assert blob == "AttachmentLeaseToken(<redacted>)" * 3
    assert rendered not in blob


def test_lease_token_rejects_invalid() -> None:
    token = AttachmentLeaseToken.generate().to_token()
    for bad in ("", "A" * 43, token + "A", object(), b"bytes"):
        with pytest.raises(AttachmentError) as raised:
            AttachmentLeaseToken.parse(bad)  # type: ignore[arg-type]
        _assert_safe_error(raised.value, "ATTACHMENT_LEASE_TOKEN_INVALID")


def test_lease_handle_redacted_and_fields_only() -> None:
    token = AttachmentLeaseToken.generate()
    handle = AttachmentLeaseHandle(token=token, lease_expires_at=_FUTURE)
    rendered = f"{handle!r}{handle!s}{handle}"
    assert "token=<redacted>" in rendered
    assert "lease_expires_at=<redacted>" in rendered
    assert token.to_token() not in rendered
    assert str(_FUTURE) not in rendered
    assert not hasattr(handle, "lease_generation")


def test_lease_ttl_and_collision_constants() -> None:
    assert LEASE_TTL_SECONDS == 300
    assert MAX_LEASE_TOKEN_COLLISION_RETRIES == 3


def test_reclaim_result_numeric_only() -> None:
    result = AttachmentLeaseReclaimResult(reclaimed=2, skipped=1)
    rendered = repr(result)
    assert "reclaimed=2" in rendered
    assert "skipped=1" in rendered
    assert "uuid" not in rendered.lower()
    assert "digest" not in rendered.lower()


@pytest.mark.asyncio
async def test_acquire_stored_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    reference = AttachmentReference.generate()
    row = _row(state="STORED", expires_at=_FUTURE)
    tracker = TxnTracker()

    async def _lock(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        return row

    async def _now(*_a: Any, **_k: Any) -> datetime:
        return _NOW

    async def _apply(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        return _row(
            state="LEASED",
            expires_at=_FUTURE,
            lease_token_digest=_k["lease_token_digest"],
            leased_at=_NOW,
            lease_expires_at=_NOW + timedelta(seconds=LEASE_TTL_SECONDS),
        )

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_reference_digest",
        _lock,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.apply_lease",
        _apply,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )
    handle = await _store(tmp_path / "spool").acquire(reference)
    assert isinstance(handle, AttachmentLeaseHandle)
    assert tracker.events.count("commit") == 1


@pytest.mark.parametrize(
    ("state", "expires_at", "lease_expires_at"),
    [
        ("LEASED", _FUTURE, _FUTURE),
        ("STORED", _PAST, None),
        ("WRITING", _FUTURE, None),
        ("DELETE_PENDING", _FUTURE, None),
    ],
)
@pytest.mark.asyncio
async def test_acquire_denied_states(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    state: str,
    expires_at: datetime,
    lease_expires_at: datetime | None,
) -> None:
    reference = AttachmentReference.generate()
    row = _row(
        state=state,
        expires_at=expires_at,
        lease_token_digest=secrets.token_bytes(32) if state == "LEASED" else None,
        leased_at=_NOW if state == "LEASED" else None,
        lease_expires_at=lease_expires_at,
    )

    async def _lock(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        return row

    async def _now(*_a: Any, **_k: Any) -> datetime:
        return _NOW

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_reference_digest",
        _lock,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").acquire(reference)
    _assert_safe_error(
        raised.value,
        "ATTACHMENT_ACCESS_DENIED",
        reference.to_token(),
        str(reference.digest()),
    )


@pytest.mark.asyncio
async def test_acquire_reclaims_expired_lease_atomically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    reference = AttachmentReference.generate()
    row = _row(
        state="LEASED",
        expires_at=_FUTURE,
        lease_token_digest=secrets.token_bytes(32),
        leased_at=_NOW - timedelta(seconds=400),
        lease_expires_at=_PAST,
    )
    events: list[str] = []

    async def _lock(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        return row

    async def _now(*_a: Any, **_k: Any) -> datetime:
        return _NOW

    async def _clear(*_a: Any, **_k: Any) -> bool:
        events.append("clear")
        return True

    async def _reload(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        events.append("reload")
        return _row(state="STORED", expires_at=_FUTURE)

    async def _apply(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        events.append("apply")
        return _row(
            state="LEASED",
            expires_at=_FUTURE,
            lease_token_digest=_k["lease_token_digest"],
            leased_at=_NOW,
            lease_expires_at=_NOW + timedelta(seconds=LEASE_TTL_SECONDS),
        )

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_reference_digest",
        _lock,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.clear_lease_to_stored",
        _clear,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        _reload,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.apply_lease",
        _apply,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    await _store(tmp_path / "spool").acquire(reference)
    assert events == ["clear", "reload", "apply"]


@pytest.mark.asyncio
async def test_acquire_generates_token_only_after_eligibility(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    reference = AttachmentReference.generate()
    row = _row(state="STORED", expires_at=_FUTURE)
    order: list[str] = []
    original_generate = AttachmentLeaseToken.generate

    def _tracked_generate() -> AttachmentLeaseToken:
        order.append("generate")
        return original_generate()

    async def _lock(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        order.append("lock")
        return row

    async def _now(*_a: Any, **_k: Any) -> datetime:
        order.append("now")
        return _NOW

    async def _apply(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        order.append("apply")
        return _row(
            state="LEASED",
            expires_at=_FUTURE,
            lease_token_digest=_k["lease_token_digest"],
            leased_at=_NOW,
            lease_expires_at=_NOW + timedelta(seconds=LEASE_TTL_SECONDS),
        )

    monkeypatch.setattr(AttachmentLeaseToken, "generate", _tracked_generate)
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_reference_digest",
        _lock,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.apply_lease",
        _apply,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    await _store(tmp_path / "spool").acquire(reference)
    assert order.index("lock") < order.index("now") < order.index("generate")


@pytest.mark.asyncio
async def test_acquire_returns_only_after_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    reference = AttachmentReference.generate()
    row = _row(state="STORED", expires_at=_FUTURE)
    tracker = TxnTracker()
    seen: list[str] = []

    async def _apply(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        return _row(
            state="LEASED",
            expires_at=_FUTURE,
            lease_token_digest=_k["lease_token_digest"],
            leased_at=_NOW,
            lease_expires_at=_NOW + timedelta(seconds=LEASE_TTL_SECONDS),
        )

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_reference_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.apply_lease",
        _apply,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )

    async def _run() -> None:
        handle = await _store(tmp_path / "spool").acquire(reference)
        seen.append("returned")
        assert handle.token.to_token() not in "".join(tracker.events)

    await _run()
    assert tracker.events.index("commit") < tracker.events.index("exit")
    assert seen == ["returned"]


@pytest.mark.asyncio
async def test_acquire_commit_failure_returns_no_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    reference = AttachmentReference.generate()
    row = _row(state="STORED", expires_at=_FUTURE)
    tracker = TxnTracker(fail_commit=True)

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_reference_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.apply_lease",
        lambda *_a, **_k: _row(
            state="LEASED",
            expires_at=_FUTURE,
            lease_token_digest=_k["lease_token_digest"],
            leased_at=_NOW,
            lease_expires_at=_NOW + timedelta(seconds=LEASE_TTL_SECONDS),
        ),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").acquire(reference)
    assert raised.value.code == "ATTACHMENT_STORE_FAILED"


def _collision_error() -> IntegrityError:
    class _Diag:
        constraint_name = "uq_attachment_spool_objects_lease_token_digest"

    class _Orig:
        pgcode = "23505"
        sqlstate = "23505"
        constraint_name = "uq_attachment_spool_objects_lease_token_digest"
        diag = _Diag()

    return IntegrityError("insert", {}, _Orig())


def _asyncpg_wrapped_collision_error() -> IntegrityError:
    """SQLAlchemy asyncpg adapter: sqlstate on wrapper, constraint on __cause__."""

    class _AsyncpgCause:
        sqlstate = "23505"
        constraint_name = "uq_attachment_spool_objects_lease_token_digest"

    class _AsyncpgWrapper:
        pgcode = "23505"
        sqlstate = "23505"
        constraint_name = None
        __cause__ = _AsyncpgCause()

    return IntegrityError("insert", {}, _AsyncpgWrapper())


@pytest.mark.parametrize(
    "exc,expected",
    [
        (_collision_error(), True),
        (_asyncpg_wrapped_collision_error(), True),
    ],
)
def test_is_lease_digest_unique_collision_true_shapes(
    exc: IntegrityError, expected: bool
) -> None:
    assert _is_lease_digest_unique_collision(exc) is expected


@pytest.mark.parametrize(
    "orig_factory",
    [
        lambda: type(
            "_Orig",
            (),
            {
                "pgcode": "23505",
                "sqlstate": "23505",
                "constraint_name": "uq_attachment_spool_objects_reference_digest",
            },
        )(),
        lambda: type(
            "_Orig",
            (),
            {
                "pgcode": "23514",
                "sqlstate": "23514",
                "constraint_name": "uq_attachment_spool_objects_lease_token_digest",
            },
        )(),
        lambda: type(
            "_Orig",
            (),
            {"pgcode": "23505", "sqlstate": "23505", "constraint_name": None},
        )(),
        lambda: type(
            "_Orig",
            (),
            {
                "pgcode": "23505",
                "sqlstate": "23505",
                "constraint_name": None,
                "__str__": lambda self: (
                    "uq_attachment_spool_objects_lease_token_digest"
                ),
            },
        )(),
        lambda: type("_Orig", (), {"pgcode": "23503"})(),
    ],
)
def test_is_lease_digest_unique_collision_false_shapes(orig_factory: Any) -> None:
    exc = IntegrityError("insert", {}, orig_factory())
    assert _is_lease_digest_unique_collision(exc) is False


def test_structured_pg_violation_fields_asyncpg_chain() -> None:
    class _Cause:
        sqlstate = "23505"
        constraint_name = "uq_attachment_spool_objects_lease_token_digest"

    class _Wrapper:
        pgcode = "23505"
        sqlstate = "23505"
        __cause__ = _Cause()

    assert _structured_pg_violation_fields(_Wrapper()) == (
        "23505",
        "uq_attachment_spool_objects_lease_token_digest",
    )


@pytest.mark.asyncio
async def test_acquire_one_digest_collision_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    reference = AttachmentReference.generate()
    row = _row(state="STORED", expires_at=_FUTURE)

    class _Session:
        def __init__(self) -> None:
            self.flush_calls = 0

        def begin_nested(self) -> Any:
            return _Nested(self)

        async def flush(self) -> None:
            self.flush_calls += 1
            if self.flush_calls == 1:
                raise _collision_error()

    class _Nested:
        def __init__(self, parent: _Session) -> None:
            self._parent = parent

        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_a: Any) -> bool:
            return False

    session = _Session()

    async def _apply(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        return _row(
            state="LEASED",
            expires_at=_FUTURE,
            lease_token_digest=_k["lease_token_digest"],
            leased_at=_NOW,
            lease_expires_at=_NOW + timedelta(seconds=LEASE_TTL_SECONDS),
        )

    tracker = TxnTracker(session=session)
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_reference_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.apply_lease",
        _apply,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )
    handle = await _store(tmp_path / "spool").acquire(reference)
    assert isinstance(handle, AttachmentLeaseHandle)
    assert session.flush_calls == 2


@pytest.mark.asyncio
async def test_acquire_three_digest_collisions_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    reference = AttachmentReference.generate()
    row = _row(state="STORED", expires_at=_FUTURE)

    class _Session:
        def begin_nested(self) -> Any:
            return _Nested()

        async def flush(self) -> None:
            raise _collision_error()

    class _Nested:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_a: Any) -> bool:
            return False

    tracker = TxnTracker(session=_Session())
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_reference_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.apply_lease",
        lambda *_a, **_k: row,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").acquire(reference)
    assert raised.value.code == "ATTACHMENT_STORE_FAILED"


@pytest.mark.asyncio
async def test_acquire_unrelated_integrity_error_not_collision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    reference = AttachmentReference.generate()
    row = _row(state="STORED", expires_at=_FUTURE)

    class _Orig:
        pgcode = "23505"
        constraint_name = "uq_attachment_spool_objects_reference_digest"

    class _Session:
        def begin_nested(self) -> Any:
            return _Nested()

        async def flush(self) -> None:
            raise IntegrityError("insert", {}, _Orig())

    class _Nested:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_a: Any) -> bool:
            return False

    tracker = TxnTracker(session=_Session())
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_reference_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.apply_lease",
        lambda *_a, **_k: row,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").acquire(reference)
    assert raised.value.code == "ATTACHMENT_STORE_FAILED"


@pytest.mark.asyncio
async def test_release_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    token = AttachmentLeaseToken.generate()
    row = _row(
        state="LEASED",
        expires_at=_PAST,
        lease_token_digest=token.digest(),
        leased_at=_NOW - timedelta(seconds=60),
        lease_expires_at=_FUTURE,
    )
    fs_calls: list[str] = []

    async def _lock(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        return row

    async def _now(*_a: Any, **_k: Any) -> datetime:
        return _NOW

    async def _clear(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        _lock,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.clear_lease_to_stored",
        _clear,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.write_ciphertext_atomic",
        lambda *_a, **_k: fs_calls.append("fs"),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    await _store(tmp_path / "spool").release(token)
    assert fs_calls == []


@pytest.mark.parametrize(
    "row_factory",
    [
        lambda: None,
        lambda: _row(state="STORED", expires_at=_FUTURE),
        lambda: _row(
            state="LEASED",
            expires_at=_FUTURE,
            lease_token_digest=secrets.token_bytes(32),
            leased_at=_NOW,
            lease_expires_at=_PAST,
        ),
        lambda: _row(state="DELETE_PENDING", expires_at=_FUTURE),
    ],
)
@pytest.mark.asyncio
async def test_release_denied_cases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    row_factory: Any,
) -> None:
    token = AttachmentLeaseToken.generate()
    row = row_factory()

    async def _lock(*_a: Any, **_k: Any) -> AttachmentSpoolRow | None:
        return row

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        _lock,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").release(token)
    _assert_safe_error(
        raised.value,
        "ATTACHMENT_ACCESS_DENIED",
        token.to_token(),
        str(token.digest()),
    )


@pytest.mark.asyncio
async def test_release_repeat_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    token = AttachmentLeaseToken.generate()
    released = {"value": False}

    async def _lock(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        if released["value"]:
            return _row(state="STORED", expires_at=_FUTURE)
        return _row(
            state="LEASED",
            expires_at=_FUTURE,
            lease_token_digest=token.digest(),
            leased_at=_NOW,
            lease_expires_at=_FUTURE,
        )

    async def _clear(*_a: Any, **_k: Any) -> bool:
        released["value"] = True
        return True

    store = _store(tmp_path / "spool")
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        _lock,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.clear_lease_to_stored",
        _clear,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    await store.release(token)
    with pytest.raises(AttachmentError) as raised:
        await store.release(token)
    assert raised.value.code == "ATTACHMENT_ACCESS_DENIED"


@pytest.mark.asyncio
async def test_reclaim_limit_validation(tmp_path: Any) -> None:
    store = _store(tmp_path / "spool")
    for bad in (0, 1001, True):
        with pytest.raises(AttachmentError) as raised:
            await store.reclaim_expired_leases(limit=bad)  # type: ignore[arg-type]
        assert raised.value.code == "ATTACHMENT_POLICY_INVALID"


@pytest.mark.asyncio
async def test_reclaim_touches_only_expired_leased(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    expired = _row(
        state="LEASED",
        expires_at=_FUTURE,
        lease_token_digest=secrets.token_bytes(32),
        leased_at=_NOW - timedelta(seconds=400),
        lease_expires_at=_PAST,
    )
    active = _row(
        state="LEASED",
        expires_at=_FUTURE,
        lease_token_digest=secrets.token_bytes(32),
        leased_at=_NOW,
        lease_expires_at=_FUTURE,
    )
    cleared: list[UUID] = []

    async def _select(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [expired, active]

    async def _now(*_a: Any, **_k: Any) -> datetime:
        return _NOW

    async def _clear(*_a: Any, row_id: UUID, **_k: Any) -> bool:
        cleared.append(row_id)
        return row_id == expired.id

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_expired_leased_for_reclaim",
        _select,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.clear_lease_to_stored",
        _clear,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    result = await _store(tmp_path / "spool").reclaim_expired_leases(limit=10)
    assert result.reclaimed == 1
    assert result.skipped == 1
    assert cleared == [expired.id]
