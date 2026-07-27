from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, UniqueConstraint

from app.db.base import Base
from app.models import Conversation, InboxMessage, OutboxMessage


_EXPECTED_CHECKS = {
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

_EXPECTED_UNIQUES = {
    "uq_conversations_channel_external_id",
    "uq_inbox_channel_external_message_id",
    "uq_outbox_source_inbox_destination",
}


def test_alembic_metadata_imports() -> None:
    assert Conversation.__tablename__ == "conversations"
    assert InboxMessage.__tablename__ == "inbox_messages"
    assert OutboxMessage.__tablename__ == "outbox_messages"
    table_names = set(Base.metadata.tables)
    assert table_names == {
        "conversations",
        "inbox_messages",
        "outbox_messages",
    }


def test_alembic_migration_has_upgrade_and_downgrade() -> None:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    revisions = list(script.walk_revisions())
    assert revisions, "expected at least one Alembic revision"
    head = revisions[0]
    assert callable(head.module.upgrade)
    assert callable(head.module.downgrade)
    text = Path(head.path).read_text(encoding="utf-8")
    assert "def upgrade()" in text
    assert "def downgrade()" in text
    assert "op.create_table" in text
    assert "op.drop_table" in text
    assert "delivery_status IN ('PENDING', 'CANCELLED')" in text
    assert "'SENT'" not in text
    assert "uq_outbox_source_inbox_destination" in text


def test_model_migration_check_and_unique_parity() -> None:
    root = Path(__file__).resolve().parents[1]
    migration = (
        root / "alembic" / "versions" / "20260727_01a_foundation.py"
    ).read_text(encoding="utf-8")

    model_checks: dict[str, str] = {}
    model_uniques: set[str] = set()
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and constraint.name:
                model_checks[constraint.name] = str(constraint.sqltext).strip()
            if isinstance(constraint, UniqueConstraint) and constraint.name:
                model_uniques.add(constraint.name)

    assert set(_EXPECTED_CHECKS) <= set(model_checks)
    assert _EXPECTED_UNIQUES <= model_uniques

    for name, sql in _EXPECTED_CHECKS.items():
        assert name in migration
        assert sql in migration
        assert model_checks[name] == sql

    for name in _EXPECTED_UNIQUES:
        assert name in migration

    # Migration must create the same unique outbox identity used by ON CONFLICT.
    assert re.search(
        r'name=["\']uq_outbox_source_inbox_destination["\']',
        migration,
    )
