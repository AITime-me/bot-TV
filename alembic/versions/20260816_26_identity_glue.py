"""IR-1: conversation↔canonical glue + identity_review_cases.

Revision ID: 20260816_26_identity_glue
Revises: 20260813_25_amo_deal_reserve
Create Date: 2026-08-16

Expand-only. No amoCRM HTTP, webhook auto-resolve, chat binding, or mode changes.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260816_26_identity_glue"
down_revision: Union[str, Sequence[str], None] = "20260813_25_amo_deal_reserve"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REVIEW_STATUS_SQL = "'OPEN', 'RESOLVED'"
_REVIEW_REASON_SQL = (
    "'AMBIGUOUS_RESOLVE', 'CONFLICTING_CANONICAL', 'CANONICAL_NOT_ACTIVE'"
)


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("canonical_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_conversations_canonical_identity_id",
        "conversations",
        "canonical_identities",
        ["canonical_identity_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_conversations_canonical_identity_id",
        "conversations",
        ["canonical_identity_id"],
        unique=False,
    )

    op.create_table(
        "identity_review_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "proposed_canonical_identity_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "resolved_canonical_identity_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("statement_timestamp()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_identity_review_cases"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_identity_review_cases_conversation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["proposed_canonical_identity_id"],
            ["canonical_identities.id"],
            name="fk_identity_review_cases_proposed_canonical",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_canonical_identity_id"],
            ["canonical_identities.id"],
            name="fk_identity_review_cases_resolved_canonical",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            f"status IN ({_REVIEW_STATUS_SQL})",
            name="ck_identity_review_cases_status",
        ),
        sa.CheckConstraint(
            f"reason_code IN ({_REVIEW_REASON_SQL})",
            name="ck_identity_review_cases_reason_code",
        ),
        sa.CheckConstraint(
            "("
            "status = 'OPEN' AND resolved_canonical_identity_id IS NULL "
            "AND resolved_at IS NULL"
            ") OR ("
            "status = 'RESOLVED' AND resolved_canonical_identity_id IS NOT NULL "
            "AND resolved_at IS NOT NULL"
            ")",
            name="ck_identity_review_cases_resolved_state",
        ),
    )
    op.create_index(
        "ix_identity_review_cases_conversation_id",
        "identity_review_cases",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        "ix_identity_review_cases_status",
        "identity_review_cases",
        ["status"],
        unique=False,
    )
    op.create_index(
        "uq_identity_review_cases_open_conversation_reason",
        "identity_review_cases",
        ["conversation_id", "reason_code"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_identity_review_cases_open_conversation_reason",
        table_name="identity_review_cases",
    )
    op.drop_index(
        "ix_identity_review_cases_status",
        table_name="identity_review_cases",
    )
    op.drop_index(
        "ix_identity_review_cases_conversation_id",
        table_name="identity_review_cases",
    )
    op.drop_table("identity_review_cases")
    op.drop_index(
        "ix_conversations_canonical_identity_id",
        table_name="conversations",
    )
    op.drop_constraint(
        "fk_conversations_canonical_identity_id",
        "conversations",
        type_="foreignkey",
    )
    op.drop_column("conversations", "canonical_identity_id")
