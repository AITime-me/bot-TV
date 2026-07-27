from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class DestinationType(str, enum.Enum):
    INTERNAL_DRAFT = "INTERNAL_DRAFT"


class DeliveryStatus(str, enum.Enum):
    PENDING = "PENDING"
    CANCELLED = "CANCELLED"
    # SENT is intentionally absent in BOT-CORE-FOUNDATION-01A.
    # No transport/channel sender exists in this stage.


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        UniqueConstraint(
            "source_inbox_id",
            "destination_type",
            name="uq_outbox_source_inbox_destination",
        ),
        CheckConstraint(
            "destination_type IN ('INTERNAL_DRAFT')",
            name="ck_outbox_destination_type",
        ),
        CheckConstraint(
            "delivery_status IN ('PENDING', 'CANCELLED')",
            name="ck_outbox_delivery_status",
        ),
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
