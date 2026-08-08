"""Master channel bindings table (CURSOR-27).

Revision ID: 20260807_17_master_bindings
Revises: 20260801_16_spool_leases
Create Date: 2026-08-07

Expand-only: new master_channel_bindings table.
No plaintext PII columns, no online-zapis-tv FK, no BOT_MODE / live channel wiring.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260807_17_master_bindings"
down_revision: Union[str, Sequence[str], None] = "20260801_16_spool_leases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHANNEL_SQL = "'synthetic', 'vk', 'max'"
_STATUS_SQL = "'ACTIVE', 'REVOKED'"
_MASTER_ID_RE = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def upgrade() -> None:
    op.create_table(
        "master_channel_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("connection_scope", sa.String(length=128), nullable=False),
        sa.Column("external_account_id", sa.String(length=128), nullable=False),
        sa.Column("master_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_master_channel_bindings"),
        sa.CheckConstraint(
            f"channel IN ({_CHANNEL_SQL})",
            name="ck_master_channel_bindings_channel",
        ),
        sa.CheckConstraint(
            f"status IN ({_STATUS_SQL})",
            name="ck_master_channel_bindings_status",
        ),
        sa.CheckConstraint(
            "char_length(connection_scope) BETWEEN 1 AND 128",
            name="ck_master_channel_bindings_connection_scope_len",
        ),
        sa.CheckConstraint(
            "char_length(external_account_id) BETWEEN 1 AND 128",
            name="ck_master_channel_bindings_external_account_id_len",
        ),
        # Printable ASCII excluding space/DEL — locale-independent, == ^[\x21-\x7E]+$
        sa.CheckConstraint(
            "connection_scope ~ '^[!-~]+$'",
            name="ck_master_channel_bindings_connection_scope_printable_ascii",
        ),
        sa.CheckConstraint(
            "external_account_id ~ '^[!-~]+$'",
            name="ck_master_channel_bindings_external_account_id_printable_ascii",
        ),
        sa.CheckConstraint(
            f"master_id ~ '{_MASTER_ID_RE}'",
            name="ck_master_channel_bindings_master_id",
        ),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND revoked_at IS NULL) OR "
            "(status = 'REVOKED' AND revoked_at IS NOT NULL)",
            name="ck_master_channel_bindings_status_revoked_at",
        ),
    )
    op.create_index(
        "uq_master_channel_bindings_active_identity",
        "master_channel_bindings",
        ["channel", "connection_scope", "external_account_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "ix_master_channel_bindings_master_id",
        "master_channel_bindings",
        ["master_id"],
        unique=False,
    )
    op.create_index(
        "ix_master_channel_bindings_identity",
        "master_channel_bindings",
        ["channel", "connection_scope", "external_account_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_master_channel_bindings_identity",
        table_name="master_channel_bindings",
    )
    op.drop_index(
        "ix_master_channel_bindings_master_id",
        table_name="master_channel_bindings",
    )
    op.drop_index(
        "uq_master_channel_bindings_active_identity",
        table_name="master_channel_bindings",
    )
    op.drop_table("master_channel_bindings")
