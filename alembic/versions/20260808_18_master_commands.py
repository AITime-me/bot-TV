"""Master command pending state + ephemeral PII purpose expand (CURSOR-28).

Revision ID: 20260808_18_master_commands
Revises: 20260807_17_master_bindings
Create Date: 2026-08-08

Expand-only:
- master_command_pendings table (durable confirmation / idempotency)
- ephemeral_pii_values kind/purpose checks for CLIENT_NAME + MASTER_BOOKING_CLIENT_WRITE
No BOT_MODE / live channel wiring / online-zapis-tv FK.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260808_18_master_commands"
down_revision: Union[str, Sequence[str], None] = "20260807_17_master_bindings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHANNEL_SQL = "'synthetic', 'vk', 'max'"
_KIND_SQL = (
    "'CLOSE_INTERVAL', 'CLOSE_DAY', 'CREATE_BOOKING', 'SCHEDULE_READ'"
)
_STATE_SQL = (
    "'AWAITING_CLARIFICATION', 'AWAITING_CONFIRMATION', 'EXECUTING', "
    "'SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED'"
)
_ACTIVE_STATES_SQL = (
    "'AWAITING_CLARIFICATION', 'AWAITING_CONFIRMATION', 'EXECUTING'"
)
_MASTER_ID_RE = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_IDEMPOTENCY_RE = _MASTER_ID_RE
_PURPOSE_SQL = (
    "'BOOKING_PHONE_WRITE', "
    "'APPROVED_STAFF_ALERT_PHONE', "
    "'AMOCRM_CONTACT_SYNC', "
    "'MASTER_BOOKING_CLIENT_WRITE'"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_ephemeral_pii_values_pii_kind",
        "ephemeral_pii_values",
        type_="check",
    )
    op.create_check_constraint(
        "ck_ephemeral_pii_values_pii_kind",
        "ephemeral_pii_values",
        "pii_kind IN ('PHONE', 'CLIENT_NAME')",
    )
    op.drop_constraint(
        "ck_ephemeral_pii_values_allowed_purpose",
        "ephemeral_pii_values",
        type_="check",
    )
    op.create_check_constraint(
        "ck_ephemeral_pii_values_allowed_purpose",
        "ephemeral_pii_values",
        f"allowed_purpose IN ({_PURPOSE_SQL})",
    )

    op.create_table(
        "master_command_pendings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("connection_scope", sa.String(length=128), nullable=False),
        sa.Column("external_account_id", sa.String(length=128), nullable=False),
        sa.Column("master_id", sa.String(length=36), nullable=False),
        sa.Column("inbound_message_id", sa.String(length=128), nullable=False),
        sa.Column("command_kind", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("command_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=36), nullable=True),
        sa.Column(
            "safe_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("phone_ref_token", sa.String(length=64), nullable=True),
        sa.Column("name_ref_token", sa.String(length=64), nullable=True),
        sa.Column(
            "pii_conversation_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "confirmation_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "execution_lease_token",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "execution_lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("result_code", sa.String(length=64), nullable=True),
        sa.Column("result_outcome", sa.String(length=64), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_master_command_pendings"),
        sa.CheckConstraint(
            f"channel IN ({_CHANNEL_SQL})",
            name="ck_master_command_pendings_channel",
        ),
        sa.CheckConstraint(
            f"command_kind IN ({_KIND_SQL})",
            name="ck_master_command_pendings_command_kind",
        ),
        sa.CheckConstraint(
            f"state IN ({_STATE_SQL})",
            name="ck_master_command_pendings_state",
        ),
        sa.CheckConstraint(
            "command_version >= 1",
            name="ck_master_command_pendings_command_version",
        ),
        sa.CheckConstraint(
            "char_length(connection_scope) BETWEEN 1 AND 128",
            name="ck_master_command_pendings_connection_scope_len",
        ),
        sa.CheckConstraint(
            "char_length(external_account_id) BETWEEN 1 AND 128",
            name="ck_master_command_pendings_external_account_id_len",
        ),
        sa.CheckConstraint(
            "char_length(inbound_message_id) BETWEEN 1 AND 128",
            name="ck_master_command_pendings_inbound_message_id_len",
        ),
        sa.CheckConstraint(
            "connection_scope ~ '^[!-~]+$'",
            name="ck_master_command_pendings_connection_scope_printable_ascii",
        ),
        sa.CheckConstraint(
            "external_account_id ~ '^[!-~]+$'",
            name="ck_master_command_pendings_external_account_id_printable_ascii",
        ),
        sa.CheckConstraint(
            "inbound_message_id ~ '^[!-~]+$'",
            name="ck_master_command_pendings_inbound_message_id_printable_ascii",
        ),
        sa.CheckConstraint(
            f"master_id ~ '{_MASTER_ID_RE}'",
            name="ck_master_command_pendings_master_id",
        ),
        sa.CheckConstraint(
            f"idempotency_key IS NULL OR idempotency_key ~ '{_IDEMPOTENCY_RE}'",
            name="ck_master_command_pendings_idempotency_key",
        ),
        sa.CheckConstraint(
            "(state IN ('AWAITING_CLARIFICATION', 'AWAITING_CONFIRMATION') "
            "AND confirmation_expires_at IS NOT NULL) OR "
            "(state NOT IN ('AWAITING_CLARIFICATION', 'AWAITING_CONFIRMATION'))",
            name="ck_master_command_pendings_confirmation_expiry",
        ),
        sa.CheckConstraint(
            "(command_kind = 'SCHEDULE_READ' AND idempotency_key IS NULL) OR "
            "(command_kind <> 'SCHEDULE_READ')",
            name="ck_master_command_pendings_schedule_no_idempotency",
        ),
        sa.CheckConstraint(
            "(command_kind IN ('CLOSE_INTERVAL', 'CLOSE_DAY', 'CREATE_BOOKING') "
            "AND (state = 'AWAITING_CLARIFICATION' OR idempotency_key IS NOT NULL)) "
            "OR (command_kind = 'SCHEDULE_READ')",
            name="ck_master_command_pendings_mutation_idempotency",
        ),
    )
    op.create_index(
        "uq_master_command_pendings_inbound",
        "master_command_pendings",
        ["channel", "connection_scope", "external_account_id", "inbound_message_id"],
        unique=True,
    )
    op.create_index(
        "uq_master_command_pendings_active_identity",
        "master_command_pendings",
        ["channel", "connection_scope", "external_account_id"],
        unique=True,
        postgresql_where=sa.text(f"state IN ({_ACTIVE_STATES_SQL})"),
    )
    op.create_index(
        "ix_master_command_pendings_identity_state",
        "master_command_pendings",
        ["channel", "connection_scope", "external_account_id", "state"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_master_command_pendings_identity_state",
        table_name="master_command_pendings",
    )
    op.drop_index(
        "uq_master_command_pendings_active_identity",
        table_name="master_command_pendings",
    )
    op.drop_index(
        "uq_master_command_pendings_inbound",
        table_name="master_command_pendings",
    )
    op.drop_table("master_command_pendings")

    op.drop_constraint(
        "ck_ephemeral_pii_values_allowed_purpose",
        "ephemeral_pii_values",
        type_="check",
    )
    op.create_check_constraint(
        "ck_ephemeral_pii_values_allowed_purpose",
        "ephemeral_pii_values",
        "allowed_purpose IN ("
        "'BOOKING_PHONE_WRITE', "
        "'APPROVED_STAFF_ALERT_PHONE', "
        "'AMOCRM_CONTACT_SYNC'"
        ")",
    )
    op.drop_constraint(
        "ck_ephemeral_pii_values_pii_kind",
        "ephemeral_pii_values",
        type_="check",
    )
    op.create_check_constraint(
        "ck_ephemeral_pii_values_pii_kind",
        "ephemeral_pii_values",
        "pii_kind = 'PHONE'",
    )
