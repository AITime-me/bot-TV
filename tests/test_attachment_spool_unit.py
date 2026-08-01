"""Unit orchestration tests for attachment spool store Stage 1A1."""

from __future__ import annotations

import base64
import secrets
import traceback
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core import attachment_fs
from app.core.attachment_keys import ActiveAttachmentKey, EnvAttachmentKeyProvider
from app.core.attachment_types import (
    MAX_PLAINTEXT_BYTES,
    MAX_REFERENCE_COLLISION_RETRIES,
    REFERENCE_DIGEST_BYTES,
    REFERENCE_TOKEN_LENGTH,
    AttachmentError,
    AttachmentHandle,
    AttachmentKind,
    AttachmentMime,
    AttachmentPurpose,
    AttachmentReconcileResult,
    AttachmentReference,
    AttachmentSpoolPolicy,
    AttachmentState,
    CiphertextInspectStatus,
    CiphertextUnlinkStatus,
)
from app.repositories.attachment_spool import AttachmentSpoolRow
from app.services.attachment_spool_store import AttachmentSpoolStore
from tests.attachment_spool_fakes import (
    TxnTracker,
    make_observing_session_scope,
    synthetic_minimal_jpeg,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_JPEG = synthetic_minimal_jpeg()


def _key_env() -> dict[str, str]:
    key = secrets.token_bytes(32)
    return {
        "ATTACHMENT_SPOOL_ACTIVE_KEY_ID": "ATTK1",
        "ATTACHMENT_SPOOL_KEY_ATTK1": base64.urlsafe_b64encode(key).decode("ascii"),
    }


def _assert_safe_error(exc: AttachmentError, code: str, *forbidden: str) -> None:
    assert exc.code == code
    assert str(exc) == code
    assert exc.__cause__ is None
    blob = str(exc) + repr(exc) + "".join(traceback.format_exception(exc))
    for item in forbidden:
        if item:
            assert item not in blob


class _CountingKeyProvider(EnvAttachmentKeyProvider):
    def __init__(self, environ: dict[str, str]) -> None:
        super().__init__(environ)
        self.active_key_calls = 0

    def get_active_key(self) -> ActiveAttachmentKey:
        self.active_key_calls += 1
        return super().get_active_key()


def _store(
    *,
    root: Path,
    key_provider: EnvAttachmentKeyProvider | None = None,
    reference_factory: Any | None = None,
) -> AttachmentSpoolStore:
    return AttachmentSpoolStore(
        session_factory=object(),  # type: ignore[arg-type]
        key_provider=key_provider or EnvAttachmentKeyProvider(_key_env()),
        policy=AttachmentSpoolPolicy(root, 900),
        reference_factory=reference_factory,
    )


def _writing_row(
    *,
    row_id: UUID,
    object_id: UUID,
    conversation_id: UUID,
    digest: bytes,
    ciphertext_size: int,
    ciphertext_sha256: bytes,
    nonce: bytes,
    plaintext_size: int,
) -> AttachmentSpoolRow:
    return AttachmentSpoolRow(
        id=row_id,
        object_id=object_id,
        conversation_id=conversation_id,
        kind="IMAGE",
        purpose="INBOUND_ATTACHMENT_RELAY",
        detected_mime="image/jpeg",
        plaintext_size=plaintext_size,
        ciphertext_size=ciphertext_size,
        ciphertext_sha256=ciphertext_sha256,
        nonce=nonce,
        key_id="ATTK1",
        crypto_version=1,
        state="WRITING",
        reference_digest=digest,
    )


def test_reference_generate_parse_digest() -> None:
    reference = AttachmentReference.generate()
    token = reference.to_token()
    assert len(token) == REFERENCE_TOKEN_LENGTH
    assert token.endswith("=")
    assert "+" not in token
    assert "/" not in token
    assert AttachmentReference.parse(token) == reference
    digest = reference.digest()
    assert type(digest) is bytes
    assert len(digest) == REFERENCE_DIGEST_BYTES
    rendered = f"{reference!r}{reference!s}{reference}"
    assert rendered == "AttachmentReference(<redacted>)" * 3
    assert token not in rendered


def test_reference_rejects_invalid() -> None:
    token = AttachmentReference.generate().to_token()
    for bad in ("", "A" * 43, token + "A", object(), b"bytes"):
        with pytest.raises(AttachmentError) as raised:
            AttachmentReference.parse(bad)  # type: ignore[arg-type]
        _assert_safe_error(raised.value, "ATTACHMENT_REFERENCE_INVALID")


def test_policy_and_enums(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    policy = AttachmentSpoolPolicy(root, 900)
    assert policy.max_plaintext_bytes == MAX_PLAINTEXT_BYTES
    assert policy.writing_grace_seconds == 600
    assert "900" not in repr(policy)
    assert str(root) not in repr(policy)
    with pytest.raises(AttachmentError):
        AttachmentSpoolPolicy(Path("relative"), 900)
    with pytest.raises(AttachmentError):
        AttachmentSpoolPolicy(root, True)  # type: ignore[arg-type]
    assert AttachmentKind.IMAGE.value == "IMAGE"
    assert AttachmentPurpose.INBOUND_ATTACHMENT_RELAY.value
    assert AttachmentPurpose.OUTBOUND_ATTACHMENT_DELIVERY.value
    assert AttachmentMime.IMAGE_JPEG.value == "image/jpeg"
    assert AttachmentMime.IMAGE_PNG.value == "image/png"
    assert AttachmentState.WRITING.value == "WRITING"
    assert AttachmentState.STORED.value == "STORED"
    assert AttachmentState.LEASED.value == "LEASED"
    assert AttachmentState.DELETE_PENDING.value == "DELETE_PENDING"


def test_handle_and_reconcile_result_redacted() -> None:
    handle = AttachmentHandle(
        reference=AttachmentReference.generate(),
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        mime=AttachmentMime.IMAGE_JPEG,
        plaintext_size=32,
    )
    rendered = f"{handle!r}{handle!s}{handle}"
    assert "reference=<redacted>" in rendered
    assert handle.reference.to_token() not in rendered
    result = AttachmentReconcileResult(
        promoted_to_stored=1,
        deleted_writing_rows=0,
        deleted_orphan_temps=0,
        deleted_orphan_finals=0,
        deleted_unrecoverable_stored=0,
        deleted_delete_pending=0,
        unsafe_skipped=0,
        io_unavailable_skipped=0,
    )
    assert "1" in repr(result)
    assert "path" not in repr(result).lower()
    assert "io_unavailable_skipped=0" in repr(result)


def test_static_import_boundaries() -> None:
    pii_gateway = (_REPO_ROOT / "app/core/pii_gateway.py").read_text(encoding="utf-8")
    dialog = (_REPO_ROOT / "app/services/dialog_context.py").read_text(encoding="utf-8")
    worker = (_REPO_ROOT / "app/services/worker_runtime.py").read_text(encoding="utf-8")
    worker_py = (_REPO_ROOT / "app/worker.py").read_text(encoding="utf-8")
    repo = (_REPO_ROOT / "app/repositories/attachment_spool.py").read_text(
        encoding="utf-8"
    )
    store = (_REPO_ROOT / "app/services/attachment_spool_store.py").read_text(
        encoding="utf-8"
    )
    model = (_REPO_ROOT / "app/models/attachment_spool.py").read_text(encoding="utf-8")
    assert "attachment_spool_store" not in pii_gateway
    assert "attachment_crypto" not in pii_gateway
    assert "attachment_spool_store" not in dialog
    assert "attachment_spool_store" not in worker
    assert "attachment_spool_store" not in worker_py
    assert "attachment_crypto" not in repo
    assert "attachment_keys" not in repo
    assert "acquire_delivery" not in store
    assert "read_for_delivery" not in store
    assert "acknowledge_delivered" not in store
    assert "mark_delete_pending" not in store
    assert "content_sha256" not in store
    assert "content_sha256" not in repo
    assert "content_sha256" not in model
    assert "plaintext_sha256" not in model
    assert "storage_path" not in model
    assert "client_filename" not in model


def test_no_public_recover_or_decrypt_api() -> None:
    names = set(dir(AttachmentSpoolStore))
    assert "store" in names
    assert "reconcile" in names
    assert "acquire" in names
    assert "release" in names
    assert "reclaim_expired_leases" in names
    assert "read" in names
    assert "acknowledge" in names
    assert "recover" not in names
    assert "decrypt" not in names
    assert "open_once" not in names
    assert "read_for_delivery" not in names
    assert "acquire_delivery" not in names
    assert "acknowledge_delivered" not in names
    assert "ack" not in names
    assert "purge" not in names


@pytest.fixture(autouse=True)
def _reconcile_without_delete_pending_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty(*_a: Any, **_k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_delete_pending_for_finalize",
        _empty,
    )


def test_collision_retry_constant() -> None:
    assert MAX_REFERENCE_COLLISION_RETRIES == 3


@pytest.mark.asyncio
async def test_store_writing_before_filesystem_and_handle_after_stored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    order: list[str] = []
    tracker = TxnTracker()
    captured: dict[str, Any] = {}
    conversation_id = uuid4()

    async def _insert(*_a: Any, **kwargs: Any) -> bool:
        order.append("insert_writing")
        captured.update(kwargs)
        assert "plaintext" not in kwargs
        assert _JPEG not in kwargs.values()
        return True

    async def _select(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        order.append("select_for_update")
        return _writing_row(
            row_id=captured["row_id"],
            object_id=captured["object_id"],
            conversation_id=conversation_id,
            digest=captured["reference_digest"],
            ciphertext_size=captured["ciphertext_size"],
            ciphertext_sha256=captured["ciphertext_sha256"],
            nonce=captured["nonce"],
            plaintext_size=captured["plaintext_size"],
        )

    async def _mark(*_a: Any, **_k: Any) -> bool:
        order.append("mark_stored")
        return True

    def _write(*_a: Any, **_k: Any) -> None:
        order.append("filesystem_write")

    def _verify(*_a: Any, **_k: Any) -> None:
        order.append("verify_final")

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.insert_writing", _insert
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        _select,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.mark_stored", _mark
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.write_ciphertext_atomic",
        _write,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.verify_ciphertext_file",
        _verify,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )

    root = tmp_path / "spool"
    root.mkdir()
    provider = _CountingKeyProvider(_key_env())
    store = _store(root=root, key_provider=provider)
    handle = await store.store(
        _JPEG,
        conversation_id=conversation_id,
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
    )
    assert isinstance(handle, AttachmentHandle)
    assert handle.mime is AttachmentMime.IMAGE_JPEG
    assert provider.active_key_calls == 1
    assert order[0] == "insert_writing"
    assert order.index("insert_writing") < order.index("filesystem_write")
    assert order.index("filesystem_write") < order.index("mark_stored")
    assert order[-1] == "mark_stored"
    assert tracker.events.count("commit") == 2
    assert type(captured["reference_digest"]) is bytes
    assert len(captured["reference_digest"]) == 32
    assert handle.reference.to_token() not in captured.values()


@pytest.mark.asyncio
async def test_writing_commit_failure_skips_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tracker = TxnTracker(fail_commit=True)
    writes = {"n": 0}

    async def _insert(*_a: Any, **_k: Any) -> bool:
        return True

    def _write(*_a: Any, **_k: Any) -> None:
        writes["n"] += 1

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.insert_writing", _insert
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.write_ciphertext_atomic",
        _write,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )
    root = tmp_path / "spool"
    root.mkdir()
    with pytest.raises(AttachmentError) as raised:
        await _store(root=root).store(
            _JPEG,
            conversation_id=uuid4(),
            kind=AttachmentKind.IMAGE,
            purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        )
    _assert_safe_error(raised.value, "ATTACHMENT_STORE_FAILED", str(root))
    assert writes["n"] == 0


@pytest.mark.asyncio
async def test_temp_write_failure_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tracker = TxnTracker()
    deleted: list[UUID] = []
    unlinked: list[UUID] = []
    captured: dict[str, Any] = {}

    async def _insert(*_a: Any, **kwargs: Any) -> bool:
        captured.update(kwargs)
        return True

    def _write(*_a: Any, **_k: Any) -> None:
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED")

    async def _delete(*_a: Any, **kwargs: Any) -> None:
        deleted.append(kwargs["row_id"])

    def _unlink(_root: Path, object_id: UUID) -> None:
        unlinked.append(object_id)

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.insert_writing", _insert
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.write_ciphertext_atomic",
        _write,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.unlink_object_files",
        _unlink,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id", _delete
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )
    root = tmp_path / "spool"
    root.mkdir()
    with pytest.raises(AttachmentError) as raised:
        await _store(root=root).store(
            _JPEG,
            conversation_id=uuid4(),
            kind=AttachmentKind.IMAGE,
            purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        )
    _assert_safe_error(raised.value, "ATTACHMENT_FILESYSTEM_FAILED")
    assert deleted == [captured["row_id"]]
    assert unlinked == [captured["object_id"]]


@pytest.mark.asyncio
async def test_stored_commit_failure_leaves_writing_and_no_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commits = {"n": 0}
    captured: dict[str, Any] = {}
    conversation_id = uuid4()

    async def _insert(*_a: Any, **kwargs: Any) -> bool:
        captured.update(kwargs)
        return True

    async def _select(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        return _writing_row(
            row_id=captured["row_id"],
            object_id=captured["object_id"],
            conversation_id=conversation_id,
            digest=captured["reference_digest"],
            ciphertext_size=captured["ciphertext_size"],
            ciphertext_sha256=captured["ciphertext_sha256"],
            nonce=captured["nonce"],
            plaintext_size=captured["plaintext_size"],
        )

    async def _mark(*_a: Any, **_k: Any) -> bool:
        return True

    def _write(*_a: Any, **_k: Any) -> None:
        return None

    def _verify(*_a: Any, **_k: Any) -> None:
        return None

    from contextlib import asynccontextmanager
    from collections.abc import AsyncIterator

    @asynccontextmanager
    async def _scope(_factory: Any) -> AsyncIterator[Any]:
        commits["n"] += 1
        try:
            yield object()
        except Exception:
            raise
        else:
            if commits["n"] >= 2:
                raise RuntimeError("synthetic STORED commit failure")

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.insert_writing", _insert
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        _select,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.mark_stored", _mark
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.write_ciphertext_atomic",
        _write,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.verify_ciphertext_file",
        _verify,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        _scope,
    )
    root = tmp_path / "spool"
    root.mkdir()
    with pytest.raises(AttachmentError) as raised:
        await _store(root=root).store(
            _JPEG,
            conversation_id=conversation_id,
            kind=AttachmentKind.IMAGE,
            purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        )
    _assert_safe_error(raised.value, "ATTACHMENT_STORE_FAILED")


@pytest.mark.asyncio
async def test_collision_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attempts = {"n": 0}
    tracker = TxnTracker()
    captured: dict[str, Any] = {}
    conversation_id = uuid4()
    references = [
        AttachmentReference.generate(),
        AttachmentReference.generate(),
    ]

    async def _insert(*_a: Any, **kwargs: Any) -> bool:
        attempts["n"] += 1
        if attempts["n"] < 2:
            return False
        captured.update(kwargs)
        return True

    async def _select(*_a: Any, **_k: Any) -> AttachmentSpoolRow:
        return _writing_row(
            row_id=captured["row_id"],
            object_id=captured["object_id"],
            conversation_id=conversation_id,
            digest=captured["reference_digest"],
            ciphertext_size=captured["ciphertext_size"],
            ciphertext_sha256=captured["ciphertext_sha256"],
            nonce=captured["nonce"],
            plaintext_size=captured["plaintext_size"],
        )

    async def _mark(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.insert_writing", _insert
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_for_update_by_id",
        _select,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.mark_stored", _mark
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.write_ciphertext_atomic",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.verify_ciphertext_file",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(tracker),
    )
    root = tmp_path / "spool"
    root.mkdir()
    handle = await _store(
        root=root, reference_factory=lambda: references.pop(0)
    ).store(
        _JPEG,
        conversation_id=conversation_id,
        kind=AttachmentKind.IMAGE,
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
    )
    assert isinstance(handle, AttachmentHandle)
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_keyboard_interrupt_passthrough(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def _raise_ki(*_a: Any, **_k: Any) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.insert_writing",
        _raise_ki,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    root = tmp_path / "spool"
    root.mkdir()
    with pytest.raises(KeyboardInterrupt):
        await _store(root=root).store(
            _JPEG,
            conversation_id=uuid4(),
            kind=AttachmentKind.IMAGE,
            purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        )


@pytest.mark.asyncio
async def test_reconcile_promote_stale_writing_valid_final(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _writing_row(
        row_id=uuid4(),
        object_id=uuid4(),
        conversation_id=uuid4(),
        digest=secrets.token_bytes(32),
        ciphertext_size=48,
        ciphertext_sha256=secrets.token_bytes(32),
        nonce=secrets.token_bytes(12),
        plaintext_size=32,
    )

    async def _candidates(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [row]

    async def _still_stale(*_a: Any, **_k: Any) -> bool:
        return True

    async def _mark(*_a: Any, **_k: Any) -> bool:
        return True

    async def _empty(*_a: Any, **_k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _candidates,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.row_still_stale_writing",
        _still_stale,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.mark_stored", _mark
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.inspect_ciphertext_file",
        lambda *_a, **_k: CiphertextInspectStatus.VALID,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.safe_unlink_object_file",
        lambda *_a, **_k: CiphertextUnlinkStatus.ALREADY_MISSING,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    root = tmp_path / "spool"
    root.mkdir()
    result = await _store(root=root).reconcile(limit=10)
    assert result.promoted_to_stored == 1
    assert result.deleted_writing_rows == 0
    assert result.io_unavailable_skipped == 0
    assert "uuid" not in repr(result).lower()


@pytest.mark.asyncio
async def test_reconcile_fresh_writing_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _writing_row(
        row_id=uuid4(),
        object_id=uuid4(),
        conversation_id=uuid4(),
        digest=secrets.token_bytes(32),
        ciphertext_size=48,
        ciphertext_sha256=secrets.token_bytes(32),
        nonce=secrets.token_bytes(12),
        plaintext_size=32,
    )
    marked = {"n": 0}
    deleted = {"n": 0}

    async def _candidates(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [row]

    async def _still_stale(*_a: Any, **_k: Any) -> bool:
        return False

    async def _mark(*_a: Any, **_k: Any) -> bool:
        marked["n"] += 1
        return True

    async def _delete(*_a: Any, **_k: Any) -> None:
        deleted["n"] += 1

    async def _empty(*_a: Any, **_k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _candidates,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.row_still_stale_writing",
        _still_stale,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.mark_stored", _mark
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id", _delete
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    root = tmp_path / "spool"
    root.mkdir()
    result = await _store(root=root).reconcile(limit=10)
    assert result.promoted_to_stored == 0
    assert result.deleted_writing_rows == 0
    assert marked["n"] == 0
    assert deleted["n"] == 0


@pytest.mark.asyncio
async def test_reconcile_mismatch_deletes_after_successful_unlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _writing_row(
        row_id=uuid4(),
        object_id=uuid4(),
        conversation_id=uuid4(),
        digest=secrets.token_bytes(32),
        ciphertext_size=48,
        ciphertext_sha256=secrets.token_bytes(32),
        nonce=secrets.token_bytes(12),
        plaintext_size=32,
    )
    deleted: list[Any] = []
    unlinks: list[bool] = []

    async def _candidates(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [row]

    async def _still_stale(*_a: Any, **_k: Any) -> bool:
        return True

    async def _delete(*_a: Any, **kwargs: Any) -> None:
        deleted.append(kwargs["row_id"])

    async def _empty(*_a: Any, **_k: Any) -> list[Any]:
        return []

    def _inspect(*_a: Any, **_k: Any) -> CiphertextInspectStatus:
        return CiphertextInspectStatus.MISMATCH

    def _unlink(*_a: Any, **kwargs: Any) -> CiphertextUnlinkStatus:
        unlinks.append(bool(kwargs.get("final")))
        return CiphertextUnlinkStatus.REMOVED

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _candidates,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.row_still_stale_writing",
        _still_stale,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id", _delete
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.inspect_ciphertext_file",
        _inspect,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.safe_unlink_object_file",
        _unlink,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    root = tmp_path / "spool"
    root.mkdir()
    result = await _store(root=root).reconcile(limit=10)
    assert result.promoted_to_stored == 0
    assert result.deleted_writing_rows == 1
    assert deleted == [row.id]
    assert True in unlinks  # final unlinked before row delete


@pytest.mark.asyncio
async def test_reconcile_writing_permission_error_preserves_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _writing_row(
        row_id=uuid4(),
        object_id=uuid4(),
        conversation_id=uuid4(),
        digest=secrets.token_bytes(32),
        ciphertext_size=48,
        ciphertext_sha256=secrets.token_bytes(32),
        nonce=secrets.token_bytes(12),
        plaintext_size=32,
    )
    deleted = {"n": 0}

    async def _candidates(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [row]

    async def _still_stale(*_a: Any, **_k: Any) -> bool:
        return True

    async def _delete(*_a: Any, **_k: Any) -> None:
        deleted["n"] += 1

    async def _empty(*_a: Any, **_k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _candidates,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.row_still_stale_writing",
        _still_stale,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id", _delete
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.inspect_ciphertext_file",
        lambda *_a, **_k: CiphertextInspectStatus.IO_UNAVAILABLE,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    root = tmp_path / "spool"
    root.mkdir()
    result = await _store(root=root).reconcile(limit=10)
    assert result.promoted_to_stored == 0
    assert result.deleted_writing_rows == 0
    assert result.io_unavailable_skipped == 1
    assert deleted["n"] == 0


@pytest.mark.asyncio
async def test_reconcile_no_files_deletes_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _writing_row(
        row_id=uuid4(),
        object_id=uuid4(),
        conversation_id=uuid4(),
        digest=secrets.token_bytes(32),
        ciphertext_size=48,
        ciphertext_sha256=secrets.token_bytes(32),
        nonce=secrets.token_bytes(12),
        plaintext_size=32,
    )

    async def _candidates(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [row]

    async def _still_stale(*_a: Any, **_k: Any) -> bool:
        return True

    async def _delete(*_a: Any, **_k: Any) -> None:
        return None

    async def _empty(*_a: Any, **_k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _candidates,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.row_still_stale_writing",
        _still_stale,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id", _delete
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.inspect_ciphertext_file",
        lambda *_a, **_k: CiphertextInspectStatus.MISSING,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.probe_object_file",
        lambda *_a, **_k: CiphertextInspectStatus.MISSING,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    root = tmp_path / "spool"
    root.mkdir()
    result = await _store(root=root).reconcile(limit=10)
    assert result.deleted_writing_rows == 1
    assert result.promoted_to_stored == 0


@pytest.mark.asyncio
async def test_reconcile_symlink_increments_unsafe_and_keeps_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _writing_row(
        row_id=uuid4(),
        object_id=uuid4(),
        conversation_id=uuid4(),
        digest=secrets.token_bytes(32),
        ciphertext_size=48,
        ciphertext_sha256=secrets.token_bytes(32),
        nonce=secrets.token_bytes(12),
        plaintext_size=32,
    )
    deleted = {"n": 0}

    async def _candidates(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [row]

    async def _still_stale(*_a: Any, **_k: Any) -> bool:
        return True

    async def _delete(*_a: Any, **_k: Any) -> None:
        deleted["n"] += 1

    async def _empty(*_a: Any, **_k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _candidates,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.row_still_stale_writing",
        _still_stale,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id", _delete
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.inspect_ciphertext_file",
        lambda *_a, **_k: CiphertextInspectStatus.UNSAFE,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    root = tmp_path / "spool"
    root.mkdir()
    result = await _store(root=root).reconcile(limit=10)
    assert result.unsafe_skipped == 1
    assert result.promoted_to_stored == 0
    assert deleted["n"] == 0


@pytest.mark.asyncio
async def test_reconcile_unlink_permission_keeps_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _writing_row(
        row_id=uuid4(),
        object_id=uuid4(),
        conversation_id=uuid4(),
        digest=secrets.token_bytes(32),
        ciphertext_size=48,
        ciphertext_sha256=secrets.token_bytes(32),
        nonce=secrets.token_bytes(12),
        plaintext_size=32,
    )
    deleted = {"n": 0}

    async def _candidates(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [row]

    async def _still_stale(*_a: Any, **_k: Any) -> bool:
        return True

    async def _delete(*_a: Any, **_k: Any) -> None:
        deleted["n"] += 1

    async def _empty(*_a: Any, **_k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _candidates,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.row_still_stale_writing",
        _still_stale,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id", _delete
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.inspect_ciphertext_file",
        lambda *_a, **_k: CiphertextInspectStatus.MISMATCH,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.safe_unlink_object_file",
        lambda *_a, **_k: CiphertextUnlinkStatus.IO_UNAVAILABLE,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    root = tmp_path / "spool"
    root.mkdir()
    result = await _store(root=root).reconcile(limit=10)
    assert result.deleted_writing_rows == 0
    assert result.io_unavailable_skipped == 1
    assert deleted["n"] == 0


@pytest.mark.asyncio
async def test_reconcile_mismatch_unlink_before_repo_delete_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _writing_row(
        row_id=uuid4(),
        object_id=uuid4(),
        conversation_id=uuid4(),
        digest=secrets.token_bytes(32),
        ciphertext_size=48,
        ciphertext_sha256=secrets.token_bytes(32),
        nonce=secrets.token_bytes(12),
        plaintext_size=32,
    )
    events: list[str] = []

    async def _candidates(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [row]

    async def _still_stale(*_a: Any, **_k: Any) -> bool:
        return True

    async def _delete(*_a: Any, **_k: Any) -> None:
        events.append("delete_row")

    async def _empty(*_a: Any, **_k: Any) -> list[Any]:
        return []

    def _unlink(*_a: Any, **kwargs: Any) -> CiphertextUnlinkStatus:
        events.append(f"unlink_final={bool(kwargs.get('final'))}")
        return CiphertextUnlinkStatus.REMOVED

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _candidates,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.row_still_stale_writing",
        _still_stale,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id", _delete
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.inspect_ciphertext_file",
        lambda *_a, **_k: CiphertextInspectStatus.MISMATCH,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.safe_unlink_object_file",
        _unlink,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    root = tmp_path / "spool"
    root.mkdir()
    await _store(root=root).reconcile(limit=10)
    assert events == ["unlink_final=True", "unlink_final=False", "delete_row"]


@pytest.mark.asyncio
async def test_reconcile_stored_mismatch_unlinks_then_deletes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stored = AttachmentSpoolRow(
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
        state="STORED",
        reference_digest=secrets.token_bytes(32),
    )
    events: list[str] = []

    async def _no_writing(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _stored(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [stored]

    async def _delete(*_a: Any, **_k: Any) -> None:
        events.append("delete_row")

    def _unlink(*_a: Any, **_k: Any) -> CiphertextUnlinkStatus:
        events.append("unlink")
        return CiphertextUnlinkStatus.REMOVED

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _no_writing,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _stored,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id", _delete
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.inspect_ciphertext_file",
        lambda *_a, **_k: CiphertextInspectStatus.MISMATCH,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.safe_unlink_object_file",
        _unlink,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    root = tmp_path / "spool"
    root.mkdir()
    result = await _store(root=root).reconcile(limit=10)
    assert result.deleted_unrecoverable_stored == 1
    assert events == ["unlink", "delete_row"]


@pytest.mark.asyncio
async def test_reconcile_stored_unlink_permission_keeps_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stored = AttachmentSpoolRow(
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
        state="STORED",
        reference_digest=secrets.token_bytes(32),
    )
    deleted = {"n": 0}

    async def _no_writing(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _stored(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [stored]

    async def _delete(*_a: Any, **_k: Any) -> None:
        deleted["n"] += 1

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _no_writing,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _stored,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id", _delete
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.inspect_ciphertext_file",
        lambda *_a, **_k: CiphertextInspectStatus.MISMATCH,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.safe_unlink_object_file",
        lambda *_a, **_k: CiphertextUnlinkStatus.IO_UNAVAILABLE,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    root = tmp_path / "spool"
    root.mkdir()
    result = await _store(root=root).reconcile(limit=10)
    assert result.deleted_unrecoverable_stored == 0
    assert result.io_unavailable_skipped == 1
    assert deleted["n"] == 0


@pytest.mark.asyncio
async def test_reconcile_transient_and_unsafe_never_call_repo_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    writing = _writing_row(
        row_id=uuid4(),
        object_id=uuid4(),
        conversation_id=uuid4(),
        digest=secrets.token_bytes(32),
        ciphertext_size=48,
        ciphertext_sha256=secrets.token_bytes(32),
        nonce=secrets.token_bytes(12),
        plaintext_size=32,
    )
    deleted = {"n": 0}
    idx = {"n": 0}
    statuses = [
        CiphertextInspectStatus.IO_UNAVAILABLE,
        CiphertextInspectStatus.UNSAFE,
    ]

    async def _candidates(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [writing]

    async def _still_stale(*_a: Any, **_k: Any) -> bool:
        return True

    async def _delete(*_a: Any, **_k: Any) -> None:
        deleted["n"] += 1

    async def _empty(*_a: Any, **_k: Any) -> list[Any]:
        return []

    def _inspect(*_a: Any, **_k: Any) -> CiphertextInspectStatus:
        status = statuses[idx["n"] % len(statuses)]
        idx["n"] += 1
        return status

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _candidates,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.row_still_stale_writing",
        _still_stale,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id", _delete
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.inspect_ciphertext_file",
        _inspect,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    root = tmp_path / "spool"
    root.mkdir()
    await _store(root=root).reconcile(limit=10)
    assert deleted["n"] == 0


@pytest.mark.asyncio
async def test_reconcile_stored_missing_deletes_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stored = AttachmentSpoolRow(
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
        state="STORED",
        reference_digest=secrets.token_bytes(32),
    )
    deleted: list[Any] = []

    async def _no_writing(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _stored(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return [stored]

    async def _delete(*_a: Any, **kwargs: Any) -> None:
        deleted.append(kwargs["row_id"])

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _no_writing,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _stored,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id", _delete
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.inspect_ciphertext_file",
        lambda *_a, **_k: CiphertextInspectStatus.MISSING,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    root = tmp_path / "spool"
    root.mkdir()
    result = await _store(root=root).reconcile(limit=10)
    assert result.deleted_unrecoverable_stored == 1
    assert deleted == [stored.id]


@pytest.mark.asyncio
async def test_reconcile_stored_io_and_symlink_preserve_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rows = [
        AttachmentSpoolRow(
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
            state="STORED",
            reference_digest=secrets.token_bytes(32),
        )
        for _ in range(2)
    ]
    statuses = [
        CiphertextInspectStatus.IO_UNAVAILABLE,
        CiphertextInspectStatus.UNSAFE,
    ]
    idx = {"n": 0}
    deleted = {"n": 0}

    async def _no_writing(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _stored(*_a: Any, **_k: Any) -> list[AttachmentSpoolRow]:
        return rows

    async def _delete(*_a: Any, **_k: Any) -> None:
        deleted["n"] += 1

    def _inspect(*_a: Any, **_k: Any) -> CiphertextInspectStatus:
        status = statuses[idx["n"]]
        idx["n"] += 1
        return status

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _no_writing,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _stored,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.delete_by_id", _delete
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.inspect_ciphertext_file",
        _inspect,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    root = tmp_path / "spool"
    root.mkdir()
    result = await _store(root=root).reconcile(limit=10)
    assert result.deleted_unrecoverable_stored == 0
    assert result.io_unavailable_skipped == 1
    assert result.unsafe_skipped == 1
    assert deleted["n"] == 0


@pytest.mark.asyncio
async def test_orphan_scan_budget_counts_invalid_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "spool"
    shard = root / "ab"
    shard.mkdir(parents=True)
    for name in ("not-a-uuid.bin", "also-bad.tmp", "zz"):
        (shard / name).write_bytes(b"x")

    async def _empty(*_a: Any, **_k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [shard],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    store = _store(root=root)
    # limit=2 should stop after two inspected entries; unknown names not deleted.
    result = await store.reconcile(limit=2)
    assert result.deleted_orphan_temps == 0
    assert result.deleted_orphan_finals == 0
    assert (shard / "not-a-uuid.bin").exists()
    assert (shard / "also-bad.tmp").exists()
    assert (shard / "zz").exists()


@pytest.mark.asyncio
async def test_orphan_scan_wrong_shard_does_not_false_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    wrong_shard = root / "aa"
    wrong_shard.mkdir()
    object_id = uuid4()
    while object_id.hex[:2] == "aa":
        object_id = uuid4()
    orphan = wrong_shard / f"{object_id}.tmp"
    orphan.write_bytes(b"orphan")
    import os
    import time

    past = time.time() - 700
    os.utime(orphan, (past, past))

    async def _empty(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _exists(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.exists_by_object_id",
        _exists,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    result = await _store(root=root).reconcile(limit=100)
    assert result.deleted_orphan_temps == 0
    assert result.unsafe_skipped == 1
    assert orphan.exists()


@pytest.mark.asyncio
async def test_orphan_scan_simulated_unlink_noop_does_not_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    object_id = uuid4()
    shard = root / object_id.hex[:2]
    shard.mkdir(parents=True)
    orphan = shard / f"{object_id}.tmp"
    orphan.write_bytes(b"orphan")
    import os
    import time

    past = time.time() - 700
    os.utime(orphan, (past, past))

    async def _empty(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _exists(*_a: Any, **_k: Any) -> bool:
        return False

    def _noop_unlink(_path: object) -> None:
        return None

    monkeypatch.setattr(os, "unlink", _noop_unlink)
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.exists_by_object_id",
        _exists,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    result = await _store(root=root).reconcile(limit=100)
    assert result.deleted_orphan_temps == 0
    assert result.io_unavailable_skipped == 1
    assert orphan.exists()


@pytest.mark.asyncio
async def test_orphan_scan_skips_directory_symlink_via_iter_shards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    object_id = uuid4()
    real = root / object_id.hex[:2]
    real.mkdir(parents=True)
    orphan = real / f"{object_id}.tmp"
    orphan.write_bytes(b"orphan")
    import os
    import time

    past = time.time() - 700
    os.utime(orphan, (past, past))
    alias_shard = "ab" if object_id.hex[:2] != "ab" else "ac"
    link = root / alias_shard
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    async def _empty(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _exists(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _empty,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.exists_by_object_id",
        _exists,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    # Real iter_shard_dirs must not follow/include the directory symlink.
    result = await _store(root=root).reconcile(limit=100)
    assert result.deleted_orphan_temps == 1
    assert not orphan.exists()
    assert link.is_symlink()
    assert alias_shard not in {p.name for p in attachment_fs.iter_shard_dirs(root)}


@pytest.mark.asyncio
async def test_reject_non_bytes_and_mime_denied(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    store = _store(root=root)
    with pytest.raises(AttachmentError) as raised:
        await store.store(
            "not-bytes",  # type: ignore[arg-type]
            conversation_id=uuid4(),
            kind=AttachmentKind.IMAGE,
            purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        )
    assert raised.value.code == "ATTACHMENT_VALUE_INVALID"
    with pytest.raises(AttachmentError) as raised2:
        await store.store(
            b"not-an-image",
            conversation_id=uuid4(),
            kind=AttachmentKind.IMAGE,
            purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        )
    assert raised2.value.code == "ATTACHMENT_MIME_DENIED"


@pytest.mark.asyncio
async def test_reconcile_ignores_leased_and_delete_pending_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stored_calls: list[str] = []

    async def _no_writing(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _stored(*_a: Any, **_k: Any) -> list[Any]:
        stored_calls.append("called")
        return []

    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stale_writing_for_reconcile",
        _no_writing,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.spool_repo.select_stored_missing_file_candidates",
        _stored,
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.attachment_fs.iter_shard_dirs",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.attachment_spool_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    root = tmp_path / "spool"
    root.mkdir()
    result = await _store(root=root).reconcile(limit=10)
    assert stored_calls == ["called"]
    assert result.promoted_to_stored == 0
    assert result.deleted_unrecoverable_stored == 0
    repo_source = (
        _REPO_ROOT / "app/repositories/attachment_spool.py"
    ).read_text(encoding="utf-8")
    assert 'select_stored_missing_file_candidates' in repo_source
    stored_fn = repo_source.split("async def select_stored_missing_file_candidates", 1)[1]
    assert 'state == "STORED"' in stored_fn.split("async def ", 1)[0]
    writing_fn = repo_source.split("async def select_stale_writing_for_reconcile", 1)[1]
    assert 'state == "WRITING"' in writing_fn.split("async def ", 1)[0]
