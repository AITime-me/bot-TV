"""Self-booking confirmed-create pending foundation (SELF-BOOKING-COMMAND-01).

Revision ID: 20260820_28_self_booking_create
Revises: 20260818_27_amocrm_deal_kind
Create Date: 2026-08-20

Expand-only: self_booking_create_pendings table.
No dialog/Booking HTTP wiring. No plaintext PII columns.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260820_28_self_booking_create"
down_revision: Union[str, Sequence[str], None] = "20260818_27_amocrm_deal_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHANNEL_SQL = "'synthetic'"
_STATE_SQL = (
    "'READY', 'EXECUTING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED'"
)
_UUID_RE = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def upgrade() -> None:
    op.create_table(
        "self_booking_create_pendings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "conversation_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column(
            "confirm_external_message_id", sa.String(length=128), nullable=False
        ),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("command_version", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=36), nullable=False),
        sa.Column("slot_id", sa.String(length=128), nullable=False),
        sa.Column("starts_at", sa.String(length=32), nullable=False),
        sa.Column("fence_context_version", sa.Integer(), nullable=False),
        sa.Column("fence_manager_epoch", sa.Integer(), nullable=False),
        sa.Column("fence_event_seq_hwm", sa.Integer(), nullable=False),
        sa.Column("personal_data_consent", sa.Boolean(), nullable=False),
        sa.Column("offer_acknowledgement", sa.Boolean(), nullable=False),
        sa.Column("phone_ref_token", sa.String(length=64), nullable=False),
        sa.Column("name_ref_token", sa.String(length=64), nullable=False),
        sa.Column(
            "execution_lease_token",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "execution_lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("result_code", sa.String(length=64), nullable=True),
        sa.Column("result_outcome", sa.String(length=64), nullable=True),
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
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            f"channel IN ({_CHANNEL_SQL})",
            name="ck_self_booking_create_pendings_channel",
        ),
        sa.CheckConstraint(
            f"state IN ({_STATE_SQL})",
            name="ck_self_booking_create_pendings_state",
        ),
        sa.CheckConstraint(
            "command_version >= 1",
            name="ck_self_booking_create_pendings_command_version",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_self_booking_create_pendings_attempt_count",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name="ck_self_booking_create_pendings_max_attempts",
        ),
        sa.CheckConstraint(
            "fence_context_version >= 0",
            name="ck_self_booking_create_pendings_fence_context_version",
        ),
        sa.CheckConstraint(
            "fence_manager_epoch >= 0",
            name="ck_self_booking_create_pendings_fence_manager_epoch",
        ),
        sa.CheckConstraint(
            "fence_event_seq_hwm >= 0",
            name="ck_self_booking_create_pendings_fence_event_seq_hwm",
        ),
        sa.CheckConstraint(
            "char_length(confirm_external_message_id) BETWEEN 1 AND 128",
            name="ck_self_booking_create_pendings_confirm_msg_len",
        ),
        sa.CheckConstraint(
            "confirm_external_message_id ~ '^[!-~]+$'",
            name="ck_self_booking_create_pendings_confirm_msg_ascii",
        ),
        sa.CheckConstraint(
            "char_length(slot_id) BETWEEN 1 AND 128",
            name="ck_self_booking_create_pendings_slot_id_len",
        ),
        sa.CheckConstraint(
            f"idempotency_key ~ '{_UUID_RE}'",
            name="ck_self_booking_create_pendings_idempotency_key",
        ),
        sa.CheckConstraint(
            "personal_data_consent IS TRUE",
            name="ck_self_booking_create_pendings_consent",
        ),
        sa.CheckConstraint(
            "offer_acknowledgement IS TRUE",
            name="ck_self_booking_create_pendings_offer",
        ),
        sa.CheckConstraint(
            "char_length(phone_ref_token) BETWEEN 1 AND 64",
            name="ck_self_booking_create_pendings_phone_ref_len",
        ),
        sa.CheckConstraint(
            "char_length(name_ref_token) BETWEEN 1 AND 64",
            name="ck_self_booking_create_pendings_name_ref_len",
        ),
    )
    op.create_index(
        "uq_self_booking_create_pendings_confirm",
        "self_booking_create_pendings",
        ["channel", "confirm_external_message_id"],
        unique=True,
    )
    op.create_index(
        "uq_self_booking_create_pendings_active_conversation",
        "self_booking_create_pendings",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('READY', 'EXECUTING')"),
    )
    op.create_index(
        "ix_self_booking_create_pendings_conversation_state",
        "self_booking_create_pendings",
        ["conversation_id", "state"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_self_booking_create_pendings_conversation_state",
        table_name="self_booking_create_pendings",
    )
    op.drop_index(
        "uq_self_booking_create_pendings_active_conversation",
        table_name="self_booking_create_pendings",
    )
    op.drop_index(
        "uq_self_booking_create_pendings_confirm",
        table_name="self_booking_create_pendings",
    )
    op.drop_table("self_booking_create_pendings")
