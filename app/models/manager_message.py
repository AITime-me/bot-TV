from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

MANAGER_MESSAGE_TEXT_MAX_LENGTH = 4000


class ManagerMessageStatus(str, enum.Enum):
    APPLIED = "APPLIED"
    STALE = "STALE"
    QUARANTINED = "QUARANTINED"


class ManagerMessage(Base):
    """Canonical durable manager-authored dialog message.

    Provider sequence is the ordering contract. Provider timestamps are kept
    for audit only and never merge client and manager timelines. Only APPLIED
    rows receive a conversation_event_seq and enter dialog context.
    """

    __tablename__ = "manager_messages"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "external_message_id",
            name="uq_manager_messages_channel_external_message_id",
        ),
        UniqueConstraint(
            "conversation_id",
            "conversation_event_seq",
            name="uq_manager_messages_conversation_event_seq",
        ),
        CheckConstraint(
            "channel IN ('synthetic')",
            name="ck_manager_messages_channel",
        ),
        CheckConstraint(
            "status IN ('APPLIED', 'STALE', 'QUARANTINED')",
            name="ck_manager_messages_status",
        ),
        CheckConstraint(
            "provider_sequence IS NULL OR provider_sequence >= 0",
            name="ck_manager_messages_provider_sequence_nonnegative",
        ),
        CheckConstraint(
            "conversation_event_seq IS NULL OR conversation_event_seq > 0",
            name="ck_manager_messages_event_seq_positive",
        ),
        CheckConstraint(
            "char_length(body_text) BETWEEN 1 AND 4000",
            name="ck_manager_messages_body_length",
        ),
        CheckConstraint(
            "("
            "status = 'APPLIED' "
            "AND provider_sequence IS NOT NULL "
            "AND conversation_event_seq IS NOT NULL"
            ") OR ("
            "status = 'STALE' "
            "AND provider_sequence IS NOT NULL "
            "AND conversation_event_seq IS NULL"
            ") OR ("
            "status = 'QUARANTINED' "
            "AND conversation_event_seq IS NULL"
            ")",
            name="ck_manager_messages_classification",
        ),
        Index(
            "ix_manager_messages_conversation_provider_sequence",
            "conversation_id",
            "provider_sequence",
        ),
        Index(
            "ix_manager_messages_conversation_event_seq",
            "conversation_id",
            "conversation_event_seq",
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
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    external_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    provider_occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    conversation_event_seq: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    classification_reason: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    conversation = relationship("Conversation", back_populates="manager_messages")

    def __repr__(self) -> str:
        return (
            f"ManagerMessage(id={self.id!r}, "
            f"conversation_id={self.conversation_id!r}, "
            f"external_message_id={self.external_message_id!r}, "
            f"provider_sequence={self.provider_sequence!r}, "
            f"status={self.status!r}, "
            f"conversation_event_seq={self.conversation_event_seq!r}, "
            "body_text=<redacted>)"
        )
