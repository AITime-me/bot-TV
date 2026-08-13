"""AMO-01B2: durable encrypted CRM OAuth token store.

Revision ID: 20260813_23_amocrm_crm_oauth
Revises: 20260812_22_amo_chat_integ_cid
Create Date: 2026-08-13

AES-256-GCM ciphertext only. No Chat secrets. No entity writes.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_23_amocrm_crm_oauth"
down_revision: Union[str, Sequence[str], None] = "20260812_22_amo_chat_integ_cid"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "amocrm_crm_oauth_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_scope", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column(
            "crypto_version",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("access_nonce", postgresql.BYTEA(), nullable=False),
        sa.Column("access_ciphertext", postgresql.BYTEA(), nullable=False),
        sa.Column("refresh_nonce", postgresql.BYTEA(), nullable=False),
        sa.Column("refresh_ciphertext", postgresql.BYTEA(), nullable=False),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_amocrm_crm_oauth_tokens"),
        sa.UniqueConstraint(
            "connection_scope",
            name="uq_amocrm_crm_oauth_tokens_connection_scope",
        ),
        sa.CheckConstraint(
            "char_length(connection_scope) BETWEEN 1 AND 64",
            name="ck_amocrm_crm_oauth_tokens_scope_len",
        ),
        sa.CheckConstraint(
            "crypto_version = 1",
            name="ck_amocrm_crm_oauth_tokens_crypto_version",
        ),
        sa.CheckConstraint(
            "octet_length(access_nonce) = 12",
            name="ck_amocrm_crm_oauth_tokens_access_nonce_len",
        ),
        sa.CheckConstraint(
            "octet_length(refresh_nonce) = 12",
            name="ck_amocrm_crm_oauth_tokens_refresh_nonce_len",
        ),
        sa.CheckConstraint(
            "octet_length(access_ciphertext) >= 16",
            name="ck_amocrm_crm_oauth_tokens_access_ct_len",
        ),
        sa.CheckConstraint(
            "octet_length(refresh_ciphertext) >= 16",
            name="ck_amocrm_crm_oauth_tokens_refresh_ct_len",
        ),
        sa.CheckConstraint(
            "key_id ~ '^[A-Z0-9_]{1,64}$'",
            name="ck_amocrm_crm_oauth_tokens_key_id",
        ),
        sa.CheckConstraint(
            "lease_version >= 0",
            name="ck_amocrm_crm_oauth_tokens_lease_version_nonnegative",
        ),
    )
    op.create_index(
        "ix_amocrm_crm_oauth_tokens_lease_until",
        "amocrm_crm_oauth_tokens",
        ["lease_until"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_amocrm_crm_oauth_tokens_lease_until",
        table_name="amocrm_crm_oauth_tokens",
    )
    op.drop_table("amocrm_crm_oauth_tokens")
