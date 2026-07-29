"""Add generation-fenced heartbeats for the continuous worker runtime.

Revision ID: 20260729_12_worker_runtime
Revises: 20260729_11_handoff_schema
Create Date: 2026-07-29

The table stores only technical process health. It contains no message text,
client identifiers, channel tokens, provider payloads, or database URLs.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260729_12_worker_runtime"
down_revision: Union[str, Sequence[str], None] = "20260729_11_handoff_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("loop_name", sa.String(length=48), nullable=False),
        sa.Column(
            "generation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_tick_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_succeeded_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_failed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "loop_name IN ('ingress', 'handoff_expiry', 'reply_plan', "
            "'outbound', 'amocrm_mirror')",
            name="ck_worker_heartbeats_loop_name",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_worker_heartbeats_consecutive_failures_nonnegative",
        ),
        sa.CheckConstraint(
            "("
            "consecutive_failures = 0 AND last_error_code IS NULL"
            ") OR ("
            "consecutive_failures > 0 "
            "AND last_failed_at IS NOT NULL "
            "AND last_error_code IS NOT NULL"
            ")",
            name="ck_worker_heartbeats_failure_consistency",
        ),
        sa.PrimaryKeyConstraint("loop_name"),
    )
    op.create_index(
        "ix_worker_heartbeats_last_succeeded_at",
        "worker_heartbeats",
        ["last_succeeded_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_worker_heartbeats_last_succeeded_at",
        table_name="worker_heartbeats",
    )
    op.drop_table("worker_heartbeats")
