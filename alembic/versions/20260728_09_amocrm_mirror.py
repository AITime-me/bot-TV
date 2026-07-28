"""Add amoCRM mirror transactional outbox (CURSOR-09).

Revision ID: 20260728_09_amocrm_mirror
Revises: 20260727_01c_reply_outbound
Create Date: 2026-07-28

One table only. No external-entity table, no amoCRM identifiers, no direction
enum: the reverse amoCRM → bot-TV flow is documented in ADR-004 but not
implemented here.

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260728_09_amocrm_mirror"
down_revision: Union[str, Sequence[str], None] = "20260727_01c_reply_outbound"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "amocrm_mirror_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.String(length=48), nullable=False),
        sa.Column("subject_kind", sa.String(length=32), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("context_version", sa.Integer(), nullable=True),
        sa.Column("mirror_key", sa.String(length=160), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "lease_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("skip_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "job_type IN ('CLIENT_MESSAGE_RECEIVED_META', "
            "'REPLY_PLAN_STATE_CHANGED', 'MANAGER_TAKEOVER', "
            "'OUTBOUND_DELIVERED_META')",
            name="ck_amocrm_mirror_job_type",
        ),
        sa.CheckConstraint(
            "subject_kind IN ('CONVERSATION', 'INBOX_MESSAGE', 'REPLY_PLAN', "
            "'OUTBOX_MESSAGE')",
            name="ck_amocrm_mirror_subject_kind",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'MIRRORED', 'SKIPPED', "
            "'FAILED', 'DEAD')",
            name="ck_amocrm_mirror_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_amocrm_mirror_attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name="ck_amocrm_mirror_max_attempts_positive",
        ),
        sa.CheckConstraint(
            "lease_version >= 0",
            name="ck_amocrm_mirror_lease_version_nonnegative",
        ),
        sa.CheckConstraint(
            "context_version IS NULL OR context_version >= 0",
            name="ck_amocrm_mirror_context_version_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mirror_key", name="uq_amocrm_mirror_key"),
    )
    op.create_index(
        "ix_amocrm_mirror_jobs_status_next_attempt_at",
        "amocrm_mirror_jobs",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_amocrm_mirror_jobs_lease_until",
        "amocrm_mirror_jobs",
        ["lease_until"],
    )
    op.create_index(
        "ix_amocrm_mirror_jobs_conversation_id",
        "amocrm_mirror_jobs",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_amocrm_mirror_jobs_conversation_id",
        table_name="amocrm_mirror_jobs",
    )
    op.drop_index(
        "ix_amocrm_mirror_jobs_lease_until",
        table_name="amocrm_mirror_jobs",
    )
    op.drop_index(
        "ix_amocrm_mirror_jobs_status_next_attempt_at",
        table_name="amocrm_mirror_jobs",
    )
    op.drop_table("amocrm_mirror_jobs")
