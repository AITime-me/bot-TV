from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.pii_gateway import (
    orm_local_column,
    repr_orm_fingerprint,
    repr_orm_literal,
)
from app.db.base import Base


class IngressEventType(str, enum.Enum):
    """Normalized event types accepted at the durable ingress boundary."""

    SYNTHETIC_MESSAGE = "SYNTHETIC_MESSAGE"


class IngressStatus(str, enum.Enum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    DEAD = "DEAD"


# Explicit finite-state transitions. PROCESSING→PROCESSING is lease reclaim
# (same status, new fencing token) and is handled outside this map.
INGRESS_TRANSITIONS: dict[IngressStatus, frozenset[IngressStatus]] = {
    IngressStatus.RECEIVED: frozenset({IngressStatus.PROCESSING}),
    IngressStatus.PROCESSING: frozenset(
        {
            IngressStatus.PROCESSED,
            IngressStatus.FAILED,
            IngressStatus.DEAD,
        }
    ),
    IngressStatus.FAILED: frozenset(
        {
            IngressStatus.PROCESSING,
            IngressStatus.DEAD,
        }
    ),
    IngressStatus.PROCESSED: frozenset(),
    IngressStatus.DEAD: frozenset(),
}


def ingress_transition_allowed(
    current: IngressStatus | str,
    target: IngressStatus | str,
) -> bool:
    current_status = (
        current if isinstance(current, IngressStatus) else IngressStatus(current)
    )
    target_status = (
        target if isinstance(target, IngressStatus) else IngressStatus(target)
    )
    return target_status in INGRESS_TRANSITIONS[current_status]


class IngressEvent(Base):
    """Durable provider-event receipt log (BOT-CORE-INGRESS-01B).

    Persisted before any source ACK. envelope_json holds only a schema-validated
    synthetic envelope — never tokens, signatures, or arbitrary raw provider
    payloads. __repr__ intentionally omits envelope contents.
    """

    __tablename__ = "ingress_events"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "external_event_id",
            name="uq_ingress_channel_external_event_id",
        ),
        CheckConstraint(
            "channel IN ('synthetic')",
            name="ck_ingress_channel",
        ),
        CheckConstraint(
            "event_type IN ('SYNTHETIC_MESSAGE')",
            name="ck_ingress_event_type",
        ),
        CheckConstraint(
            "status IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'FAILED', 'DEAD')",
            name="ck_ingress_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_ingress_attempt_count_nonnegative",
        ),
        CheckConstraint(
            "max_attempts > 0",
            name="ck_ingress_max_attempts_positive",
        ),
        CheckConstraint(
            "lease_version >= 0",
            name="ck_ingress_lease_version_nonnegative",
        ),
        Index("ix_ingress_events_status_created_at", "status", "created_at"),
        Index("ix_ingress_events_next_attempt_at", "next_attempt_at"),
        Index("ix_ingress_events_lease_until", "lease_until"),
        Index("ix_ingress_events_correlation_id", "correlation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=IngressStatus.RECEIVED.value,
        server_default=text("'RECEIVED'"),
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
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
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
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
    )
    envelope_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
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

    def __repr__(self) -> str:
        return (
            "IngressEvent("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='ingress_event_id')}, "
            f"channel={repr_orm_literal(orm_local_column(self, 'channel'))}, "
            f"external_event_id={repr_orm_fingerprint(orm_local_column(self, 'external_event_id'), purpose='external_event_id')}, "
            f"status={repr_orm_literal(orm_local_column(self, 'status'))}, "
            f"attempt_count={repr_orm_literal(orm_local_column(self, 'attempt_count'))}, "
            f"lease_version={repr_orm_literal(orm_local_column(self, 'lease_version'))}, "
            f"correlation_id={repr_orm_fingerprint(orm_local_column(self, 'correlation_id'), purpose='correlation_id')}, "
            "envelope=<redacted>)"
        )
