from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.pii_gateway import (
    orm_local_column,
    repr_orm_fingerprint,
    repr_orm_literal,
)
from app.db.base import Base


class MessageDirection(str, enum.Enum):
    INBOUND = "INBOUND"


class MessageType(str, enum.Enum):
    TEXT = "TEXT"


class ProcessingStatus(str, enum.Enum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class InboxMessage(Base):
    __tablename__ = "inbox_messages"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "external_message_id",
            name="uq_inbox_channel_external_message_id",
        ),
        CheckConstraint(
            "channel IN ('synthetic')",
            name="ck_inbox_channel",
        ),
        CheckConstraint(
            "direction IN ('INBOUND')",
            name="ck_inbox_direction",
        ),
        CheckConstraint(
            "message_type IN ('TEXT')",
            name="ck_inbox_message_type",
        ),
        CheckConstraint(
            "processing_status IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'FAILED')",
            name="ck_inbox_processing_status",
        ),
        CheckConstraint(
            "conversation_event_seq > 0",
            name="ck_inbox_conversation_event_seq_positive",
        ),
        UniqueConstraint(
            "conversation_id",
            "conversation_event_seq",
            name="uq_inbox_conversation_event_seq",
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
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    external_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    direction: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=MessageDirection.INBOUND.value,
    )
    message_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=MessageType.TEXT.value,
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    processing_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ProcessingStatus.RECEIVED.value,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conversation_event_seq: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    conversation = relationship("Conversation", back_populates="inbox_messages")
    outbox_messages = relationship("OutboxMessage", back_populates="source_inbox")

    def __repr__(self) -> str:
        return (
            "InboxMessage("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='inbox_message_id')}, "
            f"conversation_id={repr_orm_fingerprint(orm_local_column(self, 'conversation_id'), purpose='conversation_id')}, "
            f"channel={repr_orm_literal(orm_local_column(self, 'channel'))}, "
            f"external_message_id={repr_orm_fingerprint(orm_local_column(self, 'external_message_id'), purpose='external_message_id')}, "
            f"direction={repr_orm_literal(orm_local_column(self, 'direction'))}, "
            f"message_type={repr_orm_literal(orm_local_column(self, 'message_type'))}, "
            f"processing_status={repr_orm_literal(orm_local_column(self, 'processing_status'))}, "
            f"conversation_event_seq={repr_orm_literal(orm_local_column(self, 'conversation_event_seq'))}, "
            "payload=<redacted>)"
        )
