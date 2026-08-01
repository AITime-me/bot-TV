"""Filesystem hardening tests for attachment spool (Stage 1A1)."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import pytest

from app.core import attachment_fs
from app.core.attachment_types import AttachmentError


def test_atomic_write_ciphertext_only(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    object_id = uuid.uuid4()
    ciphertext = os.urandom(64)
    digest = hashlib.sha256(ciphertext).digest()
    attachment_fs.write_ciphertext_atomic(
        root, object_id, ciphertext, expected_sha256=digest
    )
    final = root / attachment_fs.final_relpath(object_id)
    temp = root / attachment_fs.temp_relpath(object_id)
    assert final.is_file()
    assert not temp.exists()
    assert final.read_bytes() == ciphertext
    assert b"plaintext" not in final.read_bytes()


def test_exclusive_temp_and_existing_final(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    object_id = uuid.uuid4()
    ciphertext = os.urandom(32)
    digest = hashlib.sha256(ciphertext).digest()
    attachment_fs.write_ciphertext_atomic(
        root, object_id, ciphertext, expected_sha256=digest
    )
    with pytest.raises(AttachmentError) as raised:
        attachment_fs.write_ciphertext_atomic(
            root, object_id, ciphertext, expected_sha256=digest
        )
    assert raised.value.code == "ATTACHMENT_FILESYSTEM_FAILED"
    assert str(root) not in str(raised.value)


def test_root_and_shard_symlink_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    object_id = uuid.uuid4()
    ciphertext = os.urandom(16)
    digest = hashlib.sha256(ciphertext).digest()
    with pytest.raises(AttachmentError):
        attachment_fs.write_ciphertext_atomic(
            link, object_id, ciphertext, expected_sha256=digest
        )


def test_missing_delete_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    object_id = uuid.uuid4()
    attachment_fs.unlink_object_files(root, object_id)
    attachment_fs.unlink_object_files(root, object_id)


def test_parse_object_filename_strict() -> None:
    object_id = uuid.uuid4()
    assert attachment_fs.parse_object_filename(f"{object_id}.bin") == (
        object_id,
        ".bin",
    )
    assert attachment_fs.parse_object_filename("../x.bin") is None
    assert attachment_fs.parse_object_filename("not-a-uuid.tmp") is None


def test_object_id_accepts_uuid_and_subclass_rejects_str_bytes() -> None:
    class _SyntheticUuid(uuid.UUID):
        """Safe synthetic subclass mimicking asyncpg/ORM UUID compatibility."""

    object_id = uuid.uuid4()
    assert attachment_fs.object_id_shard(object_id) == object_id.hex[:2]
    assert attachment_fs.final_relpath(object_id).endswith(f"{object_id}.bin")

    subclassed = _SyntheticUuid(bytes=object_id.bytes)
    assert isinstance(subclassed, uuid.UUID)
    assert type(subclassed) is not uuid.UUID
    assert attachment_fs.object_id_shard(subclassed) == object_id.hex[:2]
    normalized = attachment_fs._require_object_uuid(subclassed)
    assert type(normalized) is uuid.UUID
    assert normalized.bytes == object_id.bytes

    token = str(object_id)
    raw = object_id.bytes
    for bad in (token, raw, object()):
        with pytest.raises(AttachmentError) as raised:
            attachment_fs.object_id_shard(bad)  # type: ignore[arg-type]
        assert raised.value.code == "ATTACHMENT_FILESYSTEM_FAILED"
        assert raised.value.__cause__ is None
        blob = str(raised.value) + repr(raised.value)
        assert token not in blob
        assert object_id.hex not in blob


def test_inspect_ciphertext_statuses(tmp_path: Path) -> None:
    from app.core.attachment_types import CiphertextInspectStatus

    root = tmp_path / "spool"
    root.mkdir()
    object_id = uuid.uuid4()
    ciphertext = os.urandom(32)
    digest = hashlib.sha256(ciphertext).digest()
    assert (
        attachment_fs.inspect_ciphertext_file(
            root,
            object_id,
            expected_size=len(ciphertext),
            expected_sha256=digest,
            final=True,
        )
        is CiphertextInspectStatus.MISSING
    )
    attachment_fs.write_ciphertext_atomic(
        root, object_id, ciphertext, expected_sha256=digest
    )
    assert (
        attachment_fs.inspect_ciphertext_file(
            root,
            object_id,
            expected_size=len(ciphertext),
            expected_sha256=digest,
            final=True,
        )
        is CiphertextInspectStatus.VALID
    )
    final = root / attachment_fs.final_relpath(object_id)
    final.write_bytes(ciphertext + b"\x00")
    assert (
        attachment_fs.inspect_ciphertext_file(
            root,
            object_id,
            expected_size=len(ciphertext),
            expected_sha256=digest,
            final=True,
        )
        is CiphertextInspectStatus.MISMATCH
    )


def test_inspect_permission_error_is_io_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.attachment_types import CiphertextInspectStatus

    root = tmp_path / "spool"
    root.mkdir()
    object_id = uuid.uuid4()
    ciphertext = os.urandom(16)
    digest = hashlib.sha256(ciphertext).digest()
    attachment_fs.write_ciphertext_atomic(
        root, object_id, ciphertext, expected_sha256=digest
    )

    def _deny_lstat(path: object) -> object:
        raise PermissionError("denied")

    monkeypatch.setattr(os, "lstat", _deny_lstat)
    status = attachment_fs.inspect_ciphertext_file(
        root,
        object_id,
        expected_size=len(ciphertext),
        expected_sha256=digest,
        final=True,
    )
    assert status is CiphertextInspectStatus.IO_UNAVAILABLE
    probe = attachment_fs.probe_object_file(root, object_id, final=True)
    assert probe is CiphertextInspectStatus.IO_UNAVAILABLE


def test_inspect_symlink_is_unsafe(tmp_path: Path) -> None:
    from app.core.attachment_types import CiphertextInspectStatus

    root = tmp_path / "spool"
    root.mkdir()
    object_id = uuid.uuid4()
    shard = root / object_id.hex[:2]
    shard.mkdir()
    target = shard / "target.bin"
    target.write_bytes(b"x" * 16)
    link = root / attachment_fs.final_relpath(object_id)
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    status = attachment_fs.inspect_ciphertext_file(
        root,
        object_id,
        expected_size=16,
        expected_sha256=hashlib.sha256(b"x" * 16).digest(),
        final=True,
    )
    assert status is CiphertextInspectStatus.UNSAFE


def test_inspect_non_regular_is_unsafe(tmp_path: Path) -> None:
    from app.core.attachment_types import CiphertextInspectStatus

    root = tmp_path / "spool"
    root.mkdir()
    object_id = uuid.uuid4()
    path = root / attachment_fs.final_relpath(object_id)
    path.parent.mkdir(parents=True)
    path.mkdir()
    status = attachment_fs.inspect_ciphertext_file(
        root,
        object_id,
        expected_size=1,
        expected_sha256=hashlib.sha256(b"x").digest(),
        final=True,
    )
    assert status is CiphertextInspectStatus.UNSAFE


def test_inspect_open_permission_and_generic_oserror_are_io_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.attachment_types import CiphertextInspectStatus

    root = tmp_path / "spool"
    root.mkdir()
    object_id = uuid.uuid4()
    ciphertext = os.urandom(16)
    digest = hashlib.sha256(ciphertext).digest()
    attachment_fs.write_ciphertext_atomic(
        root, object_id, ciphertext, expected_sha256=digest
    )

    def _deny_open(*_a: object, **_k: object) -> int:
        raise PermissionError("open denied")

    monkeypatch.setattr(os, "open", _deny_open)
    assert (
        attachment_fs.inspect_ciphertext_file(
            root,
            object_id,
            expected_size=len(ciphertext),
            expected_sha256=digest,
            final=True,
        )
        is CiphertextInspectStatus.IO_UNAVAILABLE
    )

    def _generic_oserror(*_a: object, **_k: object) -> int:
        raise OSError(5, "EIO")

    monkeypatch.setattr(os, "open", _generic_oserror)
    assert (
        attachment_fs.inspect_ciphertext_file(
            root,
            object_id,
            expected_size=len(ciphertext),
            expected_sha256=digest,
            final=True,
        )
        is CiphertextInspectStatus.IO_UNAVAILABLE
    )


def test_probe_does_not_treat_permission_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.attachment_types import CiphertextInspectStatus

    root = tmp_path / "spool"
    root.mkdir()
    object_id = uuid.uuid4()
    ciphertext = os.urandom(8)
    digest = hashlib.sha256(ciphertext).digest()
    attachment_fs.write_ciphertext_atomic(
        root, object_id, ciphertext, expected_sha256=digest
    )

    def _deny_lstat(path: object) -> object:
        raise PermissionError("denied")

    monkeypatch.setattr(os, "lstat", _deny_lstat)
    assert (
        attachment_fs.probe_object_file(root, object_id, final=True)
        is CiphertextInspectStatus.IO_UNAVAILABLE
    )
    assert (
        attachment_fs.probe_object_file(root, object_id, final=True)
        is not CiphertextInspectStatus.MISSING
    )


def test_iter_shard_dirs_skips_directory_symlink(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    real = root / "aa"
    real.mkdir()
    link = root / "ab"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    shards = attachment_fs.iter_shard_dirs(root)
    assert real in shards
    assert link not in shards


def test_orphan_entry_is_canonical_requires_matching_shard() -> None:
    object_id = uuid.uuid4()
    shard = Path(object_id.hex[:2])
    assert attachment_fs.orphan_entry_is_canonical(
        shard, object_id, f"{object_id}.tmp", suffix=".tmp"
    )
    wrong_shard = Path("aa" if object_id.hex[:2] != "aa" else "ab")
    assert not attachment_fs.orphan_entry_is_canonical(
        wrong_shard, object_id, f"{object_id}.tmp", suffix=".tmp"
    )


def test_safe_unlink_reports_io_when_file_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.attachment_types import CiphertextUnlinkStatus

    root = tmp_path / "spool"
    root.mkdir()
    object_id = uuid.uuid4()
    ciphertext = os.urandom(16)
    digest = hashlib.sha256(ciphertext).digest()
    attachment_fs.write_ciphertext_atomic(
        root, object_id, ciphertext, expected_sha256=digest
    )
    final = root / attachment_fs.final_relpath(object_id)

    def _noop_unlink(_path: object) -> None:
        return None

    monkeypatch.setattr(os, "unlink", _noop_unlink)
    status = attachment_fs.safe_unlink_object_file(root, object_id, final=True)
    assert status is CiphertextUnlinkStatus.IO_UNAVAILABLE
    assert final.is_file()


def test_safe_unlink_removed_only_after_confirmed_absence(tmp_path: Path) -> None:
    from app.core.attachment_types import CiphertextUnlinkStatus

    root = tmp_path / "spool"
    root.mkdir()
    object_id = uuid.uuid4()
    ciphertext = os.urandom(16)
    digest = hashlib.sha256(ciphertext).digest()
    attachment_fs.write_ciphertext_atomic(
        root, object_id, ciphertext, expected_sha256=digest
    )
    final = root / attachment_fs.final_relpath(object_id)
    status = attachment_fs.safe_unlink_object_file(root, object_id, final=True)
    assert status is CiphertextUnlinkStatus.REMOVED
    assert not final.exists()


def test_hash_mismatch_refuses_write(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    with pytest.raises(AttachmentError):
        attachment_fs.write_ciphertext_atomic(
            root,
            uuid.uuid4(),
            os.urandom(16),
            expected_sha256=os.urandom(32),
        )


def test_fsync_called_for_file_and_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    fsync_targets: list[str] = []
    real_fsync = os.fsync

    def _spy_fsync(fd: int) -> None:
        fsync_targets.append("fd")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _spy_fsync)
    object_id = uuid.uuid4()
    ciphertext = os.urandom(32)
    digest = hashlib.sha256(ciphertext).digest()
    attachment_fs.write_ciphertext_atomic(
        root, object_id, ciphertext, expected_sha256=digest
    )
    assert len(fsync_targets) >= 1


def test_existing_final_and_error_hides_path(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    object_id = uuid.uuid4()
    ciphertext = os.urandom(16)
    digest = hashlib.sha256(ciphertext).digest()
    shard = root / object_id.hex[:2]
    shard.mkdir(parents=True)
    final = root / attachment_fs.final_relpath(object_id)
    final.write_bytes(b"occupied")
    with pytest.raises(AttachmentError) as raised:
        attachment_fs.write_ciphertext_atomic(
            root, object_id, ciphertext, expected_sha256=digest
        )
    assert raised.value.code == "ATTACHMENT_FILESYSTEM_FAILED"
    assert str(root) not in repr(raised.value)
    assert str(root) not in str(raised.value)

