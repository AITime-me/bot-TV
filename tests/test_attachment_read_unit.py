"""Unit tests for attachment spool secure read Stage 1A2B1."""

from __future__ import annotations

import base64
import secrets
import traceback
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.attachment_crypto import decrypt_bytes, encrypt_bytes
from app.core.attachment_keys import EnvAttachmentKeyProvider
from app.core.attachment_types import (
    AttachmentAad,
    AttachmentError,
    AttachmentKind,
    AttachmentLeaseToken,
    AttachmentMime,
    AttachmentPlaintext,
    AttachmentPurpose,
    AttachmentReference,
    AttachmentSpoolPolicy,
)
from app.repositories.attachment_spool import AttachmentSpoolRow
from app.services.attachment_spool_store import (
    AttachmentSpoolStore,
    _ReadCryptoSnapshot,
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
    row_id = overrides.pop("id", uuid4())
    object_id = overrides.pop("object_id", uuid4())
    conversation_id = overrides.pop("conversation_id", uuid4())
    row = AttachmentSpoolRow(
        id=row_id,
        object_id=object_id,
        conversation_id=conversation_id,
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


def _store(root: Path) -> AttachmentSpoolStore:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return AttachmentSpoolStore(
        session_factory=object(),  # type: ignore[arg-type]
        key_provider=EnvAttachmentKeyProvider(_key_env()),
        policy=AttachmentSpoolPolicy(root, 900),
    )


async def _pg_now(*_a: Any, **_k: Any) -> datetime:
    return _NOW


async def _return_row(row: AttachmentSpoolRow | None) -> AttachmentSpoolRow | None:
    return row


def _leased_row_with_encrypted() -> tuple[AttachmentSpoolRow, AttachmentLeaseToken, bytes]:
    row, token = _leased_row()
    provider = EnvAttachmentKeyProvider(_key_env())
    active = provider.get_active_key()
    aad = AttachmentAad(
        crypto_version=1,
        record_id=row.id,
        object_id=row.object_id,
        key_id=row.key_id,
        kind=AttachmentKind.IMAGE,
        conversation_id=row.conversation_id,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        mime=AttachmentMime.IMAGE_JPEG,
        plaintext_size=row.plaintext_size,
    )
    encrypted = encrypt_bytes(_JPEG, aad=aad, key_provider=provider, active_key=active)
    row = replace(
        row,
        ciphertext_size=len(encrypted.ciphertext),
        ciphertext_sha256=encrypted.ciphertext_sha256,
        nonce=encrypted.nonce,
    )
    return row, token, encrypted.ciphertext


def test_attachment_plaintext_validation_and_redaction() -> None:
    pt = AttachmentPlaintext(data=_JPEG, mime=AttachmentMime.IMAGE_JPEG)
    assert pt.mime is AttachmentMime.IMAGE_JPEG
    assert pt.data == _JPEG
    rendered = f"{pt!r}{pt!s}"
    assert "image/jpeg" in rendered
    assert "<redacted>" in rendered
    assert repr(_JPEG) not in rendered
    with pytest.raises(AttachmentError) as raised:
        AttachmentPlaintext(data=b"", mime=AttachmentMime.IMAGE_JPEG)
    _assert_safe_error(raised.value, "ATTACHMENT_VALUE_INVALID")


def test_read_crypto_snapshot_redacted() -> None:
    row, token = _leased_row()
    snap = _ReadCryptoSnapshot.from_row(row, token.digest())
    assert repr(snap) == "_ReadCryptoSnapshot(<redacted>)"


@pytest.mark.parametrize(
    "bad_token",
    [
        AttachmentReference.generate(),
        "not-a-token",
        object(),
    ],
)
@pytest.mark.asyncio
async def test_read_rejects_wrong_token_type(
    bad_token: Any, tmp_path: Path
) -> None:
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").read(bad_token)  # type: ignore[arg-type]
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED")


@pytest.mark.asyncio
async def test_read_unknown_token_denied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = AttachmentLeaseToken.generate()
    tracker = TxnTracker()
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(None),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").read(token)
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED", token.to_token())


@pytest.mark.parametrize("state", ["WRITING", "STORED", "DELETE_PENDING"])
@pytest.mark.asyncio
async def test_read_phase_a_denies_non_leased(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str
) -> None:
    row, token = _leased_row(state=state)
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
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
        await _store(tmp_path / "spool").read(token)
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED")


@pytest.mark.asyncio
async def test_read_expired_lease_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row, token = _leased_row(lease_expires_at=_PAST)
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
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
        await _store(tmp_path / "spool").read(token)
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED")


@pytest.mark.asyncio
async def test_read_success_returns_plaintext_and_mime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, token = _leased_row()
    provider = EnvAttachmentKeyProvider(_key_env())
    active = provider.get_active_key()
    aad = AttachmentAad(
        crypto_version=1,
        record_id=row.id,
        object_id=row.object_id,
        key_id=row.key_id,
        kind=AttachmentKind.IMAGE,
        conversation_id=row.conversation_id,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        mime=AttachmentMime.IMAGE_JPEG,
        plaintext_size=row.plaintext_size,
    )
    encrypted = encrypt_bytes(_JPEG, aad=aad, key_provider=provider, active_key=active)
    row = replace(
        row,
        ciphertext_size=len(encrypted.ciphertext),
        ciphertext_sha256=encrypted.ciphertext_sha256,
        nonce=encrypted.nonce,
    )
    phase_c_row = row
    fs_calls: list[tuple[UUID, int, bytes]] = []

    async def _lock_by_digest(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        return row

    async def _lock_by_id(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        return phase_c_row

    def _read_fs(
        _root: Path, object_id: UUID, *, expected_size: int, expected_sha256: bytes
    ) -> bytes:
        fs_calls.append((object_id, expected_size, expected_sha256))
        return encrypted.ciphertext

    mime_calls: list[object] = []

    def _mime_detect(_data: object) -> AttachmentMime:
        mime_calls.append(_data)
        raise AssertionError("MIME must not be redetected")

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        _lock_by_digest,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        _lock_by_id,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.read_ciphertext_verified",
        _read_fs,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.detect_attachment_mime",
        _mime_detect,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    result = await _store(tmp_path / "spool").read(token)
    assert isinstance(result, AttachmentPlaintext)
    assert result.data == _JPEG
    assert result.mime is AttachmentMime.IMAGE_JPEG
    assert mime_calls == []
    assert fs_calls == [(row.object_id, row.ciphertext_size, row.ciphertext_sha256)]
    assert repr(_JPEG) not in repr(result)


@pytest.mark.asyncio
async def test_read_phase_c_metadata_change_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, token = _leased_row()
    provider = EnvAttachmentKeyProvider(_key_env())
    active = provider.get_active_key()
    aad = AttachmentAad(
        crypto_version=1,
        record_id=row.id,
        object_id=row.object_id,
        key_id=row.key_id,
        kind=AttachmentKind.IMAGE,
        conversation_id=row.conversation_id,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        mime=AttachmentMime.IMAGE_JPEG,
        plaintext_size=row.plaintext_size,
    )
    encrypted = encrypt_bytes(_JPEG, aad=aad, key_provider=provider, active_key=active)
    row = replace(
        row,
        ciphertext_size=len(encrypted.ciphertext),
        ciphertext_sha256=encrypted.ciphertext_sha256,
        nonce=encrypted.nonce,
    )
    mutated = replace(row, ciphertext_sha256=secrets.token_bytes(32))

    async def _lock_by_id(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        return mutated

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        _lock_by_id,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.read_ciphertext_verified",
        lambda *_a, **_k: encrypted.ciphertext,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").read(token)
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED")


@pytest.mark.asyncio
async def test_read_phase_c_state_changed_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, token = _leased_row()
    provider = EnvAttachmentKeyProvider(_key_env())
    active = provider.get_active_key()
    aad = AttachmentAad(
        crypto_version=1,
        record_id=row.id,
        object_id=row.object_id,
        key_id=row.key_id,
        kind=AttachmentKind.IMAGE,
        conversation_id=row.conversation_id,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        mime=AttachmentMime.IMAGE_JPEG,
        plaintext_size=row.plaintext_size,
    )
    encrypted = encrypt_bytes(_JPEG, aad=aad, key_provider=provider, active_key=active)
    row = replace(
        row,
        ciphertext_size=len(encrypted.ciphertext),
        ciphertext_sha256=encrypted.ciphertext_sha256,
        nonce=encrypted.nonce,
    )
    stored = replace(
        row,
        state="STORED",
        lease_token_digest=None,
        leased_at=None,
        lease_expires_at=None,
    )

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        lambda *_a, **_k: _return_row(stored),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.read_ciphertext_verified",
        lambda *_a, **_k: encrypted.ciphertext,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").read(token)
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED")


@pytest.mark.asyncio
async def test_read_phase_c_missing_row_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, token = _leased_row()
    provider = EnvAttachmentKeyProvider(_key_env())
    active = provider.get_active_key()
    aad = AttachmentAad(
        crypto_version=1,
        record_id=row.id,
        object_id=row.object_id,
        key_id=row.key_id,
        kind=AttachmentKind.IMAGE,
        conversation_id=row.conversation_id,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        mime=AttachmentMime.IMAGE_JPEG,
        plaintext_size=row.plaintext_size,
    )
    encrypted = encrypt_bytes(_JPEG, aad=aad, key_provider=provider, active_key=active)
    row = replace(
        row,
        ciphertext_size=len(encrypted.ciphertext),
        ciphertext_sha256=encrypted.ciphertext_sha256,
        nonce=encrypted.nonce,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        lambda *_a, **_k: _return_row(None),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.read_ciphertext_verified",
        lambda *_a, **_k: encrypted.ciphertext,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").read(token)
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED")


@pytest.mark.asyncio
async def test_read_filesystem_failure_fixed_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, token = _leased_row()
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )

    def _fail_fs(*_a: Any, **_k: Any) -> bytes:
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED")

    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.read_ciphertext_verified",
        _fail_fs,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").read(token)
    _assert_safe_error(raised.value, "ATTACHMENT_FILESYSTEM_FAILED", str(tmp_path))


@pytest.mark.asyncio
async def test_read_decrypt_failure_access_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, token = _leased_row()
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.read_ciphertext_verified",
        lambda *_a, **_k: b"\x00" * row.ciphertext_size,
    )

    def _fail_decrypt(*_a: Any, **_k: Any) -> bytes:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID")

    monkeypatch.setattr(
        "app.services.attachment_spool_store.decrypt_bytes",
        _fail_decrypt,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").read(token)
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED")


@pytest.mark.asyncio
async def test_read_phase_a_commit_failure_returns_store_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, token, _ciphertext = _leased_row_with_encrypted()
    tracker = TxnTracker(fail_commit_number=1)
    fs_calls: list[int] = []
    decrypt_calls: list[int] = []

    def _read_fs(*_a: Any, **_k: Any) -> bytes:
        fs_calls.append(1)
        raise AssertionError("filesystem must not run on Phase A commit failure")

    def _track_decrypt(*_a: Any, **_k: Any) -> bytes:
        decrypt_calls.append(1)
        raise AssertionError("decrypt must not run on Phase A commit failure")

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.read_ciphertext_verified",
        _read_fs,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.decrypt_bytes",
        _track_decrypt,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").read(token)
    _assert_safe_error(raised.value, "ATTACHMENT_STORE_FAILED", token.to_token())
    assert fs_calls == []
    assert decrypt_calls == []
    assert tracker.events.count("commit") == 0
    assert tracker.events.count("rollback") == 1
    assert tracker.events.count("enter") == 1


@pytest.mark.asyncio
async def test_read_phase_c_commit_failure_returns_no_plaintext(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, token, ciphertext = _leased_row_with_encrypted()
    tracker = TxnTracker(fail_commit_number=2)
    fs_calls: list[int] = []
    decrypt_calls: list[int] = []
    phase_c_locks: list[int] = []
    dto_calls: list[int] = []

    async def _lock_by_id(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        phase_c_locks.append(1)
        return row

    def _read_fs(*_a: Any, **_k: Any) -> bytes:
        fs_calls.append(1)
        return ciphertext

    def _track_decrypt(*a: Any, **k: Any) -> bytes:
        decrypt_calls.append(1)
        return decrypt_bytes(*a, **k)

    class _ForbiddenPlaintext:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            dto_calls.append(1)
            raise AssertionError("plaintext dto must not be constructed")

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        _lock_by_id,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.read_ciphertext_verified",
        _read_fs,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.decrypt_bytes",
        _track_decrypt,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.AttachmentPlaintext",
        _ForbiddenPlaintext,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )
    with pytest.raises(AttachmentError) as raised:
        await _store(tmp_path / "spool").read(token)
    _assert_safe_error(raised.value, "ATTACHMENT_STORE_FAILED", token.to_token())
    assert fs_calls == [1]
    assert decrypt_calls == [1]
    assert phase_c_locks == [1]
    assert dto_calls == []
    assert tracker.events.count("commit") == 1
    assert tracker.events.count("rollback") == 1
    assert tracker.events.count("enter") == 2


@pytest.mark.asyncio
async def test_read_uses_two_transactions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row, token = _leased_row()
    provider = EnvAttachmentKeyProvider(_key_env())
    active = provider.get_active_key()
    aad = AttachmentAad(
        crypto_version=1,
        record_id=row.id,
        object_id=row.object_id,
        key_id=row.key_id,
        kind=AttachmentKind.IMAGE,
        conversation_id=row.conversation_id,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        mime=AttachmentMime.IMAGE_JPEG,
        plaintext_size=row.plaintext_size,
    )
    encrypted = encrypt_bytes(_JPEG, aad=aad, key_provider=provider, active_key=active)
    row = replace(
        row,
        ciphertext_size=len(encrypted.ciphertext),
        ciphertext_sha256=encrypted.ciphertext_sha256,
        nonce=encrypted.nonce,
    )
    tracker = TxnTracker()
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_lease_digest",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        lambda *_a, **_k: _return_row(row),
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.fetch_statement_timestamp",
        _pg_now,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.read_ciphertext_verified",
        lambda *_a, **_k: encrypted.ciphertext,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )
    await _store(tmp_path / "spool").read(token)
    assert tracker.events.count("commit") == 2
