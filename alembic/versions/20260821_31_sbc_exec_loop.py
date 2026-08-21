"""Expand worker heartbeat loop_name for self-booking CREATE drain (03L).

Revision ID: 20260821_31_sbc_exec_loop
Revises: 20260820_30_pii_admission
Create Date: 2026-08-21

Expand-only CHECK: allow ``self_booking_create`` heartbeat loop.
No CREATE HTTP, inbound, or ReplyPlan changes.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "20260821_31_sbc_exec_loop"
down_revision: Union[str, Sequence[str], None] = "20260820_30_pii_admission"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_worker_heartbeats_loop_name",
        "worker_heartbeats",
        type_="check",
    )
    op.create_check_constraint(
        "ck_worker_heartbeats_loop_name",
        "worker_heartbeats",
        "loop_name IN ("
        "'ingress', 'handoff_expiry', 'reply_plan', "
        "'outbound', 'amocrm_mirror', 'self_booking_create'"
        ")",
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM worker_heartbeats WHERE loop_name = 'self_booking_create'"
    )
    op.drop_constraint(
        "ck_worker_heartbeats_loop_name",
        "worker_heartbeats",
        type_="check",
    )
    op.create_check_constraint(
        "ck_worker_heartbeats_loop_name",
        "worker_heartbeats",
        "loop_name IN ("
        "'ingress', 'handoff_expiry', 'reply_plan', "
        "'outbound', 'amocrm_mirror'"
        ")",
    )
