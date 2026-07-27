from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Channel(str, enum.Enum):
    SYNTHETIC = "synthetic"


class ConversationStatus(str, enum.Enum):
    OPEN = "OPEN"
    HANDOFF = "HANDOFF"
    CLOSED = "CLOSED"


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "external_conversation_id",
            name="uq_conversations_channel_external_id",
        ),
        CheckConstraint(
            "channel IN ('synthetic')",
            name="ck_conversations_channel",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'HANDOFF', 'CLOSED')",
            name="ck_conversations_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    external_conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ConversationStatus.OPEN.value,
    )
    manager_takeover_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
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

    inbox_messages = relationship("InboxMessage", back_populates="conversation")
    outbox_messages = relationship("OutboxMessage", back_populates="conversation")


def conversation_allows_automatic_reply(conversation: Conversation) -> bool:
    """Whether a future pipeline may consider auto-reply for this dialog.

    Independent of outbound policy: takeover/handoff/closed always block.
    Fail-closed outbound remains the final send barrier.
    """
    if conversation.manager_takeover_at is not None:
        return False
    if conversation.status == ConversationStatus.HANDOFF.value:
        return False
    if conversation.status == ConversationStatus.CLOSED.value:
        return False
    return True
