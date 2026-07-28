from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, UniqueConstraint

from app.db.base import Base
from app.models import (
    AmoCrmMirrorJob,
    Conversation,
    InboxMessage,
    IngressEvent,
    OutboxMessage,
    ReplyPlan,
)


_EXPECTED_CHECKS_01A_STABLE = {
    "ck_conversations_channel": "channel IN ('synthetic')",
    "ck_conversations_status": "status IN ('OPEN', 'HANDOFF', 'CLOSED')",
    "ck_inbox_channel": "channel IN ('synthetic')",
    "ck_inbox_direction": "direction IN ('INBOUND')",
    "ck_inbox_message_type": "message_type IN ('TEXT')",
    "ck_inbox_processing_status": (
        "processing_status IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'FAILED')"
    ),
}

_EXPECTED_UNIQUES_01A = {
    "uq_conversations_channel_external_id",
    "uq_inbox_channel_external_message_id",
    "uq_outbox_source_inbox_destination",
}

_EXPECTED_CHECKS_01B = {
    "ck_ingress_channel": "channel IN ('synthetic')",
    "ck_ingress_event_type": "event_type IN ('SYNTHETIC_MESSAGE')",
    "ck_ingress_status": (
        "status IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'FAILED', 'DEAD')"
    ),
    "ck_ingress_attempt_count_nonnegative": "attempt_count >= 0",
    "ck_ingress_lease_version_nonnegative": "lease_version >= 0",
}

_EXPECTED_UNIQUES_01B = {
    "uq_ingress_channel_external_event_id",
}

_EXPECTED_CHECKS_01C = {
    "ck_conversations_ownership": "ownership IN ('BOT', 'MANAGER')",
    "ck_conversations_context_version_nonnegative": "context_version >= 0",
    "ck_reply_plans_plan_type": "plan_type IN ('CLIENT_REPLY', 'SERVICE_SIGNAL')",
    "ck_reply_plans_status": (
        "status IN ('PENDING', 'READY', 'PROCESSING', 'DISPATCHED', "
        "'CANCELLED', 'SUPERSEDED', 'FAILED', 'DEAD')"
    ),
    "ck_outbox_destination_type": (
        "destination_type IN ('INTERNAL_DRAFT', 'SYNTHETIC_OUTBOUND')"
    ),
    "ck_outbox_delivery_status": (
        "delivery_status IN ('PENDING', 'PROCESSING', 'DELIVERED', "
        "'FAILED', 'DEAD', 'CANCELLED')"
    ),
}

_EXPECTED_UNIQUES_01C = {
    "uq_reply_plans_conversation_context_version",
    "uq_outbox_idempotency_key",
    "uq_outbox_reply_plan_destination",
}

_EXPECTED_CHECKS_09 = {
    "ck_amocrm_mirror_job_type": (
        "job_type IN ('CLIENT_MESSAGE_RECEIVED_META', "
        "'REPLY_PLAN_STATE_CHANGED', 'MANAGER_TAKEOVER', "
        "'OUTBOUND_DELIVERED_META')"
    ),
    "ck_amocrm_mirror_subject_kind": (
        "subject_kind IN ('CONVERSATION', 'INBOX_MESSAGE', 'REPLY_PLAN', "
        "'OUTBOX_MESSAGE')"
    ),
    "ck_amocrm_mirror_status": (
        "status IN ('PENDING', 'PROCESSING', 'MIRRORED', 'SKIPPED', "
        "'FAILED', 'DEAD')"
    ),
    "ck_amocrm_mirror_attempt_count_nonnegative": "attempt_count >= 0",
    "ck_amocrm_mirror_max_attempts_positive": "max_attempts > 0",
    "ck_amocrm_mirror_lease_version_nonnegative": "lease_version >= 0",
    "ck_amocrm_mirror_context_version_nonnegative": (
        "context_version IS NULL OR context_version >= 0"
    ),
}

_EXPECTED_UNIQUES_09 = {
    "uq_amocrm_mirror_key",
}

_EXPECTED_CHECKS_10 = {
    "ck_ingress_max_attempts_positive": "max_attempts > 0",
}


def test_alembic_metadata_imports() -> None:
    assert Conversation.__tablename__ == "conversations"
    assert InboxMessage.__tablename__ == "inbox_messages"
    assert OutboxMessage.__tablename__ == "outbox_messages"
    assert IngressEvent.__tablename__ == "ingress_events"
    assert ReplyPlan.__tablename__ == "reply_plans"
    assert AmoCrmMirrorJob.__tablename__ == "amocrm_mirror_jobs"
    table_names = set(Base.metadata.tables)
    assert table_names == {
        "conversations",
        "inbox_messages",
        "outbox_messages",
        "ingress_events",
        "reply_plans",
        "amocrm_mirror_jobs",
    }


def test_alembic_migration_has_upgrade_and_downgrade() -> None:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    revisions = list(script.walk_revisions())
    assert len(revisions) >= 5
    by_id = {rev.revision: rev for rev in revisions}
    assert "20260727_01a_foundation" in by_id
    assert "20260727_01b_ingress" in by_id
    assert "20260727_01c_reply_outbound" in by_id
    assert "20260728_09_amocrm_mirror" in by_id
    assert "20260728_10_attempt_exhaustion" in by_id
    assert by_id["20260727_01b_ingress"].down_revision == "20260727_01a_foundation"
    assert by_id["20260727_01c_reply_outbound"].down_revision == "20260727_01b_ingress"
    assert (
        by_id["20260728_09_amocrm_mirror"].down_revision
        == "20260727_01c_reply_outbound"
    )
    assert (
        by_id["20260728_10_attempt_exhaustion"].down_revision
        == "20260728_09_amocrm_mirror"
    )

    for revision_id in (
        "20260727_01a_foundation",
        "20260727_01b_ingress",
        "20260727_01c_reply_outbound",
        "20260728_09_amocrm_mirror",
        "20260728_10_attempt_exhaustion",
    ):
        rev = by_id[revision_id]
        assert callable(rev.module.upgrade)
        assert callable(rev.module.downgrade)
        text = Path(rev.path).read_text(encoding="utf-8")
        assert "def upgrade()" in text
        assert "def downgrade()" in text

    foundation = Path(by_id["20260727_01a_foundation"].path).read_text(encoding="utf-8")
    assert "delivery_status IN ('PENDING', 'CANCELLED')" in foundation
    assert "'SENT'" not in foundation

    reply = Path(by_id["20260727_01c_reply_outbound"].path).read_text(encoding="utf-8")
    assert "reply_plans" in reply
    assert "SYNTHETIC_OUTBOUND" in reply
    assert "DELIVERED" in reply
    assert "'SENT'" not in reply
    assert "op.drop_table" in reply

    mirror = Path(by_id["20260728_09_amocrm_mirror"].path).read_text(encoding="utf-8")
    assert "amocrm_mirror_jobs" in mirror
    assert "op.create_table" in mirror
    assert 'op.drop_table("amocrm_mirror_jobs")' in mirror
    assert "'SENT'" not in mirror
    # CURSOR-09 ships one table only: no external-entity mapping, no direction
    # column, and no amoCRM entity semantics.
    assert mirror.count("op.create_table(") == 1
    for absent in (
        "amocrm_entity_links",
        "external_entity_id",
        'sa.Column("direction"',
    ):
        assert absent not in mirror, f"out-of-scope object in migration: {absent}"


def test_model_migration_check_and_unique_parity() -> None:
    root = Path(__file__).resolve().parents[1]
    migration_01a = (
        root / "alembic" / "versions" / "20260727_01a_foundation.py"
    ).read_text(encoding="utf-8")
    migration_01b = (
        root / "alembic" / "versions" / "20260727_01b_ingress.py"
    ).read_text(encoding="utf-8")
    migration_01c = (
        root / "alembic" / "versions" / "20260727_01c_reply_outbound.py"
    ).read_text(encoding="utf-8")
    migration_09 = (
        root / "alembic" / "versions" / "20260728_09_amocrm_mirror.py"
    ).read_text(encoding="utf-8")
    migration_10 = (
        root / "alembic" / "versions" / "20260728_10_attempt_exhaustion.py"
    ).read_text(encoding="utf-8")

    model_checks: dict[str, str] = {}
    model_uniques: set[str] = set()
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and constraint.name:
                model_checks[constraint.name] = str(constraint.sqltext).strip()
            if isinstance(constraint, UniqueConstraint) and constraint.name:
                model_uniques.add(constraint.name)

    assert set(_EXPECTED_CHECKS_01A_STABLE) <= set(model_checks)
    assert _EXPECTED_UNIQUES_01A <= model_uniques
    assert set(_EXPECTED_CHECKS_01B) <= set(model_checks)
    assert _EXPECTED_UNIQUES_01B <= model_uniques
    assert set(_EXPECTED_CHECKS_01C) <= set(model_checks)
    assert _EXPECTED_UNIQUES_01C <= model_uniques
    assert set(_EXPECTED_CHECKS_09) <= set(model_checks)
    assert _EXPECTED_UNIQUES_09 <= model_uniques
    assert set(_EXPECTED_CHECKS_10) <= set(model_checks)

    for name, sql in _EXPECTED_CHECKS_01A_STABLE.items():
        assert name in migration_01a
        assert sql in migration_01a
        assert model_checks[name] == sql

    # 01A historically created the narrow outbox checks; 01C replaces them.
    assert "destination_type IN ('INTERNAL_DRAFT')" in migration_01a
    assert "delivery_status IN ('PENDING', 'CANCELLED')" in migration_01a
    assert model_checks["ck_outbox_destination_type"] == _EXPECTED_CHECKS_01C[
        "ck_outbox_destination_type"
    ]
    assert "DELIVERED" in model_checks["ck_outbox_delivery_status"]
    assert "PROCESSING" in model_checks["ck_outbox_delivery_status"]
    assert "SENT" not in model_checks["ck_outbox_delivery_status"]
    assert _EXPECTED_CHECKS_01C["ck_outbox_destination_type"] in migration_01c
    assert "DELIVERED" in migration_01c
    assert "SYNTHETIC_OUTBOUND" in migration_01c

    for name in _EXPECTED_UNIQUES_01A:
        assert name in migration_01a

    for name, sql in _EXPECTED_CHECKS_01B.items():
        assert name in migration_01b
        assert sql in migration_01b
        assert model_checks[name] == sql

    for name in _EXPECTED_UNIQUES_01B:
        assert name in migration_01b

    for name, sql in _EXPECTED_CHECKS_01C.items():
        assert name in migration_01c
        assert model_checks[name] == sql
        # Migration may split long CHECK literals across adjacent strings.
        for token in re.findall(r"'[A-Z_]+'", sql):
            assert token in migration_01c, f"{name} missing {token}"

    for name in _EXPECTED_UNIQUES_01C:
        assert name in migration_01c

    assert re.search(
        r'name=["\']uq_reply_plans_conversation_context_version["\']',
        migration_01c,
    )

    for name, sql in _EXPECTED_CHECKS_09.items():
        assert name in migration_09
        assert model_checks[name] == sql
        for token in re.findall(r"'[A-Z_]+'", sql):
            assert token in migration_09, f"{name} missing {token}"

    for name in _EXPECTED_UNIQUES_09:
        assert name in migration_09

    assert re.search(r'name=["\']uq_amocrm_mirror_key["\']', migration_09)

    for name, sql in _EXPECTED_CHECKS_10.items():
        assert name in migration_10
        assert model_checks[name] == sql
        assert sql in migration_10
