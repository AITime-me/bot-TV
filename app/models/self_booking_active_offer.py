"""Durable self-booking active-offer snapshot (SELF-BOOKING-COMMAND-03C).

One row per conversation. Proven by DELIVERED OFFER_SLOTS outbound.
No plaintext PII.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.pii_gateway import orm_local_column, repr_orm_fingerprint
from app.db.base import Base


class SelfBookingActiveOffer(Base):
    __tablename__ = "self_booking_active_offers"
    __table_args__ = (
        CheckConstraint(
            "source_context_version >= 0",
            name="ck_self_booking_active_offers_context_version",
        ),
        CheckConstraint(
            "source_manager_epoch >= 0",
            name="ck_self_booking_active_offers_manager_epoch",
        ),
        CheckConstraint(
            "source_event_seq_hwm >= 0",
            name="ck_self_booking_active_offers_event_seq_hwm",
        ),
        CheckConstraint(
            "jsonb_typeof(offered_slots) = 'array'",
            name="ck_self_booking_active_offers_slots_array",
        ),
        CheckConstraint(
            "jsonb_array_length(offered_slots) BETWEEN 1 AND 3",
            name="ck_self_booking_active_offers_slots_len",
        ),
        Index(
            "uq_self_booking_active_offers_outbound",
            "source_outbound_id",
            unique=True,
        ),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    source_outbound_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("outbox_messages.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_context_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    source_manager_epoch: Mapped[int] = mapped_column(Integer(), nullable=False)
    source_event_seq_hwm: Mapped[int] = mapped_column(Integer(), nullable=False)
    offered_slots: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False
    )
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:
        return (
            "SelfBookingActiveOffer("
            f"conversation_id={repr_orm_fingerprint(orm_local_column(self, 'conversation_id'), purpose='conversation_id')}, "
            f"source_outbound_id={repr_orm_fingerprint(orm_local_column(self, 'source_outbound_id'), purpose='outbound_id')}, "
            f"source_context_version={orm_local_column(self, 'source_context_version')!r}, "
            "offered_slots=<redacted>)"
        )
