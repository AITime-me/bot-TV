"""Canonical Docker runtime allowlist contract for default-deny build context."""

from __future__ import annotations

from pathlib import Path

EXPECTED_DOCKER_ALLOW_RULES: tuple[str, ...] = (
    "!requirements-lock.txt",
    "!alembic.ini",
    "!alembic/",
    "!alembic/env.py",
    "!alembic/script.py.mako",
    "!alembic/versions/",
    "!alembic/versions/.gitkeep",
    "!alembic/versions/20260727_01a_foundation.py",
    "!alembic/versions/20260727_01b_ingress.py",
    "!alembic/versions/20260727_01c_reply_outbound.py",
    "!alembic/versions/20260728_09_amocrm_mirror.py",
    "!alembic/versions/20260728_10_attempt_exhaustion.py",
    "!alembic/versions/20260729_11_handoff_schema.py",
    "!alembic/versions/20260729_12_worker_runtime.py",
    "!alembic/versions/20260729_13_handoff_quarantine.py",
    "!alembic/versions/20260731_14_ephemeral_pii_values.py",
    "!alembic/versions/20260801_15_attachment_spool.py",
    "!alembic/versions/20260801_16_spool_leases.py",
    "!app/",
    "!app/__init__.py",
    "!app/channels/",
    "!app/channels/__init__.py",
    "!app/config.py",
    "!app/core/",
    "!app/core/__init__.py",
    "!app/core/outbound_policy.py",
    "!app/core/pii_gateway.py",
    "!app/core/ephemeral_pii_types.py",
    "!app/core/ephemeral_pii_keys.py",
    "!app/core/ephemeral_pii_crypto.py",
    "!app/core/attachment_types.py",
    "!app/core/attachment_keys.py",
    "!app/core/attachment_crypto.py",
    "!app/core/attachment_mime.py",
    "!app/core/attachment_fs.py",
    "!app/core/attachment_maintenance_types.py",
    "!app/core/attachment_maintenance_heartbeat.py",
    "!app/core/booking_types.py",
    "!app/core/manager_working_hours.py",
    "!app/core/booking_dialog_policy.py",
    "!app/core/booking_eligibility_remote.py",
    "!app/core/booking_eligibility_http.py",
    "!app/core/s2s_http_transport.py",
    "!app/core/s2s_http_stdlib.py",
    "!app/core/booking_eligibility_factory.py",
    "!app/db/",
    "!app/db/__init__.py",
    "!app/db/base.py",
    "!app/db/clock.py",
    "!app/db/session.py",
    "!app/db/worker_lock.py",
    "!app/http_healthcheck.py",
    "!app/integrations/",
    "!app/integrations/__init__.py",
    "!app/main.py",
    "!app/models/",
    "!app/models/__init__.py",
    "!app/models/amocrm_mirror.py",
    "!app/models/conversation.py",
    "!app/models/conversation_ops_event.py",
    "!app/models/ephemeral_pii.py",
    "!app/models/attachment_spool.py",
    "!app/models/inbox.py",
    "!app/models/ingress.py",
    "!app/models/manager_message.py",
    "!app/models/outbox.py",
    "!app/models/reply_plan.py",
    "!app/models/worker_heartbeat.py",
    "!app/repositories/",
    "!app/repositories/__init__.py",
    "!app/repositories/amocrm_mirror.py",
    "!app/repositories/conversations.py",
    "!app/repositories/ephemeral_pii.py",
    "!app/repositories/attachment_spool.py",
    "!app/repositories/ingress.py",
    "!app/repositories/manager_messages.py",
    "!app/repositories/messages.py",
    "!app/repositories/outbound.py",
    "!app/repositories/reply_plans.py",
    "!app/repositories/worker_heartbeats.py",
    "!app/schemas/",
    "!app/schemas/__init__.py",
    "!app/schemas/inbound.py",
    "!app/schemas/ingress.py",
    "!app/schemas/manager_message.py",
    "!app/services/",
    "!app/services/__init__.py",
    "!app/services/amocrm_adapter.py",
    "!app/services/amocrm_mirror.py",
    "!app/services/dialog_context.py",
    "!app/services/ephemeral_pii_store.py",
    "!app/services/attachment_spool_store.py",
    "!app/services/attachment_maintenance.py",
    "!app/services/booking_eligibility_flow.py",
    "!app/services/handoff_expiry.py",
    "!app/services/inbound.py",
    "!app/services/ingress.py",
    "!app/services/manager_messages.py",
    "!app/services/outbound_arbiter.py",
    "!app/services/reply_outbound.py",
    "!app/services/synthetic_outbound.py",
    "!app/services/takeover.py",
    "!app/services/worker_health.py",
    "!app/services/worker_runtime.py",
    "!app/attachment_maintenance.py",
    "!app/attachment_maintenance_healthcheck.py",
    "!app/worker.py",
    "!app/worker_healthcheck.py",
)

BANNED_BROAD_DOCKER_ALLOW_RULES: tuple[str, ...] = (
    "!app/**",
    "!alembic/**",
    "!app/**/*.py",
    "!alembic/**/*.py",
    "!app/**/*",
    "!alembic/**/*",
)


def dockerignore_lines(repo_root: Path | None = None) -> list[str]:
    root = repo_root or Path(__file__).resolve().parents[1]
    return (root / ".dockerignore").read_text(encoding="utf-8").splitlines()


def assert_canonical_docker_runtime_allowlist(
    lines: list[str] | None = None,
    *,
    repo_root: Path | None = None,
) -> None:
    dockerignore = lines if lines is not None else dockerignore_lines(repo_root)
    for required in (".git", ".env", ".env.*", ".venv", "tests", "docs"):
        assert required in dockerignore
    assert "**" in dockerignore

    allow_rules = [line for line in dockerignore if line.startswith("!")]
    for banned in BANNED_BROAD_DOCKER_ALLOW_RULES:
        assert banned not in dockerignore
    assert allow_rules == list(EXPECTED_DOCKER_ALLOW_RULES)
    for rule in allow_rules:
        path = rule[1:]
        assert "*" not in path
        assert "?" not in path
        assert "[" not in path
