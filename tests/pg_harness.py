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
            has_reply_plans = await session.scalar(
                text("SELECT to_regclass('public.reply_plans') IS NOT NULL")
            )
            has_mirror_jobs = await session.scalar(
                text("SELECT to_regclass('public.amocrm_mirror_jobs') IS NOT NULL")
            )
            has_manager_messages = await session.scalar(
                text("SELECT to_regclass('public.manager_messages') IS NOT NULL")
            )
            has_worker_heartbeats = await session.scalar(
                text(
                    "SELECT to_regclass('public.worker_heartbeats') IS NOT NULL"
                )
            )
            has_ops_events = await session.scalar(
                text(
                    "SELECT to_regclass('public.conversation_ops_events') "
                    "IS NOT NULL"
                )
            )
            has_ephemeral_pii = await session.scalar(
                text(
                    "SELECT to_regclass('public.ephemeral_pii_values') "
                    "IS NOT NULL"
                )
            )
            has_attachment_spool = await session.scalar(
                text(
                    "SELECT to_regclass('public.attachment_spool_objects') "
                    "IS NOT NULL"
                )
            )
            has_master_bindings = await session.scalar(
                text(
                    "SELECT to_regclass('public.master_channel_bindings') "
                    "IS NOT NULL"
                )
            )
            has_master_commands = await session.scalar(
                text(
                    "SELECT to_regclass('public.master_command_pendings') "
                    "IS NOT NULL"
                )
            )
            has_identity_links = await session.scalar(
                text(
                    "SELECT to_regclass('public.external_identity_links') "
                    "IS NOT NULL"
                )
            )
            has_canonical_identities = await session.scalar(
                text(
                    "SELECT to_regclass('public.canonical_identities') "
                    "IS NOT NULL"
                )
            )
            tables = ["outbox_messages", "inbox_messages", "conversations"]
            if has_reply_plans:
                tables.insert(0, "reply_plans")
            if has_mirror_jobs:
                tables.insert(0, "amocrm_mirror_jobs")
            if has_manager_messages:
                tables.insert(0, "manager_messages")
            if has_ops_events:
                tables.insert(0, "conversation_ops_events")
            if has_ephemeral_pii:
                tables.insert(0, "ephemeral_pii_values")
            if has_attachment_spool:
                tables.insert(0, "attachment_spool_objects")
            if has_master_commands:
                tables.insert(0, "master_command_pendings")
            if has_master_bindings:
                tables.insert(0, "master_channel_bindings")
            if has_identity_links:
                tables.insert(0, "external_identity_links")
            if has_canonical_identities:
                tables.insert(0, "canonical_identities")
            if has_worker_heartbeats:
                tables.insert(0, "worker_heartbeats")
            if has_ingress:
                tables.append("ingress_events")
            await session.execute(
                text(
                    "TRUNCATE "
                    + ", ".join(tables)
                    + " RESTART IDENTITY CASCADE"
                )
            )
