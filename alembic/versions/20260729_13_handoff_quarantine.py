"""Quarantine poisoned handoff-expiry rows without destroying audit history.

Revision ID: 20260729_13_handoff_quarantine
Revises: 20260729_12_worker_runtime
Create Date: 2026-07-29

Adds Conversation active-quarantine snapshot columns, rebuilds the due
partial index so active quarantine is excluded, and creates append-only
conversation_ops_events for durable QUARANTINED/CLEARED history.

No message text, contacts, raw payloads, BOT_MODE, or EMERGENCY_LOCK changes.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260729_13_handoff_quarantine"
down_revision: Union[str, Sequence[str], None] = "20260729_12_worker_runtime"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REASON_CODES = (
    "HANDOFF_DEFERRED_PLAN_MISSING",
    "HANDOFF_DEFERRED_PLAN_TYPE",
    "HANDOFF_DEFERRED_PLAN_NOT_OPEN",
    "HANDOFF_DEFERRED_PLAN_CONTEXT",
    "HANDOFF_DEFERRED_PLAN_MANAGER_EPOCH",
    "HANDOFF_DEFERRED_PLAN_EVENT_SEQ",
    "HANDOFF_DEFERRED_PLAN_DEADLINE",
    "HANDOFF_DEFERRED_PLAN_MARKER",
    "HANDOFF_EXPIRY_UNSUPPORTED_STATE",
)
_REASON_SQL = ", ".join(f"'{code}'" for code in _REASON_CODES)
_CLEAR_PATH_SQL = "'MANAGER_MESSAGE_APPLIED'"


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "handoff_quarantined_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "conversations",
        sa.Column("handoff_quarantine_reason", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "handoff_quarantine_cleared_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "handoff_quarantine_clear_path",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_conversations_handoff_quarantine_consistency",
        "conversations",
        "("
        "handoff_quarantined_at IS NULL "
        "AND handoff_quarantine_reason IS NULL "
        "AND handoff_quarantine_cleared_at IS NULL "
        "AND handoff_quarantine_clear_path IS NULL"
        ") OR ("
        "handoff_quarantined_at IS NOT NULL "
        "AND handoff_quarantine_reason IS NOT NULL "
        "AND handoff_quarantine_cleared_at IS NULL "
        "AND handoff_quarantine_clear_path IS NULL"
        ") OR ("
        "handoff_quarantined_at IS NOT NULL "
        "AND handoff_quarantine_reason IS NOT NULL "
        "AND handoff_quarantine_cleared_at IS NOT NULL "
        "AND handoff_quarantine_clear_path IS NOT NULL"
        ")",
    )
    op.create_check_constraint(
        "ck_conversations_handoff_quarantine_reason",
        "conversations",
        "handoff_quarantine_reason IS NULL OR "
        f"handoff_quarantine_reason IN ({_REASON_SQL})",
    )
    op.create_check_constraint(
        "ck_conversations_handoff_quarantine_clear_path",
        "conversations",
        "handoff_quarantine_clear_path IS NULL OR "
        f"handoff_quarantine_clear_path IN ({_CLEAR_PATH_SQL})",
    )

    op.drop_index("ix_conversations_handoff_due", table_name="conversations")
    op.create_index(
        "ix_conversations_handoff_due",
        "conversations",
        ["handoff_deadline_at"],
        postgresql_where=sa.text(
            "status = 'HANDOFF' AND ownership = 'MANAGER' "
            "AND handoff_state IN ('HUMAN_ACTIVE', 'HUMAN_PAUSE') "
            "AND ("
            "handoff_quarantined_at IS NULL "
            "OR handoff_quarantine_cleared_at IS NOT NULL"
            ")"
        ),
    )

    op.create_table(
        "conversation_ops_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("clear_path", sa.String(length=64), nullable=True),
        sa.Column(
            "manager_epoch",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "context_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ("
            "'HANDOFF_EXPIRY_QUARANTINED', "
            "'HANDOFF_QUARANTINE_CLEARED'"
            ")",
            name="ck_conversation_ops_events_event_type",
        ),
        sa.CheckConstraint(
            f"reason_code IN ({_REASON_SQL})",
            name="ck_conversation_ops_events_reason_code",
        ),
        sa.CheckConstraint(
            "("
            "event_type = 'HANDOFF_EXPIRY_QUARANTINED' "
            "AND clear_path IS NULL"
            ") OR ("
            "event_type = 'HANDOFF_QUARANTINE_CLEARED' "
            f"AND clear_path IN ({_CLEAR_PATH_SQL})"
            ")",
            name="ck_conversation_ops_events_clear_path",
        ),
        sa.CheckConstraint(
            "manager_epoch >= 0",
            name="ck_conversation_ops_events_manager_epoch_nonnegative",
        ),
        sa.CheckConstraint(
            "context_version >= 0",
            name="ck_conversation_ops_events_context_version_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversation_ops_events_conversation_created",
        "conversation_ops_events",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_ops_events_conversation_created",
        table_name="conversation_ops_events",
    )
    op.drop_table("conversation_ops_events")

    op.drop_index("ix_conversations_handoff_due", table_name="conversations")
    op.create_index(
        "ix_conversations_handoff_due",
        "conversations",
        ["handoff_deadline_at"],
        postgresql_where=sa.text(
            "status = 'HANDOFF' AND ownership = 'MANAGER' "
            "AND handoff_state IN ('HUMAN_ACTIVE', 'HUMAN_PAUSE')"
        ),
    )

    op.drop_constraint(
        "ck_conversations_handoff_quarantine_clear_path",
        "conversations",
        type_="check",
    )
    op.drop_constraint(
        "ck_conversations_handoff_quarantine_reason",
        "conversations",
        type_="check",
    )
    op.drop_constraint(
        "ck_conversations_handoff_quarantine_consistency",
        "conversations",
        type_="check",
    )
    op.drop_column("conversations", "handoff_quarantine_clear_path")
    op.drop_column("conversations", "handoff_quarantine_cleared_at")
    op.drop_column("conversations", "handoff_quarantine_reason")
    op.drop_column("conversations", "handoff_quarantined_at")
