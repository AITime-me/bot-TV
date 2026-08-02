"""Attachment maintenance SUCCESS-cycle heartbeat (CURSOR-14).

Filesystem-only signal under existing container tmpfs. No PII, secrets,
object IDs, paths, or DB rows. Shared by the maintenance process writer and
the Docker healthcheck reader.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

HEARTBEAT_SCHEMA_VERSION: Final[int] = 1
DEFAULT_HEARTBEAT_PATH: Final[Path] = Path(
    "/tmp/bot-tv-attachment-maintenance-heartbeat.json"
)
MAX_HEARTBEAT_BYTES: Final[int] = 256
DEFAULT_STALE_SECONDS: Final[int] = 180
MIN_STALE_SECONDS: Final[int] = 30
MAX_STALE_SECONDS: Final[int] = 86400
FUTURE_SKEW_SECONDS: Final[int] = 30
STALE_SECONDS_ENV: Final[str] = "ATTACHMENT_MAINTENANCE_HEARTBEAT_STALE_SECONDS"

HEARTBEAT_MISSING: Final[str] = "HEARTBEAT_MISSING"
HEARTBEAT_STALE: Final[str] = "HEARTBEAT_STALE"
HEARTBEAT_MALFORMED: Final[str] = "HEARTBEAT_MALFORMED"
HEARTBEAT_FUTURE: Final[str] = "HEARTBEAT_FUTURE"
HEARTBEAT_SYMLINK: Final[str] = "HEARTBEAT_SYMLINK"
HEARTBEAT_OVERSIZED: Final[str] = "HEARTBEAT_OVERSIZED"
HEARTBEAT_IO: Final[str] = "HEARTBEAT_IO"
HEARTBEAT_CONFIG_INVALID: Final[str] = "HEARTBEAT_CONFIG_INVALID"
HEARTBEAT_WRITE_FAILED: Final[str] = "HEARTBEAT_WRITE_FAILED"

_ALLOWED_KEYS: Final[frozenset[str]] = frozenset({"v", "completed_at"})

_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_BINARY"):
    _OPEN_FLAGS |= os.O_BINARY
if hasattr(os, "O_CLOEXEC"):
    _OPEN_FLAGS |= os.O_CLOEXEC
if hasattr(os, "O_NOFOLLOW"):
    _OPEN_FLAGS |= os.O_NOFOLLOW

_FILE_MODE = 0o600


class HeartbeatError(Exception):
    """Fail-closed heartbeat error with a safe machine-readable code only."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or code == "":
            raise ValueError("heartbeat error code must be a non-empty str")
        self.code = code
        super().__init__(code)


def _reject_json_constant(_value: str) -> None:
    raise HeartbeatError(HEARTBEAT_MALFORMED)


def _object_pairs_no_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str:
            raise HeartbeatError(HEARTBEAT_MALFORMED)
        if key in out:
            raise HeartbeatError(HEARTBEAT_MALFORMED)
        out[key] = value
    return out


def serialize_heartbeat(completed_at: datetime) -> bytes:
    """Return exact compact UTF-8 JSON payload including trailing newline."""
    if not isinstance(completed_at, datetime):
        raise HeartbeatError(HEARTBEAT_MALFORMED)
    if completed_at.tzinfo is None or completed_at.utcoffset() is None:
        raise HeartbeatError(HEARTBEAT_MALFORMED)
    if completed_at.utcoffset() != timedelta(0):
        raise HeartbeatError(HEARTBEAT_MALFORMED)
    payload = {
        "v": HEARTBEAT_SCHEMA_VERSION,
        "completed_at": completed_at.isoformat(),
    }
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n"
    raw = text.encode("utf-8")
    if len(raw) > MAX_HEARTBEAT_BYTES:
        raise HeartbeatError(HEARTBEAT_OVERSIZED)
    return raw


def _unlink_best_effort(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except Exception:
        return


def _lstat_final_or_missing(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if getattr(exc, "errno", None) == getattr(__import__("errno"), "ENOENT"):
            return None
        raise HeartbeatError(HEARTBEAT_IO) from None


def write_attachment_maintenance_heartbeat(
    completed_at: datetime,
    *,
    path: Path | None = None,
) -> None:
    """Atomically write a SUCCESS-cycle heartbeat under the same tmpfs directory."""
    target = DEFAULT_HEARTBEAT_PATH if path is None else path
    if not isinstance(target, Path):
        raise HeartbeatError(HEARTBEAT_WRITE_FAILED)
    try:
        raw = serialize_heartbeat(completed_at)
    except HeartbeatError:
        raise
    except Exception:
        raise HeartbeatError(HEARTBEAT_WRITE_FAILED) from None

    parent = target.parent
    try:
        # Production default must stay under container tmpfs `/tmp`.
        if path is None and parent != Path("/tmp"):
            raise HeartbeatError(HEARTBEAT_WRITE_FAILED)
        parent_stat = os.lstat(parent)
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
            raise HeartbeatError(HEARTBEAT_WRITE_FAILED)
    except HeartbeatError:
        raise
    except Exception:
        raise HeartbeatError(HEARTBEAT_WRITE_FAILED) from None

    final_stat = _lstat_final_or_missing(target)
    if final_stat is not None:
        if stat.S_ISLNK(final_stat.st_mode):
            raise HeartbeatError(HEARTBEAT_SYMLINK)
        if not stat.S_ISREG(final_stat.st_mode):
            raise HeartbeatError(HEARTBEAT_WRITE_FAILED)

    tmp_name = f".{target.name}.tmp.{os.getpid()}"
    tmp_path = parent / tmp_name
    fd: int | None = None
    try:
        fd = os.open(tmp_path, _OPEN_FLAGS, _FILE_MODE)
        if os.path.islink(tmp_path):
            raise HeartbeatError(HEARTBEAT_WRITE_FAILED)
        written = 0
        view = memoryview(raw)
        while written < len(raw):
            n = os.write(fd, view[written:])
            if n <= 0:
                raise HeartbeatError(HEARTBEAT_WRITE_FAILED)
            written += n
        os.fsync(fd)
    except HeartbeatError:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        _unlink_best_effort(tmp_path)
        raise
    except (KeyboardInterrupt, SystemExit):
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        _unlink_best_effort(tmp_path)
        raise
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        _unlink_best_effort(tmp_path)
        raise HeartbeatError(HEARTBEAT_WRITE_FAILED) from None
    else:
        try:
            os.close(fd)
            fd = None
            # Re-check final path immediately before replace.
            final_stat = _lstat_final_or_missing(target)
            if final_stat is not None:
                if stat.S_ISLNK(final_stat.st_mode):
                    raise HeartbeatError(HEARTBEAT_SYMLINK)
                if not stat.S_ISREG(final_stat.st_mode):
                    raise HeartbeatError(HEARTBEAT_WRITE_FAILED)
            os.replace(tmp_path, target)
            try:
                os.chmod(target, _FILE_MODE)
            except Exception:
                pass
        except HeartbeatError:
            _unlink_best_effort(tmp_path)
            raise
        except (KeyboardInterrupt, SystemExit):
            _unlink_best_effort(tmp_path)
            raise
        except Exception:
            _unlink_best_effort(tmp_path)
            raise HeartbeatError(HEARTBEAT_WRITE_FAILED) from None


def parse_stale_seconds(raw: str | None) -> int:
    """Parse ATTACHMENT_MAINTENANCE_HEARTBEAT_STALE_SECONDS fail-closed."""
    if raw is None or raw == "":
        return DEFAULT_STALE_SECONDS
    if type(raw) is not str:
        raise HeartbeatError(HEARTBEAT_CONFIG_INVALID)
    if raw != raw.strip():
        raise HeartbeatError(HEARTBEAT_CONFIG_INVALID)
    if not raw.isdigit() or (len(raw) > 1 and raw.startswith("0")):
        raise HeartbeatError(HEARTBEAT_CONFIG_INVALID)
    value = int(raw)
    if type(value) is not int or isinstance(value, bool):
        raise HeartbeatError(HEARTBEAT_CONFIG_INVALID)
    if not MIN_STALE_SECONDS <= value <= MAX_STALE_SECONDS:
        raise HeartbeatError(HEARTBEAT_CONFIG_INVALID)
    return value


def _parse_completed_at(raw: object) -> datetime:
    if type(raw) is not str or raw == "":
        raise HeartbeatError(HEARTBEAT_MALFORMED)
    try:
        normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        raise HeartbeatError(HEARTBEAT_MALFORMED) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HeartbeatError(HEARTBEAT_MALFORMED)
    if parsed.utcoffset() != timedelta(0):
        raise HeartbeatError(HEARTBEAT_MALFORMED)
    return parsed


def read_and_validate_heartbeat(
    *,
    path: Path | None = None,
    now: datetime | None = None,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
) -> datetime:
    """Read and validate heartbeat; return completed_at on success."""
    if type(stale_seconds) is not int or isinstance(stale_seconds, bool):
        raise HeartbeatError(HEARTBEAT_CONFIG_INVALID)
    if not MIN_STALE_SECONDS <= stale_seconds <= MAX_STALE_SECONDS:
        raise HeartbeatError(HEARTBEAT_CONFIG_INVALID)

    target = DEFAULT_HEARTBEAT_PATH if path is None else path
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        raise HeartbeatError(HEARTBEAT_CONFIG_INVALID)
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise HeartbeatError(HEARTBEAT_CONFIG_INVALID)

    try:
        st = os.lstat(target)
    except FileNotFoundError:
        raise HeartbeatError(HEARTBEAT_MISSING) from None
    except OSError:
        raise HeartbeatError(HEARTBEAT_IO) from None

    if stat.S_ISLNK(st.st_mode):
        raise HeartbeatError(HEARTBEAT_SYMLINK)
    if not stat.S_ISREG(st.st_mode):
        raise HeartbeatError(HEARTBEAT_MALFORMED)
    if st.st_size <= 0:
        raise HeartbeatError(HEARTBEAT_MALFORMED)
    if st.st_size > MAX_HEARTBEAT_BYTES:
        raise HeartbeatError(HEARTBEAT_OVERSIZED)

    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(target, flags)
        try:
            raw = os.read(fd, MAX_HEARTBEAT_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        raise HeartbeatError(HEARTBEAT_IO) from None

    if len(raw) > MAX_HEARTBEAT_BYTES:
        raise HeartbeatError(HEARTBEAT_OVERSIZED)
    if raw == b"":
        raise HeartbeatError(HEARTBEAT_MALFORMED)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HeartbeatError(HEARTBEAT_MALFORMED) from None

    try:
        data = json.loads(
            text,
            object_pairs_hook=_object_pairs_no_duplicates,
            parse_constant=_reject_json_constant,
        )
    except HeartbeatError:
        raise
    except Exception:
        raise HeartbeatError(HEARTBEAT_MALFORMED) from None

    if type(data) is not dict:
        raise HeartbeatError(HEARTBEAT_MALFORMED)
    if set(data.keys()) != _ALLOWED_KEYS:
        raise HeartbeatError(HEARTBEAT_MALFORMED)
    version = data["v"]
    if type(version) is not int or isinstance(version, bool):
        raise HeartbeatError(HEARTBEAT_MALFORMED)
    if version != HEARTBEAT_SCHEMA_VERSION:
        raise HeartbeatError(HEARTBEAT_MALFORMED)

    completed_at = _parse_completed_at(data["completed_at"])
    if completed_at > now + timedelta(seconds=FUTURE_SKEW_SECONDS):
        raise HeartbeatError(HEARTBEAT_FUTURE)
    age = (now - completed_at).total_seconds()
    if age > stale_seconds:
        raise HeartbeatError(HEARTBEAT_STALE)
    return completed_at
