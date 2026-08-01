"""Attachment spool lease lifecycle columns and constraints (Stage 1A2A).

Revision ID: 20260801_16_spool_leases
Revises: 20260801_15_attachment_spool
Create Date: 2026-08-01

Expand-only: lease columns, LEASED/DELETE_PENDING states, lease indexes.
No plaintext, raw tokens, production secrets, or BOT_MODE changes.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260801_16_spool_leases"
down_revision: Union[str, Sequence[str], None] = "20260801_15_attachment_spool"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATE_LEASE_CHECK = (
    "(state = 'WRITING' AND lease_token_digest IS NULL) OR "
    "(state = 'STORED' AND lease_token_digest IS NULL) OR "
    "(state = 'LEASED' AND lease_token_digest IS NOT NULL "
    "AND lease_expires_at > leased_at) OR "
    "(state = 'DELETE_PENDING')"
)


def upgrade() -> None:
    op.add_column(
        "attachment_spool_objects",
        sa.Column("lease_token_digest", postgresql.BYTEA(), nullable=True),
    )
    op.add_column(
        "attachment_spool_objects",
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "attachment_spool_objects",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint(
        "ck_attachment_spool_objects_state",
        "attachment_spool_objects",
        type_="check",
    )
    op.create_check_constraint(
        "ck_attachment_spool_objects_state",
        "attachment_spool_objects",
        "state IN ('WRITING', 'STORED', 'LEASED', 'DELETE_PENDING')",
    )
    op.create_check_constraint(
        "ck_attachment_spool_objects_lease_digest_len",
        "attachment_spool_objects",
        "lease_token_digest IS NULL OR octet_length(lease_token_digest) = 32",
    )
    op.create_check_constraint(
        "ck_attachment_spool_objects_lease_fields_all_or_none",
        "attachment_spool_objects",
        "(lease_token_digest IS NULL AND leased_at IS NULL AND lease_expires_at IS NULL) "
        "OR (lease_token_digest IS NOT NULL AND leased_at IS NOT NULL "
        "AND lease_expires_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_attachment_spool_objects_state_lease",
        "attachment_spool_objects",
        _STATE_LEASE_CHECK,
    )
    op.create_index(
        "uq_attachment_spool_objects_lease_token_digest",
        "attachment_spool_objects",
        ["lease_token_digest"],
        unique=True,
        postgresql_where=sa.text("lease_token_digest IS NOT NULL"),
    )
    op.create_index(
        "ix_attachment_spool_objects_leased_expires_at",
        "attachment_spool_objects",
        ["lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("state = 'LEASED'"),
    )
    op.create_index(
        "ix_attachment_spool_objects_object_expiry_purge",
        "attachment_spool_objects",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("state IN ('STORED', 'LEASED')"),
    )


def downgrade() -> None:
    conn = op.get_bind()
    blocked = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM attachment_spool_objects "
            "WHERE state IN ('LEASED', 'DELETE_PENDING')"
        )
    ).scalar_one()
    if blocked:
        raise RuntimeError(
            "Cannot downgrade attachment spool lease migration: "
            "LEASED or DELETE_PENDING rows remain"
        )

    op.drop_index(
        "ix_attachment_spool_objects_object_expiry_purge",
        table_name="attachment_spool_objects",
    )
    op.drop_index(
        "ix_attachment_spool_objects_leased_expires_at",
        table_name="attachment_spool_objects",
    )
    op.drop_index(
        "uq_attachment_spool_objects_lease_token_digest",
        table_name="attachment_spool_objects",
    )
    op.drop_constraint(
        "ck_attachment_spool_objects_state_lease",
        "attachment_spool_objects",
        type_="check",
    )
    op.drop_constraint(
        "ck_attachment_spool_objects_lease_fields_all_or_none",
        "attachment_spool_objects",
        type_="check",
    )
    op.drop_constraint(
        "ck_attachment_spool_objects_lease_digest_len",
        "attachment_spool_objects",
        type_="check",
    )
    op.drop_constraint(
        "ck_attachment_spool_objects_state",
        "attachment_spool_objects",
        type_="check",
    )
    op.create_check_constraint(
        "ck_attachment_spool_objects_state",
        "attachment_spool_objects",
        "state IN ('WRITING', 'STORED')",
    )
    op.drop_column("attachment_spool_objects", "lease_expires_at")
    op.drop_column("attachment_spool_objects", "leased_at")
    op.drop_column("attachment_spool_objects", "lease_token_digest")
