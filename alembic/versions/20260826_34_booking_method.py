"""A2.2 booking-method analytics pendings + worker loop.

Revision ID: 20260826_34_booking_method
Revises: 20260825_33_teya_reliability
Create Date: 2026-08-26
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_34_booking_method"
down_revision: Union[str, Sequence[str], None] = "20260825_33_teya_reliability"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATE_SQL = (
    "'DISCOVERED', 'RESOLVING', 'APPLYING', 'DONE', 'MANUAL_REVIEW', 'SKIPPED'"
)
_CREATOR_SQL = "'SELF_SERVICE', 'MANAGER', 'MASTER'"


def upgrade() -> None:
    op.create_table(
        "booking_method_analytics_pendings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("appointment_id", sa.UUID(), nullable=False),
        sa.Column(
            "purpose",
            sa.String(length=48),
            nullable=False,
            server_default="BOOKING_CREATION_METHOD",
        ),
        sa.Column("creator_kind", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=48), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.UUID(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("amocrm_contact_id", sa.String(length=32), nullable=True),
        sa.Column("amocrm_deal_id", sa.String(length=32), nullable=True),
        sa.Column("result_code", sa.String(length=64), nullable=True),
        sa.Column("result_outcome", sa.String(length=64), nullable=True),
        sa.Column("manual_review_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"state IN ({_STATE_SQL})",
            name="ck_booking_method_analytics_pendings_state",
        ),
        sa.CheckConstraint(
            f"creator_kind IN ({_CREATOR_SQL})",
            name="ck_booking_method_analytics_pendings_creator_kind",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_booking_method_analytics_pendings_attempt_count",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name="ck_booking_method_analytics_pendings_max_attempts",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "appointment_id",
            "purpose",
            name="uq_booking_method_analytics_pendings_appt_purpose",
        ),
    )
    op.create_index(
        "ix_booking_method_analytics_pendings_claim",
        "booking_method_analytics_pendings",
        ["state", "next_retry_at", "lease_expires_at"],
        unique=False,
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
        "'teya_request_orchestrator', 'teya_request_reconciliation', "
        "'booking_method_analytics'"
        ")",
    )
    op.execute(
        sa.text(
            "INSERT INTO worker_heartbeats ("
            "loop_name, generation_id, worker_id, started_at, "
            "consecutive_failures, updated_at"
            ") SELECT "
            "'booking_method_analytics', "
            "gen_random_uuid(), 'bootstrap', now(), 0, now() "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM worker_heartbeats "
            "WHERE loop_name = 'booking_method_analytics'"
            ")"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM worker_heartbeats "
            "WHERE loop_name = 'booking_method_analytics'"
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
        "'teya_request_orchestrator', 'teya_request_reconciliation'"
        ")",
    )
    op.drop_index(
        "ix_booking_method_analytics_pendings_claim",
        table_name="booking_method_analytics_pendings",
    )
    op.drop_table("booking_method_analytics_pendings")
