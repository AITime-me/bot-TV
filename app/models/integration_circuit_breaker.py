"""Durable integration circuit breaker state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_STATE_SQL = "'CLOSED', 'OPEN', 'HALF_OPEN'"


class IntegrationCircuitBreaker(Base):
    __tablename__ = "integration_circuit_breakers"
    __table_args__ = (
        CheckConstraint(
            f"state IN ({_STATE_SQL})",
            name="ck_integration_circuit_breakers_state",
        ),
        CheckConstraint(
            "failure_count >= 0",
            name="ck_integration_circuit_breakers_failure_count",
        ),
        CheckConstraint(
            "half_open_successes >= 0",
            name="ck_integration_circuit_breakers_half_open_successes",
        ),
    )

    key: Mapped[str] = mapped_column(String(64), primary_key=True, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    half_open_successes: Mapped[int] = mapped_column(Integer(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:
        return (
            "IntegrationCircuitBreaker("
            f"key={self.key!r}, "
            f"state={self.state!r}, "
            f"failure_count={self.failure_count!r})"
        )
