"""AMO-01B1: durable Chat message projection queue + ledger.

Revision ID: 20260812_21_amocrm_chat_proj
Revises: 20260812_20_amocrm_mgr_ingress
Create Date: 2026-08-12

No message text. No OAuth/CRM REST. No chat/contact/deal create.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260812_21_amocrm_chat_proj"
down_revision: Union[str, Sequence[str], None] = "20260812_20_amocrm_mgr_ingress"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "amocrm_message_projections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("integration_msgid", sa.String(length=40), nullable=False),
        sa.Column("amocrm_message_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
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
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "lease_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("skip_reason", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("statement_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("statement_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_amocrm_message_projections_conversation_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_amocrm_message_projections"),
        sa.UniqueConstraint(
            "source_kind",
            "source_id",
            name="uq_amocrm_message_projections_source",
        ),
        sa.UniqueConstraint(
            "integration_msgid",
            name="uq_amocrm_message_projections_integration_msgid",
        ),
        sa.UniqueConstraint(
            "amocrm_message_id",
            name="uq_amocrm_message_projections_amocrm_message_id",
        ),
        sa.CheckConstraint(
            "source_kind IN ('CLIENT_INBOUND', 'BOT_OUTBOUND')",
            name="ck_amocrm_message_projections_source_kind",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'PROJECTED', 'SKIPPED', "
            "'FAILED', 'DEAD')",
            name="ck_amocrm_message_projections_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_amocrm_message_projections_attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name="ck_amocrm_message_projections_max_attempts_positive",
        ),
        sa.CheckConstraint(
            "lease_version >= 0",
            name="ck_amocrm_message_projections_lease_version_nonnegative",
        ),
        sa.CheckConstraint(
            "integration_msgid ~ '^[cb][0-9a-f]{32}$'",
            name="ck_amocrm_message_projections_integration_msgid_format",
        ),
        sa.CheckConstraint(
            "(status = 'PROJECTED' AND amocrm_message_id IS NOT NULL) OR "
            "(status <> 'PROJECTED')",
            name="ck_amocrm_message_projections_projected_has_amo_id",
        ),
    )
    op.execute(
        "ALTER TABLE amocrm_message_projections "
        "ALTER COLUMN status SET DEFAULT 'PENDING'"
    )
    op.create_index(
        "ix_amocrm_message_projections_status_next_attempt_at",
        "amocrm_message_projections",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_amocrm_message_projections_lease_until",
        "amocrm_message_projections",
        ["lease_until"],
    )
    op.create_index(
        "ix_amocrm_message_projections_conversation_id",
        "amocrm_message_projections",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_amocrm_message_projections_conversation_id",
        table_name="amocrm_message_projections",
    )
    op.drop_index(
        "ix_amocrm_message_projections_lease_until",
        table_name="amocrm_message_projections",
    )
    op.drop_index(
        "ix_amocrm_message_projections_status_next_attempt_at",
        table_name="amocrm_message_projections",
    )
    op.drop_table("amocrm_message_projections")
