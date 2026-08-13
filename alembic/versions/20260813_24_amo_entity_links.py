"""AMO-01B2: conversation-scoped CRM entity links.

Revision ID: 20260813_24_amo_entity_links
Revises: 20260813_23_amocrm_crm_oauth
Create Date: 2026-08-13

CONTACT | TECHNICAL_DEAL. ACTIVE unique per conversation+kind.
No chat/contact/deal create side-effects in this migration.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_24_amo_entity_links"
down_revision: Union[str, Sequence[str], None] = "20260813_23_amocrm_crm_oauth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "amocrm_entity_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_kind", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_amocrm_entity_links_conversation_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_amocrm_entity_links"),
        sa.CheckConstraint(
            "entity_kind IN ('CONTACT', 'TECHNICAL_DEAL')",
            name="ck_amocrm_entity_links_entity_kind",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')",
            name="ck_amocrm_entity_links_status",
        ),
        sa.CheckConstraint(
            "char_length(external_id) >= 1",
            name="ck_amocrm_entity_links_external_id_nonempty",
        ),
    )
    op.create_index(
        "ix_amocrm_entity_links_conversation_id",
        "amocrm_entity_links",
        ["conversation_id"],
    )
    op.create_index(
        "ix_amocrm_entity_links_status",
        "amocrm_entity_links",
        ["status"],
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


def downgrade() -> None:
    op.drop_index(
        "uq_amocrm_entity_links_active_kind_external",
        table_name="amocrm_entity_links",
    )
    op.drop_index(
        "uq_amocrm_entity_links_active_conversation_kind",
        table_name="amocrm_entity_links",
    )
    op.drop_index(
        "ix_amocrm_entity_links_status",
        table_name="amocrm_entity_links",
    )
    op.drop_index(
        "ix_amocrm_entity_links_conversation_id",
        table_name="amocrm_entity_links",
    )
    op.drop_table("amocrm_entity_links")
