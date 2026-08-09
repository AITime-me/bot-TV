"""Identity Resolution tables (CURSOR-30).

Revision ID: 20260809_19_identity_resolution
Revises: 20260808_18_master_commands
Create Date: 2026-08-09

Expand-only: canonical_identities + external_identity_links.
No live amoCRM/VK/MAX/n8n wiring. No BOT_MODE changes. No name columns.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260809_19_identity_resolution"
down_revision: Union[str, Sequence[str], None] = "20260808_18_master_commands"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CANONICAL_STATUS_SQL = "'ACTIVE', 'ARCHIVED'"
_LINK_STATUS_SQL = "'ACTIVE', 'REVOKED'"
_CONFIDENCE_SQL = "'CONFIRMED', 'SECONDARY'"
_ENTITY_KIND_SQL = (
    "'CHANNEL_ACCOUNT', 'PHONE', 'EMAIL', 'ONLINE_ZAPIS_CLIENT', "
    "'AMOCRM_CONTACT', 'AMOCRM_BUYER_CARD', 'AMOCRM_TECHNICAL_DEAL'"
)


def upgrade() -> None:
    op.create_table(
        "canonical_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_canonical_identities"),
        sa.CheckConstraint(
            f"status IN ({_CANONICAL_STATUS_SQL})",
            name="ck_canonical_identities_status",
        ),
    )

    op.create_table(
        "external_identity_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canonical_identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("connection_scope", sa.String(length=128), nullable=False),
        sa.Column("entity_kind", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_external_identity_links"),
        sa.ForeignKeyConstraint(
            ["canonical_identity_id"],
            ["canonical_identities.id"],
            name="fk_external_identity_links_canonical",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"entity_kind IN ({_ENTITY_KIND_SQL})",
            name="ck_external_identity_links_entity_kind",
        ),
        sa.CheckConstraint(
            f"status IN ({_LINK_STATUS_SQL})",
            name="ck_external_identity_links_status",
        ),
        sa.CheckConstraint(
            f"confidence IN ({_CONFIDENCE_SQL})",
            name="ck_external_identity_links_confidence",
        ),
        sa.CheckConstraint(
            "char_length(provider) BETWEEN 1 AND 64",
            name="ck_external_identity_links_provider_len",
        ),
        sa.CheckConstraint(
            "char_length(connection_scope) BETWEEN 1 AND 128",
            name="ck_external_identity_links_connection_scope_len",
        ),
        sa.CheckConstraint(
            "char_length(external_id) BETWEEN 1 AND 256",
            name="ck_external_identity_links_external_id_len",
        ),
        sa.CheckConstraint(
            "char_length(source) BETWEEN 1 AND 64",
            name="ck_external_identity_links_source_len",
        ),
        sa.CheckConstraint(
            "provider ~ '^[!-~]+$'",
            name="ck_external_identity_links_provider_printable_ascii",
        ),
        sa.CheckConstraint(
            "connection_scope ~ '^[!-~]+$'",
            name="ck_external_identity_links_connection_scope_printable_ascii",
        ),
        sa.CheckConstraint(
            "external_id ~ '^[!-~]+$'",
            name="ck_external_identity_links_external_id_printable_ascii",
        ),
        sa.CheckConstraint(
            "source ~ '^[!-~]+$'",
            name="ck_external_identity_links_source_printable_ascii",
        ),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND revoked_at IS NULL) OR "
            "(status = 'REVOKED' AND revoked_at IS NOT NULL)",
            name="ck_external_identity_links_status_revoked_at",
        ),
    )
    op.create_index(
        "uq_external_identity_links_active_key",
        "external_identity_links",
        ["provider", "connection_scope", "entity_kind", "external_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    # One ACTIVE amoCRM deal-id role: Buyer Card XOR technical/chat deal.
    op.create_index(
        "uq_external_identity_links_active_amocrm_deal_role",
        "external_identity_links",
        ["provider", "connection_scope", "external_id"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'ACTIVE' AND entity_kind IN "
            "('AMOCRM_BUYER_CARD', 'AMOCRM_TECHNICAL_DEAL')"
        ),
    )
    op.create_index(
        "ix_external_identity_links_canonical",
        "external_identity_links",
        ["canonical_identity_id"],
        unique=False,
    )
    op.create_index(
        "ix_external_identity_links_lookup",
        "external_identity_links",
        ["provider", "connection_scope", "entity_kind", "external_id"],
        unique=False,
    )
    op.create_index(
        "ix_external_identity_links_kind_external",
        "external_identity_links",
        ["entity_kind", "external_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_external_identity_links_kind_external",
        table_name="external_identity_links",
    )
    op.drop_index(
        "ix_external_identity_links_lookup",
        table_name="external_identity_links",
    )
    op.drop_index(
        "ix_external_identity_links_canonical",
        table_name="external_identity_links",
    )
    op.drop_index(
        "uq_external_identity_links_active_amocrm_deal_role",
        table_name="external_identity_links",
    )
    op.drop_index(
        "uq_external_identity_links_active_key",
        table_name="external_identity_links",
    )
    op.drop_table("external_identity_links")
    op.drop_table("canonical_identities")
