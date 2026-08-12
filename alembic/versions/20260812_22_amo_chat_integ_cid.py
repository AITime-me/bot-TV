"""AMO-01B1: additive integration_conversation_id on chat bindings.

Revision ID: 20260812_22_amo_chat_integ_cid
Revises: 20260812_21_amocrm_chat_proj
Create Date: 2026-08-12

Nullable. Captured from Chat webhook message.conversation.client_id.
No chat/contact/deal create.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_22_amo_chat_integ_cid"
down_revision: Union[str, Sequence[str], None] = "20260812_21_amocrm_chat_proj"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "amocrm_chat_bindings",
        sa.Column(
            "integration_conversation_id",
            sa.String(length=128),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_amocrm_chat_bindings_integ_cid_nonempty",
        "amocrm_chat_bindings",
        "integration_conversation_id IS NULL OR "
        "char_length(integration_conversation_id) >= 1",
    )
    op.create_index(
        "ix_amocrm_chat_bindings_integration_conversation_id",
        "amocrm_chat_bindings",
        ["integration_conversation_id"],
        unique=True,
        postgresql_where=sa.text("integration_conversation_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_amocrm_chat_bindings_integration_conversation_id",
        table_name="amocrm_chat_bindings",
    )
    op.drop_constraint(
        "ck_amocrm_chat_bindings_integ_cid_nonempty",
        "amocrm_chat_bindings",
        type_="check",
    )
    op.drop_column("amocrm_chat_bindings", "integration_conversation_id")
