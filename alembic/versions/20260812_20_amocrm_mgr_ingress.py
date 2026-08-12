"""AMO-01A: durable amoCRM manager ingress + chat binding.

Revision ID: 20260812_20_amocrm_mgr_ingress
Revises: 20260809_19_identity_resolution
Create Date: 2026-08-12

Expand-only:
- ingress_events may accept channel=amocrm + AMOCRM_MANAGER_MESSAGE
- composite channel/event pairing fail-closed
- widen ingress/manager external ids for amo:{chat}:{message} namespace
- amocrm_chat_bindings maps amo chat id → existing conversation (synthetic)

No outbound amoCRM HTTP. No OAuth/CRM REST. Conversations/inbox channel checks
stay synthetic-only. manager_messages.channel stays synthetic; AMO provenance
uses namespaced external_message_id.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260812_20_amocrm_mgr_ingress"
down_revision: Union[str, Sequence[str], None] = "20260809_19_identity_resolution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PAIRING_SQL = "(channel = 'synthetic' AND event_type = 'SYNTHETIC_MESSAGE') OR (channel = 'amocrm' AND event_type = 'AMOCRM_MANAGER_MESSAGE')"


def upgrade() -> None:
    op.drop_constraint("ck_ingress_channel", "ingress_events", type_="check")
    op.create_check_constraint(
        "ck_ingress_channel",
        "ingress_events",
        "channel IN ('synthetic', 'amocrm')",
    )
    op.drop_constraint("ck_ingress_event_type", "ingress_events", type_="check")
    op.create_check_constraint(
        "ck_ingress_event_type",
        "ingress_events",
        "event_type IN ('SYNTHETIC_MESSAGE', 'AMOCRM_MANAGER_MESSAGE')",
    )
    op.create_check_constraint(
        "ck_ingress_channel_event_pairing",
        "ingress_events",
        _PAIRING_SQL,
    )

    op.alter_column(
        "ingress_events",
        "external_event_id",
        existing_type=sa.String(length=128),
        type_=sa.String(length=256),
        existing_nullable=False,
    )
    op.alter_column(
        "manager_messages",
        "external_message_id",
        existing_type=sa.String(length=128),
        type_=sa.String(length=256),
        existing_nullable=False,
    )

    op.create_table(
        "amocrm_chat_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amocrm_chat_id", sa.String(length=128), nullable=False),
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
            name="fk_amocrm_chat_bindings_conversation_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_amocrm_chat_bindings"),
        sa.UniqueConstraint(
            "conversation_id",
            name="uq_amocrm_chat_bindings_conversation_id",
        ),
        sa.UniqueConstraint(
            "amocrm_chat_id",
            name="uq_amocrm_chat_bindings_amocrm_chat_id",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')",
            name="ck_amocrm_chat_bindings_status",
        ),
        sa.CheckConstraint(
            "char_length(amocrm_chat_id) >= 1",
            name="ck_amocrm_chat_bindings_chat_id_nonempty",
        ),
    )
    op.create_index(
        "ix_amocrm_chat_bindings_status",
        "amocrm_chat_bindings",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_amocrm_chat_bindings_status", table_name="amocrm_chat_bindings")
    op.drop_table("amocrm_chat_bindings")

    # Fail closed: refuse downgrade while amocrm ingress rows exist.
    conn = op.get_bind()
    remaining = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM ingress_events "
            "WHERE channel = 'amocrm' OR event_type = 'AMOCRM_MANAGER_MESSAGE'"
        )
    ).scalar_one()
    if remaining:
        raise RuntimeError("AMOCRM_INGRESS_ROWS_PRESENT")

    op.drop_constraint(
        "ck_ingress_channel_event_pairing",
        "ingress_events",
        type_="check",
    )
    op.drop_constraint("ck_ingress_event_type", "ingress_events", type_="check")
    op.create_check_constraint(
        "ck_ingress_event_type",
        "ingress_events",
        "event_type IN ('SYNTHETIC_MESSAGE')",
    )
    op.drop_constraint("ck_ingress_channel", "ingress_events", type_="check")
    op.create_check_constraint(
        "ck_ingress_channel",
        "ingress_events",
        "channel IN ('synthetic')",
    )

    op.alter_column(
        "manager_messages",
        "external_message_id",
        existing_type=sa.String(length=256),
        type_=sa.String(length=128),
        existing_nullable=False,
    )
    op.alter_column(
        "ingress_events",
        "external_event_id",
        existing_type=sa.String(length=256),
        type_=sa.String(length=128),
        existing_nullable=False,
    )
