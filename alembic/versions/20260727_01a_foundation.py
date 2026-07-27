"""Create conversations, inbox_messages, outbox_messages.

Revision ID: 20260727_01a_foundation
Revises:
Create Date: 2026-07-27

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260727_01a_foundation"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("external_conversation_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("manager_takeover_at", sa.DateTime(timezone=True), nullable=True),
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
            "channel IN ('synthetic')",
            name="ck_conversations_channel",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'HANDOFF', 'CLOSED')",
            name="ck_conversations_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel",
            "external_conversation_id",
            name="uq_conversations_channel_external_id",
        ),
    )

    op.create_table(
        "inbox_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("external_message_id", sa.String(length=128), nullable=False),
        sa.Column("direction", sa.String(length=32), nullable=False),
        sa.Column("message_type", sa.String(length=32), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processing_status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "channel IN ('synthetic')",
            name="ck_inbox_channel",
        ),
        sa.CheckConstraint(
            "direction IN ('INBOUND')",
            name="ck_inbox_direction",
        ),
        sa.CheckConstraint(
            "message_type IN ('TEXT')",
            name="ck_inbox_message_type",
        ),
        sa.CheckConstraint(
            "processing_status IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'FAILED')",
            name="ck_inbox_processing_status",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel",
            "external_message_id",
            name="uq_inbox_channel_external_message_id",
        ),
    )
    op.create_index(
        "ix_inbox_messages_conversation_id",
        "inbox_messages",
        ["conversation_id"],
    )

    op.create_table(
        "outbox_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_inbox_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("destination_type", sa.String(length=32), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("delivery_status", sa.String(length=32), nullable=False),
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
            "destination_type IN ('INTERNAL_DRAFT')",
            name="ck_outbox_destination_type",
        ),
        sa.CheckConstraint(
            "delivery_status IN ('PENDING', 'CANCELLED')",
            name="ck_outbox_delivery_status",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_inbox_id"],
            ["inbox_messages.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_inbox_id",
            "destination_type",
            name="uq_outbox_source_inbox_destination",
        ),
    )
    op.create_index(
        "ix_outbox_messages_conversation_id",
        "outbox_messages",
        ["conversation_id"],
    )
    op.create_index(
        "ix_outbox_messages_source_inbox_id",
        "outbox_messages",
        ["source_inbox_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_messages_source_inbox_id", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_conversation_id", table_name="outbox_messages")
    op.drop_table("outbox_messages")
    op.drop_index("ix_inbox_messages_conversation_id", table_name="inbox_messages")
    op.drop_table("inbox_messages")
    op.drop_table("conversations")
