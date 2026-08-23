"""Static contracts for bot-TV staging backup + isolated restore-test ops."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "ops"


def test_ops_scripts_present() -> None:
    required = [
        OPS / "staging-backup-db.sh",
        OPS / "isolated-restore-test.sh",
        OPS / "lib" / "staging-ops-common.sh",
        OPS / "lib" / "isolated-restore-test-common.sh",
        OPS / "lib" / "isolated-restore-test-policy.sh",
        OPS / "lib" / "fake-docker-irt.sh",
        OPS / "tests" / "isolated-restore-test-harness.sh",
        ROOT / "docs" / "operations" / "staging-backup.md",
        ROOT / "docs" / "operations" / "isolated-restore-test.md",
    ]
    missing = [str(p.relative_to(ROOT)) for p in required if not p.is_file()]
    assert not missing, f"missing ops artifacts: {missing}"


def test_irt_staging_only_and_isolation_markers() -> None:
    text = (OPS / "isolated-restore-test.sh").read_text(encoding="utf-8")
    assert "--network none" in text
    assert "--pull=never" in text
    assert "bot-tv-rt-staging-" in text
    assert "com.bot-tv.component" in text or "IRT_LABEL_COMPONENT" in text
    assert "production|staging" not in text
    assert "--migration-proof" not in text
    assert "offline-runner" not in text


def test_staging_backup_uses_fc_and_no_secret_echo() -> None:
    common = (OPS / "lib" / "staging-ops-common.sh").read_text(encoding="utf-8")
    backup = (OPS / "staging-backup-db.sh").read_text(encoding="utf-8")
    assert "pg_dump" in common and "-Fc" in common
    assert "tv_bot_stage-postgres-1" in common
    assert "bot_tv_stage" in common
    assert "echo \"$password\"" not in common
    assert "echo $password" not in common
    assert "--dry-run" in backup


def test_forbidden_live_containers_listed() -> None:
    common = (OPS / "lib" / "isolated-restore-test-common.sh").read_text(encoding="utf-8")
    for name in (
        "tv_bot_stage-postgres-1",
        "tv_bot_stage-api-1",
        "tv_bot_stage-worker-1",
    ):
        assert name in common
