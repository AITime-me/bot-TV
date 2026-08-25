"""Durable feed cursor for Teya BookingRequest ingest (singleton row)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

TEYA_REQUEST_FEED_CURSOR_ID = "default"


class TeyaRequestFeedCursor(Base):
    """Singleton cursor for NEW BookingRequest feed pages."""

    __tablename__ = "teya_request_feed_cursors"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, nullable=False)
    cursor_created_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cursor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:
        return (
            "TeyaRequestFeedCursor("
            f"id={self.id!r}, "
            f"cursor_created_at={self.cursor_created_at!r}, "
            "cursor_id=<redacted>)"
        )
