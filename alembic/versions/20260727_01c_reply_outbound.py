"""Add ReplyPlan, conversation context, and outbound arbiter fields (01C).

Revision ID: 20260727_01c_reply_outbound
Revises: 20260727_01b_ingress
Create Date: 2026-07-27

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260727_01c_reply_outbound"
down_revision: Union[str, Sequence[str], None] = "20260727_01b_ingress"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "ownership",
            sa.String(length=32),
            server_default=sa.text("'BOT'"),
            nullable=False,
        ),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "context_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "conversations",
        sa.Column("last_client_activity_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("active_reply_plan_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_conversations_ownership",
        "conversations",
        "ownership IN ('BOT', 'MANAGER')",
    )
    op.create_check_constraint(
        "ck_conversations_context_version_nonnegative",
        "conversations",
        "context_version >= 0",
    )

    op.create_table(
        "reply_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("context_version", sa.Integer(), nullable=False),
        sa.Column("plan_type", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "bot_response_delay_ms",
            sa.Integer(),
            server_default=sa.text("5000"),
            nullable=False,
        ),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("cancel_reason", sa.String(length=64), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "lease_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "plan_type IN ('CLIENT_REPLY', 'SERVICE_SIGNAL')",
            name="ck_reply_plans_plan_type",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'READY', 'PROCESSING', 'DISPATCHED', "
            "'CANCELLED', 'SUPERSEDED', 'FAILED', 'DEAD')",
            name="ck_reply_plans_status",
        ),
        sa.CheckConstraint(
            "bot_response_delay_ms >= 0",
            name="ck_reply_plans_delay_nonnegative",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_reply_plans_attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name="ck_reply_plans_max_attempts_positive",
        ),
        sa.CheckConstraint(
            "lease_version >= 0",
            name="ck_reply_plans_lease_version_nonnegative",
        ),
        sa.CheckConstraint(
            "context_version >= 0",
            name="ck_reply_plans_context_version_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "context_version",
            name="uq_reply_plans_conversation_context_version",
        ),
    )
    op.create_index(
        "ix_reply_plans_status_not_before",
        "reply_plans",
        ["status", "not_before"],
    )
    op.create_index("ix_reply_plans_lease_until", "reply_plans", ["lease_until"])
    op.create_index(
        "ix_reply_plans_conversation_id",
        "reply_plans",
        ["conversation_id"],
    )

    op.create_foreign_key(
        "fk_conversations_active_reply_plan_id",
        "conversations",
        "reply_plans",
        ["active_reply_plan_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_constraint("ck_outbox_destination_type", "outbox_messages", type_="check")
    op.drop_constraint("ck_outbox_delivery_status", "outbox_messages", type_="check")

    op.add_column(
        "outbox_messages",
        sa.Column("reply_plan_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "outbox_messages",
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "outbox_messages",
        sa.Column("context_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "outbox_messages",
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "outbox_messages",
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "outbox_messages",
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
    )
    op.add_column(
        "outbox_messages",
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "outbox_messages",
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "outbox_messages",
        sa.Column(
            "lease_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "outbox_messages",
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "outbox_messages",
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    op.create_foreign_key(
        "fk_outbox_messages_reply_plan_id",
        "outbox_messages",
        "reply_plans",
        ["reply_plan_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_outbox_messages_reply_plan_id",
        "outbox_messages",
        ["reply_plan_id"],
    )
    op.create_index(
        "ix_outbox_messages_status_not_before",
        "outbox_messages",
        ["delivery_status", "not_before"],
    )
    op.create_index(
        "ix_outbox_messages_lease_until",
        "outbox_messages",
        ["lease_until"],
    )
    op.create_unique_constraint(
        "uq_outbox_idempotency_key",
        "outbox_messages",
        ["idempotency_key"],
    )
    op.create_unique_constraint(
        "uq_outbox_reply_plan_destination",
        "outbox_messages",
        ["reply_plan_id", "destination_type"],
    )
    op.create_check_constraint(
        "ck_outbox_destination_type",
        "outbox_messages",
        "destination_type IN ('INTERNAL_DRAFT', 'SYNTHETIC_OUTBOUND')",
    )
    op.create_check_constraint(
        "ck_outbox_delivery_status",
        "outbox_messages",
        "delivery_status IN ('PENDING', 'PROCESSING', 'DELIVERED', "
        "'FAILED', 'DEAD', 'CANCELLED')",
    )
    op.create_check_constraint(
        "ck_outbox_attempt_count_nonnegative",
        "outbox_messages",
        "attempt_count >= 0",
    )
    op.create_check_constraint(
        "ck_outbox_max_attempts_positive",
        "outbox_messages",
        "max_attempts > 0",
    )
    op.create_check_constraint(
        "ck_outbox_lease_version_nonnegative",
        "outbox_messages",
        "lease_version >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_outbox_lease_version_nonnegative", "outbox_messages", type_="check")
    op.drop_constraint("ck_outbox_max_attempts_positive", "outbox_messages", type_="check")
    op.drop_constraint("ck_outbox_attempt_count_nonnegative", "outbox_messages", type_="check")
    op.drop_constraint("ck_outbox_delivery_status", "outbox_messages", type_="check")
    op.drop_constraint("ck_outbox_destination_type", "outbox_messages", type_="check")
    op.drop_constraint("uq_outbox_reply_plan_destination", "outbox_messages", type_="unique")
    op.drop_constraint("uq_outbox_idempotency_key", "outbox_messages", type_="unique")
    op.drop_index("ix_outbox_messages_lease_until", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_status_not_before", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_reply_plan_id", table_name="outbox_messages")
    op.drop_constraint("fk_outbox_messages_reply_plan_id", "outbox_messages", type_="foreignkey")

    op.drop_column("outbox_messages", "correlation_id")
    op.drop_column("outbox_messages", "lease_until")
    op.drop_column("outbox_messages", "lease_version")
    op.drop_column("outbox_messages", "lease_token")
    op.drop_column("outbox_messages", "lease_owner")
    op.drop_column("outbox_messages", "max_attempts")
    op.drop_column("outbox_messages", "attempt_count")
    op.drop_column("outbox_messages", "not_before")
    op.drop_column("outbox_messages", "context_version")
    op.drop_column("outbox_messages", "idempotency_key")
    op.drop_column("outbox_messages", "reply_plan_id")

    op.create_check_constraint(
        "ck_outbox_destination_type",
        "outbox_messages",
        "destination_type IN ('INTERNAL_DRAFT')",
    )
    op.create_check_constraint(
        "ck_outbox_delivery_status",
        "outbox_messages",
        "delivery_status IN ('PENDING', 'CANCELLED')",
    )

    op.drop_constraint(
        "fk_conversations_active_reply_plan_id",
        "conversations",
        type_="foreignkey",
    )
    op.drop_index("ix_reply_plans_conversation_id", table_name="reply_plans")
    op.drop_index("ix_reply_plans_lease_until", table_name="reply_plans")
    op.drop_index("ix_reply_plans_status_not_before", table_name="reply_plans")
    op.drop_table("reply_plans")

    op.drop_constraint(
        "ck_conversations_context_version_nonnegative",
        "conversations",
        type_="check",
    )
    op.drop_constraint("ck_conversations_ownership", "conversations", type_="check")
    op.drop_column("conversations", "active_reply_plan_id")
    op.drop_column("conversations", "last_client_activity_at")
    op.drop_column("conversations", "context_version")
    op.drop_column("conversations", "ownership")
