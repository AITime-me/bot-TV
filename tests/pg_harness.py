"""Shared PostgreSQL helpers for foundation and ingress integration tests."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.session import create_engine
from tests.foundation_test_db import (
    PgDatabaseUnavailableError,
    SecretDatabaseUrl,
    UnsafeTestDatabaseError,
    as_secret_database_url,
    assert_safe_test_database_url,
    resolve_secret_test_database_url,
    scrub_secrets,
)

import pytest


def require_safe_test_url() -> SecretDatabaseUrl:
    url = resolve_secret_test_database_url()
    if url is None:
        pytest.skip(
            "PostgreSQL unavailable: set BOT_TV_TEST_DATABASE_URL "
            "(database name must contain a discrete 'test' segment) to run "
            "foundation/ingress integration tests; DATABASE_URL is never used"
        )
    try:
        assert_safe_test_database_url(url)
    except UnsafeTestDatabaseError as error:
        pytest.fail(f"unsafe BOT_TV_TEST_DATABASE_URL: {error}")
    return url


async def assert_postgres_reachable(url: str | SecretDatabaseUrl) -> None:
    """Fail hard when a safe test URL is set but PostgreSQL is unreachable."""
    secret = as_secret_database_url(url)
    assert_safe_test_database_url(secret)
    settings = Settings(database_url=secret.reveal())
    engine = create_engine(settings)
    failure: str | None = None
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        failure = (
            "BOT_TV_TEST_DATABASE_URL is set but PostgreSQL is unreachable at "
            f"{secret.target()} "
            f"({type(exc).__name__}: {scrub_secrets(str(exc), secret)})"
        )
    finally:
        await engine.dispose()
    if failure is not None:
        raise PgDatabaseUnavailableError(failure)


async def truncate_foundation_tables(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        async with session.begin():
            present = await session.scalar(
                text("SELECT to_regclass('public.conversations') IS NOT NULL")
            )
            if not present:
                return
            has_ingress = await session.scalar(
                text("SELECT to_regclass('public.ingress_events') IS NOT NULL")
            )
            if has_ingress:
                await session.execute(
                    text(
                        "TRUNCATE outbox_messages, inbox_messages, conversations, "
                        "ingress_events RESTART IDENTITY CASCADE"
                    )
                )
            else:
                await session.execute(
                    text(
                        "TRUNCATE outbox_messages, inbox_messages, conversations "
                        "RESTART IDENTITY CASCADE"
                    )
                )
