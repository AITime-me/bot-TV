"""Unit tests for attachment maintenance heartbeat contract (CURSOR-14)."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.attachment_maintenance_heartbeat import (
    DEFAULT_STALE_SECONDS,
    FUTURE_SKEW_SECONDS,
    HEARTBEAT_CONFIG_INVALID,
    HEARTBEAT_FUTURE,
    HEARTBEAT_MALFORMED,
    HEARTBEAT_MISSING,
    HEARTBEAT_OVERSIZED,
    HEARTBEAT_SCHEMA_VERSION,
    HEARTBEAT_STALE,
    HEARTBEAT_SYMLINK,
    HEARTBEAT_WRITE_FAILED,
    MAX_HEARTBEAT_BYTES,
    HeartbeatError,
    parse_stale_seconds,
    read_and_validate_heartbeat,
    serialize_heartbeat,
    write_attachment_maintenance_heartbeat,
)

_T0 = datetime(2026, 8, 2, 17, 24, 6, 123456, tzinfo=timezone.utc)


def _write_raw(path: Path, raw: bytes) -> None:
    path.write_bytes(raw)


def test_serialize_exact_compact_schema() -> None:
    raw = serialize_heartbeat(_T0)
    assert raw.endswith(b"\n")
    assert len(raw) <= MAX_HEARTBEAT_BYTES
    text = raw.decode("utf-8")
    assert text == (
        '{"v":1,"completed_at":"2026-08-02T17:24:06.123456+00:00"}\n'
    )
    data = json.loads(text)
    assert set(data) == {"v", "completed_at"}
    assert data["v"] == HEARTBEAT_SCHEMA_VERSION
    assert "object" not in text.lower()
    assert "key" not in text.lower()
    assert "error" not in text.lower()
    assert "secret" not in text.lower()


def test_atomic_write_mode_and_replace(tmp_path: Path) -> None:
    path = tmp_path / "bot-tv-attachment-maintenance-heartbeat.json"
    write_attachment_maintenance_heartbeat(_T0, path=path)
    assert path.is_file()
    assert not path.is_symlink()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == serialize_heartbeat(_T0)
    # no leftover temps
    temps = list(tmp_path.glob(".*.tmp.*"))
    assert temps == []

    later = _T0 + timedelta(seconds=60)
    write_attachment_maintenance_heartbeat(later, path=path)
    assert path.read_bytes() == serialize_heartbeat(later)


def test_final_symlink_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("x", encoding="utf-8")
    link = tmp_path / "bot-tv-attachment-maintenance-heartbeat.json"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(HeartbeatError) as raised:
        write_attachment_maintenance_heartbeat(_T0, path=link)
    assert raised.value.code == HEARTBEAT_SYMLINK


def test_non_regular_final_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bot-tv-attachment-maintenance-heartbeat.json"
    path.mkdir()
    with pytest.raises(HeartbeatError) as raised:
        write_attachment_maintenance_heartbeat(_T0, path=path)
    assert raised.value.code == HEARTBEAT_WRITE_FAILED


def test_missing_final_accepted(tmp_path: Path) -> None:
    path = tmp_path / "missing-heartbeat.json"
    write_attachment_maintenance_heartbeat(_T0, path=path)
    assert path.is_file()


def test_read_fresh_accepted(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    write_attachment_maintenance_heartbeat(_T0, path=path)
    got = read_and_validate_heartbeat(
        path=path,
        now=_T0 + timedelta(seconds=10),
        stale_seconds=180,
    )
    assert got == _T0


def test_read_stale_rejected(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    write_attachment_maintenance_heartbeat(_T0, path=path)
    with pytest.raises(HeartbeatError) as raised:
        read_and_validate_heartbeat(
            path=path,
            now=_T0 + timedelta(seconds=DEFAULT_STALE_SECONDS + 1),
            stale_seconds=DEFAULT_STALE_SECONDS,
        )
    assert raised.value.code == HEARTBEAT_STALE


def test_read_future_beyond_skew_rejected(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    future = _T0 + timedelta(seconds=FUTURE_SKEW_SECONDS + 5)
    write_attachment_maintenance_heartbeat(future, path=path)
    with pytest.raises(HeartbeatError) as raised:
        read_and_validate_heartbeat(path=path, now=_T0, stale_seconds=180)
    assert raised.value.code == HEARTBEAT_FUTURE


def test_read_small_allowed_clock_skew(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    slightly_future = _T0 + timedelta(seconds=FUTURE_SKEW_SECONDS)
    write_attachment_maintenance_heartbeat(slightly_future, path=path)
    got = read_and_validate_heartbeat(path=path, now=_T0, stale_seconds=180)
    assert got == slightly_future


def test_missing_rejected(tmp_path: Path) -> None:
    with pytest.raises(HeartbeatError) as raised:
        read_and_validate_heartbeat(path=tmp_path / "absent.json", now=_T0)
    assert raised.value.code == HEARTBEAT_MISSING


def test_malformed_json_rejected(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    _write_raw(path, b"{not-json\n")
    with pytest.raises(HeartbeatError) as raised:
        read_and_validate_heartbeat(path=path, now=_T0)
    assert raised.value.code == HEARTBEAT_MALFORMED


def test_duplicate_json_keys_rejected(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    _write_raw(
        path,
        b'{"v":1,"completed_at":"2026-08-02T17:24:06.123456+00:00",'
        b'"completed_at":"2026-08-02T17:24:06.123456+00:00"}\n',
    )
    with pytest.raises(HeartbeatError) as raised:
        read_and_validate_heartbeat(path=path, now=_T0)
    assert raised.value.code == HEARTBEAT_MALFORMED


def test_unknown_keys_rejected(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    _write_raw(
        path,
        b'{"v":1,"completed_at":"2026-08-02T17:24:06.123456+00:00","extra":1}\n',
    )
    with pytest.raises(HeartbeatError) as raised:
        read_and_validate_heartbeat(path=path, now=_T0)
    assert raised.value.code == HEARTBEAT_MALFORMED


def test_missing_keys_rejected(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    _write_raw(path, b'{"v":1}\n')
    with pytest.raises(HeartbeatError) as raised:
        read_and_validate_heartbeat(path=path, now=_T0)
    assert raised.value.code == HEARTBEAT_MALFORMED


def test_wrong_version_rejected(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    _write_raw(
        path,
        b'{"v":2,"completed_at":"2026-08-02T17:24:06.123456+00:00"}\n',
    )
    with pytest.raises(HeartbeatError) as raised:
        read_and_validate_heartbeat(path=path, now=_T0)
    assert raised.value.code == HEARTBEAT_MALFORMED


def test_bool_version_rejected(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    _write_raw(
        path,
        b'{"v":true,"completed_at":"2026-08-02T17:24:06.123456+00:00"}\n',
    )
    with pytest.raises(HeartbeatError) as raised:
        read_and_validate_heartbeat(path=path, now=_T0)
    assert raised.value.code == HEARTBEAT_MALFORMED


def test_naive_datetime_rejected_on_write() -> None:
    with pytest.raises(HeartbeatError) as raised:
        serialize_heartbeat(datetime(2026, 8, 2, 17, 24, 6))
    assert raised.value.code == HEARTBEAT_MALFORMED


def test_non_utc_offset_rejected_on_write() -> None:
    from datetime import timezone as tz

    offset = tz(timedelta(hours=5))
    with pytest.raises(HeartbeatError) as raised:
        serialize_heartbeat(datetime(2026, 8, 2, 17, 24, 6, tzinfo=offset))
    assert raised.value.code == HEARTBEAT_MALFORMED


def test_oversized_rejected(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    _write_raw(path, b"x" * (MAX_HEARTBEAT_BYTES + 1))
    with pytest.raises(HeartbeatError) as raised:
        read_and_validate_heartbeat(path=path, now=_T0)
    assert raised.value.code == HEARTBEAT_OVERSIZED


def test_empty_file_rejected(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    _write_raw(path, b"")
    with pytest.raises(HeartbeatError) as raised:
        read_and_validate_heartbeat(path=path, now=_T0)
    assert raised.value.code == HEARTBEAT_MALFORMED


def test_invalid_utf8_rejected(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    _write_raw(path, b"\xff\xfe{\n")
    with pytest.raises(HeartbeatError) as raised:
        read_and_validate_heartbeat(path=path, now=_T0)
    assert raised.value.code == HEARTBEAT_MALFORMED


def test_symlink_read_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    write_attachment_maintenance_heartbeat(_T0, path=real)
    link = tmp_path / "link.json"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(HeartbeatError) as raised:
        read_and_validate_heartbeat(path=link, now=_T0 + timedelta(seconds=1))
    assert raised.value.code == HEARTBEAT_SYMLINK


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_STALE_SECONDS),
        ("", DEFAULT_STALE_SECONDS),
        ("180", 180),
        ("30", 30),
        ("86400", 86400),
    ],
)
def test_parse_stale_seconds_ok(raw: str | None, expected: int) -> None:
    assert parse_stale_seconds(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["0", "29", "86401", "0180", "180.0", "true", "False", " 180", "180 ", "-1", "nope"],
)
def test_parse_stale_seconds_invalid(raw: str) -> None:
    with pytest.raises(HeartbeatError) as raised:
        parse_stale_seconds(raw)
    assert raised.value.code == HEARTBEAT_CONFIG_INVALID
