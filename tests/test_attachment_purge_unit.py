"""Unit tests for attachment spool expiry purge Stage 1A2B3."""

from __future__ import annotations

import base64
import inspect
import re
import secrets
import traceback
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.attachment_keys import EnvAttachmentKeyProvider
from app.core.attachment_types import (
    MAX_PURGE_BATCH,
    AttachmentError,
    AttachmentPurgeResult,
    AttachmentSpoolPolicy,
)
from app.repositories import attachment_spool as spool_repo
from app.repositories.attachment_spool import AttachmentSpoolRow
from app.services.attachment_spool_store import (
    AttachmentSpoolStore,
    _DeletePendingFinalizeSnapshot,
)
from tests.attachment_spool_fakes import (
    TxnTracker,
    make_observing_session_scope,
    synthetic_minimal_jpeg,
)

_UTC = timezone.utc
_NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=_UTC)
_PAST = _NOW - timedelta(minutes=1)
_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
_JPEG = synthetic_minimal_jpeg()


def _key_env() -> dict[str, str]:
    return {
        "ATTACHMENT_SPOOL_ACTIVE_KEY_ID": "ATTK1",
        "ATTACHMENT_SPOOL_KEY_ATTK1": _KEY_B64,
    }


def _assert_safe_error(exc: AttachmentError, code: str, *forbidden: str) -> None:
    assert exc.code == code
    assert str(exc) == code
    assert exc.__cause__ is None
    blob = str(exc) + repr(exc) + "".join(traceback.format_exception(exc))
    for item in forbidden:
        if item:
            assert item not in blob


def _store(root: Path) -> AttachmentSpoolStore:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return AttachmentSpoolStore(
        session_factory=object(),  # type: ignore[arg-type]
        key_provider=EnvAttachmentKeyProvider(_key_env()),
        policy=AttachmentSpoolPolicy(root, 900),
    )


def _stored_row(**overrides: Any) -> AttachmentSpoolRow:
    return AttachmentSpoolRow(
        id=overrides.pop("id", uuid4()),
        object_id=overrides.pop("object_id", uuid4()),
        conversation_id=overrides.pop("conversation_id", uuid4()),
        kind="IMAGE",
        purpose="INBOUND_ATTACHMENT_RELAY",
        detected_mime="image/jpeg",
        plaintext_size=len(_JPEG),
        ciphertext_size=len(_JPEG) + 16,
        ciphertext_sha256=overrides.pop(
            "ciphertext_sha256", secrets.token_bytes(32)
        ),
        nonce=overrides.pop("nonce", secrets.token_bytes(12)),
        key_id="ATTK1",
        crypto_version=1,
        state=overrides.pop("state", "STORED"),
        reference_digest=overrides.pop(
            "reference_digest", secrets.token_bytes(32)
        ),
        expires_at=overrides.pop("expires_at", _PAST),
        lease_token_digest=overrides.pop("lease_token_digest", None),
        leased_at=overrides.pop("leased_at", None),
        lease_expires_at=overrides.pop("lease_expires_at", None),
        **overrides,
    )


def _leased_row(**overrides: Any) -> tuple[AttachmentSpoolRow, Any]:
    from app.core.attachment_types import AttachmentLeaseToken

    token = AttachmentLeaseToken.generate()
    row = _stored_row(
        state="LEASED",
        lease_token_digest=token.digest(),
        leased_at=_NOW - timedelta(minutes=10),
        lease_expires_at=_PAST,
        expires_at=_PAST,
        **overrides,
    )
    return row, token


def _dp_from_stored(row: AttachmentSpoolRow) -> AttachmentSpoolRow:
    return replace(row, state="DELETE_PENDING")


def _dp_from_leased(row: AttachmentSpoolRow) -> AttachmentSpoolRow:
    return replace(
        row,
        state="DELETE_PENDING",
        lease_token_digest=None,
        leased_at=None,
        lease_expires_at=None,
    )


def _update_sections(sql: str) -> tuple[str, str, str]:
    upper = sql.upper()
    set_idx = upper.index("SET")
    where_idx = upper.index("WHERE")
    returning_idx = upper.index("RETURNING")
    return (
        sql[set_idx:where_idx],
        sql[where_idx:returning_idx],
        sql[returning_idx:],
    )


def test_purge_expired_method_exists() -> None:
    assert hasattr(AttachmentSpoolStore, "purge_expired")
    assert not hasattr(AttachmentSpoolStore, "purge")
    sig = inspect.signature(AttachmentSpoolStore.purge_expired)
    params = list(sig.parameters)
    assert params == ["self", "limit"]
    assert sig.parameters["limit"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["limit"].default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_purge_limit_validation(tmp_path: Path) -> None:
    store = _store(tmp_path / "spool")
    with pytest.raises(TypeError):
        await store.purge_expired()  # type: ignore[call-arg]
    for bad in (True, False, 0, -1, MAX_PURGE_BATCH + 1, 1.5, "10", None):
        with pytest.raises(AttachmentError) as raised:
            await store.purge_expired(limit=bad)  # type: ignore[arg-type]
        _assert_safe_error(raised.value, "ATTACHMENT_POLICY_INVALID")


def test_purge_result_dto_validation() -> None:
    ok = AttachmentPurgeResult(
        transitioned_stored=0,
        transitioned_leased=0,
        deleted=0,
        unsafe_skipped=0,
        io_unavailable_skipped=0,
        skipped=0,
    )
    assert ok.deleted == 0
    for kwargs in (
        {"transitioned_stored": True},
        {"deleted": -1},
        {"skipped": False},
    ):
        base = {
            "transitioned_stored": 0,
            "transitioned_leased": 0,
            "deleted": 0,
            "unsafe_skipped": 0,
            "io_unavailable_skipped": 0,
            "skipped": 0,
        }
        base.update(kwargs)
        with pytest.raises(AttachmentError) as raised:
            AttachmentPurgeResult(**base)  # type: ignore[arg-type]
        assert raised.value.code == "ATTACHMENT_RECONCILE_FAILED"


@pytest.mark.asyncio
async def test_select_expired_for_purge_sql_predicates() -> None:
    captured: list[Any] = []
    commit_calls = 0

    class _Session:
        async def scalars(self, stmt: Any, *_a: Any, **_k: Any) -> Any:
            captured.append(stmt)

            class _Result:
                def all(self) -> list[Any]:
                    return []

            return _Result()

        async def commit(self) -> None:
            nonlocal commit_calls
            commit_calls += 1

    await spool_repo.select_expired_for_purge(_Session(), limit=10)
    assert commit_calls == 0
    compiled = captured[0].compile(
        dialect=__import__(
            "sqlalchemy.dialects.postgresql", fromlist=["dialect"]
        ).dialect()
    )
    sql = str(compiled).lower()
    params = {str(k).lower(): str(v).lower() for k, v in compiled.params.items()}
    param_values = " ".join(params.values())
    assert "statement_timestamp()" in sql
    assert "for update" in sql
    assert "skip locked" in sql
    assert "expires_at" in sql
    assert "limit" in sql
    assert "order by" in sql
    assert "writing" not in sql and "writing" not in param_values
    assert "delete_pending" not in sql and "delete_pending" not in param_values
    assert "stored" in param_values
    assert "leased" in param_values


@pytest.mark.asyncio
async def test_transition_expired_stored_sql() -> None:
    captured: list[Any] = []
    commit_calls = 0

    class _Session:
        async def execute(self, stmt: Any, *_a: Any, **_k: Any) -> Any:
            captured.append(stmt)

            class _Result:
                def scalar_one_or_none(self) -> None:
                    return None

            return _Result()

        async def commit(self) -> None:
            nonlocal commit_calls
            commit_calls += 1

    await spool_repo.transition_expired_stored_to_delete_pending(
        _Session(), row_id=uuid4()
    )
    assert commit_calls == 0
    compiled = captured[0].compile(
        dialect=__import__(
            "sqlalchemy.dialects.postgresql", fromlist=["dialect"]
        ).dialect()
    )
    sql = str(compiled)
    set_section, where_section, returning_section = _update_sections(sql)
    assert "statement_timestamp()" in set_section.lower()
    assert "lease_token_digest" not in set_section.lower()
    assert "expires_at" in where_section.lower()
    assert "statement_timestamp()" in where_section.lower()
    assert "returning" in returning_section.lower()


@pytest.mark.asyncio
async def test_transition_expired_leased_clears_lease_sql() -> None:
    captured: list[Any] = []

    class _Session:
        async def execute(self, stmt: Any, *_a: Any, **_k: Any) -> Any:
            captured.append(stmt)

            class _Result:
                def scalar_one_or_none(self) -> None:
                    return None

            return _Result()

    await spool_repo.transition_expired_leased_to_delete_pending(
        _Session(), row_id=uuid4()
    )
    compiled = captured[0].compile(
        dialect=__import__(
            "sqlalchemy.dialects.postgresql", fromlist=["dialect"]
        ).dialect()
    )
    sql = str(compiled)
    set_section, where_section, _returning = _update_sections(sql)
    set_l = set_section.lower()
    where_l = where_section.lower()
    assert "lease_token_digest" in set_l
    assert "leased_at" in set_l
    assert "lease_expires_at" in set_l
    assert "statement_timestamp()" in set_l
    assert "lease_expires_at" in where_l
    assert "expires_at" in where_l
    assert re.search(r"lease_expires_at\s+is\s+not\s+null", where_l)
    assert "statement_timestamp()" in where_l


async def _run_purge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    candidates: list[AttachmentSpoolRow],
    transitions: dict[UUID, AttachmentSpoolRow | None] | None = None,
    finalize_outcomes: list[str] | None = None,
    tracker: TxnTracker | None = None,
    select_error: Exception | None = None,
    transition_error: Exception | None = None,
    snapshot_error: Exception | None = None,
) -> tuple[
    AttachmentPurgeResult,
    list[_DeletePendingFinalizeSnapshot],
    TxnTracker,
]:
    tracker = tracker or TxnTracker()
    transitions = transitions or {}
    finalize_calls: list[_DeletePendingFinalizeSnapshot] = []
    outcomes = list(finalize_outcomes or [])

    async def _select(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        if select_error is not None:
            raise select_error
        return list(candidates)

    async def _tx_stored(
        *_a: Any, row_id: UUID, **_k: Any
    ) -> AttachmentSpoolRow | None:
        if transition_error is not None:
            raise transition_error
        return transitions.get(row_id)

    async def _tx_leased(
        *_a: Any, row_id: UUID, **_k: Any
    ) -> AttachmentSpoolRow | None:
        if transition_error is not None:
            raise transition_error
        return transitions.get(row_id)

    async def _finalize(
        _self: AttachmentSpoolStore, snapshot: _DeletePendingFinalizeSnapshot
    ) -> str:
        finalize_calls.append(snapshot)
        if not outcomes:
            return "deleted"
        return outcomes.pop(0)

    original_from_row = _DeletePendingFinalizeSnapshot.from_row

    @classmethod  # type: ignore[misc]
    def _from_row(
        cls: Any, row: AttachmentSpoolRow
    ) -> _DeletePendingFinalizeSnapshot:
        if snapshot_error is not None:
            raise snapshot_error
        return original_from_row(row)

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_expired_for_purge",
        _select,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo"
        ".transition_expired_stored_to_delete_pending",
        _tx_stored,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo"
        ".transition_expired_leased_to_delete_pending",
        _tx_leased,
    )
    monkeypatch.setattr(
        AttachmentSpoolStore,
        "_finalize_delete_pending",
        _finalize,
    )
    monkeypatch.setattr(
        _DeletePendingFinalizeSnapshot,
        "from_row",
        _from_row,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )
    result = await _store(tmp_path / "spool").purge_expired(limit=10)
    return result, finalize_calls, tracker


@pytest.mark.asyncio
async def test_purge_stored_and_leased_counters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    stored = _stored_row()
    leased, _token = _leased_row()
    result, calls, tracker = await _run_purge(
        monkeypatch,
        tmp_path,
        candidates=[stored, leased],
        transitions={
            stored.id: _dp_from_stored(stored),
            leased.id: _dp_from_leased(leased),
        },
        finalize_outcomes=["deleted", "deleted"],
    )
    assert result.transitioned_stored == 1
    assert result.transitioned_leased == 1
    assert result.deleted == 2
    assert result.skipped == 0
    assert calls[1].lease_token_digest is None
    assert calls[1].leased_at is None
    assert calls[1].lease_expires_at is None
    assert "commit" in tracker.events


@pytest.mark.asyncio
async def test_purge_commit_before_finalizer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    stored = _stored_row()
    order: list[str] = []

    async def _select(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        order.append("select")
        return [stored]

    async def _tx(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        order.append("transition")
        return _dp_from_stored(stored)

    async def _finalize(_self: AttachmentSpoolStore, _s: Any) -> str:
        order.append("finalize")
        return "deleted"

    tracker = TxnTracker()
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_expired_for_purge",
        _select,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo"
        ".transition_expired_stored_to_delete_pending",
        _tx,
    )
    monkeypatch.setattr(
        AttachmentSpoolStore,
        "_finalize_delete_pending",
        _finalize,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )
    await _store(tmp_path / "spool").purge_expired(limit=5)
    assert order == ["select", "transition", "finalize"]
    assert tracker.events == ["enter", "commit", "exit"]


@pytest.mark.asyncio
async def test_purge_transition_none_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    stored = _stored_row()
    result, calls, _tracker = await _run_purge(
        monkeypatch,
        tmp_path,
        candidates=[stored],
        transitions={stored.id: None},
    )
    assert result.transitioned_stored == 0
    assert result.skipped == 1
    assert result.deleted == 0
    assert calls == []


@pytest.mark.asyncio
async def test_purge_limit_passed_to_selector(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    seen: list[int] = []

    async def _select(*_a: Any, limit: int, **_k: Any) -> list[Any]:
        seen.append(limit)
        return []

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_expired_for_purge",
        _select,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    await _store(tmp_path / "spool").purge_expired(limit=7)
    assert seen == [7]


@pytest.mark.parametrize(
    "outcome,deleted,skipped,unsafe,io",
    [
        ("deleted", 1, 0, 0, 0),
        ("already_gone", 0, 1, 0, 0),
        ("conflict", 0, 1, 0, 0),
        ("fs_unsafe", 0, 0, 1, 0),
        ("fs_io", 0, 0, 0, 1),
    ],
)
@pytest.mark.asyncio
async def test_purge_finalizer_nonfatal_mapping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome: str,
    deleted: int,
    skipped: int,
    unsafe: int,
    io: int,
) -> None:
    stored = _stored_row()
    result, _calls, _tracker = await _run_purge(
        monkeypatch,
        tmp_path,
        candidates=[stored],
        transitions={stored.id: _dp_from_stored(stored)},
        finalize_outcomes=[outcome],
    )
    assert result.deleted == deleted
    assert result.skipped == skipped
    assert result.unsafe_skipped == unsafe
    assert result.io_unavailable_skipped == io
    assert result.transitioned_stored == 1


@pytest.mark.asyncio
async def test_purge_store_failed_fatal_partial_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    first = _stored_row()
    second = _stored_row()
    finalize_calls: list[UUID] = []

    async def _select(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [first, second]

    async def _tx(*_a: Any, row_id: UUID, **_k: Any) -> AttachmentSpoolRow:
        row = first if row_id == first.id else second
        return _dp_from_stored(row)

    async def _finalize(
        _self: AttachmentSpoolStore, snapshot: _DeletePendingFinalizeSnapshot
    ) -> str:
        finalize_calls.append(snapshot.row_id)
        if snapshot.row_id == first.id:
            return "deleted"
        return "store_failed"

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_expired_for_purge",
        _select,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo"
        ".transition_expired_stored_to_delete_pending",
        _tx,
    )
    monkeypatch.setattr(
        AttachmentSpoolStore,
        "_finalize_delete_pending",
        _finalize,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").purge_expired(limit=10)
    _assert_safe_error(
        raised.value,
        "ATTACHMENT_RECONCILE_FAILED",
        str(first.id),
        str(second.id),
    )
    assert finalize_calls == [first.id, second.id]


@pytest.mark.asyncio
async def test_purge_store_failed_does_not_finalize_remaining(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    rows = [_stored_row() for _ in range(3)]
    finalize_calls: list[UUID] = []

    async def _select(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return list(rows)

    async def _tx(*_a: Any, row_id: UUID, **_k: Any) -> AttachmentSpoolRow:
        for row in rows:
            if row.id == row_id:
                return _dp_from_stored(row)
        raise AssertionError("unexpected row")

    async def _finalize(
        _self: AttachmentSpoolStore, snapshot: _DeletePendingFinalizeSnapshot
    ) -> str:
        finalize_calls.append(snapshot.row_id)
        if len(finalize_calls) == 2:
            return "store_failed"
        return "deleted"

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_expired_for_purge",
        _select,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo"
        ".transition_expired_stored_to_delete_pending",
        _tx,
    )
    monkeypatch.setattr(
        AttachmentSpoolStore,
        "_finalize_delete_pending",
        _finalize,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").purge_expired(limit=10)
    assert raised.value.code == "ATTACHMENT_RECONCILE_FAILED"
    assert finalize_calls == [rows[0].id, rows[1].id]


@pytest.mark.asyncio
async def test_purge_select_failure_reconcile_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    with pytest.raises(AttachmentError) as raised:
        await _run_purge(
            monkeypatch,
            tmp_path,
            candidates=[],
            select_error=RuntimeError("select boom"),
        )
    _assert_safe_error(raised.value, "ATTACHMENT_RECONCILE_FAILED", "select boom")


@pytest.mark.asyncio
async def test_purge_transition_failure_reconcile_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    stored = _stored_row()
    with pytest.raises(AttachmentError) as raised:
        await _run_purge(
            monkeypatch,
            tmp_path,
            candidates=[stored],
            transition_error=RuntimeError("update boom"),
        )
    _assert_safe_error(raised.value, "ATTACHMENT_RECONCILE_FAILED", "update boom")


@pytest.mark.asyncio
async def test_purge_commit_failure_no_unlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    stored = _stored_row()
    unlink_calls: list[UUID] = []
    finalize_calls: list[str] = []

    async def _select(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [stored]

    async def _tx(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        return _dp_from_stored(stored)

    async def _finalize(_self: AttachmentSpoolStore, _s: Any) -> str:
        finalize_calls.append("called")
        return "deleted"

    def _unlink(_root: Path, object_id: UUID) -> Any:
        unlink_calls.append(object_id)
        raise AssertionError("unlink must not run")

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_expired_for_purge",
        _select,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo"
        ".transition_expired_stored_to_delete_pending",
        _tx,
    )
    monkeypatch.setattr(
        AttachmentSpoolStore,
        "_finalize_delete_pending",
        _finalize,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.unlink_final",
        _unlink,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker(fail_commit=True)),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").purge_expired(limit=10)
    _assert_safe_error(raised.value, "ATTACHMENT_RECONCILE_FAILED")
    assert finalize_calls == []
    assert unlink_calls == []


@pytest.mark.asyncio
async def test_purge_snapshot_failure_no_unlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    stored = _stored_row()
    with pytest.raises(AttachmentError) as raised:
        await _run_purge(
            monkeypatch,
            tmp_path,
            candidates=[stored],
            transitions={stored.id: _dp_from_stored(stored)},
            snapshot_error=RuntimeError("snapshot boom"),
        )
    _assert_safe_error(raised.value, "ATTACHMENT_RECONCILE_FAILED", "snapshot boom")


@pytest.mark.asyncio
async def test_purge_snapshot_attachment_error_maps_to_reconcile_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    stored = _stored_row()
    finalize_calls: list[str] = []
    tracker = TxnTracker()

    async def _select(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [stored]

    async def _tx(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        return _dp_from_stored(stored)

    async def _finalize(_self: AttachmentSpoolStore, _s: Any) -> str:
        finalize_calls.append("called")
        return "deleted"

    @classmethod  # type: ignore[misc]
    def _from_row(_cls: Any, _row: AttachmentSpoolRow) -> Any:
        raise AttachmentError("ATTACHMENT_STORE_FAILED")

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_expired_for_purge",
        _select,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo"
        ".transition_expired_stored_to_delete_pending",
        _tx,
    )
    monkeypatch.setattr(
        AttachmentSpoolStore,
        "_finalize_delete_pending",
        _finalize,
    )
    monkeypatch.setattr(
        _DeletePendingFinalizeSnapshot,
        "from_row",
        _from_row,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").purge_expired(limit=10)
    _assert_safe_error(raised.value, "ATTACHMENT_RECONCILE_FAILED")
    assert raised.value.code != "ATTACHMENT_STORE_FAILED"
    assert finalize_calls == []
    assert "rollback" in tracker.events
    assert "commit" not in tracker.events


def test_null_lease_snapshot_matches() -> None:
    row = replace(
        _stored_row(),
        state="DELETE_PENDING",
        lease_token_digest=None,
        leased_at=None,
        lease_expires_at=None,
    )
    snap = _DeletePendingFinalizeSnapshot.from_row(row)
    assert snap.lease_token_digest is None
    assert snap.matches_locked_row(row)


def test_ack_still_requires_digest_for_delete_pending() -> None:
    source = inspect.getsource(AttachmentSpoolStore.acknowledge)
    assert "select_for_update_by_lease_digest" in source
    assert 'row.state == "DELETE_PENDING"' in source
    assert "lease_token_digest != lease_digest" in source
