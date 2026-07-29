from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# Stable project-local advisory-lock key. It protects the singleton runtime
# connection only; it is not a credential and is never derived from user data.
_WORKER_ADVISORY_LOCK_KEY = 731_071_086_657


class WorkerAlreadyRunningError(RuntimeError):
    """Another process owns the singleton worker runtime lock."""


@asynccontextmanager
async def worker_singleton_lock(engine: AsyncEngine) -> AsyncIterator[None]:
    """Hold a PostgreSQL session advisory lock for the worker lifetime."""
    async with engine.connect() as connection:
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:lock_key)"),
            {"lock_key": _WORKER_ADVISORY_LOCK_KEY},
        )
        await connection.commit()
        if acquired is not True:
            raise WorkerAlreadyRunningError("WORKER_ALREADY_RUNNING")
        try:
            yield
        finally:
            try:
                await connection.scalar(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": _WORKER_ADVISORY_LOCK_KEY},
                )
                await connection.commit()
            except Exception:
                # Closing the session releases every session advisory lock.
                # Preserve the original shutdown/failure instead of replacing
                # it with an unlock error.
                pass
