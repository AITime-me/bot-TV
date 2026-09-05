"""Add VK_CLIENT_OUTBOUND destination for closed client proof sends.

Revision ID: 20260905_40_vk_client_outbound
Revises: 20260904_39_shadow_drafts
Create Date: 2026-09-05

Expand destination CHECK and admission/delivered CHECKs so ADMITTED/DELIVERED
are legal for SYNTHETIC_OUTBOUND and VK_CLIENT_OUTBOUND. Lease/state CHECKs
unchanged. SENT remains absent.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "20260905_40_vk_client_outbound"
down_revision: Union[str, Sequence[str], None] = "20260904_39_shadow_drafts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LIVE_DEST = (
    "destination_type IN ('SYNTHETIC_OUTBOUND', 'VK_CLIENT_OUTBOUND')"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_outbox_destination_type", "outbox_messages", type_="check"
    )
    op.create_check_constraint(
        "ck_outbox_destination_type",
        "outbox_messages",
        "destination_type IN ("
        "'INTERNAL_DRAFT', 'SYNTHETIC_OUTBOUND', 'VK_CLIENT_OUTBOUND'"
        ")",
    )

    op.drop_constraint(
        "ck_outbox_admitted_destination", "outbox_messages", type_="check"
    )
    op.create_check_constraint(
        "ck_outbox_admitted_destination",
        "outbox_messages",
        "admitted_at IS NULL OR ("
        f"{_LIVE_DEST} "
        "AND delivery_status IN ('ADMITTED', 'DELIVERED', 'DEAD')"
        ")",
    )

    op.drop_constraint(
        "ck_outbox_admitted_state", "outbox_messages", type_="check"
    )
    op.create_check_constraint(
        "ck_outbox_admitted_state",
        "outbox_messages",
        "delivery_status <> 'ADMITTED' OR ("
        f"{_LIVE_DEST} AND admitted_at IS NOT NULL"
        ")",
    )

    op.drop_constraint(
        "ck_outbox_delivered_after_admission",
        "outbox_messages",
        type_="check",
    )
    op.create_check_constraint(
        "ck_outbox_delivered_after_admission",
        "outbox_messages",
        "destination_type NOT IN ('SYNTHETIC_OUTBOUND', 'VK_CLIENT_OUTBOUND') "
        "OR delivery_status <> 'DELIVERED' "
        "OR admitted_at IS NOT NULL",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE outbox_messages "
        "SET delivery_status = 'DEAD', "
        "lease_owner = NULL, lease_token = NULL, lease_until = NULL "
        "WHERE destination_type = 'VK_CLIENT_OUTBOUND' "
        "AND delivery_status IN ('PENDING', 'PROCESSING', 'ADMITTED', 'FAILED')"
    )
    op.execute(
        "DELETE FROM outbox_messages "
        "WHERE destination_type = 'VK_CLIENT_OUTBOUND'"
    )

    op.drop_constraint(
        "ck_outbox_delivered_after_admission",
        "outbox_messages",
        type_="check",
    )
    op.create_check_constraint(
        "ck_outbox_delivered_after_admission",
        "outbox_messages",
        "destination_type <> 'SYNTHETIC_OUTBOUND' "
        "OR delivery_status <> 'DELIVERED' "
        "OR admitted_at IS NOT NULL",
    )

    op.drop_constraint(
        "ck_outbox_admitted_state", "outbox_messages", type_="check"
    )
    op.create_check_constraint(
        "ck_outbox_admitted_state",
        "outbox_messages",
        "delivery_status <> 'ADMITTED' OR ("
        "destination_type = 'SYNTHETIC_OUTBOUND' AND admitted_at IS NOT NULL"
        ")",
    )

    op.drop_constraint(
        "ck_outbox_admitted_destination", "outbox_messages", type_="check"
    )
    op.create_check_constraint(
        "ck_outbox_admitted_destination",
        "outbox_messages",
        "admitted_at IS NULL OR ("
        "destination_type = 'SYNTHETIC_OUTBOUND' "
        "AND delivery_status IN ('ADMITTED', 'DELIVERED', 'DEAD')"
        ")",
    )

    op.drop_constraint(
        "ck_outbox_destination_type", "outbox_messages", type_="check"
    )
    op.create_check_constraint(
        "ck_outbox_destination_type",
        "outbox_messages",
        "destination_type IN ('INTERNAL_DRAFT', 'SYNTHETIC_OUTBOUND')",
    )
