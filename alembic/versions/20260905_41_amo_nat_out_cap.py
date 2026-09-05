"""CAPTURE-ONLY table for native amoCRM outgoing_message webhooks.

Revision ID: 20260905_41_amo_nat_out_cap
Revises: 20260905_40_vk_client_outbound
Create Date: 2026-09-05

Expand-only. No ingress_events / manager_messages / Chat binding changes.
No worker consumer for this table.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260905_41_amo_nat_out_cap"
down_revision: Union[str, Sequence[str], None] = "20260905_40_vk_client_outbound"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "amocrm_native_outgoing_captures",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("amocrm_message_id", sa.String(length=128), nullable=False),
        sa.Column("talk_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("origin", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=True),
        sa.Column("author_id", sa.String(length=128), nullable=True),
        sa.Column("author_type", sa.String(length=32), nullable=True),
        sa.Column("author_user_id", sa.String(length=64), nullable=True),
        sa.Column("recipient_id", sa.String(length=128), nullable=True),
        sa.Column("recipient_type", sa.String(length=32), nullable=True),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("message_type", sa.String(length=32), nullable=False),
        sa.Column(
            "provider_created_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("account_id", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("statement_timestamp()"),
        ),
        sa.UniqueConstraint(
            "amocrm_message_id",
            name="uq_amocrm_native_outgoing_captures_message_id",
        ),
        sa.CheckConstraint(
            "char_length(amocrm_message_id) BETWEEN 1 AND 128",
            name="ck_amocrm_native_outgoing_captures_message_id_len",
        ),
        sa.CheckConstraint(
            "char_length(chat_id) BETWEEN 1 AND 128",
            name="ck_amocrm_native_outgoing_captures_chat_id_len",
        ),
        sa.CheckConstraint(
            "talk_id > 0",
            name="ck_amocrm_native_outgoing_captures_talk_id_positive",
        ),
        sa.CheckConstraint(
            "contact_id > 0",
            name="ck_amocrm_native_outgoing_captures_contact_id_positive",
        ),
        sa.CheckConstraint(
            "source_id IS NULL OR source_id > 0",
            name="ck_amocrm_native_outgoing_captures_source_id_positive",
        ),
        sa.CheckConstraint(
            "char_length(origin) BETWEEN 1 AND 64",
            name="ck_amocrm_native_outgoing_captures_origin_len",
        ),
        sa.CheckConstraint(
            "type = 'outgoing'",
            name="ck_amocrm_native_outgoing_captures_type",
        ),
        sa.CheckConstraint(
            "message_type = 'text'",
            name="ck_amocrm_native_outgoing_captures_message_type",
        ),
    )
    op.create_index(
        "ix_amocrm_native_outgoing_captures_talk_received",
        "amocrm_native_outgoing_captures",
        ["talk_id", "received_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_amocrm_native_outgoing_captures_talk_received",
        table_name="amocrm_native_outgoing_captures",
    )
    op.drop_table("amocrm_native_outgoing_captures")
