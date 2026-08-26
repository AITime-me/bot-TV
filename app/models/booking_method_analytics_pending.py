"""Durable A2.2 booking-method analytics pendings.

Stores appointment_id + creator_kind + workflow state only. No plaintext phone.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.booking_method_types import PURPOSE
from app.core.pii_gateway import orm_local_column, repr_orm_fingerprint
from app.db.base import Base

_STATE_SQL = (
    "'DISCOVERED', 'RESOLVING', 'APPLYING', 'DONE', 'MANUAL_REVIEW', 'SKIPPED'"
)
_CREATOR_SQL = "'SELF_SERVICE', 'MANAGER', 'MASTER'"


class BookingMethodAnalyticsPending(Base):
    __tablename__ = "booking_method_analytics_pendings"
    __table_args__ = (
        CheckConstraint(
            f"state IN ({_STATE_SQL})",
            name="ck_booking_method_analytics_pendings_state",
        ),
        CheckConstraint(
            f"creator_kind IN ({_CREATOR_SQL})",
            name="ck_booking_method_analytics_pendings_creator_kind",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_booking_method_analytics_pendings_attempt_count",
        ),
        CheckConstraint(
            "max_attempts >= 1",
            name="ck_booking_method_analytics_pendings_max_attempts",
        ),
        UniqueConstraint(
            "appointment_id",
            "purpose",
            name="uq_booking_method_analytics_pendings_appt_purpose",
        ),
        Index(
            "ix_booking_method_analytics_pendings_claim",
            "state",
            "next_retry_at",
            "lease_expires_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, nullable=False
    )
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    purpose: Mapped[str] = mapped_column(
        String(48), nullable=False, default=PURPOSE
    )
    creator_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(48), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer(), nullable=False)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    amocrm_contact_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    amocrm_deal_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manual_review_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:
        return (
            "BookingMethodAnalyticsPending("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='pending_id')}, "
            "appointment_id=<redacted>, "
            f"purpose={orm_local_column(self, 'purpose')!r}, "
            f"creator_kind={orm_local_column(self, 'creator_kind')!r}, "
            f"state={orm_local_column(self, 'state')!r}, "
            f"attempt_count={orm_local_column(self, 'attempt_count')!r}, "
            f"result_code={orm_local_column(self, 'result_code')!r}, "
            "amocrm_contact_id=<redacted>, "
            "amocrm_deal_id=<redacted>)"
        )
