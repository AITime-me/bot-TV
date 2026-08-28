from __future__ import annotations

import asyncio
import base64
import json
import secrets
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_crm_oauth_keys import EnvAmoCrmOauthKeyProvider
from app.core.amocrm_crm_oauth_types import KEY_SIZE_BYTES
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.amocrm_crm_rest_http import AmoCrmCrmRestHttpClient
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.db.session import session_scope
from app.repositories import amocrm_crm_oauth_tokens as oauth_repo
from app.services.amocrm_crm_oauth_lifecycle_worker import (
    AmoCrmCrmOauthLifecycleWorker,
)
from tests.pg_harness import truncate_foundation_tables

_KEY_B64 = base64.urlsafe_b64encode(
    secrets.token_bytes(KEY_SIZE_BYTES)
).decode("ascii")
_CONFIG = AmoCrmCrmRestConfig(
    enabled=True,
    client_id="client-id",
    client_secret="client-secret",
    api_base_url="https://example.amocrm.ru",
    redirect_uri="https://example.com/oauth",
    connection_scope="default",
)


def _provider() -> EnvAmoCrmOauthKeyProvider:
    return EnvAmoCrmOauthKeyProvider(
        {
            "AMOCRM_CRM_OAUTH_ACTIVE_KEY_ID": "K1",
            "AMOCRM_CRM_OAUTH_KEY_K1": _KEY_B64,
        }
    )


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[S2sHttpRequest] = []
        self.responses: list[S2sHttpResponse] = []

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        if not self.responses:
            raise AssertionError("unexpected second remote refresh")
        return self.responses.pop(0)


def _install_claim_barrier(
    monkeypatch: pytest.MonkeyPatch,
    *,
    parties: int = 2,
) -> asyncio.Barrier:
    import app.core.amocrm_crm_rest_http as rest_http

    barrier = asyncio.Barrier(parties)
    real_claim = oauth_repo.claim_refresh_lease

    async def gated_claim(session, **kwargs):  # type: ignore[no-untyped-def]
        await barrier.wait()
        return await real_claim(session, **kwargs)

    monkeypatch.setattr(
        rest_http.oauth_repo,
        "claim_refresh_lease",
        gated_claim,
    )
    return barrier


def _success_response(
    *,
    access_token: str,
    refresh_token: str,
) -> S2sHttpResponse:
    return S2sHttpResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=json.dumps(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_in": 3600,
            }
        ).encode("utf-8"),
    )


@pytest_asyncio.fixture(autouse=True)
async def cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


@pytest.mark.asyncio
async def test_lifecycle_refresh_persists_pair_expiry_and_stays_idle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = _provider()
    before = datetime.now(timezone.utc)
    async with session_scope(session_factory) as session:
        await oauth_repo.upsert_token_pair(
            session,
            access_token="access-before",
            refresh_token="refresh-before",
            key_provider=provider,
            access_expires_at=before - timedelta(seconds=1),
        )

    transport = _FakeTransport()
    transport.responses.append(
        _success_response(
            access_token="access-after",
            refresh_token="refresh-after",
        )
    )
    oauth = AmoCrmCrmRestHttpClient(
        _CONFIG,
        session_factory=session_factory,
        key_provider=provider,
        transport=transport,
        worker_id="oauth-lifecycle-persist",
    )
    worker = AmoCrmCrmOauthLifecycleWorker(
        session_factory,
        worker_id="oauth-lifecycle-persist",
        config=_CONFIG,
        oauth=oauth,
    )

    await worker.tick()
    await worker.tick()

    assert len(transport.calls) == 1
    async with session_scope(session_factory) as session:
        row = await oauth_repo.get_by_scope(session)
        assert row is not None
        tokens = oauth_repo.decrypt_row(row, key_provider=provider)
        assert tokens.access_token == "access-after"
        assert tokens.refresh_token == "refresh-after"
        assert tokens.access_expires_at is not None
        assert tokens.access_expires_at > before + timedelta(minutes=50)


@pytest.mark.asyncio
async def test_concurrent_lifecycle_ticks_make_one_remote_refresh(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_claim_barrier(monkeypatch, parties=2)
    provider = _provider()
    async with session_scope(session_factory) as session:
        await oauth_repo.upsert_token_pair(
            session,
            access_token="access-before",
            refresh_token="refresh-before",
            key_provider=provider,
            access_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )

    transport = _FakeTransport()
    transport.responses.append(
        _success_response(
            access_token="access-after",
            refresh_token="refresh-after",
        )
    )

    def build_worker(worker_id: str) -> AmoCrmCrmOauthLifecycleWorker:
        oauth = AmoCrmCrmRestHttpClient(
            _CONFIG,
            session_factory=session_factory,
            key_provider=provider,
            transport=transport,
            worker_id=worker_id,
        )
        return AmoCrmCrmOauthLifecycleWorker(
            session_factory,
            worker_id=worker_id,
            config=_CONFIG,
            oauth=oauth,
        )

    await asyncio.gather(
        build_worker("oauth-concurrent-a").tick(),
        build_worker("oauth-concurrent-b").tick(),
    )

    assert len(transport.calls) == 1
    async with session_scope(session_factory) as session:
        row = await oauth_repo.get_by_scope(session)
        assert row is not None
        tokens = oauth_repo.decrypt_row(row, key_provider=provider)
        assert tokens.access_token == "access-after"
        assert tokens.refresh_token == "refresh-after"


@pytest.mark.asyncio
async def test_lifecycle_and_reactive_401_concurrent_one_remote_post(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_claim_barrier(monkeypatch, parties=2)
    provider = _provider()
    async with session_scope(session_factory) as session:
        await oauth_repo.upsert_token_pair(
            session,
            access_token="access-before",
            refresh_token="refresh-before",
            key_provider=provider,
            access_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )

    transport = _FakeTransport()
    transport.responses.append(
        _success_response(
            access_token="access-after",
            refresh_token="refresh-after",
        )
    )

    lifecycle_oauth = AmoCrmCrmRestHttpClient(
        _CONFIG,
        session_factory=session_factory,
        key_provider=provider,
        transport=transport,
        worker_id="lifecycle-peer",
    )
    reactive_oauth = AmoCrmCrmRestHttpClient(
        _CONFIG,
        session_factory=session_factory,
        key_provider=provider,
        transport=transport,
        worker_id="reactive-401",
    )
    lifecycle = AmoCrmCrmOauthLifecycleWorker(
        session_factory,
        worker_id="lifecycle-peer",
        config=_CONFIG,
        oauth=lifecycle_oauth,
    )

    await asyncio.gather(
        lifecycle.tick(),
        reactive_oauth.refresh_tokens(if_still_access_token="access-before"),
    )

    assert len(transport.calls) == 1


@pytest.mark.asyncio
@pytest.mark.no_foundation_row_cleanup
async def test_alembic_rev35_to_rev36_preserves_existing_heartbeats(
    pg_database_url,
    pg_engine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from pathlib import Path

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncEngine

    from tests.foundation_test_db import (
        assert_safe_test_database_url,
        run_alembic_command_async,
    )

    assert_safe_test_database_url(pg_database_url)
    alembic_ini = str(Path(__file__).resolve().parents[1] / "alembic.ini")
    await pg_engine.dispose()
    try:
        await run_alembic_command_async(
            alembic_ini=alembic_ini,
            command_name="downgrade",
            revision="20260828_35_acquisition_source",
            database_url=pg_database_url,
        )
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO worker_heartbeats "
                        "(loop_name, generation_id, worker_id, started_at, "
                        "consecutive_failures, updated_at) "
                        "VALUES ('ingress', gen_random_uuid(), 'pre-existing', "
                        "now(), 0, now())"
                    )
                )
                before = (
                    await session.execute(
                        text(
                            "SELECT loop_name FROM worker_heartbeats "
                            "ORDER BY loop_name"
                        )
                    )
                ).scalars().all()

        await run_alembic_command_async(
            alembic_ini=alembic_ini,
            command_name="upgrade",
            revision="20260828_36_amocrm_oauth_loop",
            database_url=pg_database_url,
        )
        async with session_factory() as session:
            after_upgrade = (
                await session.execute(
                    text(
                        "SELECT loop_name FROM worker_heartbeats "
                        "ORDER BY loop_name"
                    )
                )
            ).scalars().all()
            assert "ingress" in after_upgrade
            assert "amocrm_crm_oauth_lifecycle" in after_upgrade
            ingress_rows = (
                await session.execute(
                    text(
                        "SELECT worker_id FROM worker_heartbeats "
                        "WHERE loop_name = 'ingress'"
                    )
                )
            ).all()
            assert len(ingress_rows) == 1
            assert ingress_rows[0][0] == "pre-existing"
            lifecycle_rows = (
                await session.execute(
                    text(
                        "SELECT worker_id FROM worker_heartbeats "
                        "WHERE loop_name = 'amocrm_crm_oauth_lifecycle'"
                    )
                )
            ).all()
            assert len(lifecycle_rows) == 1
            assert lifecycle_rows[0][0] == "bootstrap"

        await run_alembic_command_async(
            alembic_ini=alembic_ini,
            command_name="downgrade",
            revision="20260828_35_acquisition_source",
            database_url=pg_database_url,
        )
        async with session_factory() as session:
            after_downgrade = (
                await session.execute(
                    text(
                        "SELECT loop_name FROM worker_heartbeats "
                        "ORDER BY loop_name"
                    )
                )
            ).scalars().all()
            assert after_downgrade == before
            assert "amocrm_crm_oauth_lifecycle" not in after_downgrade
    finally:
        await pg_engine.dispose()
        await run_alembic_command_async(
            alembic_ini=alembic_ini,
            command_name="upgrade",
            revision="head",
            database_url=pg_database_url,
        )
        await pg_engine.dispose()
