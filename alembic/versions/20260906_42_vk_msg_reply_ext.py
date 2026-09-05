"""VK CLIENT message_reply external takeover + send receipt.

Revision ID: 20260906_42_vk_msg_reply_ext
Revises: 20260905_41_amo_nat_out_cap
Create Date: 2026-09-06

- ingress VK_CLIENT_MESSAGE_REPLY
- outbox.provider_message_id for own-echo match
- conversations.vk_client_external_reply_hwm ordering HWM
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_42_vk_msg_reply_ext"
down_revision: Union[str, Sequence[str], None] = "20260905_41_amo_nat_out_cap"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_ingress_channel_event_pairing", "ingress_events", type_="check")
    op.drop_constraint("ck_ingress_event_type", "ingress_events", type_="check")
    op.create_check_constraint(
        "ck_ingress_event_type",
        "ingress_events",
        "event_type IN ("
        "'SYNTHETIC_MESSAGE', 'AMOCRM_MANAGER_MESSAGE', "
        "'VK_CLIENT_MESSAGE', 'VK_CLIENT_MESSAGE_REPLY'"
        ")",
    )
    op.create_check_constraint(
        "ck_ingress_channel_event_pairing",
        "ingress_events",
        "(channel = 'synthetic' AND event_type = 'SYNTHETIC_MESSAGE') OR "
        "(channel = 'amocrm' AND event_type = 'AMOCRM_MANAGER_MESSAGE') OR "
        "(channel = 'vk' AND event_type IN ("
        "'VK_CLIENT_MESSAGE', 'VK_CLIENT_MESSAGE_REPLY'"
        "))",
    )

    op.add_column(
        "outbox_messages",
        sa.Column("provider_message_id", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_outbox_provider_message_id_positive",
        "outbox_messages",
        "provider_message_id IS NULL OR provider_message_id > 0",
    )
    op.create_index(
        "uq_outbox_vk_provider_message_id",
        "outbox_messages",
        ["provider_message_id"],
        unique=True,
        postgresql_where=sa.text(
            "provider_message_id IS NOT NULL "
            "AND destination_type = 'VK_CLIENT_OUTBOUND'"
        ),
    )

    op.add_column(
        "conversations",
        sa.Column("vk_client_external_reply_hwm", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "vk_client_external_reply_hwm")

    op.drop_index(
        "uq_outbox_vk_provider_message_id",
        table_name="outbox_messages",
    )
    op.drop_constraint(
        "ck_outbox_provider_message_id_positive",
        "outbox_messages",
        type_="check",
    )
    op.drop_column("outbox_messages", "provider_message_id")

    op.drop_constraint("ck_ingress_channel_event_pairing", "ingress_events", type_="check")
    op.drop_constraint("ck_ingress_event_type", "ingress_events", type_="check")
    op.create_check_constraint(
        "ck_ingress_event_type",
        "ingress_events",
        "event_type IN ("
        "'SYNTHETIC_MESSAGE', 'AMOCRM_MANAGER_MESSAGE', 'VK_CLIENT_MESSAGE'"
        ")",
    )
    op.create_check_constraint(
        "ck_ingress_channel_event_pairing",
        "ingress_events",
        "(channel = 'synthetic' AND event_type = 'SYNTHETIC_MESSAGE') OR "
        "(channel = 'amocrm' AND event_type = 'AMOCRM_MANAGER_MESSAGE') OR "
        "(channel = 'vk' AND event_type = 'VK_CLIENT_MESSAGE')",
    )
