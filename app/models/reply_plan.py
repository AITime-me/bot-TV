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

BOT_RESPONSE_DELAY_MS = 5000


class ReplyPlanType(str, enum.Enum):
    CLIENT_REPLY = "CLIENT_REPLY"
    # Service signals (handoff/CRM/alert) are reserved; not implemented in 01C.
    SERVICE_SIGNAL = "SERVICE_SIGNAL"


class ReplyPlanStatus(str, enum.Enum):
    PENDING = "PENDING"
    READY = "READY"
    PROCESSING = "PROCESSING"
    DISPATCHED = "DISPATCHED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"
    DEAD = "DEAD"


REPLY_PLAN_TRANSITIONS: dict[ReplyPlanStatus, frozenset[ReplyPlanStatus]] = {
    ReplyPlanStatus.PENDING: frozenset(
        {
            ReplyPlanStatus.READY,
            ReplyPlanStatus.PROCESSING,
            ReplyPlanStatus.CANCELLED,
            ReplyPlanStatus.SUPERSEDED,
        }
    ),
    ReplyPlanStatus.READY: frozenset(
        {
            ReplyPlanStatus.PROCESSING,
            ReplyPlanStatus.CANCELLED,
            ReplyPlanStatus.SUPERSEDED,
        }
    ),
    ReplyPlanStatus.PROCESSING: frozenset(
        {
            ReplyPlanStatus.DISPATCHED,
            ReplyPlanStatus.FAILED,
            ReplyPlanStatus.DEAD,
            ReplyPlanStatus.CANCELLED,
            ReplyPlanStatus.SUPERSEDED,
        }
    ),
    ReplyPlanStatus.FAILED: frozenset(
        {
            ReplyPlanStatus.PROCESSING,
            ReplyPlanStatus.DEAD,
            ReplyPlanStatus.CANCELLED,
            ReplyPlanStatus.SUPERSEDED,
        }
    ),
    ReplyPlanStatus.DISPATCHED: frozenset(),
    ReplyPlanStatus.CANCELLED: frozenset(),
    ReplyPlanStatus.SUPERSEDED: frozenset(),
    ReplyPlanStatus.DEAD: frozenset(),
}

TERMINAL_REPLY_PLAN_STATUSES = frozenset(
    {
        ReplyPlanStatus.DISPATCHED,
        ReplyPlanStatus.CANCELLED,
        ReplyPlanStatus.SUPERSEDED,
        ReplyPlanStatus.DEAD,
    }
)


def reply_plan_transition_allowed(
    current: ReplyPlanStatus | str,
    target: ReplyPlanStatus | str,
) -> bool:
    current_status = (
        current if isinstance(current, ReplyPlanStatus) else ReplyPlanStatus(current)
    )
    target_status = (
        target if isinstance(target, ReplyPlanStatus) else ReplyPlanStatus(target)
    )
    return target_status in REPLY_PLAN_TRANSITIONS[current_status]


class ReplyPlan(Base):
    """Persisted reply orchestration plan (BOT-CORE-REPLY-OUTBOUND-01C).

    Delay is stored as not_before in PostgreSQL — never as process sleep.
    payload_json is a synthetic redacted envelope only.
    """

    __tablename__ = "reply_plans"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "context_version",
            name="uq_reply_plans_conversation_context_version",
        ),
        CheckConstraint(
            "plan_type IN ('CLIENT_REPLY', 'SERVICE_SIGNAL')",
            name="ck_reply_plans_plan_type",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'READY', 'PROCESSING', 'DISPATCHED', "
            "'CANCELLED', 'SUPERSEDED', 'FAILED', 'DEAD')",
            name="ck_reply_plans_status",
        ),
        CheckConstraint(
            "bot_response_delay_ms >= 0",
            name="ck_reply_plans_delay_nonnegative",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_reply_plans_attempt_count_nonnegative",
        ),
        CheckConstraint(
            "max_attempts > 0",
            name="ck_reply_plans_max_attempts_positive",
        ),
        CheckConstraint(
            "lease_version >= 0",
            name="ck_reply_plans_lease_version_nonnegative",
        ),
        CheckConstraint(
            "context_version >= 0",
            name="ck_reply_plans_context_version_nonnegative",
        ),
        Index("ix_reply_plans_status_not_before", "status", "not_before"),
        Index("ix_reply_plans_lease_until", "lease_until"),
        Index("ix_reply_plans_conversation_id", "conversation_id"),
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
    context_version: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ReplyPlanType.CLIENT_REPLY.value,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ReplyPlanStatus.PENDING.value,
        server_default=text("'PENDING'"),
    )
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bot_response_delay_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=BOT_RESPONSE_DELAY_MS,
        server_default=text(str(BOT_RESPONSE_DELAY_MS)),
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    cancel_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
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

    conversation = relationship(
        "Conversation",
        back_populates="reply_plans",
        foreign_keys=[conversation_id],
    )
    outbound_messages = relationship("OutboxMessage", back_populates="reply_plan")

    def __repr__(self) -> str:
        return (
            f"ReplyPlan(id={self.id!r}, conversation_id={self.conversation_id!r}, "
            f"context_version={self.context_version!r}, status={self.status!r}, "
            f"plan_type={self.plan_type!r}, lease_version={self.lease_version!r}, "
            f"payload=<redacted>)"
        )
