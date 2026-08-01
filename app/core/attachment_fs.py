"""Filesystem helpers for attachment spool ciphertext only.

Spool root must not be writable by untrusted OS users. Paths are derived only
from random object_id values — never from client filenames or external input.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Final

from app.core.attachment_types import (
    SHA256_DIGEST_BYTES,
    AttachmentError,
    CiphertextInspectStatus,
    CiphertextUnlinkStatus,
)

_UUID_HEX_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY if hasattr(os, "O_BINARY") else (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL
)
if hasattr(os, "O_NOFOLLOW"):
    _OPEN_FLAGS |= os.O_NOFOLLOW

_FILE_MODE = 0o600
_DIR_MODE = 0o700
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0)
if hasattr(os, "O_NOFOLLOW"):
    _READ_FLAGS |= os.O_NOFOLLOW


def _is_confirmed_missing(exc: BaseException) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    if isinstance(exc, OSError) and exc.errno == errno.ENOENT:
        return True
    return False


def _is_io_unavailable(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if not isinstance(exc, OSError):
        return False
    if exc.errno == errno.ENOENT:
        return False
    return True


def _lstat_confirms_missing(path: Path) -> bool:
    try:
        os.lstat(path)
    except (KeyboardInterrupt, SystemExit):
        raise
    except OSError as exc:
        return _is_confirmed_missing(exc)
    return False


def _is_safe_shard_dir_entry(entry: os.DirEntry[str]) -> bool:
    name = entry.name
    if len(name) != 2:
        return False
    if any(c not in "0123456789abcdef" for c in name):
        return False
    try:
        st = os.lstat(entry.path)
    except (KeyboardInterrupt, SystemExit):
        raise
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    file_attributes = getattr(st, "st_file_attributes", 0)
    if file_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        return False
    return True


def orphan_entry_is_canonical(
    shard_dir: Path,
    object_id: uuid.UUID,
    entry_name: str,
    *,
    suffix: str,
) -> bool:
    """True when scandir entry matches the internal canonical object path."""
    object_id = _require_object_uuid(object_id)
    if type(entry_name) is not str or type(suffix) is not str:
        return False
    if shard_dir.name != object_id_shard(object_id):
        return False
    return entry_name == f"{object_id}{suffix}"


def _require_object_uuid(object_id: object) -> uuid.UUID:
    """Accept stdlib or UUID-compatible subclass; normalize to uuid.UUID.

    Rejects str/bytes/arbitrary objects. No string parsing/coercion.
    """
    if not isinstance(object_id, uuid.UUID):
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
    try:
        return uuid.UUID(bytes=object_id.bytes)
    except (AttributeError, TypeError, ValueError):
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None


def object_id_shard(object_id: uuid.UUID) -> str:
    object_id = _require_object_uuid(object_id)
    return object_id.hex[:2]


def temp_relpath(object_id: uuid.UUID) -> str:
    return f"{object_id_shard(object_id)}/{object_id}.tmp"


def final_relpath(object_id: uuid.UUID) -> str:
    return f"{object_id_shard(object_id)}/{object_id}.bin"


def _ensure_under_root(root: Path, candidate: Path) -> Path:
    try:
        root_resolved = root.resolve(strict=False)
        candidate_resolved = candidate.resolve(strict=False)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError:
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
    return candidate


def _require_dir_not_symlink(path: Path) -> None:
    try:
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode):
                raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
    except AttachmentError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None


def _require_absolute_path(root: object) -> Path:
    # Path is abstract; concrete values are PosixPath/WindowsPath.
    if not isinstance(root, Path):
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
    if not root.is_absolute():
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
    return root


def ensure_spool_root(root: Path) -> None:
    root = _require_absolute_path(root)
    try:
        if root.is_symlink():
            raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
        if not root.exists():
            os.makedirs(root, mode=_DIR_MODE, exist_ok=True)
        _require_dir_not_symlink(root)
    except AttachmentError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None


def ensure_shard_directory(root: Path, object_id: uuid.UUID) -> Path:
    root = _require_absolute_path(root)
    object_id = _require_object_uuid(object_id)
    ensure_spool_root(root)
    _require_dir_not_symlink(root)
    shard = object_id_shard(object_id)
    shard_dir = _ensure_under_root(root, root / shard)
    try:
        if shard_dir.exists() or shard_dir.is_symlink():
            _require_dir_not_symlink(shard_dir)
        else:
            os.mkdir(shard_dir, mode=_DIR_MODE)
            _require_dir_not_symlink(shard_dir)
    except AttachmentError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
    return shard_dir


def write_ciphertext_atomic(
    root: Path,
    object_id: uuid.UUID,
    ciphertext: bytes,
    *,
    expected_sha256: bytes,
) -> None:
    """Write ciphertext via exclusive temp + fsync + atomic rename."""
    object_id = _require_object_uuid(object_id)
    if type(ciphertext) is not bytes or ciphertext == b"":
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
    if type(expected_sha256) is not bytes or len(expected_sha256) != SHA256_DIGEST_BYTES:
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
    if hashlib.sha256(ciphertext).digest() != expected_sha256:
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None

    shard_dir = ensure_shard_directory(root, object_id)
    temp_path = _ensure_under_root(root, root / temp_relpath(object_id))
    final_path = _ensure_under_root(root, root / final_relpath(object_id))

    if temp_path.is_symlink() or final_path.is_symlink():
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
    if final_path.exists():
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None

    fd: int | None = None
    try:
        fd = os.open(temp_path, _OPEN_FLAGS, _FILE_MODE)
        if os.path.islink(temp_path):
            raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
        written = 0
        view = memoryview(ciphertext)
        while written < len(ciphertext):
            n = os.write(fd, view[written:])
            if n <= 0:
                raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
            written += n
        os.fsync(fd)
    except AttachmentError:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        _unlink_best_effort(temp_path)
        raise
    except (KeyboardInterrupt, SystemExit):
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        _unlink_best_effort(temp_path)
        raise
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        _unlink_best_effort(temp_path)
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
    else:
        try:
            os.close(fd)
            fd = None
            # Verify temp before rename.
            verify_ciphertext_file(
                root,
                object_id,
                expected_size=len(ciphertext),
                expected_sha256=expected_sha256,
                final=False,
            )
            if final_path.exists() or final_path.is_symlink():
                raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
            os.replace(temp_path, final_path)
            _fsync_directory(shard_dir)
        except AttachmentError:
            _unlink_best_effort(temp_path)
            _unlink_best_effort(final_path)
            raise
        except (KeyboardInterrupt, SystemExit):
            _unlink_best_effort(temp_path)
            raise
        except Exception:
            _unlink_best_effort(temp_path)
            _unlink_best_effort(final_path)
            raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None


def inspect_ciphertext_file(
    root: Path,
    object_id: uuid.UUID,
    *,
    expected_size: int,
    expected_sha256: bytes,
    final: bool,
) -> CiphertextInspectStatus:
    """Inspect ciphertext without destroying data on transient IO errors."""
    object_id = _require_object_uuid(object_id)
    if type(expected_size) is not int or isinstance(expected_size, bool) or expected_size < 0:
        return CiphertextInspectStatus.UNSAFE
    if type(expected_sha256) is not bytes or len(expected_sha256) != SHA256_DIGEST_BYTES:
        return CiphertextInspectStatus.UNSAFE
    try:
        root = _require_absolute_path(root)
        rel = final_relpath(object_id) if final else temp_relpath(object_id)
        path = _ensure_under_root(root, root / rel)
    except AttachmentError:
        return CiphertextInspectStatus.UNSAFE

    try:
        if path.is_symlink():
            return CiphertextInspectStatus.UNSAFE
        st = os.lstat(path)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        if _is_confirmed_missing(exc):
            return CiphertextInspectStatus.MISSING
        if _is_io_unavailable(exc):
            return CiphertextInspectStatus.IO_UNAVAILABLE
        return CiphertextInspectStatus.IO_UNAVAILABLE

    if not stat.S_ISREG(st.st_mode):
        return CiphertextInspectStatus.UNSAFE
    size_mismatch = st.st_size != expected_size

    fd: int | None = None
    try:
        fd = os.open(path, _READ_FLAGS)
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            return CiphertextInspectStatus.UNSAFE
        if fst.st_size != expected_size:
            size_mismatch = True
        digest = hashlib.sha256()
        remaining = fst.st_size
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        hash_ok = digest.digest() == expected_sha256
        if size_mismatch or fst.st_size != expected_size or not hash_ok:
            return CiphertextInspectStatus.MISMATCH
        return CiphertextInspectStatus.VALID
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        if _is_confirmed_missing(exc):
            return CiphertextInspectStatus.MISSING
        if _is_io_unavailable(exc):
            return CiphertextInspectStatus.IO_UNAVAILABLE
        return CiphertextInspectStatus.IO_UNAVAILABLE
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass


def probe_object_file(
    root: Path,
    object_id: uuid.UUID,
    *,
    final: bool,
) -> CiphertextInspectStatus:
    """Presence/type probe without hash. VALID means regular file present."""
    object_id = _require_object_uuid(object_id)
    try:
        root = _require_absolute_path(root)
        rel = final_relpath(object_id) if final else temp_relpath(object_id)
        path = _ensure_under_root(root, root / rel)
    except AttachmentError:
        return CiphertextInspectStatus.UNSAFE
    try:
        if path.is_symlink():
            return CiphertextInspectStatus.UNSAFE
        st = os.lstat(path)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        if _is_confirmed_missing(exc):
            return CiphertextInspectStatus.MISSING
        if _is_io_unavailable(exc):
            return CiphertextInspectStatus.IO_UNAVAILABLE
        return CiphertextInspectStatus.IO_UNAVAILABLE
    if not stat.S_ISREG(st.st_mode):
        return CiphertextInspectStatus.UNSAFE
    return CiphertextInspectStatus.VALID


def verify_ciphertext_file(
    root: Path,
    object_id: uuid.UUID,
    *,
    expected_size: int,
    expected_sha256: bytes,
    final: bool,
) -> None:
    """Store-path helper: require VALID or raise fixed filesystem error."""
    status = inspect_ciphertext_file(
        root,
        object_id,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
        final=final,
    )
    if status is not CiphertextInspectStatus.VALID:
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None


def safe_unlink_object_file(
    root: Path,
    object_id: uuid.UUID,
    *,
    final: bool,
) -> CiphertextUnlinkStatus:
    """Unlink a regular ciphertext file. Never follows symlinks."""
    object_id = _require_object_uuid(object_id)
    try:
        root = _require_absolute_path(root)
        rel = final_relpath(object_id) if final else temp_relpath(object_id)
        path = _ensure_under_root(root, root / rel)
    except AttachmentError:
        return CiphertextUnlinkStatus.UNSAFE
    try:
        if path.is_symlink():
            return CiphertextUnlinkStatus.UNSAFE
        st = os.lstat(path)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        if _is_confirmed_missing(exc):
            return CiphertextUnlinkStatus.ALREADY_MISSING
        if _is_io_unavailable(exc):
            return CiphertextUnlinkStatus.IO_UNAVAILABLE
        return CiphertextUnlinkStatus.IO_UNAVAILABLE
    if not stat.S_ISREG(st.st_mode):
        return CiphertextUnlinkStatus.UNSAFE
    try:
        os.unlink(path)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        if _is_confirmed_missing(exc):
            return CiphertextUnlinkStatus.ALREADY_MISSING
        if _is_io_unavailable(exc):
            return CiphertextUnlinkStatus.IO_UNAVAILABLE
        return CiphertextUnlinkStatus.IO_UNAVAILABLE
    if _lstat_confirms_missing(path):
        return CiphertextUnlinkStatus.REMOVED
    return CiphertextUnlinkStatus.IO_UNAVAILABLE


def unlink_succeeded(status: CiphertextUnlinkStatus) -> bool:
    return status in {
        CiphertextUnlinkStatus.REMOVED,
        CiphertextUnlinkStatus.ALREADY_MISSING,
    }


def unlink_temp(root: Path, object_id: uuid.UUID) -> CiphertextUnlinkStatus:
    """Safe unlink of temp path for object_id."""
    return safe_unlink_object_file(root, object_id, final=False)


def unlink_final(root: Path, object_id: uuid.UUID) -> CiphertextUnlinkStatus:
    """Safe unlink of final path for object_id."""
    return safe_unlink_object_file(root, object_id, final=True)


def unlink_object_files(
    root: Path, object_id: uuid.UUID
) -> tuple[CiphertextUnlinkStatus, CiphertextUnlinkStatus]:
    """Safe unlink of temp and final. Returns (temp_status, final_status)."""
    object_id = _require_object_uuid(object_id)
    return (
        unlink_temp(root, object_id),
        unlink_final(root, object_id),
    )


def _unlink_best_effort(path: Path) -> None:
    try:
        if path.is_symlink():
            return
        if path.exists():
            path.unlink()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return
    try:
        os.fsync(fd)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        pass
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


def parse_object_filename(name: str) -> tuple[uuid.UUID, str] | None:
    """Return (object_id, suffix) for exact internal names only."""
    if type(name) is not str:
        return None
    if name.endswith(".tmp"):
        stem = name[:-4]
        suffix = ".tmp"
    elif name.endswith(".bin"):
        stem = name[:-4]
        suffix = ".bin"
    else:
        return None
    if _UUID_HEX_RE.fullmatch(stem) is None:
        return None
    try:
        return uuid.UUID(stem), suffix
    except Exception:
        return None


def iter_shard_dirs(root: Path) -> list[Path]:
    _require_dir_not_symlink(root)
    results: list[Path] = []
    try:
        for entry in os.scandir(root):
            if not _is_safe_shard_dir_entry(entry):
                continue
            results.append(Path(entry.path))
    except AttachmentError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
    return results


def file_mtime_age_seconds(path: Path) -> float | None:
    try:
        if path.is_symlink():
            return None
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return None
        return max(0.0, float(__import__("time").time() - st.st_mtime))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return None
