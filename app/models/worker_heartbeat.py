from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

INGRESS_LOOP = "ingress"
HANDOFF_EXPIRY_LOOP = "handoff_expiry"
REPLY_PLAN_LOOP = "reply_plan"
OUTBOUND_LOOP = "outbound"
AMOCRM_MIRROR_LOOP = "amocrm_mirror"
SELF_BOOKING_CREATE_LOOP = "self_booking_create"
TEYA_REQUEST_ORCHESTRATOR_LOOP = "teya_request_orchestrator"
TEYA_REQUEST_RECONCILIATION_LOOP = "teya_request_reconciliation"

REQUIRED_WORKER_LOOPS = (
    INGRESS_LOOP,
    HANDOFF_EXPIRY_LOOP,
    REPLY_PLAN_LOOP,
    OUTBOUND_LOOP,
    AMOCRM_MIRROR_LOOP,
    SELF_BOOKING_CREATE_LOOP,
    TEYA_REQUEST_ORCHESTRATOR_LOOP,
    TEYA_REQUEST_RECONCILIATION_LOOP,
)

_LOOP_CHECK = ", ".join(f"'{name}'" for name in REQUIRED_WORKER_LOOPS)


class WorkerHeartbeat(Base):
    """Latest durable heartbeat for one required runtime loop.

    ``generation_id`` fences a previous process after restart. Registration
    replaces the generation for all loops; every later update requires the
    same generation, so an old process cannot make a new worker look healthy.
    """

    __tablename__ = "worker_heartbeats"
    __table_args__ = (
        CheckConstraint(
            f"loop_name IN ({_LOOP_CHECK})",
            name="ck_worker_heartbeats_loop_name",
        ),
        CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_worker_heartbeats_consecutive_failures_nonnegative",
        ),
        CheckConstraint(
            "("
            "consecutive_failures = 0 AND last_error_code IS NULL"
            ") OR ("
            "consecutive_failures > 0 "
            "AND last_failed_at IS NOT NULL "
            "AND last_error_code IS NOT NULL"
            ")",
            name="ck_worker_heartbeats_failure_consistency",
        ),
        Index(
            "ix_worker_heartbeats_last_succeeded_at",
            "last_succeeded_at",
        ),
    )

    loop_name: Mapped[str] = mapped_column(String(48), primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    last_tick_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_succeeded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    last_error_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    def __repr__(self) -> str:
        return (
            f"WorkerHeartbeat(loop_name={self.loop_name!r}, "
            f"generation_id={self.generation_id!r}, "
            f"worker_id={self.worker_id!r}, "
            f"consecutive_failures={self.consecutive_failures!r}, "
            f"last_error_code={self.last_error_code!r})"
        )
