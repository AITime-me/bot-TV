"""Add required amoCRM OAuth lifecycle worker heartbeat.

Revision ID: 20260828_36_amocrm_oauth_loop
Revises: 20260828_35_acquisition_source
Create Date: 2026-08-28
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_36_amocrm_oauth_loop"
down_revision: Union[str, Sequence[str], None] = (
    "20260828_35_acquisition_source"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PREVIOUS_LOOPS = (
    "'ingress', 'handoff_expiry', 'reply_plan', 'outbound', "
    "'amocrm_mirror', 'self_booking_create', "
    "'teya_request_orchestrator', 'teya_request_reconciliation', "
    "'booking_method_analytics', 'acquisition_source_analytics'"
)
_OAUTH_LIFECYCLE_LOOP_SQL = "'amocrm_crm_oauth_lifecycle'"


def upgrade() -> None:
    op.drop_constraint(
        "ck_worker_heartbeats_loop_name",
        "worker_heartbeats",
        type_="check",
    )
    op.create_check_constraint(
        "ck_worker_heartbeats_loop_name",
        "worker_heartbeats",
        f"loop_name IN ({_PREVIOUS_LOOPS}, {_OAUTH_LIFECYCLE_LOOP_SQL})",
    )
    op.execute(
        sa.text(
            "INSERT INTO worker_heartbeats ("
            "loop_name, generation_id, worker_id, started_at, "
            "consecutive_failures, updated_at"
            ") SELECT "
            f"{_OAUTH_LIFECYCLE_LOOP_SQL}, "
            "gen_random_uuid(), 'bootstrap', now(), 0, now() "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM worker_heartbeats "
            f"WHERE loop_name = {_OAUTH_LIFECYCLE_LOOP_SQL}"
            ")"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM worker_heartbeats "
            f"WHERE loop_name = {_OAUTH_LIFECYCLE_LOOP_SQL}"
        )
    )
    op.drop_constraint(
        "ck_worker_heartbeats_loop_name",
        "worker_heartbeats",
        type_="check",
    )
    op.create_check_constraint(
        "ck_worker_heartbeats_loop_name",
        "worker_heartbeats",
        f"loop_name IN ({_PREVIOUS_LOOPS})",
    )
