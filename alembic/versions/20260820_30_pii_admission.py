"""Self-booking PII admission map (SELF-BOOKING-COMMAND-03H).

Revision ID: 20260820_30_pii_admission
Revises: 20260820_29_active_offer
Create Date: 2026-08-20

Expand-only: self_booking_pii_admissions table.
No CONFIRM, admit_confirmed, CREATE, or ingress wiring.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260820_30_pii_admission"
down_revision: Union[str, Sequence[str], None] = "20260820_29_active_offer"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "self_booking_pii_admissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "conversation_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("phone_ref_token", sa.String(length=64), nullable=False),
        sa.Column("name_ref_token", sa.String(length=64), nullable=False),
        sa.Column("content_mac", postgresql.BYTEA(), nullable=False),
        sa.Column("mac_key_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("statement_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(request_id) BETWEEN 1 AND 128",
            name="ck_self_booking_pii_admissions_request_id_len",
        ),
        sa.CheckConstraint(
            "request_id ~ '^[!-~]+$'",
            name="ck_self_booking_pii_admissions_request_id_ascii",
        ),
        sa.CheckConstraint(
            "char_length(phone_ref_token) BETWEEN 1 AND 64",
            name="ck_self_booking_pii_admissions_phone_ref_len",
        ),
        sa.CheckConstraint(
            "char_length(name_ref_token) BETWEEN 1 AND 64",
            name="ck_self_booking_pii_admissions_name_ref_len",
        ),
        sa.CheckConstraint(
            "octet_length(content_mac) = 32",
            name="ck_self_booking_pii_admissions_content_mac_len",
        ),
        sa.CheckConstraint(
            "mac_key_id ~ '^[A-Z0-9_]{1,64}$'",
            name="ck_self_booking_pii_admissions_mac_key_id",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "request_id",
            name="uq_self_booking_pii_admissions_request",
        ),
    )


def downgrade() -> None:
    op.drop_table("self_booking_pii_admissions")
