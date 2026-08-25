"""Teya request orchestrator pendings + worker heartbeat loop expand.

Revision ID: 20260825_32_teya_req_orch
Revises: 20260821_31_sbc_exec_loop
Create Date: 2026-08-25

Expand-only: teya_request_pendings table + heartbeat loop_name CHECK.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260825_32_teya_req_orch"
down_revision: Union[str, Sequence[str], None] = "20260821_31_sbc_exec_loop"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATE_SQL = (
    "'DISCOVERED', 'IDENTITY', 'CRM_READY', 'RECONCILED', 'CONTACT_ROUTE', "
    "'READY_TO_BOOK', 'WAITING_CONTACT', 'BOOKING', 'VERIFYING', 'DONE', "
    "'FAIL_CLOSED', 'RECONCILIATION_REQUIRED'"
)
_UUID_RE = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def upgrade() -> None:
    op.create_table(
        "teya_request_pendings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=48), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "lease_token", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "lease_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "next_retry_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("result_code", sa.String(length=64), nullable=True),
        sa.Column("result_outcome", sa.String(length=64), nullable=True),
        sa.Column(
            "contact_route_outcome", sa.String(length=64), nullable=True
        ),
        sa.Column("amocrm_contact_id", sa.String(length=32), nullable=True),
        sa.Column("amocrm_deal_id", sa.String(length=32), nullable=True),
        sa.Column("amocrm_task_id", sa.String(length=32), nullable=True),
        sa.Column("structured_note", sa.Text(), nullable=True),
        sa.Column("selected_starts_at", sa.String(length=64), nullable=True),
        sa.Column("book_idempotency_key", sa.String(length=36), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            f"state IN ({_STATE_SQL})",
            name="ck_teya_request_pendings_state",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_teya_request_pendings_attempt_count",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name="ck_teya_request_pendings_max_attempts",
        ),
        sa.CheckConstraint(
            f"book_idempotency_key IS NULL OR book_idempotency_key ~ '{_UUID_RE}'",
            name="ck_teya_request_pendings_book_idempotency_key",
        ),
    )
    op.create_index(
        "uq_teya_request_pendings_request_id",
        "teya_request_pendings",
        ["request_id"],
        unique=True,
    )
    op.create_index(
        "ix_teya_request_pendings_claim",
        "teya_request_pendings",
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
        "'ingress', 'handoff_expiry', 'reply_plan', "
        "'outbound', 'amocrm_mirror', 'self_booking_create', "
        "'teya_request_orchestrator'"
        ")",
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM worker_heartbeats "
        "WHERE loop_name = 'teya_request_orchestrator'"
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
        "'outbound', 'amocrm_mirror', 'self_booking_create'"
        ")",
    )
    op.drop_index(
        "ix_teya_request_pendings_claim",
        table_name="teya_request_pendings",
    )
    op.drop_index(
        "uq_teya_request_pendings_request_id",
        table_name="teya_request_pendings",
    )
    op.drop_table("teya_request_pendings")
