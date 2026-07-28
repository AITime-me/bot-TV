from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def db_now(session: AsyncSession) -> datetime:
    """Return the PostgreSQL transaction timestamp as the only clock of record.

    Scheduling columns (``not_before``, ``lease_until``) and the server-side
    ``created_at``/``updated_at`` defaults all resolve to ``now()`` of the same
    transaction, so a skewed application host cannot shift persisted deadlines.
    """
    moment = await session.scalar(select(func.now()))
    if moment is None:
        raise RuntimeError("DB_CLOCK_UNAVAILABLE")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


async def resolve_moment(session: AsyncSession, now: datetime | None) -> datetime:
    """Use an explicitly injected instant, otherwise the PostgreSQL clock."""
    if now is not None:
        return now
    return await db_now(session)
