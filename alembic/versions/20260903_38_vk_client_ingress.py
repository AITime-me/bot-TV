"""VK CLIENT shadow ingress: real vk channel + VK_CLIENT_MESSAGE.

Revision ID: 20260903_38_vk_client_ingress
Revises: 20260829_37_control_plane
Create Date: 2026-09-03

Expand-only:
- conversations / inbox_messages may accept channel=vk
- ingress_events may accept channel=vk + VK_CLIENT_MESSAGE
- composite channel/event pairing fail-closed includes vk

Preserves existing synthetic / amocrm rows. No outbound VK. No amoCRM redesign.
Shadow-observer client ingress only — native studio VK→amoCRM stays untouched.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "20260903_38_vk_client_ingress"
down_revision: Union[str, Sequence[str], None] = "20260829_37_control_plane"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PAIRING_SQL = "(channel = 'synthetic' AND event_type = 'SYNTHETIC_MESSAGE') OR (channel = 'amocrm' AND event_type = 'AMOCRM_MANAGER_MESSAGE') OR (channel = 'vk' AND event_type = 'VK_CLIENT_MESSAGE')"


def upgrade() -> None:
    op.drop_constraint("ck_conversations_channel", "conversations", type_="check")
    op.create_check_constraint(
        "ck_conversations_channel",
        "conversations",
        "channel IN ('synthetic', 'vk')",
    )

    op.drop_constraint("ck_inbox_channel", "inbox_messages", type_="check")
    op.create_check_constraint(
        "ck_inbox_channel",
        "inbox_messages",
        "channel IN ('synthetic', 'vk')",
    )

    op.drop_constraint(
        "ck_ingress_channel_event_pairing", "ingress_events", type_="check"
    )
    op.drop_constraint("ck_ingress_channel", "ingress_events", type_="check")
    op.create_check_constraint(
        "ck_ingress_channel",
        "ingress_events",
        "channel IN ('synthetic', 'amocrm', 'vk')",
    )
    op.drop_constraint("ck_ingress_event_type", "ingress_events", type_="check")
    op.create_check_constraint(
        "ck_ingress_event_type",
        "ingress_events",
        "event_type IN ('SYNTHETIC_MESSAGE', 'AMOCRM_MANAGER_MESSAGE', 'VK_CLIENT_MESSAGE')",
    )
    op.create_check_constraint(
        "ck_ingress_channel_event_pairing",
        "ingress_events",
        _PAIRING_SQL,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_ingress_channel_event_pairing", "ingress_events", type_="check"
    )
    op.drop_constraint("ck_ingress_event_type", "ingress_events", type_="check")
    op.create_check_constraint(
        "ck_ingress_event_type",
        "ingress_events",
        "event_type IN ('SYNTHETIC_MESSAGE', 'AMOCRM_MANAGER_MESSAGE')",
    )
    op.drop_constraint("ck_ingress_channel", "ingress_events", type_="check")
    op.create_check_constraint(
        "ck_ingress_channel",
        "ingress_events",
        "channel IN ('synthetic', 'amocrm')",
    )
    op.create_check_constraint(
        "ck_ingress_channel_event_pairing",
        "ingress_events",
        "(channel = 'synthetic' AND event_type = 'SYNTHETIC_MESSAGE') OR "
        "(channel = 'amocrm' AND event_type = 'AMOCRM_MANAGER_MESSAGE')",
    )

    op.drop_constraint("ck_inbox_channel", "inbox_messages", type_="check")
    op.create_check_constraint(
        "ck_inbox_channel",
        "inbox_messages",
        "channel IN ('synthetic')",
    )

    op.drop_constraint("ck_conversations_channel", "conversations", type_="check")
    op.create_check_constraint(
        "ck_conversations_channel",
        "conversations",
        "channel IN ('synthetic')",
    )
