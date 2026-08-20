"""Durable self-booking PII admission map (SELF-BOOKING-COMMAND-03H).

Opaque refs + content MAC only. No plaintext phone/name.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import BYTEA, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.pii_gateway import orm_local_column, repr_orm_fingerprint
from app.db.base import Base

_REQUEST_ID_RE = r"^[!-~]+$"
_KEY_ID_RE = r"^[A-Z0-9_]{1,64}$"


class SelfBookingPiiAdmission(Base):
    """Maps (conversation_id, request_id) → opaque PII refs + content MAC."""

    __tablename__ = "self_booking_pii_admissions"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "request_id",
            name="uq_self_booking_pii_admissions_request",
        ),
        CheckConstraint(
            "char_length(request_id) BETWEEN 1 AND 128",
            name="ck_self_booking_pii_admissions_request_id_len",
        ),
        CheckConstraint(
            f"request_id ~ '{_REQUEST_ID_RE}'",
            name="ck_self_booking_pii_admissions_request_id_ascii",
        ),
        CheckConstraint(
            "char_length(phone_ref_token) BETWEEN 1 AND 64",
            name="ck_self_booking_pii_admissions_phone_ref_len",
        ),
        CheckConstraint(
            "char_length(name_ref_token) BETWEEN 1 AND 64",
            name="ck_self_booking_pii_admissions_name_ref_len",
        ),
        CheckConstraint(
            "octet_length(content_mac) = 32",
            name="ck_self_booking_pii_admissions_content_mac_len",
        ),
        CheckConstraint(
            f"mac_key_id ~ '{_KEY_ID_RE}'",
            name="ck_self_booking_pii_admissions_mac_key_id",
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
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    phone_ref_token: Mapped[str] = mapped_column(String(64), nullable=False)
    name_ref_token: Mapped[str] = mapped_column(String(64), nullable=False)
    content_mac: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    mac_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:
        return (
            "SelfBookingPiiAdmission("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='admission_id')}, "
            f"conversation_id={repr_orm_fingerprint(orm_local_column(self, 'conversation_id'), purpose='conversation_id')}, "
            "request_id=<redacted>, "
            "phone_ref_token=<redacted>, "
            "name_ref_token=<redacted>, "
            "content_mac=<redacted>, "
            "mac_key_id=<redacted>)"
        )
