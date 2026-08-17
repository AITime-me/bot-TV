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
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Channel(str, enum.Enum):
    SYNTHETIC = "synthetic"


class ConversationStatus(str, enum.Enum):
    OPEN = "OPEN"
    HANDOFF = "HANDOFF"
    CLOSED = "CLOSED"


class ConversationOwnership(str, enum.Enum):
    BOT = "BOT"
    MANAGER = "MANAGER"


class HandoffState(str, enum.Enum):
    BOT_ACTIVE = "BOT_ACTIVE"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    HUMAN_PAUSE = "HUMAN_PAUSE"


# Stable technical codes only — never free text or PII.
HANDOFF_QUARANTINE_REASONS: frozenset[str] = frozenset(
    {
        "HANDOFF_DEFERRED_PLAN_MISSING",
        "HANDOFF_DEFERRED_PLAN_TYPE",
        "HANDOFF_DEFERRED_PLAN_NOT_OPEN",
        "HANDOFF_DEFERRED_PLAN_CONTEXT",
        "HANDOFF_DEFERRED_PLAN_MANAGER_EPOCH",
        "HANDOFF_DEFERRED_PLAN_EVENT_SEQ",
        "HANDOFF_DEFERRED_PLAN_DEADLINE",
        "HANDOFF_DEFERRED_PLAN_MARKER",
        "HANDOFF_EXPIRY_UNSUPPORTED_STATE",
    }
)
HANDOFF_QUARANTINE_CLEAR_PATHS: frozenset[str] = frozenset(
    {
        "MANAGER_MESSAGE_APPLIED",
    }
)
HANDOFF_QUARANTINE_CLEAR_PATH_MANAGER_MESSAGE = "MANAGER_MESSAGE_APPLIED"

_HANDOFF_QUARANTINE_REASON_SQL = ", ".join(
    f"'{code}'" for code in sorted(HANDOFF_QUARANTINE_REASONS)
)
_HANDOFF_QUARANTINE_CLEAR_PATH_SQL = ", ".join(
    f"'{code}'" for code in sorted(HANDOFF_QUARANTINE_CLEAR_PATHS)
)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "external_conversation_id",
            name="uq_conversations_channel_external_id",
        ),
        Index(
            "ix_conversations_handoff_due",
            "handoff_deadline_at",
            postgresql_where=text(
                "status = 'HANDOFF' AND ownership = 'MANAGER' "
                "AND handoff_state IN ('HUMAN_ACTIVE', 'HUMAN_PAUSE') "
                "AND ("
                "handoff_quarantined_at IS NULL "
                "OR handoff_quarantine_cleared_at IS NOT NULL"
                ")"
            ),
        ),
        CheckConstraint(
            "channel IN ('synthetic')",
            name="ck_conversations_channel",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'HANDOFF', 'CLOSED')",
            name="ck_conversations_status",
        ),
        CheckConstraint(
            "ownership IN ('BOT', 'MANAGER')",
            name="ck_conversations_ownership",
        ),
        CheckConstraint(
            "context_version >= 0",
            name="ck_conversations_context_version_nonnegative",
        ),
        CheckConstraint(
            "handoff_state IN ('BOT_ACTIVE', 'HUMAN_ACTIVE', 'HUMAN_PAUSE')",
            name="ck_conversations_handoff_state",
        ),
        CheckConstraint(
            "manager_epoch >= 0",
            name="ck_conversations_manager_epoch_nonnegative",
        ),
        CheckConstraint(
            "current_event_seq >= 0",
            name="ck_conversations_current_event_seq_nonnegative",
        ),
        CheckConstraint(
            "manager_sequence_hwm IS NULL OR manager_sequence_hwm >= 0",
            name="ck_conversations_manager_sequence_hwm_nonnegative",
        ),
        Index(
            "ix_conversations_canonical_identity_id",
            "canonical_identity_id",
        ),
        CheckConstraint(
            "("
            "status = 'CLOSED' AND ownership = 'BOT' "
            "AND handoff_state = 'BOT_ACTIVE' "
            "AND handoff_deadline_at IS NULL "
            "AND human_pause_anchor_at IS NULL "
            "AND manager_takeover_at IS NULL"
            ") OR ("
            "status = 'OPEN' AND ownership = 'BOT' "
            "AND handoff_state = 'BOT_ACTIVE' "
            "AND handoff_deadline_at IS NULL "
            "AND human_pause_anchor_at IS NULL "
            "AND manager_takeover_at IS NULL"
            ") OR ("
            "status = 'HANDOFF' AND ownership = 'MANAGER' "
            "AND handoff_state = 'HUMAN_ACTIVE' "
            "AND handoff_deadline_at IS NOT NULL "
            "AND human_pause_anchor_at IS NULL "
            "AND manager_takeover_at IS NOT NULL"
            ") OR ("
            "status = 'HANDOFF' AND ownership = 'MANAGER' "
            "AND handoff_state = 'HUMAN_PAUSE' "
            "AND handoff_deadline_at IS NOT NULL "
            "AND human_pause_anchor_at IS NOT NULL "
            "AND manager_takeover_at IS NOT NULL"
            ")",
            name="ck_conversations_handoff_consistency",
        ),
        CheckConstraint(
            "("
            "handoff_quarantined_at IS NULL "
            "AND handoff_quarantine_reason IS NULL "
            "AND handoff_quarantine_cleared_at IS NULL "
            "AND handoff_quarantine_clear_path IS NULL"
            ") OR ("
            "handoff_quarantined_at IS NOT NULL "
            "AND handoff_quarantine_reason IS NOT NULL "
            "AND handoff_quarantine_cleared_at IS NULL "
            "AND handoff_quarantine_clear_path IS NULL"
            ") OR ("
            "handoff_quarantined_at IS NOT NULL "
            "AND handoff_quarantine_reason IS NOT NULL "
            "AND handoff_quarantine_cleared_at IS NOT NULL "
            "AND handoff_quarantine_clear_path IS NOT NULL"
            ")",
            name="ck_conversations_handoff_quarantine_consistency",
        ),
        CheckConstraint(
            "handoff_quarantine_reason IS NULL OR "
            f"handoff_quarantine_reason IN ({_HANDOFF_QUARANTINE_REASON_SQL})",
            name="ck_conversations_handoff_quarantine_reason",
        ),
        CheckConstraint(
            "handoff_quarantine_clear_path IS NULL OR "
            "handoff_quarantine_clear_path IN "
            f"({_HANDOFF_QUARANTINE_CLEAR_PATH_SQL})",
            name="ck_conversations_handoff_quarantine_clear_path",
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
    ownership: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ConversationOwnership.BOT.value,
        server_default=text("'BOT'"),
    )
    context_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    handoff_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=HandoffState.BOT_ACTIVE.value,
        server_default=text("'BOT_ACTIVE'"),
    )
    manager_epoch: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    current_event_seq: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    manager_sequence_hwm: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    handoff_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    human_pause_anchor_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_client_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    manager_takeover_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    active_reply_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "reply_plans.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_conversations_active_reply_plan_id",
        ),
        nullable=True,
    )
    canonical_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "canonical_identities.id",
            ondelete="SET NULL",
            name="fk_conversations_canonical_identity_id",
        ),
        nullable=True,
    )
    handoff_quarantined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    handoff_quarantine_reason: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    handoff_quarantine_cleared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    handoff_quarantine_clear_path: Mapped[str | None] = mapped_column(
        String(64),
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
    manager_messages = relationship("ManagerMessage", back_populates="conversation")
    outbox_messages = relationship("OutboxMessage", back_populates="conversation")
    reply_plans = relationship(
        "ReplyPlan",
        back_populates="conversation",
        foreign_keys="ReplyPlan.conversation_id",
    )


def conversation_allows_automatic_reply(conversation: Conversation) -> bool:
    """Whether a future pipeline may consider auto-reply for this dialog.

    Independent of outbound policy: takeover/handoff/closed/manager ownership
    always block. Fail-closed outbound remains the final send barrier.
    """
    if conversation.manager_takeover_at is not None:
        return False
    if conversation.handoff_state != HandoffState.BOT_ACTIVE.value:
        return False
    if conversation.ownership == ConversationOwnership.MANAGER.value:
        return False
    if conversation.status == ConversationStatus.HANDOFF.value:
        return False
    if conversation.status == ConversationStatus.CLOSED.value:
        return False
    return True


def handoff_expiry_quarantine_is_active(conversation: Conversation) -> bool:
    """True when expiry processing must skip this dialog until recovery."""
    return (
        conversation.handoff_quarantined_at is not None
        and conversation.handoff_quarantine_cleared_at is None
    )
