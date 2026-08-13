"""AMO-01B2: TECHNICAL_DEAL create reservation + reconcile fence.

Revision ID: 20260813_25_amo_deal_reserve
Revises: 20260813_24_amo_entity_links
Create Date: 2026-08-13

Extends amocrm_entity_links for durable per-conversation create fencing.
No Chat/B1b. No notes/tasks. No Filtering API dependency.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_25_amo_deal_reserve"
down_revision: Union[str, Sequence[str], None] = "20260813_24_amo_entity_links"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(
        "uq_amocrm_entity_links_active_conversation_kind",
        table_name="amocrm_entity_links",
    )
    op.drop_index(
        "uq_amocrm_entity_links_active_kind_external",
        table_name="amocrm_entity_links",
    )
    op.drop_constraint(
        "ck_amocrm_entity_links_status",
        "amocrm_entity_links",
        type_="check",
    )
    op.drop_constraint(
        "ck_amocrm_entity_links_external_id_nonempty",
        "amocrm_entity_links",
        type_="check",
    )

    op.add_column(
        "amocrm_entity_links",
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "amocrm_entity_links",
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "amocrm_entity_links",
        sa.Column(
            "lease_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "amocrm_entity_links",
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "amocrm_entity_links",
        sa.Column(
            "create_submitted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.alter_column(
        "amocrm_entity_links",
        "external_id",
        existing_type=sa.String(length=128),
        nullable=True,
    )
    op.alter_column(
        "amocrm_entity_links",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
    )

    op.create_check_constraint(
        "ck_amocrm_entity_links_status",
        "amocrm_entity_links",
        "status IN ('ACTIVE', 'REVOKED', 'RESERVED', 'RECONCILE_REQUIRED')",
    )
    op.create_check_constraint(
        "ck_amocrm_entity_links_external_id_state",
        "amocrm_entity_links",
        "("
        "status IN ('RESERVED', 'RECONCILE_REQUIRED') "
        "AND (external_id IS NULL OR char_length(external_id) >= 1)"
        ") OR ("
        "status IN ('ACTIVE', 'REVOKED') "
        "AND external_id IS NOT NULL AND char_length(external_id) >= 1"
        ")",
    )
    op.create_check_constraint(
        "ck_amocrm_entity_links_lease_version_nonnegative",
        "amocrm_entity_links",
        "lease_version >= 0",
    )
    op.create_index(
        "ix_amocrm_entity_links_lease_until",
        "amocrm_entity_links",
        ["lease_until"],
    )
    op.create_index(
        "uq_amocrm_entity_links_open_conversation_kind",
        "amocrm_entity_links",
        ["conversation_id", "entity_kind"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('ACTIVE', 'RESERVED', 'RECONCILE_REQUIRED')"
        ),
    )
    op.create_index(
        "uq_amocrm_entity_links_active_kind_external",
        "amocrm_entity_links",
        ["entity_kind", "external_id"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'ACTIVE' AND external_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_amocrm_entity_links_active_kind_external",
        table_name="amocrm_entity_links",
    )
    op.drop_index(
        "uq_amocrm_entity_links_open_conversation_kind",
        table_name="amocrm_entity_links",
    )
    op.drop_index(
        "ix_amocrm_entity_links_lease_until",
        table_name="amocrm_entity_links",
    )
    op.drop_constraint(
        "ck_amocrm_entity_links_lease_version_nonnegative",
        "amocrm_entity_links",
        type_="check",
    )
    op.drop_constraint(
        "ck_amocrm_entity_links_external_id_state",
        "amocrm_entity_links",
        type_="check",
    )
    op.drop_constraint(
        "ck_amocrm_entity_links_status",
        "amocrm_entity_links",
        type_="check",
    )
    op.drop_column("amocrm_entity_links", "create_submitted_at")
    op.drop_column("amocrm_entity_links", "lease_until")
    op.drop_column("amocrm_entity_links", "lease_version")
    op.drop_column("amocrm_entity_links", "lease_token")
    op.drop_column("amocrm_entity_links", "lease_owner")
    op.execute(
        "UPDATE amocrm_entity_links SET external_id = 'unknown' "
        "WHERE external_id IS NULL"
    )
    op.alter_column(
        "amocrm_entity_links",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
    op.alter_column(
        "amocrm_entity_links",
        "external_id",
        existing_type=sa.String(length=128),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_amocrm_entity_links_status",
        "amocrm_entity_links",
        "status IN ('ACTIVE', 'REVOKED')",
    )
    op.create_check_constraint(
        "ck_amocrm_entity_links_external_id_nonempty",
        "amocrm_entity_links",
        "char_length(external_id) >= 1",
    )
    op.create_index(
        "uq_amocrm_entity_links_active_conversation_kind",
        "amocrm_entity_links",
        ["conversation_id", "entity_kind"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "uq_amocrm_entity_links_active_kind_external",
        "amocrm_entity_links",
        ["entity_kind", "external_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
