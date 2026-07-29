from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.conversation import (
    HANDOFF_QUARANTINE_CLEAR_PATHS,
    HANDOFF_QUARANTINE_REASONS,
)


class ConversationOpsEventType(str, enum.Enum):
    HANDOFF_EXPIRY_QUARANTINED = "HANDOFF_EXPIRY_QUARANTINED"
    HANDOFF_QUARANTINE_CLEARED = "HANDOFF_QUARANTINE_CLEARED"


_REASON_SQL = ", ".join(f"'{code}'" for code in sorted(HANDOFF_QUARANTINE_REASONS))
_CLEAR_PATH_SQL = ", ".join(
    f"'{code}'" for code in sorted(HANDOFF_QUARANTINE_CLEAR_PATHS)
)


class ConversationOpsEvent(Base):
    """Append-only operational history for a dialog.

    Application code may INSERT only. There is no repository API to UPDATE or
    DELETE rows. Events carry technical codes and fencing integers - never
    message text, contacts, or free-form payload.
    """

    __tablename__ = "conversation_ops_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ("
            "'HANDOFF_EXPIRY_QUARANTINED', "
            "'HANDOFF_QUARANTINE_CLEARED'"
            ")",
            name="ck_conversation_ops_events_event_type",
        ),
        CheckConstraint(
            f"reason_code IN ({_REASON_SQL})",
            name="ck_conversation_ops_events_reason_code",
        ),
        CheckConstraint(
            "("
            "event_type = 'HANDOFF_EXPIRY_QUARANTINED' "
            "AND clear_path IS NULL"
            ") OR ("
            "event_type = 'HANDOFF_QUARANTINE_CLEARED' "
            f"AND clear_path IN ({_CLEAR_PATH_SQL})"
            ")",
            name="ck_conversation_ops_events_clear_path",
        ),
        CheckConstraint(
            "manager_epoch >= 0",
            name="ck_conversation_ops_events_manager_epoch_nonnegative",
        ),
        CheckConstraint(
            "context_version >= 0",
            name="ck_conversation_ops_events_context_version_nonnegative",
        ),
        Index(
            "ix_conversation_ops_events_conversation_created",
            "conversation_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    clear_path: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manager_epoch: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    context_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )

    def __repr__(self) -> str:
        return (
            "ConversationOpsEvent("
            f"id={self.id!r}, "
            f"conversation_id={self.conversation_id!r}, "
            f"event_type={self.event_type!r}, "
            f"reason_code={self.reason_code!r}, "
            f"clear_path={self.clear_path!r})"
        )
