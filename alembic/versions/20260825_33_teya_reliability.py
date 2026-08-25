"""Teya reliability: MANUAL_REVIEW, feed cursor, circuit breaker, recon loop.

Revision ID: 20260825_33_teya_reliability
Revises: 20260825_32_teya_req_orch
Create Date: 2026-08-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_33_teya_reliability"
down_revision: Union[str, Sequence[str], None] = "20260825_32_teya_req_orch"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATE_SQL = (
    "'DISCOVERED', 'IDENTITY', 'CRM_READY', 'RECONCILED', 'CONTACT_ROUTE', "
    "'READY_TO_BOOK', 'WAITING_CONTACT', 'BOOKING', 'VERIFYING', 'DONE', "
    "'FAIL_CLOSED', 'RECONCILIATION_REQUIRED', 'MANUAL_REVIEW'"
)

_PREV_STATE_SQL = (
    "'DISCOVERED', 'IDENTITY', 'CRM_READY', 'RECONCILED', 'CONTACT_ROUTE', "
    "'READY_TO_BOOK', 'WAITING_CONTACT', 'BOOKING', 'VERIFYING', 'DONE', "
    "'FAIL_CLOSED', 'RECONCILIATION_REQUIRED'"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_teya_request_pendings_state",
        "teya_request_pendings",
        type_="check",
    )
    op.create_check_constraint(
        "ck_teya_request_pendings_state",
        "teya_request_pendings",
        f"state IN ({_STATE_SQL})",
    )
    op.add_column(
        "teya_request_pendings",
        sa.Column("manual_review_reason", sa.String(length=64), nullable=True),
    )

    op.create_table(
        "teya_request_feed_cursors",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("cursor_created_at", sa.String(length=64), nullable=True),
        sa.Column("cursor_id", sa.String(length=36), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "integration_circuit_breakers",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("half_open_successes", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('CLOSED', 'OPEN', 'HALF_OPEN')",
            name="ck_integration_circuit_breakers_state",
        ),
        sa.CheckConstraint(
            "failure_count >= 0",
            name="ck_integration_circuit_breakers_failure_count",
        ),
        sa.CheckConstraint(
            "half_open_successes >= 0",
            name="ck_integration_circuit_breakers_half_open_successes",
        ),
        sa.PrimaryKeyConstraint("key"),
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
        "'ingress', 'handoff_expiry', 'reply_plan', 'outbound', "
        "'amocrm_mirror', 'self_booking_create', "
        "'teya_request_orchestrator', 'teya_request_reconciliation'"
        ")",
    )
    op.execute(
        sa.text(
            "INSERT INTO worker_heartbeats ("
            "loop_name, generation_id, worker_id, started_at, "
            "consecutive_failures, updated_at"
            ") SELECT "
            "'teya_request_reconciliation', "
            "gen_random_uuid(), 'bootstrap', now(), 0, now() "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM worker_heartbeats "
            "WHERE loop_name = 'teya_request_reconciliation'"
            ")"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM worker_heartbeats "
            "WHERE loop_name = 'teya_request_reconciliation'"
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
        "loop_name IN ("
        "'ingress', 'handoff_expiry', 'reply_plan', 'outbound', "
        "'amocrm_mirror', 'self_booking_create', "
        "'teya_request_orchestrator'"
        ")",
    )

    op.drop_table("integration_circuit_breakers")
    op.drop_table("teya_request_feed_cursors")

    op.execute(
        sa.text(
            "UPDATE teya_request_pendings SET state = 'RECONCILIATION_REQUIRED' "
            "WHERE state = 'MANUAL_REVIEW'"
        )
    )
    op.drop_column("teya_request_pendings", "manual_review_reason")
    op.drop_constraint(
        "ck_teya_request_pendings_state",
        "teya_request_pendings",
        type_="check",
    )
    op.create_check_constraint(
        "ck_teya_request_pendings_state",
        "teya_request_pendings",
        f"state IN ({_PREV_STATE_SQL})",
    )
