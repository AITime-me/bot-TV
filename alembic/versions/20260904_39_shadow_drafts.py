"""QA Yandex shadow draft storage (one row per inbox message).

Revision ID: 20260904_39_shadow_drafts
Revises: 20260903_38_vk_client_ingress
Create Date: 2026-09-04

Expand-only: create yandex_shadow_drafts.
Never feeds ReplyPlan / outbox / CRM / booking / client delivery.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260904_39_shadow_drafts"
down_revision: Union[str, Sequence[str], None] = "20260903_38_vk_client_ingress"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DISPOSITION_SQL = (
    "disposition IN ('REPLY', 'HANDOFF', 'DENIED', 'PROVIDER_ERROR')"
)
_REASON_CODE_SQL = (
    "reason_code IN ("
    "'OK', 'GATE_DENIED', 'SETTINGS_NOT_USABLE', 'KNOWLEDGE_NOT_USABLE', "
    "'LIVE_FACTS_NOT_USABLE', 'GENERATION_NOT_ALLOWED', "
    "'PROVIDER_NOT_CONFIGURED', 'SHADOW_FEATURE_DISABLED', "
    "'HANDOFF_ACTIVE', 'MANAGER_TAKEOVER', 'EMERGENCY_LOCK', "
    "'CONTEXT_NOT_READY', 'PROMPT_BUDGET_EXCEEDED', "
    "'PROVIDER_TIMEOUT', 'PROVIDER_TRANSPORT_ERROR', "
    "'PROVIDER_REMOTE_REJECTED', 'PROVIDER_RESPONSE_INVALID', "
    "'PROVIDER_RESPONSE_TOO_LARGE', 'PROVIDER_EMPTY', "
    "'PROVIDER_CONFIG_INVALID', 'PROVIDER_ERROR'"
    ")"
)


def upgrade() -> None:
    op.create_table(
        "yandex_shadow_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "inbox_message_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "conversation_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("handoff_required", sa.Boolean(), nullable=False),
        sa.Column("generated_text", sa.Text(), nullable=True),
        sa.Column(
            "provenance_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "generation_metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            _DISPOSITION_SQL,
            name="ck_yandex_shadow_drafts_disposition",
        ),
        sa.CheckConstraint(
            _REASON_CODE_SQL,
            name="ck_yandex_shadow_drafts_reason_code",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(provenance_json) = 'object'",
            name="ck_yandex_shadow_drafts_provenance_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(generation_metadata_json) = 'object'",
            name="ck_yandex_shadow_drafts_metadata_object",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["inbox_message_id"],
            ["inbox_messages.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "inbox_message_id",
            name="uq_yandex_shadow_drafts_inbox_message_id",
        ),
    )
    op.create_index(
        "ix_yandex_shadow_drafts_conversation_created",
        "yandex_shadow_drafts",
        ["conversation_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_yandex_shadow_drafts_conversation_created",
        table_name="yandex_shadow_drafts",
    )
    op.drop_table("yandex_shadow_drafts")
