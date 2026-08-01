"""Unit tests for attachment spool acknowledge Stage 1A2B2."""

from __future__ import annotations

import asyncio
import base64
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
    AttachmentError,
    AttachmentKind,
    AttachmentLeaseToken,
    AttachmentMime,
    AttachmentPurpose,
    AttachmentReference,
    AttachmentReconcileResult,
    AttachmentSpoolPolicy,
    CiphertextUnlinkStatus,
)
from app.repositories.attachment_spool import AttachmentSpoolRow
from app.repositories import attachment_spool as spool_repo
from app.services.attachment_spool_store import (
    AttachmentSpoolStore,
    _DeletePendingFinalizeSnapshot,
)
from tests.attachment_spool_fakes import TxnTracker, make_observing_session_scope, synthetic_minimal_jpeg

_UTC = timezone.utc
_NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=_UTC)
_FUTURE = _NOW + timedelta(minutes=5)
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


def _leased_row(
    *,
    token: AttachmentLeaseToken | None = None,
    lease_expires_at: datetime | None = _FUTURE,
    state: str = "LEASED",
    **overrides: Any,
) -> tuple[AttachmentSpoolRow, AttachmentLeaseToken]:
    token = token or AttachmentLeaseToken.generate()
    digest = token.digest()
    row = AttachmentSpoolRow(
        id=overrides.pop("id", uuid4()),
        object_id=overrides.pop("object_id", uuid4()),
        conversation_id=overrides.pop("conversation_id", uuid4()),
        kind="IMAGE",
        purpose="INBOUND_ATTACHMENT_RELAY",
        detected_mime="image/jpeg",
        plaintext_size=len(_JPEG),
        ciphertext_size=len(_JPEG) + 16,
        ciphertext_sha256=secrets.token_bytes(32),
        nonce=secrets.token_bytes(12),
        key_id="ATTK1",
        crypto_version=1,
        state=state,
        reference_digest=secrets.token_bytes(32),
        expires_at=_FUTURE + timedelta(hours=1),
        lease_token_digest=digest,
        leased_at=_NOW,
        lease_expires_at=lease_expires_at,
        **overrides,
    )
    return row, token


def _delete_pending_row(
    token: AttachmentLeaseToken | None = None,
    *,
    lease_expires_at: datetime | None = _PAST,
    **overrides: Any,
) -> tuple[AttachmentSpoolRow, AttachmentLeaseToken]:
    row, token = _leased_row(
        token=token,
        lease_expires_at=lease_expires_at,
        state="DELETE_PENDING",
        **overrides,
    )
    return row, token


def _store(root: Path) -> AttachmentSpoolStore:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return AttachmentSpoolStore(
        session_factory=object(),  # type: ignore[arg-type]
        key_provider=EnvAttachmentKeyProvider(_key_env()),
        policy=AttachmentSpoolPolicy(root, 900),
    )


async def _return_row(row: AttachmentSpoolRow | None) -> AttachmentSpoolRow | None:
    return row


async def _noop_delete(*_a: Any, **_k: Any) -> None:
    return None


def test_delete_pending_snapshot_redacted() -> None:
    row, _ = _delete_pending_row()
    snap = _DeletePendingFinalizeSnapshot.from_row(row)
    assert repr(snap) == "_DeletePendingFinalizeSnapshot(<redacted>)"


class _LeaseTokenSubclass(AttachmentLeaseToken):
    pass


@pytest.mark.parametrize(
    "bad_token",
    [
        AttachmentReference.generate(),
        "not-a-token",
        object(),
        _LeaseTokenSubclass.generate(),
    ],
)
@pytest.mark.asyncio
async def test_ack_rejects_wrong_token_type(
    bad_token: Any, tmp_path: Path
) -> None:
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").acknowledge(bad_token)  # type: ignore[arg-type]
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED")


@pytest.mark.asyncio
async def test_ack_unknown_digest_denied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = AttachmentLeaseToken.generate()
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(None),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").acknowledge(token)
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED", token.to_token())


@pytest.mark.parametrize("state", ["STORED", "WRITING"])
@pytest.mark.asyncio
async def test_ack_denies_non_leased_non_dp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str
) -> None:
    row, token = _leased_row(state=state)
    row = replace(row, lease_token_digest=None, leased_at=None, lease_expires_at=None)
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").acknowledge(token)
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED")


@pytest.mark.asyncio
async def test_ack_expired_leased_denied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    row, token = _leased_row(lease_expires_at=_PAST)
    transition_calls: list[tuple[UUID, bytes]] = []

    async def _transition(
        _session: Any, *, row_id: UUID, lease_token_digest: bytes
    ) -> AttachmentSpoolRow | None:
        transition_calls.append((row_id, lease_token_digest))
        return None

    unlink_calls: list[UUID] = []

    def _unlink(_root: Path, object_id: UUID) -> CiphertextUnlinkStatus:
        unlink_calls.append(object_id)
        return CiphertextUnlinkStatus.REMOVED

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.transition_leased_to_delete_pending",
        _transition,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.unlink_final",
        _unlink,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").acknowledge(token)
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED")
    assert transition_calls == [(row.id, token.digest())]
    assert unlink_calls == []


@pytest.mark.asyncio
async def test_ack_transition_none_denied_no_unlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Service contract only: conditional transition None → ACCESS_DENIED, no unlink."""
    row, token = _leased_row()
    transition_calls: list[tuple[UUID, bytes]] = []
    unlink_calls: list[UUID] = []

    async def _transition(
        _session: Any, *, row_id: UUID, lease_token_digest: bytes
    ) -> AttachmentSpoolRow | None:
        transition_calls.append((row_id, lease_token_digest))
        return None

    def _unlink(_root: Path, object_id: UUID) -> CiphertextUnlinkStatus:
        unlink_calls.append(object_id)
        return CiphertextUnlinkStatus.REMOVED

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.transition_leased_to_delete_pending",
        _transition,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.unlink_final",
        _unlink,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").acknowledge(token)
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED")
    assert transition_calls == [(row.id, token.digest())]
    assert unlink_calls == []


@pytest.mark.asyncio
async def test_ack_leased_success_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    row, token = _leased_row()
    dp_row = replace(row, state="DELETE_PENDING")
    transition_args: list[tuple[UUID, bytes]] = []
    unlink_calls: list[UUID] = []
    delete_calls: list[UUID] = []

    async def _transition(
        _session: Any, *, row_id: UUID, lease_token_digest: bytes
    ) -> AttachmentSpoolRow | None:
        transition_args.append((row_id, lease_token_digest))
        return dp_row

    def _unlink(_root: Path, object_id: UUID) -> CiphertextUnlinkStatus:
        unlink_calls.append(object_id)
        return CiphertextUnlinkStatus.REMOVED

    async def _lock_by_id(*_a: Any, **kwargs: Any) -> AttachmentSpoolRow:
        assert kwargs["row_id"] == row.id
        return dp_row

    async def _delete(_session: Any, *, row_id: UUID) -> None:
        delete_calls.append(row_id)

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.transition_leased_to_delete_pending",
        _transition,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.unlink_final",
        _unlink,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        _lock_by_id,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id",
        _delete,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    await _store(tmp_path / "spool").acknowledge(token)
    assert transition_args == [(row.id, token.digest())]
    assert unlink_calls == [row.object_id]
    assert delete_calls == [row.id]


@pytest.mark.asyncio
async def test_ack_delete_pending_retry_allows_expired_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, token = _delete_pending_row(lease_expires_at=_PAST)
    transition_calls: list[str] = []

    async def _transition(*_a: Any, **_k: Any) -> AttachmentSpoolRow | None:
        transition_calls.append("called")
        return None

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.transition_leased_to_delete_pending",
        _transition,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.unlink_final",
        lambda *_a, **_k: CiphertextUnlinkStatus.ALREADY_MISSING,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id",
        lambda *_a, **_k: _noop_delete(),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    await _store(tmp_path / "spool").acknowledge(token)
    assert transition_calls == []


@pytest.mark.asyncio
async def test_ack_delete_pending_wrong_digest_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, token = _delete_pending_row()
    row = replace(row, lease_token_digest=secrets.token_bytes(32))
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").acknowledge(token)
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED")


@pytest.mark.asyncio
async def test_ack_phase_a_commit_failure_no_unlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, token = _leased_row()
    dp_row = replace(row, state="DELETE_PENDING")
    unlink_calls: list[UUID] = []

    async def _transition(*_a: Any, **_k: Any) -> AttachmentSpoolRow | None:
        return dp_row

    def _unlink(_root: Path, object_id: UUID) -> CiphertextUnlinkStatus:
        unlink_calls.append(object_id)
        return CiphertextUnlinkStatus.REMOVED

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.transition_leased_to_delete_pending",
        _transition,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.unlink_final",
        _unlink,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker(fail_commit_number=1)),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").acknowledge(token)
    _assert_safe_error(raised.value, "ATTACHMENT_STORE_FAILED", token.to_token())
    assert unlink_calls == []


@pytest.mark.asyncio
async def test_ack_delete_pending_retry_phase_a_commit_failure_no_unlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, token = _delete_pending_row()
    unlink_calls: list[UUID] = []
    finalize_calls: list[str] = []

    def _unlink(_root: Path, object_id: UUID) -> CiphertextUnlinkStatus:
        unlink_calls.append(object_id)
        return CiphertextUnlinkStatus.REMOVED

    async def _finalize(_self: AttachmentSpoolStore, _snapshot: Any) -> str:
        finalize_calls.append("called")
        return "deleted"

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.unlink_final",
        _unlink,
    )
    monkeypatch.setattr(
        AttachmentSpoolStore,
        "_finalize_delete_pending",
        _finalize,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker(fail_commit_number=1)),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").acknowledge(token)
    _assert_safe_error(raised.value, "ATTACHMENT_STORE_FAILED", token.to_token())
    assert unlink_calls == []
    assert finalize_calls == []


@pytest.mark.asyncio
async def test_ack_phase_c_commit_failure_after_unlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, token = _leased_row()
    dp_row = replace(row, state="DELETE_PENDING")

    async def _transition(*_a: Any, **_k: Any) -> AttachmentSpoolRow | None:
        return dp_row

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.transition_leased_to_delete_pending",
        _transition,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.unlink_final",
        lambda *_a, **_k: CiphertextUnlinkStatus.REMOVED,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        lambda *_a, **_k: _return_row(dp_row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id",
        lambda *_a, **_k: _noop_delete(),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker(fail_commit_number=2)),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").acknowledge(token)
    _assert_safe_error(raised.value, "ATTACHMENT_STORE_FAILED", token.to_token())


@pytest.mark.parametrize(
    ("unlink_status", "expected_code"),
    [
        (CiphertextUnlinkStatus.UNSAFE, "ATTACHMENT_FILESYSTEM_FAILED"),
        (CiphertextUnlinkStatus.IO_UNAVAILABLE, "ATTACHMENT_FILESYSTEM_FAILED"),
    ],
)
@pytest.mark.asyncio
async def test_ack_filesystem_failure_after_transition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unlink_status: CiphertextUnlinkStatus,
    expected_code: str,
) -> None:
    row, token = _leased_row()
    dp_row = replace(row, state="DELETE_PENDING")
    delete_calls: list[UUID] = []

    async def _transition(*_a: Any, **_k: Any) -> AttachmentSpoolRow | None:
        return dp_row

    async def _delete(_session: Any, *, row_id: UUID) -> None:
        delete_calls.append(row_id)

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.transition_leased_to_delete_pending",
        _transition,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.unlink_final",
        lambda *_a, **_k: unlink_status,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id",
        _delete,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").acknowledge(token)
    _assert_safe_error(raised.value, expected_code)
    assert delete_calls == []


@pytest.mark.parametrize(
    "field,mutator",
    [
        ("object_id", lambda r: replace(r, object_id=uuid4())),
        ("conversation_id", lambda r: replace(r, conversation_id=uuid4())),
        ("kind", lambda r: replace(r, kind="BAD")),
        ("purpose", lambda r: replace(r, purpose="OUTBOUND_ATTACHMENT_DELIVERY")),
        ("detected_mime", lambda r: replace(r, detected_mime="image/png")),
        ("reference_digest", lambda r: replace(r, reference_digest=secrets.token_bytes(32))),
        ("plaintext_size", lambda r: replace(r, plaintext_size=r.plaintext_size + 1)),
        ("ciphertext_size", lambda r: replace(r, ciphertext_size=r.ciphertext_size + 1)),
        ("ciphertext_sha256", lambda r: replace(r, ciphertext_sha256=secrets.token_bytes(32))),
        ("nonce", lambda r: replace(r, nonce=secrets.token_bytes(12))),
        ("key_id", lambda r: replace(r, key_id="ATTK2")),
        ("crypto_version", lambda r: replace(r, crypto_version=2)),
        ("expires_at", lambda r: replace(r, expires_at=r.expires_at + timedelta(seconds=1) if r.expires_at else _FUTURE)),
        ("lease_token_digest", lambda r: replace(r, lease_token_digest=secrets.token_bytes(32))),
        ("leased_at", lambda r: replace(r, leased_at=_NOW + timedelta(seconds=1))),
        ("lease_expires_at", lambda r: replace(r, lease_expires_at=_FUTURE + timedelta(seconds=1))),
        ("state", lambda r: replace(r, state="LEASED")),
    ],
)
@pytest.mark.asyncio
async def test_ack_phase_c_metadata_mismatch_denied(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    mutator: Any,
) -> None:
    row, token = _delete_pending_row()
    phase_c_row = mutator(row)
    delete_calls: list[UUID] = []

    async def _delete(_session: Any, *, row_id: UUID) -> None:
        delete_calls.append(row_id)

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.unlink_final",
        lambda *_a, **_k: CiphertextUnlinkStatus.REMOVED,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        lambda *_a, **_k: _return_row(phase_c_row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id",
        _delete,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").acknowledge(token)
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED")
    assert delete_calls == []


def test_snapshot_matches_locked_row_systematic() -> None:
    row, _ = _delete_pending_row()
    snap = _DeletePendingFinalizeSnapshot.from_row(row)
    assert snap.matches_locked_row(row)
    fields = [
        "object_id",
        "conversation_id",
        "kind",
        "purpose",
        "detected_mime",
        "reference_digest",
        "plaintext_size",
        "ciphertext_size",
        "ciphertext_sha256",
        "nonce",
        "key_id",
        "crypto_version",
        "expires_at",
        "lease_token_digest",
        "leased_at",
        "lease_expires_at",
        "state",
    ]
    for field in fields:
        if field == "kind":
            mutated = replace(row, kind="BAD")
        elif field == "purpose":
            mutated = replace(row, purpose="OUTBOUND_ATTACHMENT_DELIVERY")
        elif field == "detected_mime":
            mutated = replace(row, detected_mime="image/png")
        elif field == "object_id":
            mutated = replace(row, object_id=uuid4())
        elif field == "conversation_id":
            mutated = replace(row, conversation_id=uuid4())
        elif field == "reference_digest":
            mutated = replace(row, reference_digest=secrets.token_bytes(32))
        elif field == "plaintext_size":
            mutated = replace(row, plaintext_size=row.plaintext_size + 1)
        elif field == "ciphertext_size":
            mutated = replace(row, ciphertext_size=row.ciphertext_size + 1)
        elif field == "ciphertext_sha256":
            mutated = replace(row, ciphertext_sha256=secrets.token_bytes(32))
        elif field == "nonce":
            mutated = replace(row, nonce=secrets.token_bytes(12))
        elif field == "key_id":
            mutated = replace(row, key_id="ATTK2")
        elif field == "crypto_version":
            mutated = replace(row, crypto_version=2)
        elif field == "expires_at":
            mutated = replace(row, expires_at=row.expires_at + timedelta(seconds=1) if row.expires_at else _FUTURE)
        elif field == "lease_token_digest":
            mutated = replace(row, lease_token_digest=secrets.token_bytes(32))
        elif field == "leased_at":
            mutated = replace(row, leased_at=_NOW + timedelta(seconds=1))
        elif field == "lease_expires_at":
            mutated = replace(row, lease_expires_at=_FUTURE + timedelta(seconds=1))
        elif field == "state":
            mutated = replace(row, state="LEASED")
        else:
            raise AssertionError(field)
        assert not snap.matches_locked_row(mutated), field


@pytest.mark.asyncio
async def test_reconcile_delete_pending_increments_counter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, _ = _delete_pending_row()

    async def _no_writing(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _dp_rows(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [row]

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _no_writing,
    )
    async def _empty_stored(*_a: Any, **_k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _empty_stored,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_delete_pending_for_finalize",
        _dp_rows,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.unlink_final",
        lambda *_a, **_k: CiphertextUnlinkStatus.REMOVED,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id",
        lambda *_a, **_k: _noop_delete(),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    result = await _store(tmp_path / "spool").reconcile(limit=10)
    assert result.deleted_delete_pending == 1
    assert result.promoted_to_stored == 0


async def _reconcile_delete_pending_with_finalize_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    finalize_outcome: str,
) -> Any:
    row, _ = _delete_pending_row()

    async def _no_writing(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _dp_rows(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [row]

    async def _empty_stored(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _finalize(_self: AttachmentSpoolStore, _snapshot: Any) -> str:
        return finalize_outcome

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _no_writing,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _empty_stored,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_delete_pending_for_finalize",
        _dp_rows,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
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
    return await _store(tmp_path / "spool").reconcile(limit=10)


@pytest.mark.parametrize(
    "finalize_outcome",
    ["already_gone", "conflict", "store_failed"],
)
@pytest.mark.asyncio
async def test_reconcile_delete_pending_non_deleted_outcomes_do_not_increment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    finalize_outcome: str,
) -> None:
    result = await _reconcile_delete_pending_with_finalize_outcome(
        monkeypatch, tmp_path, finalize_outcome=finalize_outcome
    )
    assert result.deleted_delete_pending == 0
    assert result.promoted_to_stored == 0
    assert result.deleted_writing_rows == 0


@pytest.mark.parametrize(
    "finalize_outcome,expected_unsafe,expected_io",
    [
        ("fs_unsafe", 1, 0),
        ("fs_io", 0, 1),
    ],
)
@pytest.mark.asyncio
async def test_reconcile_delete_pending_filesystem_failure_does_not_increment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    finalize_outcome: str,
    expected_unsafe: int,
    expected_io: int,
) -> None:
    result = await _reconcile_delete_pending_with_finalize_outcome(
        monkeypatch, tmp_path, finalize_outcome=finalize_outcome
    )
    assert result.deleted_delete_pending == 0
    assert result.unsafe_skipped == expected_unsafe
    assert result.io_unavailable_skipped == expected_io


@pytest.mark.asyncio
async def test_reconcile_delete_pending_select_commit_failure_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _no_writing(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _empty_stored(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _dp_rows(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [_delete_pending_row()[0]]

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _no_writing,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _empty_stored,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_delete_pending_for_finalize",
        _dp_rows,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker(fail_commit_number=3)),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").reconcile(limit=10)
    _assert_safe_error(raised.value, "ATTACHMENT_RECONCILE_FAILED")


def test_reconcile_result_rejects_bool_delete_pending_counter() -> None:
    with pytest.raises(AttachmentError):
        AttachmentReconcileResult(
            promoted_to_stored=0,
            deleted_writing_rows=0,
            deleted_orphan_temps=0,
            deleted_orphan_finals=0,
            deleted_unrecoverable_stored=0,
            deleted_delete_pending=True,  # type: ignore[arg-type]
            unsafe_skipped=0,
            io_unavailable_skipped=0,
        )


@pytest.mark.asyncio
async def test_ack_post_delete_row_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    token = AttachmentLeaseToken.generate()
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(None),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").acknowledge(token)
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED", token.to_token())


@pytest.mark.asyncio
async def test_ack_phase_c_row_gone_after_unlink_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, token = _delete_pending_row()
    delete_calls: list[UUID] = []

    async def _delete(_session: Any, *, row_id: UUID) -> None:
        delete_calls.append(row_id)

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.unlink_final",
        lambda *_a, **_k: CiphertextUnlinkStatus.REMOVED,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        lambda *_a, **_k: _return_row(None),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id",
        _delete,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    await _store(tmp_path / "spool").acknowledge(token)
    assert delete_calls == []


@pytest.mark.asyncio
async def test_ack_unlink_before_db_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, token = _delete_pending_row()
    order: list[str] = []

    def _unlink(_root: Path, object_id: UUID) -> CiphertextUnlinkStatus:
        order.append("unlink")
        return CiphertextUnlinkStatus.REMOVED

    async def _delete(_session: Any, *, row_id: UUID) -> None:
        order.append("delete")

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.unlink_final",
        _unlink,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id",
        _delete,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    await _store(tmp_path / "spool").acknowledge(token)
    assert order == ["unlink", "delete"]


def test_reconcile_result_requires_delete_pending_counter() -> None:
    with pytest.raises(AttachmentError):
        AttachmentReconcileResult(
            promoted_to_stored=0,
            deleted_writing_rows=0,
            deleted_orphan_temps=0,
            deleted_orphan_finals=0,
            deleted_unrecoverable_stored=0,
            deleted_delete_pending=-1,
            unsafe_skipped=0,
            io_unavailable_skipped=0,
        )


def _normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.lower()).strip()


def _update_sections(sql: str) -> tuple[str, str, str]:
    normalized = _normalize_sql(sql)
    set_match = re.search(r"\bset\b(.+?)\bwhere\b", normalized)
    where_match = re.search(r"\bwhere\b(.+?)(\breturning\b|$)", normalized)
    returning_match = re.search(r"\breturning\b(.+)$", normalized)
    assert set_match is not None
    assert where_match is not None
    assert returning_match is not None
    return set_match.group(1).strip(), where_match.group(1).strip(), returning_match.group(1).strip()


@pytest.mark.asyncio
async def test_transition_leased_to_delete_pending_sql_predicates() -> None:
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

    session = _Session()
    row_id = uuid4()
    digest = secrets.token_bytes(32)
    await spool_repo.transition_leased_to_delete_pending(
        session, row_id=row_id, lease_token_digest=digest
    )
    assert len(captured) == 1
    assert commit_calls == 0
    compiled = captured[0].compile(
        dialect=__import__(
            "sqlalchemy.dialects.postgresql", fromlist=["dialect"]
        ).dialect()
    )
    sql = str(compiled)
    params = compiled.params
    set_section, where_section, returning_section = _update_sections(sql)

    assert params.get("state") == "DELETE_PENDING"
    assert "state" in set_section
    assert "updated_at" in set_section
    assert "statement_timestamp()" in set_section
    assert "lease_token_digest" not in set_section
    assert "leased_at" not in set_section
    assert "lease_expires_at" not in set_section

    assert "attachment_spool_objects.id" in where_section
    assert "state" in where_section
    assert "lease_token_digest" in where_section
    assert re.search(r"lease_expires_at\s+is\s+not\s+null", where_section)
    assert re.search(
        r"lease_expires_at\s*>\s*statement_timestamp\(\)",
        where_section,
    )
    assert returning_section != ""
