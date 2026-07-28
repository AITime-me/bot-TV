from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class DestinationType(str, enum.Enum):
    INTERNAL_DRAFT = "INTERNAL_DRAFT"
    SYNTHETIC_OUTBOUND = "SYNTHETIC_OUTBOUND"


class DeliveryStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    # DELIVERED means accepted by the synthetic sink only — never a real channel send.
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    DEAD = "DEAD"
    CANCELLED = "CANCELLED"
    # SENT remains intentionally absent: no transport/channel sender exists.


OUTBOUND_TRANSITIONS: dict[DeliveryStatus, frozenset[DeliveryStatus]] = {
    DeliveryStatus.PENDING: frozenset(
        {
            DeliveryStatus.PROCESSING,
            DeliveryStatus.CANCELLED,
        }
    ),
    DeliveryStatus.PROCESSING: frozenset(
        {
            DeliveryStatus.DELIVERED,
            DeliveryStatus.FAILED,
            DeliveryStatus.DEAD,
            DeliveryStatus.CANCELLED,
        }
    ),
    DeliveryStatus.FAILED: frozenset(
        {
            DeliveryStatus.PROCESSING,
            DeliveryStatus.DEAD,
            DeliveryStatus.CANCELLED,
        }
    ),
    DeliveryStatus.DELIVERED: frozenset(),
    DeliveryStatus.DEAD: frozenset(),
    DeliveryStatus.CANCELLED: frozenset(),
}


def outbound_transition_allowed(
    current: DeliveryStatus | str,
    target: DeliveryStatus | str,
) -> bool:
    current_status = (
        current if isinstance(current, DeliveryStatus) else DeliveryStatus(current)
    )
    target_status = (
        target if isinstance(target, DeliveryStatus) else DeliveryStatus(target)
    )
    return target_status in OUTBOUND_TRANSITIONS[current_status]


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        UniqueConstraint(
            "source_inbox_id",
            "destination_type",
            name="uq_outbox_source_inbox_destination",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_outbox_idempotency_key",
        ),
        UniqueConstraint(
            "reply_plan_id",
            "destination_type",
            name="uq_outbox_reply_plan_destination",
        ),
        CheckConstraint(
            "destination_type IN ('INTERNAL_DRAFT', 'SYNTHETIC_OUTBOUND')",
            name="ck_outbox_destination_type",
        ),
        CheckConstraint(
            "delivery_status IN ('PENDING', 'PROCESSING', 'DELIVERED', "
            "'FAILED', 'DEAD', 'CANCELLED')",
            name="ck_outbox_delivery_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_outbox_attempt_count_nonnegative",
        ),
        CheckConstraint(
            "max_attempts > 0",
            name="ck_outbox_max_attempts_positive",
        ),
        CheckConstraint(
            "lease_version >= 0",
            name="ck_outbox_lease_version_nonnegative",
        ),
        Index("ix_outbox_messages_status_not_before", "delivery_status", "not_before"),
        Index("ix_outbox_messages_lease_until", "lease_until"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_inbox_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    reply_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reply_plans.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    context_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    destination_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DestinationType.INTERNAL_DRAFT.value,
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    delivery_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DeliveryStatus.PENDING.value,
    )
    not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5,
        server_default=text("5"),
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    lease_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    conversation = relationship("Conversation", back_populates="outbox_messages")
    source_inbox = relationship("InboxMessage", back_populates="outbox_messages")
    reply_plan = relationship("ReplyPlan", back_populates="outbound_messages")

    def __repr__(self) -> str:
        return (
            f"OutboxMessage(id={self.id!r}, conversation_id={self.conversation_id!r}, "
            f"destination_type={self.destination_type!r}, "
            f"delivery_status={self.delivery_status!r}, "
            f"context_version={self.context_version!r}, "
            f"idempotency_key={self.idempotency_key!r}, payload=<redacted>)"
        )
