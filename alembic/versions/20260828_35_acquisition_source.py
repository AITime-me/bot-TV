"""A2.3b2 acquisition-source analytics pendings + worker loop.

Revision ID: 20260828_35_acquisition_source
Revises: 20260826_34_booking_method
Create Date: 2026-08-28
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_35_acquisition_source"
down_revision: Union[str, Sequence[str], None] = "20260826_34_booking_method"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATE_SQL = (
    "'DISCOVERED', 'RESOLVING', 'APPLYING', 'DONE', 'MANUAL_REVIEW', 'SKIPPED'"
)
_OWNER_KIND_SQL = "'APPOINTMENT', 'BOOKING_REQUEST'"
_SOURCE_KEY_SQL = "'VK_ADS', 'VK_CONTENT', 'YANDEX', 'TWO_GIS'"


def upgrade() -> None:
    op.create_table(
        "acquisition_source_analytics_pendings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("evidence_id", sa.UUID(), nullable=False),
        sa.Column(
            "purpose",
            sa.String(length=48),
            nullable=False,
            server_default="SOURCE_PRIMARY",
        ),
        sa.Column("owner_kind", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("source_key", sa.String(length=32), nullable=False),
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
            name="ck_acquisition_source_analytics_pendings_state",
        ),
        sa.CheckConstraint(
            f"owner_kind IN ({_OWNER_KIND_SQL})",
            name="ck_acquisition_source_analytics_pendings_owner_kind",
        ),
        sa.CheckConstraint(
            f"source_key IN ({_SOURCE_KEY_SQL})",
            name="ck_acquisition_source_analytics_pendings_source_key",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_acquisition_source_analytics_pendings_attempt_count",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name="ck_acquisition_source_analytics_pendings_max_attempts",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "evidence_id",
            "purpose",
            name="uq_acquisition_source_analytics_pendings_evidence_purpose",
        ),
    )
    op.create_index(
        "ix_acquisition_source_analytics_pendings_claim",
        "acquisition_source_analytics_pendings",
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
        "'booking_method_analytics', 'acquisition_source_analytics'"
        ")",
    )
    op.execute(
        sa.text(
            "INSERT INTO worker_heartbeats ("
            "loop_name, generation_id, worker_id, started_at, "
            "consecutive_failures, updated_at"
            ") SELECT "
            "'acquisition_source_analytics', "
            "gen_random_uuid(), 'bootstrap', now(), 0, now() "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM worker_heartbeats "
            "WHERE loop_name = 'acquisition_source_analytics'"
            ")"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM worker_heartbeats "
            "WHERE loop_name = 'acquisition_source_analytics'"
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
        "'teya_request_orchestrator', 'teya_request_reconciliation', "
        "'booking_method_analytics'"
        ")",
    )
    op.drop_index(
        "ix_acquisition_source_analytics_pendings_claim",
        table_name="acquisition_source_analytics_pendings",
    )
    op.drop_table("acquisition_source_analytics_pendings")
