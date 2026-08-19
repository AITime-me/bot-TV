"""Add AMOCRM_DEAL and restrict Lead-role XOR to Deal+Technical Lead.

Revision ID: 20260818_27_amocrm_deal_kind
Revises: 20260816_26_identity_glue
Create Date: 2026-08-18

Buyer Card is amoCRM Customer (separate id namespace from Lead).
AMOCRM_DEAL and AMOCRM_TECHNICAL_DEAL are Lead roles: one Lead id cannot
be ACTIVE in both. Expand-only. No row rewrites.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260818_27_amocrm_deal_kind"
down_revision: Union[str, Sequence[str], None] = "20260816_26_identity_glue"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_ENTITY_KIND_SQL = (
    "'CHANNEL_ACCOUNT', 'PHONE', 'EMAIL', 'ONLINE_ZAPIS_CLIENT', "
    "'AMOCRM_CONTACT', 'AMOCRM_BUYER_CARD', 'AMOCRM_TECHNICAL_DEAL'"
)
_NEW_ENTITY_KIND_SQL = (
    "'CHANNEL_ACCOUNT', 'PHONE', 'EMAIL', 'ONLINE_ZAPIS_CLIENT', "
    "'AMOCRM_CONTACT', 'AMOCRM_BUYER_CARD', 'AMOCRM_TECHNICAL_DEAL', "
    "'AMOCRM_DEAL'"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_external_identity_links_entity_kind",
        "external_identity_links",
        type_="check",
    )
    op.create_check_constraint(
        "ck_external_identity_links_entity_kind",
        "external_identity_links",
        f"entity_kind IN ({_NEW_ENTITY_KIND_SQL})",
    )
    op.drop_index(
        "uq_external_identity_links_active_amocrm_deal_role",
        table_name="external_identity_links",
    )
    op.create_index(
        "uq_external_identity_links_active_amocrm_deal_role",
        "external_identity_links",
        ["provider", "connection_scope", "external_id"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'ACTIVE' AND entity_kind IN "
            "('AMOCRM_DEAL', 'AMOCRM_TECHNICAL_DEAL')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_external_identity_links_active_amocrm_deal_role",
        table_name="external_identity_links",
    )
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
    op.drop_constraint(
        "ck_external_identity_links_entity_kind",
        "external_identity_links",
        type_="check",
    )
    op.create_check_constraint(
        "ck_external_identity_links_entity_kind",
        "external_identity_links",
        f"entity_kind IN ({_OLD_ENTITY_KIND_SQL})",
    )
