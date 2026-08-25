"""Repository for durable Teya BookingRequest feed cursor."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.teya_request_feed_cursor import (
    TEYA_REQUEST_FEED_CURSOR_ID,
    TeyaRequestFeedCursor,
)


async def get_cursor(
    session: AsyncSession,
) -> tuple[str | None, str | None]:
    row = await session.get(TeyaRequestFeedCursor, TEYA_REQUEST_FEED_CURSOR_ID)
    if row is None:
        return None, None
    return row.cursor_created_at, row.cursor_id


async def save_cursor(
    session: AsyncSession,
    *,
    created_at: str,
    cursor_id: str,
    now: datetime,
) -> None:
    stmt = insert(TeyaRequestFeedCursor).values(
        id=TEYA_REQUEST_FEED_CURSOR_ID,
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


async def clear_cursor(session: AsyncSession, *, now: datetime) -> None:
    row = await session.get(TeyaRequestFeedCursor, TEYA_REQUEST_FEED_CURSOR_ID)
    if row is None:
        return
    row.cursor_created_at = None
    row.cursor_id = None
    row.updated_at = now
