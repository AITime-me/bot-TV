"""Add resumable handoff state, dialog ordering, and outbound fencing.

Revision ID: 20260729_11_handoff_schema
Revises: 20260728_10_attempt_exhaustion
Create Date: 2026-07-29

This migration deliberately fences all legacy unfinished bot replies. A
legacy MANAGER/HANDOFF row remains human-owned with an infinite protective
deadline; the migration never invents a fresh 15-minute resume window.

The downgrade is a schema downgrade only. It cannot reconstruct the legacy
ownership/status inconsistencies or reopen rows cancelled by the upgrade.
Operational rollback therefore requires the mandatory pre-upgrade database
backup described by the release procedure.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260729_11_handoff_schema"
down_revision: Union[str, Sequence[str], None] = "20260728_10_attempt_exhaustion"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_HANDOFF_CONSISTENCY_SQL = (
    "("
    "status = 'CLOSED' AND ownership = 'BOT' "
    "AND handoff_state = 'BOT_ACTIVE' "
    "AND handoff_deadline_at IS NULL "
    "AND human_pause_anchor_at IS NULL "
    "AND manager_takeover_at IS NULL"
    ") OR ("
    "status = 'OPEN' AND ownership = 'BOT' "
    "AND handoff_state = 'BOT_ACTIVE' "
    "AND handoff_deadline_at IS NULL "
    "AND human_pause_anchor_at IS NULL "
    "AND manager_takeover_at IS NULL"
    ") OR ("
    "status = 'HANDOFF' AND ownership = 'MANAGER' "
    "AND handoff_state = 'HUMAN_ACTIVE' "
    "AND handoff_deadline_at IS NOT NULL "
    "AND human_pause_anchor_at IS NULL "
    "AND manager_takeover_at IS NOT NULL"
    ") OR ("
    "status = 'HANDOFF' AND ownership = 'MANAGER' "
    "AND handoff_state = 'HUMAN_PAUSE' "
    "AND handoff_deadline_at IS NOT NULL "
    "AND human_pause_anchor_at IS NOT NULL "
    "AND manager_takeover_at IS NOT NULL"
    ")"
)


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("handoff_state", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "manager_epoch",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "current_event_seq",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "conversations",
        sa.Column("manager_sequence_hwm", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("handoff_deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("human_pause_anchor_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "inbox_messages",
        sa.Column("conversation_event_seq", sa.BigInteger(), nullable=True),
    )

    op.add_column(
        "reply_plans",
        sa.Column(
            "manager_epoch",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "reply_plans",
        sa.Column(
            "event_seq_hwm",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )

    op.drop_constraint("ck_outbox_delivery_status", "outbox_messages", type_="check")
    op.add_column(
        "outbox_messages",
        sa.Column(
            "manager_epoch",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "outbox_messages",
        sa.Column(
            "event_seq_hwm",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "outbox_messages",
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_outbox_delivery_status",
        "outbox_messages",
        "delivery_status IN ('PENDING', 'PROCESSING', 'ADMITTED', 'DELIVERED', "
        "'FAILED', 'DEAD', 'CANCELLED')",
    )

    # Deterministic event order for all legacy client messages. UUID is the
    # final stable tie-breaker when provider and database timestamps coincide.
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    row_number() OVER (
                        PARTITION BY conversation_id
                        ORDER BY received_at, created_at, id
                    )::bigint AS event_seq
                FROM inbox_messages
            )
            UPDATE inbox_messages AS inbox
            SET conversation_event_seq = ranked.event_seq
            FROM ranked
            WHERE inbox.id = ranked.id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE conversations AS conversation
            SET current_event_seq = COALESCE(events.max_event_seq, 0)
            FROM (
                SELECT conversation_id, max(conversation_event_seq) AS max_event_seq
                FROM inbox_messages
                GROUP BY conversation_id
            ) AS events
            WHERE conversation.id = events.conversation_id
            """
        )
    )

    # Normalize every legacy ownership/status combination explicitly. Only a
    # previously explicit MANAGER/HANDOFF row remains human-owned. Its infinite
    # deadline is a protective sentinel, not a fabricated resume time.
    op.execute(
        sa.text(
            """
            UPDATE conversations
            SET
                handoff_state = CASE
                    WHEN status = 'CLOSED' THEN 'BOT_ACTIVE'
                    WHEN ownership = 'MANAGER' AND status = 'HANDOFF'
                        THEN 'HUMAN_ACTIVE'
                    ELSE 'BOT_ACTIVE'
                END,
                manager_epoch = CASE
                    WHEN status <> 'CLOSED'
                         AND ownership = 'MANAGER'
                         AND status = 'HANDOFF'
                        THEN 1
                    ELSE 0
                END,
                ownership = CASE
                    WHEN status <> 'CLOSED'
                         AND ownership = 'MANAGER'
                         AND status = 'HANDOFF'
                        THEN 'MANAGER'
                    ELSE 'BOT'
                END,
                status = CASE
                    WHEN status = 'CLOSED' THEN 'CLOSED'
                    WHEN ownership = 'MANAGER' AND status = 'HANDOFF'
                        THEN 'HANDOFF'
                    ELSE 'OPEN'
                END,
                manager_takeover_at = CASE
                    WHEN status <> 'CLOSED'
                         AND ownership = 'MANAGER'
                         AND status = 'HANDOFF'
                        THEN COALESCE(manager_takeover_at, updated_at, created_at)
                    ELSE NULL
                END,
                handoff_deadline_at = CASE
                    WHEN status <> 'CLOSED'
                         AND ownership = 'MANAGER'
                         AND status = 'HANDOFF'
                        THEN 'infinity'::timestamptz
                    ELSE NULL
                END,
                human_pause_anchor_at = NULL,
                manager_sequence_hwm = NULL,
                active_reply_plan_id = NULL
            """
        )
    )

    # All legacy open plans are fenced before any new worker can observe the
    # upgraded schema. Terminal history is preserved; every lease is cleared.
    op.execute(
        sa.text(
            """
            UPDATE reply_plans AS plan
            SET
                manager_epoch = conversation.manager_epoch,
                event_seq_hwm = conversation.current_event_seq,
                status = CASE
                    WHEN plan.status IN (
                        'PENDING', 'READY', 'PROCESSING', 'FAILED'
                    ) THEN 'CANCELLED'
                    ELSE plan.status
                END,
                cancel_reason = CASE
                    WHEN plan.status IN (
                        'PENDING', 'READY', 'PROCESSING', 'FAILED'
                    ) THEN 'HANDOFF_SCHEMA_MIGRATION'
                    ELSE plan.cancel_reason
                END,
                lease_owner = NULL,
                lease_token = NULL,
                lease_until = NULL
            FROM conversations AS conversation
            WHERE plan.conversation_id = conversation.id
            """
        )
    )

    # Synthetic outbound is the only client-send-shaped queue. Its unfinished
    # legacy rows are cancelled; terminal history remains. INTERNAL_DRAFT rows
    # are retained because they are manager hints, not client sends.
    op.execute(
        sa.text(
            """
            UPDATE outbox_messages AS outbound
            SET
                manager_epoch = conversation.manager_epoch,
                event_seq_hwm = conversation.current_event_seq
            FROM conversations AS conversation
            WHERE outbound.conversation_id = conversation.id
            """
        )
    )
    # The previous statement intentionally uses only common columns in its
    # FROM update. Apply queue-specific fencing separately for clarity.
    op.execute(
        sa.text(
            """
            UPDATE outbox_messages
            SET
                delivery_status = CASE
                    WHEN destination_type = 'SYNTHETIC_OUTBOUND'
                         AND delivery_status IN ('PENDING', 'PROCESSING', 'FAILED')
                        THEN 'CANCELLED'
                    ELSE delivery_status
                END,
                lease_owner = CASE
                    WHEN destination_type = 'SYNTHETIC_OUTBOUND' THEN NULL
                    ELSE lease_owner
                END,
                lease_token = CASE
                    WHEN destination_type = 'SYNTHETIC_OUTBOUND' THEN NULL
                    ELSE lease_token
                END,
                lease_until = CASE
                    WHEN destination_type = 'SYNTHETIC_OUTBOUND' THEN NULL
                    ELSE lease_until
                END,
                admitted_at = CASE
                    WHEN destination_type = 'SYNTHETIC_OUTBOUND'
                         AND delivery_status = 'DELIVERED'
                        THEN COALESCE(updated_at, created_at)
                    ELSE NULL
                END
            """
        )
    )

    op.alter_column(
        "conversations",
        "handoff_state",
        existing_type=sa.String(length=32),
        nullable=False,
        server_default=sa.text("'BOT_ACTIVE'"),
    )
    op.alter_column(
        "inbox_messages",
        "conversation_event_seq",
        existing_type=sa.BigInteger(),
        nullable=False,
    )

    op.create_check_constraint(
        "ck_conversations_handoff_state",
        "conversations",
        "handoff_state IN ('BOT_ACTIVE', 'HUMAN_ACTIVE', 'HUMAN_PAUSE')",
    )
    op.create_check_constraint(
        "ck_conversations_manager_epoch_nonnegative",
        "conversations",
        "manager_epoch >= 0",
    )
    op.create_check_constraint(
        "ck_conversations_current_event_seq_nonnegative",
        "conversations",
        "current_event_seq >= 0",
    )
    op.create_check_constraint(
        "ck_conversations_manager_sequence_hwm_nonnegative",
        "conversations",
        "manager_sequence_hwm IS NULL OR manager_sequence_hwm >= 0",
    )
    op.create_check_constraint(
        "ck_conversations_handoff_consistency",
        "conversations",
        _HANDOFF_CONSISTENCY_SQL,
    )

    op.create_check_constraint(
        "ck_inbox_conversation_event_seq_positive",
        "inbox_messages",
        "conversation_event_seq > 0",
    )
    op.create_unique_constraint(
        "uq_inbox_conversation_event_seq",
        "inbox_messages",
        ["conversation_id", "conversation_event_seq"],
    )

    op.create_check_constraint(
        "ck_reply_plans_manager_epoch_nonnegative",
        "reply_plans",
        "manager_epoch >= 0",
    )
    op.create_check_constraint(
        "ck_reply_plans_event_seq_hwm_nonnegative",
        "reply_plans",
        "event_seq_hwm >= 0",
    )

    op.create_check_constraint(
        "ck_outbox_manager_epoch_nonnegative",
        "outbox_messages",
        "manager_epoch >= 0",
    )
    op.create_check_constraint(
        "ck_outbox_event_seq_hwm_nonnegative",
        "outbox_messages",
        "event_seq_hwm >= 0",
    )
    op.create_check_constraint(
        "ck_outbox_admitted_destination",
        "outbox_messages",
        "admitted_at IS NULL OR ("
        "destination_type = 'SYNTHETIC_OUTBOUND' "
        "AND delivery_status IN ('ADMITTED', 'DELIVERED', 'DEAD')"
        ")",
    )
    op.create_check_constraint(
        "ck_outbox_admitted_state",
        "outbox_messages",
        "delivery_status <> 'ADMITTED' OR ("
        "destination_type = 'SYNTHETIC_OUTBOUND' AND admitted_at IS NOT NULL"
        ")",
    )
    op.create_check_constraint(
        "ck_outbox_delivered_after_admission",
        "outbox_messages",
        "destination_type <> 'SYNTHETIC_OUTBOUND' "
        "OR delivery_status <> 'DELIVERED' "
        "OR admitted_at IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_outbox_lease_complete",
        "outbox_messages",
        "("
        "lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL"
        ") OR ("
        "lease_owner IS NOT NULL AND lease_token IS NOT NULL "
        "AND lease_until IS NOT NULL"
        ")",
    )
    op.create_check_constraint(
        "ck_outbox_unleased_states",
        "outbox_messages",
        "delivery_status NOT IN ('PENDING', 'FAILED', 'DELIVERED', "
        "'DEAD', 'CANCELLED') OR ("
        "lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL"
        ")",
    )
    op.create_check_constraint(
        "ck_outbox_processing_lease",
        "outbox_messages",
        "delivery_status <> 'PROCESSING' OR ("
        "lease_owner IS NOT NULL AND lease_token IS NOT NULL "
        "AND lease_until IS NOT NULL"
        ")",
    )

    op.create_table(
        "manager_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("external_message_id", sa.String(length=128), nullable=False),
        sa.Column("provider_sequence", sa.BigInteger(), nullable=True),
        sa.Column(
            "provider_occurred_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("conversation_event_seq", sa.BigInteger(), nullable=True),
        sa.Column("classification_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('synthetic')",
            name="ck_manager_messages_channel",
        ),
        sa.CheckConstraint(
            "status IN ('APPLIED', 'STALE', 'QUARANTINED')",
            name="ck_manager_messages_status",
        ),
        sa.CheckConstraint(
            "provider_sequence IS NULL OR provider_sequence >= 0",
            name="ck_manager_messages_provider_sequence_nonnegative",
        ),
        sa.CheckConstraint(
            "conversation_event_seq IS NULL OR conversation_event_seq > 0",
            name="ck_manager_messages_event_seq_positive",
        ),
        sa.CheckConstraint(
            "char_length(body_text) BETWEEN 1 AND 4000",
            name="ck_manager_messages_body_length",
        ),
        sa.CheckConstraint(
            "("
            "status = 'APPLIED' "
            "AND provider_sequence IS NOT NULL "
            "AND conversation_event_seq IS NOT NULL"
            ") OR ("
            "status = 'STALE' "
            "AND provider_sequence IS NOT NULL "
            "AND conversation_event_seq IS NULL"
            ") OR ("
            "status = 'QUARANTINED' "
            "AND conversation_event_seq IS NULL"
            ")",
            name="ck_manager_messages_classification",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel",
            "external_message_id",
            name="uq_manager_messages_channel_external_message_id",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "conversation_event_seq",
            name="uq_manager_messages_conversation_event_seq",
        ),
    )
    op.create_index(
        "ix_manager_messages_conversation_provider_sequence",
        "manager_messages",
        ["conversation_id", "provider_sequence"],
    )
    op.create_index(
        "ix_manager_messages_conversation_event_seq",
        "manager_messages",
        ["conversation_id", "conversation_event_seq"],
    )
    op.create_index(
        "ix_conversations_handoff_due",
        "conversations",
        ["handoff_deadline_at"],
        postgresql_where=sa.text(
            "status = 'HANDOFF' AND ownership = 'MANAGER' "
            "AND handoff_state IN ('HUMAN_ACTIVE', 'HUMAN_PAUSE')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversations_handoff_due",
        table_name="conversations",
    )
    op.drop_index(
        "ix_manager_messages_conversation_event_seq",
        table_name="manager_messages",
    )
    op.drop_index(
        "ix_manager_messages_conversation_provider_sequence",
        table_name="manager_messages",
    )
    op.drop_table("manager_messages")

    op.drop_constraint(
        "ck_outbox_processing_lease",
        "outbox_messages",
        type_="check",
    )
    op.drop_constraint(
        "ck_outbox_unleased_states",
        "outbox_messages",
        type_="check",
    )
    op.drop_constraint(
        "ck_outbox_lease_complete",
        "outbox_messages",
        type_="check",
    )
    op.drop_constraint(
        "ck_outbox_delivered_after_admission",
        "outbox_messages",
        type_="check",
    )
    op.drop_constraint(
        "ck_outbox_admitted_state",
        "outbox_messages",
        type_="check",
    )
    op.drop_constraint(
        "ck_outbox_admitted_destination",
        "outbox_messages",
        type_="check",
    )
    op.drop_constraint(
        "ck_outbox_event_seq_hwm_nonnegative",
        "outbox_messages",
        type_="check",
    )
    op.drop_constraint(
        "ck_outbox_manager_epoch_nonnegative",
        "outbox_messages",
        type_="check",
    )
    op.drop_constraint("ck_outbox_delivery_status", "outbox_messages", type_="check")
    # ADMITTED does not exist in the previous schema. A schema-only downgrade
    # fences such rows as CANCELLED; restoring their former data requires the
    # pre-upgrade database backup.
    op.execute(
        sa.text(
            """
            UPDATE outbox_messages
            SET delivery_status = 'CANCELLED'
            WHERE delivery_status = 'ADMITTED'
            """
        )
    )
    op.create_check_constraint(
        "ck_outbox_delivery_status",
        "outbox_messages",
        "delivery_status IN ('PENDING', 'PROCESSING', 'DELIVERED', "
        "'FAILED', 'DEAD', 'CANCELLED')",
    )
    op.drop_column("outbox_messages", "admitted_at")
    op.drop_column("outbox_messages", "event_seq_hwm")
    op.drop_column("outbox_messages", "manager_epoch")

    op.drop_constraint(
        "ck_reply_plans_event_seq_hwm_nonnegative",
        "reply_plans",
        type_="check",
    )
    op.drop_constraint(
        "ck_reply_plans_manager_epoch_nonnegative",
        "reply_plans",
        type_="check",
    )
    op.drop_column("reply_plans", "event_seq_hwm")
    op.drop_column("reply_plans", "manager_epoch")

    op.drop_constraint(
        "uq_inbox_conversation_event_seq",
        "inbox_messages",
        type_="unique",
    )
    op.drop_constraint(
        "ck_inbox_conversation_event_seq_positive",
        "inbox_messages",
        type_="check",
    )
    op.drop_column("inbox_messages", "conversation_event_seq")

    op.drop_constraint(
        "ck_conversations_handoff_consistency",
        "conversations",
        type_="check",
    )
    op.drop_constraint(
        "ck_conversations_manager_sequence_hwm_nonnegative",
        "conversations",
        type_="check",
    )
    op.drop_constraint(
        "ck_conversations_current_event_seq_nonnegative",
        "conversations",
        type_="check",
    )
    op.drop_constraint(
        "ck_conversations_manager_epoch_nonnegative",
        "conversations",
        type_="check",
    )
    op.drop_constraint(
        "ck_conversations_handoff_state",
        "conversations",
        type_="check",
    )
    op.drop_column("conversations", "human_pause_anchor_at")
    op.drop_column("conversations", "handoff_deadline_at")
    op.drop_column("conversations", "manager_sequence_hwm")
    op.drop_column("conversations", "current_event_seq")
    op.drop_column("conversations", "manager_epoch")
    op.drop_column("conversations", "handoff_state")
