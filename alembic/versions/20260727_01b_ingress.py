"""Add durable ingress_events table for BOT-CORE-INGRESS-01B.

Revision ID: 20260727_01b_ingress
Revises: 20260727_01a_foundation
Create Date: 2026-07-27

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260727_01b_ingress"
down_revision: Union[str, Sequence[str], None] = "20260727_01a_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ingress_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("external_event_id", sa.String(length=128), nullable=False),
        sa.Column("external_conversation_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'RECEIVED'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
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
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "envelope_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
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
            "channel IN ('synthetic')",
            name="ck_ingress_channel",
        ),
        sa.CheckConstraint(
            "event_type IN ('SYNTHETIC_MESSAGE')",
            name="ck_ingress_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'FAILED', 'DEAD')",
            name="ck_ingress_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_ingress_attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            "lease_version >= 0",
            name="ck_ingress_lease_version_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel",
            "external_event_id",
            name="uq_ingress_channel_external_event_id",
        ),
    )
    op.create_index(
        "ix_ingress_events_status_created_at",
        "ingress_events",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_ingress_events_next_attempt_at",
        "ingress_events",
        ["next_attempt_at"],
    )
    op.create_index(
        "ix_ingress_events_lease_until",
        "ingress_events",
        ["lease_until"],
    )
    op.create_index(
        "ix_ingress_events_correlation_id",
        "ingress_events",
        ["correlation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ingress_events_correlation_id", table_name="ingress_events")
    op.drop_index("ix_ingress_events_lease_until", table_name="ingress_events")
    op.drop_index("ix_ingress_events_next_attempt_at", table_name="ingress_events")
    op.drop_index("ix_ingress_events_status_created_at", table_name="ingress_events")
    op.drop_table("ingress_events")
