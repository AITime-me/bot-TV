"""Encrypted attachment spool metadata table (Stage 1A1).

Revision ID: 20260801_15_attachment_spool
Revises: 20260731_14_ephemeral_pii_values
Create Date: 2026-08-01

Expand-only: new attachment_spool_objects table. No existing table changes.
No plaintext, raw reference, production secrets, or BOT_MODE changes.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260801_15_attachment_spool"
down_revision: Union[str, Sequence[str], None] = "20260731_14_ephemeral_pii_values"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PURPOSE_SQL = (
    "'INBOUND_ATTACHMENT_RELAY', "
    "'OUTBOUND_ATTACHMENT_DELIVERY'"
)
_MIME_SQL = "'image/jpeg', 'image/png'"
_MAX_PLAINTEXT = 5 * 1024 * 1024
_GCM_TAG = 16


def upgrade() -> None:
    op.create_table(
        "attachment_spool_objects",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reference_digest", postgresql.BYTEA(), nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("detected_mime", sa.String(length=64), nullable=False),
        sa.Column("plaintext_size", sa.Integer(), nullable=False),
        sa.Column("ciphertext_size", sa.Integer(), nullable=False),
        sa.Column("ciphertext_sha256", postgresql.BYTEA(), nullable=False),
        sa.Column("nonce", postgresql.BYTEA(), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("crypto_version", sa.SmallInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
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
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_attachment_spool_objects"),
        sa.UniqueConstraint(
            "reference_digest",
            name="uq_attachment_spool_objects_reference_digest",
        ),
        sa.UniqueConstraint(
            "object_id",
            name="uq_attachment_spool_objects_object_id",
        ),
        sa.CheckConstraint(
            "octet_length(reference_digest) = 32",
            name="ck_attachment_spool_objects_reference_digest_len",
        ),
        sa.CheckConstraint(
            "octet_length(ciphertext_sha256) = 32",
            name="ck_attachment_spool_objects_ciphertext_sha256_len",
        ),
        sa.CheckConstraint(
            "octet_length(nonce) = 12",
            name="ck_attachment_spool_objects_nonce_len",
        ),
        sa.CheckConstraint(
            "crypto_version = 1",
            name="ck_attachment_spool_objects_crypto_version",
        ),
        sa.CheckConstraint(
            "kind = 'IMAGE'",
            name="ck_attachment_spool_objects_kind",
        ),
        sa.CheckConstraint(
            f"purpose IN ({_PURPOSE_SQL})",
            name="ck_attachment_spool_objects_purpose",
        ),
        sa.CheckConstraint(
            f"detected_mime IN ({_MIME_SQL})",
            name="ck_attachment_spool_objects_detected_mime",
        ),
        sa.CheckConstraint(
            "state IN ('WRITING', 'STORED')",
            name="ck_attachment_spool_objects_state",
        ),
        sa.CheckConstraint(
            f"plaintext_size > 0 AND plaintext_size <= {_MAX_PLAINTEXT}",
            name="ck_attachment_spool_objects_plaintext_size",
        ),
        sa.CheckConstraint(
            f"ciphertext_size = plaintext_size + {_GCM_TAG}",
            name="ck_attachment_spool_objects_ciphertext_size",
        ),
        sa.CheckConstraint(
            "key_id ~ '^[A-Z0-9_]{1,64}$'",
            name="ck_attachment_spool_objects_key_id",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_attachment_spool_objects_expires_after_created",
        ),
    )
    op.create_index(
        "ix_attachment_spool_objects_expires_at",
        "attachment_spool_objects",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_attachment_spool_objects_state_updated_at",
        "attachment_spool_objects",
        ["state", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_attachment_spool_objects_state_updated_at",
        table_name="attachment_spool_objects",
    )
    op.drop_index(
        "ix_attachment_spool_objects_expires_at",
        table_name="attachment_spool_objects",
    )
    op.drop_table("attachment_spool_objects")
