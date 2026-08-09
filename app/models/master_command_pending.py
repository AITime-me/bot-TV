"""Durable master command pending rows (CURSOR-28).

Confirmation + clarification + execution lease. No plaintext phone/name.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.pii_gateway import orm_local_column, repr_orm_fingerprint
from app.db.base import Base

_CHANNEL_SQL = "'synthetic', 'vk', 'max'"
_KIND_SQL = "'CLOSE_INTERVAL', 'CLOSE_DAY', 'CREATE_BOOKING', 'SCHEDULE_READ'"
_STATE_SQL = (
    "'AWAITING_CLARIFICATION', 'AWAITING_CONFIRMATION', 'EXECUTING', "
    "'SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED'"
)
_MASTER_ID_RE = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class MasterCommandPending(Base):
    __tablename__ = "master_command_pendings"
    __table_args__ = (
        CheckConstraint(
            f"channel IN ({_CHANNEL_SQL})",
            name="ck_master_command_pendings_channel",
        ),
        CheckConstraint(
            f"command_kind IN ({_KIND_SQL})",
            name="ck_master_command_pendings_command_kind",
        ),
        CheckConstraint(
            f"state IN ({_STATE_SQL})",
            name="ck_master_command_pendings_state",
        ),
        CheckConstraint(
            "command_version >= 1",
            name="ck_master_command_pendings_command_version",
        ),
        CheckConstraint(
            "char_length(connection_scope) BETWEEN 1 AND 128",
            name="ck_master_command_pendings_connection_scope_len",
        ),
        CheckConstraint(
            "char_length(external_account_id) BETWEEN 1 AND 128",
            name="ck_master_command_pendings_external_account_id_len",
        ),
        CheckConstraint(
            "char_length(inbound_message_id) BETWEEN 1 AND 128",
            name="ck_master_command_pendings_inbound_message_id_len",
        ),
        CheckConstraint(
            "connection_scope ~ '^[!-~]+$'",
            name="ck_master_command_pendings_connection_scope_printable_ascii",
        ),
        CheckConstraint(
            "external_account_id ~ '^[!-~]+$'",
            name="ck_master_command_pendings_external_account_id_printable_ascii",
        ),
        CheckConstraint(
            "inbound_message_id ~ '^[!-~]+$'",
            name="ck_master_command_pendings_inbound_message_id_printable_ascii",
        ),
        CheckConstraint(
            f"master_id ~ '{_MASTER_ID_RE}'",
            name="ck_master_command_pendings_master_id",
        ),
        CheckConstraint(
            f"idempotency_key IS NULL OR idempotency_key ~ '{_MASTER_ID_RE}'",
            name="ck_master_command_pendings_idempotency_key",
        ),
        Index(
            "uq_master_command_pendings_inbound",
            "channel",
            "connection_scope",
            "external_account_id",
            "inbound_message_id",
            unique=True,
        ),
        Index(
            "uq_master_command_pendings_active_identity",
            "channel",
            "connection_scope",
            "external_account_id",
            unique=True,
            postgresql_where=text(
                "state IN ("
                "'AWAITING_CLARIFICATION', 'AWAITING_CONFIRMATION', 'EXECUTING')"
            ),
        ),
        Index(
            "ix_master_command_pendings_identity_state",
            "channel",
            "connection_scope",
            "external_account_id",
            "state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, nullable=False
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    connection_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    master_id: Mapped[str] = mapped_column(String(36), nullable=False)
    inbound_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    command_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(36), nullable=True)
    safe_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    phone_ref_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name_ref_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pii_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    confirmation_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    execution_lease_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    execution_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:
        return (
            "MasterCommandPending("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='pending_id')}, "
            f"channel={orm_local_column(self, 'channel')!r}, "
            "connection_scope=<redacted>, "
            "external_account_id=<redacted>, "
            "master_id=<redacted>, "
            "inbound_message_id=<redacted>, "
            f"command_kind={orm_local_column(self, 'command_kind')!r}, "
            f"state={orm_local_column(self, 'state')!r}, "
            f"command_version={orm_local_column(self, 'command_version')!r}, "
            "idempotency_key=<redacted>, "
            "phone_ref_token=<redacted>, "
            "name_ref_token=<redacted>)"
        )
