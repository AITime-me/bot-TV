from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    BigInteger,
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

from app.core.pii_gateway import (
    orm_local_column,
    repr_orm_fingerprint,
    repr_orm_literal,
)
from app.db.base import Base


class DestinationType(str, enum.Enum):
    INTERNAL_DRAFT = "INTERNAL_DRAFT"
    SYNTHETIC_OUTBOUND = "SYNTHETIC_OUTBOUND"
    VK_CLIENT_OUTBOUND = "VK_CLIENT_OUTBOUND"


class DeliveryStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    # ADMITTED is the durable point after which manager events cannot cancel.
    ADMITTED = "ADMITTED"
    # DELIVERED = destination transport/sink confirmed success.
    # SYNTHETIC_OUTBOUND: in-process synthetic sink. VK_CLIENT_OUTBOUND: VK send.
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    DEAD = "DEAD"
    CANCELLED = "CANCELLED"
    # SENT remains intentionally absent.


OUTBOUND_TRANSITIONS: dict[DeliveryStatus, frozenset[DeliveryStatus]] = {
    DeliveryStatus.PENDING: frozenset(
        {
            DeliveryStatus.PROCESSING,
            DeliveryStatus.CANCELLED,
        }
    ),
    DeliveryStatus.PROCESSING: frozenset(
        {
            DeliveryStatus.ADMITTED,
            DeliveryStatus.FAILED,
            DeliveryStatus.DEAD,
            DeliveryStatus.CANCELLED,
        }
    ),
    DeliveryStatus.ADMITTED: frozenset(
        {
            DeliveryStatus.DELIVERED,
            DeliveryStatus.DEAD,
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
            "destination_type IN ("
            "'INTERNAL_DRAFT', 'SYNTHETIC_OUTBOUND', 'VK_CLIENT_OUTBOUND'"
            ")",
            name="ck_outbox_destination_type",
        ),
        CheckConstraint(
            "delivery_status IN ('PENDING', 'PROCESSING', 'ADMITTED', 'DELIVERED', "
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
        CheckConstraint(
            "manager_epoch >= 0",
            name="ck_outbox_manager_epoch_nonnegative",
        ),
        CheckConstraint(
            "event_seq_hwm >= 0",
            name="ck_outbox_event_seq_hwm_nonnegative",
        ),
        CheckConstraint(
            "admitted_at IS NULL OR ("
            "destination_type IN ('SYNTHETIC_OUTBOUND', 'VK_CLIENT_OUTBOUND') "
            "AND delivery_status IN ('ADMITTED', 'DELIVERED', 'DEAD')"
            ")",
            name="ck_outbox_admitted_destination",
        ),
        CheckConstraint(
            "delivery_status <> 'ADMITTED' OR ("
            "destination_type IN ('SYNTHETIC_OUTBOUND', 'VK_CLIENT_OUTBOUND') "
            "AND admitted_at IS NOT NULL"
            ")",
            name="ck_outbox_admitted_state",
        ),
        CheckConstraint(
            "destination_type NOT IN ('SYNTHETIC_OUTBOUND', 'VK_CLIENT_OUTBOUND') "
            "OR delivery_status <> 'DELIVERED' "
            "OR admitted_at IS NOT NULL",
            name="ck_outbox_delivered_after_admission",
        ),
        CheckConstraint(
            "("
            "lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL"
            ") OR ("
            "lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_until IS NOT NULL"
            ")",
            name="ck_outbox_lease_complete",
        ),
        CheckConstraint(
            "delivery_status NOT IN ('PENDING', 'FAILED', 'DELIVERED', "
            "'DEAD', 'CANCELLED') OR ("
            "lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL"
            ")",
            name="ck_outbox_unleased_states",
        ),
        CheckConstraint(
            "delivery_status <> 'PROCESSING' OR ("
            "lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_until IS NOT NULL"
            ")",
            name="ck_outbox_processing_lease",
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
    manager_epoch: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    event_seq_hwm: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
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
    admitted_at: Mapped[datetime | None] = mapped_column(
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
            "OutboxMessage("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='outbox_message_id')}, "
            f"conversation_id={repr_orm_fingerprint(orm_local_column(self, 'conversation_id'), purpose='conversation_id')}, "
            f"destination_type={repr_orm_literal(orm_local_column(self, 'destination_type'))}, "
            f"delivery_status={repr_orm_literal(orm_local_column(self, 'delivery_status'))}, "
            f"context_version={repr_orm_literal(orm_local_column(self, 'context_version'))}, "
            f"idempotency_key={repr_orm_fingerprint(orm_local_column(self, 'idempotency_key'), purpose='idempotency_key')}, "
            "payload=<redacted>)"
        )
