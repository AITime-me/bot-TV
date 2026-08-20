"""Durable self-booking confirmed-create pendings (SELF-BOOKING-COMMAND-01).

Post-confirmation command row. No plaintext phone/name.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.pii_gateway import orm_local_column, repr_orm_fingerprint
from app.db.base import Base

_CHANNEL_SQL = "'synthetic'"
_STATE_SQL = (
    "'READY', 'EXECUTING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED'"
)
_UUID_RE = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class SelfBookingCreatePending(Base):
    __tablename__ = "self_booking_create_pendings"
    __table_args__ = (
        CheckConstraint(
            f"channel IN ({_CHANNEL_SQL})",
            name="ck_self_booking_create_pendings_channel",
        ),
        CheckConstraint(
            f"state IN ({_STATE_SQL})",
            name="ck_self_booking_create_pendings_state",
        ),
        CheckConstraint(
            "command_version >= 1",
            name="ck_self_booking_create_pendings_command_version",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_self_booking_create_pendings_attempt_count",
        ),
        CheckConstraint(
            "max_attempts >= 1",
            name="ck_self_booking_create_pendings_max_attempts",
        ),
        CheckConstraint(
            "fence_context_version >= 0",
            name="ck_self_booking_create_pendings_fence_context_version",
        ),
        CheckConstraint(
            "fence_manager_epoch >= 0",
            name="ck_self_booking_create_pendings_fence_manager_epoch",
        ),
        CheckConstraint(
            "fence_event_seq_hwm >= 0",
            name="ck_self_booking_create_pendings_fence_event_seq_hwm",
        ),
        CheckConstraint(
            "char_length(confirm_external_message_id) BETWEEN 1 AND 128",
            name="ck_self_booking_create_pendings_confirm_msg_len",
        ),
        CheckConstraint(
            "confirm_external_message_id ~ '^[!-~]+$'",
            name="ck_self_booking_create_pendings_confirm_msg_ascii",
        ),
        CheckConstraint(
            "char_length(slot_id) BETWEEN 1 AND 128",
            name="ck_self_booking_create_pendings_slot_id_len",
        ),
        CheckConstraint(
            f"idempotency_key ~ '{_UUID_RE}'",
            name="ck_self_booking_create_pendings_idempotency_key",
        ),
        CheckConstraint(
            "personal_data_consent IS TRUE",
            name="ck_self_booking_create_pendings_consent",
        ),
        CheckConstraint(
            "offer_acknowledgement IS TRUE",
            name="ck_self_booking_create_pendings_offer",
        ),
        CheckConstraint(
            "char_length(phone_ref_token) BETWEEN 1 AND 64",
            name="ck_self_booking_create_pendings_phone_ref_len",
        ),
        CheckConstraint(
            "char_length(name_ref_token) BETWEEN 1 AND 64",
            name="ck_self_booking_create_pendings_name_ref_len",
        ),
        Index(
            "uq_self_booking_create_pendings_confirm",
            "channel",
            "confirm_external_message_id",
            unique=True,
        ),
        Index(
            "uq_self_booking_create_pendings_active_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("state IN ('READY', 'EXECUTING')"),
        ),
        Index(
            "ix_self_booking_create_pendings_conversation_state",
            "conversation_id",
            "state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    confirm_external_message_id: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    command_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer(), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(36), nullable=False)
    slot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    starts_at: Mapped[str] = mapped_column(String(32), nullable=False)
    fence_context_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    fence_manager_epoch: Mapped[int] = mapped_column(Integer(), nullable=False)
    fence_event_seq_hwm: Mapped[int] = mapped_column(Integer(), nullable=False)
    personal_data_consent: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    offer_acknowledgement: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    phone_ref_token: Mapped[str] = mapped_column(String(64), nullable=False)
    name_ref_token: Mapped[str] = mapped_column(String(64), nullable=False)
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
            "SelfBookingCreatePending("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='pending_id')}, "
            f"conversation_id={repr_orm_fingerprint(orm_local_column(self, 'conversation_id'), purpose='conversation_id')}, "
            f"channel={orm_local_column(self, 'channel')!r}, "
            "confirm_external_message_id=<redacted>, "
            f"state={orm_local_column(self, 'state')!r}, "
            f"command_version={orm_local_column(self, 'command_version')!r}, "
            f"attempt_count={orm_local_column(self, 'attempt_count')!r}, "
            "idempotency_key=<redacted>, "
            "slot_id=<redacted>, "
            "starts_at=<redacted>, "
            "phone_ref_token=<redacted>, "
            "name_ref_token=<redacted>)"
        )
