from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, UniqueConstraint

from app.db.base import Base
from app.models import Conversation, InboxMessage, IngressEvent, OutboxMessage


_EXPECTED_CHECKS_01A = {
    "ck_conversations_channel": "channel IN ('synthetic')",
    "ck_conversations_status": "status IN ('OPEN', 'HANDOFF', 'CLOSED')",
    "ck_inbox_channel": "channel IN ('synthetic')",
    "ck_inbox_direction": "direction IN ('INBOUND')",
    "ck_inbox_message_type": "message_type IN ('TEXT')",
    "ck_inbox_processing_status": (
        "processing_status IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'FAILED')"
    ),
    "ck_outbox_destination_type": "destination_type IN ('INTERNAL_DRAFT')",
    "ck_outbox_delivery_status": "delivery_status IN ('PENDING', 'CANCELLED')",
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


def test_alembic_metadata_imports() -> None:
    assert Conversation.__tablename__ == "conversations"
    assert InboxMessage.__tablename__ == "inbox_messages"
    assert OutboxMessage.__tablename__ == "outbox_messages"
    assert IngressEvent.__tablename__ == "ingress_events"
    table_names = set(Base.metadata.tables)
    assert table_names == {
        "conversations",
        "inbox_messages",
        "outbox_messages",
        "ingress_events",
    }


def test_alembic_migration_has_upgrade_and_downgrade() -> None:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    revisions = list(script.walk_revisions())
    assert len(revisions) >= 2
    by_id = {rev.revision: rev for rev in revisions}
    assert "20260727_01a_foundation" in by_id
    assert "20260727_01b_ingress" in by_id
    assert by_id["20260727_01b_ingress"].down_revision == "20260727_01a_foundation"

    for revision_id in ("20260727_01a_foundation", "20260727_01b_ingress"):
        rev = by_id[revision_id]
        assert callable(rev.module.upgrade)
        assert callable(rev.module.downgrade)
        text = Path(rev.path).read_text(encoding="utf-8")
        assert "def upgrade()" in text
        assert "def downgrade()" in text

    foundation = Path(by_id["20260727_01a_foundation"].path).read_text(encoding="utf-8")
    assert "op.create_table" in foundation
    assert "op.drop_table" in foundation
    assert "delivery_status IN ('PENDING', 'CANCELLED')" in foundation
    assert "'SENT'" not in foundation
    assert "uq_outbox_source_inbox_destination" in foundation

    ingress = Path(by_id["20260727_01b_ingress"].path).read_text(encoding="utf-8")
    assert "ingress_events" in ingress
    assert "uq_ingress_channel_external_event_id" in ingress
    assert "DEAD" in ingress
    assert "op.drop_table" in ingress


def test_model_migration_check_and_unique_parity() -> None:
    root = Path(__file__).resolve().parents[1]
    migration_01a = (
        root / "alembic" / "versions" / "20260727_01a_foundation.py"
    ).read_text(encoding="utf-8")
    migration_01b = (
        root / "alembic" / "versions" / "20260727_01b_ingress.py"
    ).read_text(encoding="utf-8")

    model_checks: dict[str, str] = {}
    model_uniques: set[str] = set()
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and constraint.name:
                model_checks[constraint.name] = str(constraint.sqltext).strip()
            if isinstance(constraint, UniqueConstraint) and constraint.name:
                model_uniques.add(constraint.name)

    assert set(_EXPECTED_CHECKS_01A) <= set(model_checks)
    assert _EXPECTED_UNIQUES_01A <= model_uniques
    assert set(_EXPECTED_CHECKS_01B) <= set(model_checks)
    assert _EXPECTED_UNIQUES_01B <= model_uniques

    for name, sql in _EXPECTED_CHECKS_01A.items():
        assert name in migration_01a
        assert sql in migration_01a
        assert model_checks[name] == sql

    for name in _EXPECTED_UNIQUES_01A:
        assert name in migration_01a

    for name, sql in _EXPECTED_CHECKS_01B.items():
        assert name in migration_01b
        assert sql in migration_01b
        assert model_checks[name] == sql

    for name in _EXPECTED_UNIQUES_01B:
        assert name in migration_01b

    assert re.search(
        r'name=["\']uq_outbox_source_inbox_destination["\']',
        migration_01a,
    )
    assert re.search(
        r'name=["\']uq_ingress_channel_external_event_id["\']',
        migration_01b,
    )
