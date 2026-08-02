"""Unit tests for attachment maintenance Docker healthcheck CLI (CURSOR-14)."""

from __future__ import annotations

import importlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.attachment_maintenance_healthcheck import check_heartbeat, main
from app.core.attachment_maintenance_heartbeat import (
    HEARTBEAT_CONFIG_INVALID,
    HEARTBEAT_FUTURE,
    HEARTBEAT_MALFORMED,
    HEARTBEAT_MISSING,
    HEARTBEAT_OVERSIZED,
    HEARTBEAT_STALE,
    HEARTBEAT_SYMLINK,
    MAX_HEARTBEAT_BYTES,
    STALE_SECONDS_ENV,
    write_attachment_maintenance_heartbeat,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_T0 = datetime(2026, 8, 2, 17, 24, 6, 123456, tzinfo=timezone.utc)


def test_check_fresh_exit_zero_silent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "hb.json"
    write_attachment_maintenance_heartbeat(_T0, path=path)
    monkeypatch.setenv(STALE_SECONDS_ENV, "180")
    # Freeze "now" via read path injection through check_heartbeat path only:
    # write a heartbeat relative to wall clock instead.
    now = datetime.now(timezone.utc)
    write_attachment_maintenance_heartbeat(now, path=path)
    code = check_heartbeat(path=path, environ={STALE_SECONDS_ENV: "180"})
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""
    assert captured.err == ""


def test_check_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = check_heartbeat(path=tmp_path / "absent.json", environ={})
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.strip() == HEARTBEAT_MISSING


def test_check_stale(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "hb.json"
    write_attachment_maintenance_heartbeat(
        datetime.now(timezone.utc) - timedelta(seconds=200),
        path=path,
    )
    code = check_heartbeat(path=path, environ={STALE_SECONDS_ENV: "180"})
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.strip() == HEARTBEAT_STALE
    assert str(path) not in captured.err


def test_check_malformed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "hb.json"
    path.write_text("{bad", encoding="utf-8")
    code = check_heartbeat(path=path, environ={})
    assert code == 1
    assert capsys.readouterr().err.strip() == HEARTBEAT_MALFORMED


def test_check_future(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "hb.json"
    write_attachment_maintenance_heartbeat(
        datetime.now(timezone.utc) + timedelta(seconds=120),
        path=path,
    )
    code = check_heartbeat(path=path, environ={})
    assert code == 1
    assert capsys.readouterr().err.strip() == HEARTBEAT_FUTURE


def test_check_symlink(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real = tmp_path / "real.json"
    write_attachment_maintenance_heartbeat(datetime.now(timezone.utc), path=real)
    link = tmp_path / "link.json"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable")
    code = check_heartbeat(path=link, environ={})
    assert code == 1
    assert capsys.readouterr().err.strip() == HEARTBEAT_SYMLINK


def test_check_oversized(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "hb.json"
    path.write_bytes(b"x" * (MAX_HEARTBEAT_BYTES + 1))
    code = check_heartbeat(path=path, environ={})
    assert code == 1
    assert capsys.readouterr().err.strip() == HEARTBEAT_OVERSIZED


def test_invalid_stale_env(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "hb.json"
    write_attachment_maintenance_heartbeat(datetime.now(timezone.utc), path=path)
    code = check_heartbeat(path=path, environ={STALE_SECONDS_ENV: "nope"})
    assert code == 1
    assert capsys.readouterr().err.strip() == HEARTBEAT_CONFIG_INVALID


def test_no_traceback_and_no_env_leak(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "hb.json"
    path.write_text("{}", encoding="utf-8")
    secret_env = {
        STALE_SECONDS_ENV: "180",
        "DATABASE_URL": "postgresql://bot:secret@127.0.0.1/db",
        "ATTACHMENT_SPOOL_KEY_STAGE_V1": "abc",
    }
    code = check_heartbeat(path=path, environ=secret_env)
    err = capsys.readouterr().err
    assert code == 1
    assert "Traceback" not in err
    assert "secret" not in err
    assert "DATABASE_URL" not in err
    assert "ATTACHMENT_SPOOL_KEY" not in err
    assert str(path) not in err


def test_module_has_no_db_or_keyring_imports() -> None:
    source = Path(
        importlib.import_module("app.attachment_maintenance_healthcheck").__file__
        or ""
    ).read_text(encoding="utf-8")
    assert "from app.db" not in source
    assert "import app.db" not in source
    assert "from app.config" not in source
    assert "import app.config" not in source
    assert "attachment_keys" not in source
    assert "AttachmentSpoolStore" not in source
    assert "create_engine" not in source


def test_python_b_module_contract(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    write_attachment_maintenance_heartbeat(datetime.now(timezone.utc), path=path)
    env = {
        **dict(**{k: v for k, v in __import__("os").environ.items()}),
        STALE_SECONDS_ENV: "180",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    # Invoke check via -c to avoid relying on DEFAULT path in module main.
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            (
                "from pathlib import Path; "
                "from app.attachment_maintenance_healthcheck import check_heartbeat; "
                f"raise SystemExit(check_heartbeat(path=Path(r'{path}')))"
            ),
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_main_uses_default_path_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "app.attachment_maintenance_healthcheck.DEFAULT_HEARTBEAT_PATH",
        Path("/tmp/bot-tv-attachment-maintenance-heartbeat-missing-for-test.json"),
    )
    assert main() == 1
    assert capsys.readouterr().err.strip() == HEARTBEAT_MISSING
