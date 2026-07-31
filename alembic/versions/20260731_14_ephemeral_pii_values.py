"""Encrypted ephemeral PII values table (Stage 2B).

Revision ID: 20260731_14_ephemeral_pii_values
Revises: 20260729_13_handoff_quarantine
Create Date: 2026-07-31

Expand-only: new ephemeral_pii_values table. No existing table changes.
No plaintext, raw reference, production secrets, or BOT_MODE changes.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260731_14_ephemeral_pii_values"
down_revision: Union[str, Sequence[str], None] = "20260729_13_handoff_quarantine"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PURPOSE_SQL = (
    "'BOOKING_PHONE_WRITE', "
    "'APPROVED_STAFF_ALERT_PHONE', "
    "'AMOCRM_CONTACT_SYNC'"
)


def upgrade() -> None:
    op.create_table(
        "ephemeral_pii_values",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reference_digest", postgresql.BYTEA(), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pii_kind", sa.String(length=32), nullable=False),
        sa.Column("allowed_purpose", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", postgresql.BYTEA(), nullable=False),
        sa.Column("nonce", postgresql.BYTEA(), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("crypto_version", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("statement_timestamp()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ephemeral_pii_values"),
        sa.UniqueConstraint(
            "reference_digest",
            name="uq_ephemeral_pii_values_reference_digest",
        ),
        sa.CheckConstraint(
            "octet_length(reference_digest) = 32",
            name="ck_ephemeral_pii_values_reference_digest_len",
        ),
        sa.CheckConstraint(
            "octet_length(nonce) = 12",
            name="ck_ephemeral_pii_values_nonce_len",
        ),
        sa.CheckConstraint(
            "octet_length(ciphertext) >= 16",
            name="ck_ephemeral_pii_values_ciphertext_len",
        ),
        sa.CheckConstraint(
            "crypto_version = 1",
            name="ck_ephemeral_pii_values_crypto_version",
        ),
        sa.CheckConstraint(
            "pii_kind = 'PHONE'",
            name="ck_ephemeral_pii_values_pii_kind",
        ),
        sa.CheckConstraint(
            f"allowed_purpose IN ({_PURPOSE_SQL})",
            name="ck_ephemeral_pii_values_allowed_purpose",
        ),
        sa.CheckConstraint(
            "key_id ~ '^[A-Z0-9_]{1,64}$'",
            name="ck_ephemeral_pii_values_key_id",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_ephemeral_pii_values_expires_after_created",
        ),
    )
    op.create_index(
        "ix_ephemeral_pii_values_expires_at",
        "ephemeral_pii_values",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ephemeral_pii_values_expires_at",
        table_name="ephemeral_pii_values",
    )
    op.drop_table("ephemeral_pii_values")
