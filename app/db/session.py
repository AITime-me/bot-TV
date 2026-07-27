from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings

# Repository INSERT ... ON CONFLICT DO NOTHING + subsequent SELECT assumes
# READ COMMITTED so each statement observes a fresh snapshot. Callers must
# not raise the isolation level without adjusting repository semantics.
_DEFAULT_ISOLATION_LEVEL = "READ COMMITTED"


def create_engine(settings: Settings) -> AsyncEngine:
    # Never log settings.async_database_url — it may contain credentials.
    return create_async_engine(
        settings.async_database_url,
        pool_pre_ping=True,
        isolation_level=_DEFAULT_ISOLATION_LEVEL,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Caller-owned unit of work: begin → yield → commit, or rollback on error.

    Services and repositories must only flush(); they must not commit or
    rollback. Partial conversation/inbox/outbox work is discarded on exception.
    """
    async with session_factory() as session:
        async with session.begin():
            yield session
