"""Repository for durable Teya / booking-method feed cursors.

Rows are keyed by ``id`` (cursor namespace). Default remains the Teya
BookingRequest cursor (``TEYA_REQUEST_FEED_CURSOR_ID`` = ``\"default\"``).
A2.2 booking-method uses ``BOOKING_METHOD_FEED_CURSOR_ID`` = ``\"booking_method\"``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.booking_method_types import FEED_CURSOR_ID as BOOKING_METHOD_FEED_CURSOR_ID
from app.models.teya_request_feed_cursor import (
    TEYA_REQUEST_FEED_CURSOR_ID,
    TeyaRequestFeedCursor,
)

__all__ = (
    "BOOKING_METHOD_FEED_CURSOR_ID",
    "TEYA_REQUEST_FEED_CURSOR_ID",
    "clear_cursor",
    "get_cursor",
    "save_cursor",
)


async def get_cursor(
    session: AsyncSession,
    *,
    cursor_id: str = TEYA_REQUEST_FEED_CURSOR_ID,
) -> tuple[str | None, str | None]:
    row = await session.get(TeyaRequestFeedCursor, cursor_id)
    if row is None:
        return None, None
    return row.cursor_created_at, row.cursor_id


async def save_cursor(
    session: AsyncSession,
    *,
    created_at: str,
    cursor_id: str,
    now: datetime,
    feed_cursor_id: str = TEYA_REQUEST_FEED_CURSOR_ID,
) -> None:
    """Persist page cursor. ``feed_cursor_id`` selects the namespace row (default Teya)."""

    stmt = insert(TeyaRequestFeedCursor).values(
        id=feed_cursor_id,
        cursor_created_at=created_at,
        cursor_id=cursor_id,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[TeyaRequestFeedCursor.id],
        set_={
            "cursor_created_at": created_at,
            "cursor_id": cursor_id,
            "updated_at": now,
        },
    )
    await session.execute(stmt)


async def clear_cursor(
    session: AsyncSession,
    *,
    now: datetime,
    cursor_id: str = TEYA_REQUEST_FEED_CURSOR_ID,
) -> None:
    row = await session.get(TeyaRequestFeedCursor, cursor_id)
    if row is None:
        return
    row.cursor_created_at = None
    row.cursor_id = None
    row.updated_at = now
