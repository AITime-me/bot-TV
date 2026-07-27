"""Shared PostgreSQL fixtures for foundation and ingress integration tests.

Fixtures are not autouse: unit tests never pull in a database engine.
Destructive cleanup stays module-local so it cannot force PG on unrelated tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.session import create_engine, create_session_factory
from tests.foundation_test_db import (
    SecretDatabaseUrl,
    assert_safe_test_database_url,
    run_alembic_command_async,
)
from tests.pg_harness import assert_postgres_reachable, require_safe_test_url

import app.models  # noqa: F401 — register metadata

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"


@pytest.fixture(scope="session")
def pg_database_url() -> SecretDatabaseUrl:
    return require_safe_test_url()


@pytest_asyncio.fixture(scope="session")
async def pg_engine(
    pg_database_url: SecretDatabaseUrl,
) -> AsyncIterator[AsyncEngine]:
    assert_safe_test_database_url(pg_database_url)
    await assert_postgres_reachable(pg_database_url)

    settings = Settings(database_url=pg_database_url.reveal())
    engine = create_engine(settings)

    await run_alembic_command_async(
        alembic_ini=_ALEMBIC_INI,
        command_name="upgrade",
        revision="head",
        database_url=pg_database_url,
    )
    await engine.dispose()
    try:
        yield engine
    finally:
        await engine.dispose()
        try:
            await run_alembic_command_async(
                alembic_ini=_ALEMBIC_INI,
                command_name="downgrade",
                revision="base",
                database_url=pg_database_url,
            )
        finally:
            await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(
    pg_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(pg_engine)
