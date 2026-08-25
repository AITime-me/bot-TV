"""Durable Teya BookingRequest orchestrator pendings.

Stores workflow state + opaque online-zapis request_id only. No plaintext phone.
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
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.pii_gateway import orm_local_column, repr_orm_fingerprint
from app.db.base import Base

_STATE_SQL = (
    "'DISCOVERED', 'IDENTITY', 'CRM_READY', 'RECONCILED', 'CONTACT_ROUTE', "
    "'READY_TO_BOOK', 'WAITING_CONTACT', 'BOOKING', 'VERIFYING', 'DONE', "
    "'FAIL_CLOSED', 'RECONCILIATION_REQUIRED', 'MANUAL_REVIEW'"
)
_UUID_RE = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class TeyaRequestPending(Base):
    __tablename__ = "teya_request_pendings"
    __table_args__ = (
        CheckConstraint(
            f"state IN ({_STATE_SQL})",
            name="ck_teya_request_pendings_state",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_teya_request_pendings_attempt_count",
        ),
        CheckConstraint(
            "max_attempts >= 1",
            name="ck_teya_request_pendings_max_attempts",
        ),
        CheckConstraint(
            f"book_idempotency_key IS NULL OR book_idempotency_key ~ '{_UUID_RE}'",
            name="ck_teya_request_pendings_book_idempotency_key",
        ),
        Index(
            "uq_teya_request_pendings_request_id",
            "request_id",
            unique=True,
        ),
        Index(
            "ix_teya_request_pendings_claim",
            "state",
            "next_retry_at",
            "lease_expires_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, nullable=False
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
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
    result_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manual_review_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    contact_route_outcome: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    amocrm_contact_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    amocrm_deal_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    amocrm_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    structured_note: Mapped[str | None] = mapped_column(Text(), nullable=True)
    selected_starts_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    book_idempotency_key: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:
        return (
            "TeyaRequestPending("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='pending_id')}, "
            "request_id=<redacted>, "
            f"state={orm_local_column(self, 'state')!r}, "
            f"attempt_count={orm_local_column(self, 'attempt_count')!r}, "
            f"result_code={orm_local_column(self, 'result_code')!r}, "
            f"contact_route_outcome={orm_local_column(self, 'contact_route_outcome')!r}, "
            "amocrm_contact_id=<redacted>, "
            "amocrm_deal_id=<redacted>, "
            "amocrm_task_id=<redacted>, "
            "selected_starts_at=<redacted>, "
            "book_idempotency_key=<redacted>)"
        )
