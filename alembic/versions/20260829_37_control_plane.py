"""Add control_plane_snapshots + control_plane_snapshot worker loop.

Revision ID: 20260829_37_control_plane
Revises: 20260828_36_amocrm_oauth_loop
Create Date: 2026-08-29
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260829_37_control_plane"
down_revision: Union[str, Sequence[str], None] = "20260828_36_amocrm_oauth_loop"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PREVIOUS_LOOPS = (
    "'ingress', 'handoff_expiry', 'reply_plan', 'outbound', "
    "'amocrm_mirror', 'self_booking_create', "
    "'teya_request_orchestrator', 'teya_request_reconciliation', "
    "'booking_method_analytics', 'acquisition_source_analytics', "
    "'amocrm_crm_oauth_lifecycle'"
)
_CONTROL_PLANE_LOOP_SQL = "'control_plane_snapshot'"


def upgrade() -> None:
    op.create_table(
        "control_plane_snapshots",
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("publication_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "usable",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "kind IN ('SETTINGS', 'KNOWLEDGE')",
            name="ck_control_plane_snapshots_kind",
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name="ck_control_plane_snapshots_schema_version",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_control_plane_snapshots_version",
        ),
        sa.CheckConstraint(
            "char_length(publication_id) BETWEEN 1 AND 64",
            name="ck_control_plane_snapshots_publication_id_len",
        ),
        sa.CheckConstraint(
            "char_length(checksum) = 64",
            name="ck_control_plane_snapshots_checksum_len",
        ),
        sa.CheckConstraint(
            "checksum ~ '^[0-9a-f]{64}$'",
            name="ck_control_plane_snapshots_checksum_hex",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_control_plane_snapshots_payload_object",
        ),
        sa.PrimaryKeyConstraint("kind"),
    )
    op.create_index(
        "ix_control_plane_snapshots_verified_at",
        "control_plane_snapshots",
        ["verified_at"],
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
        f"loop_name IN ({_PREVIOUS_LOOPS}, {_CONTROL_PLANE_LOOP_SQL})",
    )
    op.execute(
        sa.text(
            "INSERT INTO worker_heartbeats ("
            "loop_name, generation_id, worker_id, started_at, "
            "consecutive_failures, updated_at"
            ") SELECT "
            f"{_CONTROL_PLANE_LOOP_SQL}, "
            "gen_random_uuid(), 'bootstrap', now(), 0, now() "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM worker_heartbeats "
            f"WHERE loop_name = {_CONTROL_PLANE_LOOP_SQL}"
            ")"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM worker_heartbeats "
            f"WHERE loop_name = {_CONTROL_PLANE_LOOP_SQL}"
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
    op.drop_index(
        "ix_control_plane_snapshots_verified_at",
        table_name="control_plane_snapshots",
    )
    op.drop_table("control_plane_snapshots")
