"""Self-booking active-offer foundation (SELF-BOOKING-COMMAND-03C).

Revision ID: 20260820_29_active_offer
Revises: 20260820_28_self_booking_create
Create Date: 2026-08-20

Expand-only: self_booking_active_offers table.
No PII, confirm schema, admit, or CREATE wiring.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260820_29_active_offer"
down_revision: Union[str, Sequence[str], None] = "20260820_28_self_booking_create"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "self_booking_active_offers",
        sa.Column(
            "conversation_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "source_outbound_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("source_context_version", sa.Integer(), nullable=False),
        sa.Column("source_manager_epoch", sa.Integer(), nullable=False),
        sa.Column("source_event_seq_hwm", sa.Integer(), nullable=False),
        sa.Column(
            "offered_slots",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "activated_at",
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
        sa.CheckConstraint(
            "source_context_version >= 0",
            name="ck_self_booking_active_offers_context_version",
        ),
        sa.CheckConstraint(
            "source_manager_epoch >= 0",
            name="ck_self_booking_active_offers_manager_epoch",
        ),
        sa.CheckConstraint(
            "source_event_seq_hwm >= 0",
            name="ck_self_booking_active_offers_event_seq_hwm",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(offered_slots) = 'array'",
            name="ck_self_booking_active_offers_slots_array",
        ),
        sa.CheckConstraint(
            "jsonb_array_length(offered_slots) BETWEEN 1 AND 3",
            name="ck_self_booking_active_offers_slots_len",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_outbound_id"],
            ["outbox_messages.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("conversation_id"),
    )
    op.create_index(
        "uq_self_booking_active_offers_outbound",
        "self_booking_active_offers",
        ["source_outbound_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_self_booking_active_offers_outbound",
        table_name="self_booking_active_offers",
    )
    op.drop_table("self_booking_active_offers")
