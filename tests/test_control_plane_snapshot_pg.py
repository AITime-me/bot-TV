"""Real PostgreSQL proofs for control-plane durable snapshot consumer."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.booking_eligibility_http import BookingEligibilityHttpConfig
from app.core.control_plane_http import (
    ControlPlaneFetchCode,
    ControlPlaneHttpClient,
    ControlPlaneKnowledgeFetchResult,
    ControlPlaneSettingsFetchResult,
)
from app.core.control_plane_remote import (
    BOT_KNOWLEDGE_NOT_PUBLISHED_CODE,
    BOT_KNOWLEDGE_PUBLICATION_INVALID_CODE,
    BOT_SETTINGS_NOT_PUBLISHED_CODE,
    BOT_SETTINGS_PUBLICATION_INVALID_CODE,
    KNOWLEDGE_ROUTE_PATH,
    SETTINGS_ROUTE_PATH,
)
from app.core.control_plane_types import (
    ControlPlaneKindReadiness,
    ControlPlaneOverallReadiness,
    ControlPlaneSnapshotKind,
    KnowledgePublicationV1,
    SettingsPublicationV1,
    parse_knowledge_publication_v1,
    parse_settings_publication_v1,
)
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.db.session import session_scope
from app.models.worker_heartbeat import (
    CONTROL_PLANE_SNAPSHOT_LOOP,
)
from app.repositories import control_plane_snapshots as snapshot_repo
from app.services.control_plane_snapshot_service import ControlPlaneSnapshotService
from app.services.control_plane_snapshot_worker import ControlPlaneSnapshotWorker
from tests.pg_harness import truncate_foundation_tables

_CHECKSUM_A = "a" * 64
_CHECKSUM_B = "b" * 64
_CHECKSUM_C = "c" * 64
_PUB_V3 = "11111111-1111-4111-8111-111111111111"
_PUB_V1 = "22222222-2222-4222-8222-222222222222"
_PUB_V2 = "33333333-3333-4333-8333-333333333333"
_TOKEN = "t" * 32


def _settings_payload() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "desiredAdminState": {
            "isEnabled": True,
            "mode": "AUTO",
            "responseMode": "AUTO",
        },
        "provider": "NONE",
        "channels": {
            "siteWidget": False,
            "vk": False,
            "max": False,
            "telegram": False,
            "whatsapp": False,
        },
        "contentPolicy": {
            "mainInstruction": "instruction",
            "knowledgeBaseNote": None,
            "handoffRules": None,
            "taggingRules": None,
            "safetyRules": None,
        },
        "limits": {
            "maxMessagesPerClient": 20,
            "maxDailyMessages": 200,
            "logRetentionDays": 30,
            "errorLogRetentionDays": 90,
            "maxStoredBotEvents": 5000,
        },
        "operationalSafety": {
            "emergencyLockOwnedByBotCoreEnv": True,
            "effectiveRuntimeModeOwnedByBotCoreEnv": True,
        },
    }


def _settings_env(
    *,
    publication_id: str = _PUB_V3,
    version: int = 3,
    checksum: str = _CHECKSUM_A,
) -> dict[str, Any]:
    return {
        "ok": True,
        "schemaVersion": 1,
        "publicationId": publication_id,
        "version": version,
        "checksum": checksum,
        "publishedAt": "2026-08-01T12:00:00.000Z",
        "sourceUpdatedAt": "2026-08-01T11:00:00.000Z",
        "settings": _settings_payload(),
    }


def _knowledge_env(
    *,
    publication_id: str = _PUB_V3,
    version: int = 3,
    checksum: str = _CHECKSUM_A,
) -> dict[str, Any]:
    return {
        "ok": True,
        "schemaVersion": 1,
        "knowledgePublicationId": publication_id,
        "version": version,
        "checksum": checksum,
        "publishedAt": "2026-08-01T12:00:00.000Z",
        "entries": [
            {
                "key": "faq-general",
                "category": "FAQ",
                "title": "Общий вопрос",
                "content": "Ответ без цен.",
                "tags": ["general"],
                "serviceId": None,
            }
        ],
    }


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[S2sHttpRequest] = []
        self.by_path: dict[str, S2sHttpResponse | Exception] = {}

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        auth = req.headers.get("Authorization") or req.headers.get("authorization")
        assert auth == f"Bearer {_TOKEN}"
        for route, response in self.by_path.items():
            if req.url.endswith(route):
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"unexpected url={req.url!r}")


def _json_response(status: int, body: object) -> S2sHttpResponse:
    return S2sHttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        body=json.dumps(body).encode("utf-8"),
    )


class _ScriptedRemote:
    def __init__(self) -> None:
        self.settings_queue: list[ControlPlaneSettingsFetchResult] = []
        self.knowledge_queue: list[ControlPlaneKnowledgeFetchResult] = []

    def fetch_settings(self) -> ControlPlaneSettingsFetchResult:
        if not self.settings_queue:
            raise AssertionError("settings queue empty")
        return self.settings_queue.pop(0)

    def fetch_knowledge(self) -> ControlPlaneKnowledgeFetchResult:
        if not self.knowledge_queue:
            raise AssertionError("knowledge queue empty")
        return self.knowledge_queue.pop(0)


def _ok_settings(
    *,
    publication_id: str = _PUB_V3,
    version: int = 3,
    checksum: str = _CHECKSUM_A,
) -> ControlPlaneSettingsFetchResult:
    pub = parse_settings_publication_v1(
        _settings_env(
            publication_id=publication_id, version=version, checksum=checksum
        )
    )
    return ControlPlaneSettingsFetchResult(
        code=ControlPlaneFetchCode.OK, publication=pub
    )


def _ok_knowledge(
    *,
    publication_id: str = _PUB_V3,
    version: int = 3,
    checksum: str = _CHECKSUM_A,
) -> ControlPlaneKnowledgeFetchResult:
    pub = parse_knowledge_publication_v1(
        _knowledge_env(
            publication_id=publication_id, version=version, checksum=checksum
        )
    )
    return ControlPlaneKnowledgeFetchResult(
        code=ControlPlaneFetchCode.OK, publication=pub
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
async def test_empty_cache_not_ready(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = ControlPlaneSnapshotService(
        session_factory, remote=None, max_stale_seconds=300
    )
    state = await service.load_state_from_cache()
    assert state.overall is ControlPlaneOverallReadiness.NOT_READY
    assert state.settings.usable is False
    assert state.knowledge.usable is False


@pytest.mark.asyncio
async def test_successful_settings_and_knowledge_persistence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remote = _ScriptedRemote()
    remote.settings_queue.append(_ok_settings())
    remote.knowledge_queue.append(_ok_knowledge())
    service = ControlPlaneSnapshotService(
        session_factory, remote=remote, max_stale_seconds=300
    )
    state = await service.refresh()
    assert state.overall is ControlPlaneOverallReadiness.READY
    assert state.settings.readiness is ControlPlaneKindReadiness.READY_FRESH
    assert state.knowledge.readiness is ControlPlaneKindReadiness.READY_FRESH
    assert state.settings.identity is not None
    assert state.settings.identity.publication_id == _PUB_V3
    assert state.settings.identity.checksum == _CHECKSUM_A

    async with session_scope(session_factory) as session:
        settings_row = await snapshot_repo.get_by_kind(
            session, kind=ControlPlaneSnapshotKind.SETTINGS
        )
        knowledge_row = await snapshot_repo.get_by_kind(
            session, kind=ControlPlaneSnapshotKind.KNOWLEDGE
        )
    assert settings_row is not None and settings_row.usable is True
    assert knowledge_row is not None and knowledge_row.usable is True
    assert settings_row.version == 3
    assert knowledge_row.checksum == _CHECKSUM_A


@pytest.mark.asyncio
async def test_restart_loads_durable_verified_cache(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remote = _ScriptedRemote()
    remote.settings_queue.append(_ok_settings())
    remote.knowledge_queue.append(_ok_knowledge())
    first = ControlPlaneSnapshotService(
        session_factory, remote=remote, max_stale_seconds=300
    )
    await first.refresh()

    restarted = ControlPlaneSnapshotService(
        session_factory, remote=None, max_stale_seconds=300
    )
    state = await restarted.load_state_from_cache()
    assert state.overall is ControlPlaneOverallReadiness.READY
    assert state.settings.usable is True
    assert state.knowledge.usable is True
    assert state.settings.identity is not None
    assert state.settings.identity.checksum == _CHECKSUM_A


@pytest.mark.asyncio
async def test_replacement_by_new_publication(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remote = _ScriptedRemote()
    remote.settings_queue.extend(
        [
            _ok_settings(version=3, checksum=_CHECKSUM_A),
            _ok_settings(
                publication_id=_PUB_V2, version=4, checksum=_CHECKSUM_C
            ),
        ]
    )
    remote.knowledge_queue.extend(
        [
            _ok_knowledge(version=3, checksum=_CHECKSUM_A),
            _ok_knowledge(
                publication_id=_PUB_V2, version=4, checksum=_CHECKSUM_C
            ),
        ]
    )
    service = ControlPlaneSnapshotService(
        session_factory, remote=remote, max_stale_seconds=300
    )
    first = await service.refresh()
    second = await service.refresh()
    assert first.settings.identity is not None
    assert second.settings.identity is not None
    assert first.settings.identity.checksum == _CHECKSUM_A
    assert second.settings.identity.checksum == _CHECKSUM_C
    assert second.settings.identity.publication_id == _PUB_V2

    async with session_scope(session_factory) as session:
        row = await snapshot_repo.get_by_kind(
            session, kind=ControlPlaneSnapshotKind.SETTINGS
        )
    assert row is not None
    assert row.publication_id == _PUB_V2
    assert row.checksum == _CHECKSUM_C
    assert row.version == 4


@pytest.mark.asyncio
async def test_legal_rollback_to_lower_version_accepted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remote = _ScriptedRemote()
    remote.settings_queue.extend(
        [
            _ok_settings(version=3, checksum=_CHECKSUM_A),
            _ok_settings(
                publication_id=_PUB_V1, version=1, checksum=_CHECKSUM_B
            ),
        ]
    )
    remote.knowledge_queue.extend(
        [
            _ok_knowledge(version=3, checksum=_CHECKSUM_A),
            _ok_knowledge(
                publication_id=_PUB_V1, version=1, checksum=_CHECKSUM_B
            ),
        ]
    )
    service = ControlPlaneSnapshotService(
        session_factory, remote=remote, max_stale_seconds=300
    )
    await service.refresh()
    rolled = await service.refresh()
    assert rolled.overall is ControlPlaneOverallReadiness.READY
    assert rolled.settings.identity is not None
    assert rolled.settings.identity.version == 1
    assert rolled.settings.identity.checksum == _CHECKSUM_B


@pytest.mark.asyncio
async def test_same_publication_id_checksum_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remote = _ScriptedRemote()
    remote.settings_queue.extend([_ok_settings(), _ok_settings()])
    remote.knowledge_queue.extend([_ok_knowledge(), _ok_knowledge()])
    service = ControlPlaneSnapshotService(
        session_factory, remote=remote, max_stale_seconds=300
    )
    first = await service.refresh()
    second = await service.refresh()
    assert first.settings.identity == second.settings.identity
    assert first.knowledge.identity == second.knowledge.identity


@pytest.mark.asyncio
async def test_network_5xx_uses_stale_grace(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remote = _ScriptedRemote()
    remote.settings_queue.extend(
        [
            _ok_settings(),
            ControlPlaneSettingsFetchResult(code=ControlPlaneFetchCode.UNAVAILABLE),
        ]
    )
    remote.knowledge_queue.extend(
        [
            _ok_knowledge(),
            ControlPlaneKnowledgeFetchResult(
                code=ControlPlaneFetchCode.UNAVAILABLE
            ),
        ]
    )
    service = ControlPlaneSnapshotService(
        session_factory, remote=remote, max_stale_seconds=300
    )
    await service.refresh()
    stale = await service.refresh()
    assert stale.overall is ControlPlaneOverallReadiness.READY
    assert stale.settings.readiness is ControlPlaneKindReadiness.READY_STALE
    assert stale.knowledge.readiness is ControlPlaneKindReadiness.READY_STALE
    assert stale.settings.usable is True


@pytest.mark.asyncio
async def test_stale_expiry_not_ready(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remote = _ScriptedRemote()
    remote.settings_queue.append(_ok_settings())
    remote.knowledge_queue.append(_ok_knowledge())
    service = ControlPlaneSnapshotService(
        session_factory, remote=remote, max_stale_seconds=30
    )
    await service.refresh()

    async with session_scope(session_factory) as session:
        old = datetime.now(timezone.utc) - timedelta(seconds=120)
        await session.execute(
            text(
                "UPDATE control_plane_snapshots "
                "SET verified_at = :verified_at, fetched_at = :verified_at"
            ),
            {"verified_at": old},
        )

    remote.settings_queue.append(
        ControlPlaneSettingsFetchResult(code=ControlPlaneFetchCode.UNAVAILABLE)
    )
    remote.knowledge_queue.append(
        ControlPlaneKnowledgeFetchResult(code=ControlPlaneFetchCode.UNAVAILABLE)
    )
    expired = await service.refresh()
    assert expired.overall is ControlPlaneOverallReadiness.NOT_READY
    assert expired.settings.usable is False
    assert expired.settings.error_code == "STALE_EXPIRED"


@pytest.mark.asyncio
async def test_explicit_404_never_uses_stale_cache(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remote = _ScriptedRemote()
    remote.settings_queue.extend(
        [
            _ok_settings(),
            ControlPlaneSettingsFetchResult(
                code=ControlPlaneFetchCode.NOT_PUBLISHED
            ),
        ]
    )
    remote.knowledge_queue.extend(
        [
            _ok_knowledge(),
            ControlPlaneKnowledgeFetchResult(
                code=ControlPlaneFetchCode.NOT_PUBLISHED
            ),
        ]
    )
    service = ControlPlaneSnapshotService(
        session_factory, remote=remote, max_stale_seconds=300
    )
    await service.refresh()
    after = await service.refresh()
    assert after.overall is ControlPlaneOverallReadiness.NOT_READY
    assert after.settings.usable is False
    assert after.settings.error_code == "NOT_PUBLISHED"
    assert after.settings.readiness is ControlPlaneKindReadiness.NOT_READY

    async with session_scope(session_factory) as session:
        row = await snapshot_repo.get_by_kind(
            session, kind=ControlPlaneSnapshotKind.SETTINGS
        )
    assert row is not None
    assert row.usable is False
    assert row.last_error_code == "NOT_PUBLISHED"


@pytest.mark.asyncio
async def test_explicit_409_never_uses_stale_cache(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remote = _ScriptedRemote()
    remote.settings_queue.extend(
        [
            _ok_settings(),
            ControlPlaneSettingsFetchResult(code=ControlPlaneFetchCode.INVALID),
        ]
    )
    remote.knowledge_queue.extend(
        [
            _ok_knowledge(),
            ControlPlaneKnowledgeFetchResult(code=ControlPlaneFetchCode.INVALID),
        ]
    )
    service = ControlPlaneSnapshotService(
        session_factory, remote=remote, max_stale_seconds=300
    )
    await service.refresh()
    after = await service.refresh()
    assert after.settings.readiness is ControlPlaneKindReadiness.INVALID
    assert after.settings.usable is False
    assert after.knowledge.usable is False


@pytest.mark.asyncio
async def test_auth_error_never_uses_stale_cache(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remote = _ScriptedRemote()
    remote.settings_queue.extend(
        [
            _ok_settings(),
            ControlPlaneSettingsFetchResult(code=ControlPlaneFetchCode.AUTH_ERROR),
        ]
    )
    remote.knowledge_queue.extend(
        [
            _ok_knowledge(),
            ControlPlaneKnowledgeFetchResult(
                code=ControlPlaneFetchCode.AUTH_ERROR
            ),
        ]
    )
    service = ControlPlaneSnapshotService(
        session_factory, remote=remote, max_stale_seconds=300
    )
    await service.refresh()
    after = await service.refresh()
    assert after.settings.readiness is ControlPlaneKindReadiness.AUTH_ERROR
    assert after.settings.usable is False


@pytest.mark.asyncio
async def test_corrupt_local_cache_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remote = _ScriptedRemote()
    remote.settings_queue.append(_ok_settings())
    remote.knowledge_queue.append(_ok_knowledge())
    service = ControlPlaneSnapshotService(
        session_factory, remote=remote, max_stale_seconds=300
    )
    await service.refresh()

    async with session_scope(session_factory) as session:
        await session.execute(
            text(
                "UPDATE control_plane_snapshots "
                "SET payload = CAST(:payload AS jsonb) "
                "WHERE kind = 'SETTINGS'"
            ),
            {"payload": json.dumps({"broken": True})},
        )

    remote.settings_queue.append(
        ControlPlaneSettingsFetchResult(code=ControlPlaneFetchCode.UNAVAILABLE)
    )
    remote.knowledge_queue.append(
        ControlPlaneKnowledgeFetchResult(code=ControlPlaneFetchCode.UNAVAILABLE)
    )
    state = await service.refresh()
    assert state.settings.usable is False
    assert state.settings.error_code == "CACHE_CORRUPT"


@pytest.mark.asyncio
async def test_concurrent_refresh_protection_no_torn_cache(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = asyncio.Barrier(2)
    real_lock = snapshot_repo.try_acquire_refresh_lock

    async def gated_lock(session):  # type: ignore[no-untyped-def]
        await barrier.wait()
        return await real_lock(session)

    monkeypatch.setattr(
        "app.services.control_plane_snapshot_service.snapshot_repo.try_acquire_refresh_lock",
        gated_lock,
    )

    remote_a = _ScriptedRemote()
    remote_a.settings_queue.append(
        _ok_settings(publication_id=_PUB_V3, version=3, checksum=_CHECKSUM_A)
    )
    remote_a.knowledge_queue.append(
        _ok_knowledge(publication_id=_PUB_V3, version=3, checksum=_CHECKSUM_A)
    )
    remote_b = _ScriptedRemote()
    remote_b.settings_queue.append(
        _ok_settings(publication_id=_PUB_V2, version=4, checksum=_CHECKSUM_C)
    )
    remote_b.knowledge_queue.append(
        _ok_knowledge(publication_id=_PUB_V2, version=4, checksum=_CHECKSUM_C)
    )

    service_a = ControlPlaneSnapshotService(
        session_factory, remote=remote_a, max_stale_seconds=300
    )
    service_b = ControlPlaneSnapshotService(
        session_factory, remote=remote_b, max_stale_seconds=300
    )

    results = await asyncio.gather(service_a.refresh(), service_b.refresh())
    winners = [r for r in results if r.overall is ControlPlaneOverallReadiness.READY]
    assert len(winners) >= 1

    async with session_scope(session_factory) as session:
        settings_row = await snapshot_repo.get_by_kind(
            session, kind=ControlPlaneSnapshotKind.SETTINGS
        )
        knowledge_row = await snapshot_repo.get_by_kind(
            session, kind=ControlPlaneSnapshotKind.KNOWLEDGE
        )
    assert settings_row is not None and knowledge_row is not None
    # No torn pair: both kinds share the same publication identity.
    assert settings_row.publication_id == knowledge_row.publication_id
    assert settings_row.checksum == knowledge_row.checksum


@pytest.mark.asyncio
async def test_http_bare_404_never_uses_stale_via_real_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    transport = _FakeTransport()
    transport.by_path[SETTINGS_ROUTE_PATH] = _json_response(
        200, _settings_env()
    )
    transport.by_path[KNOWLEDGE_ROUTE_PATH] = _json_response(
        200, _knowledge_env()
    )
    client = ControlPlaneHttpClient(
        BookingEligibilityHttpConfig(
            base_url="https://example.test",
            bearer_token=_TOKEN,
        ),
        transport,
    )
    service = ControlPlaneSnapshotService(
        session_factory, remote=client, max_stale_seconds=300
    )
    ready = await service.refresh()
    assert ready.overall is ControlPlaneOverallReadiness.READY

    transport.by_path[SETTINGS_ROUTE_PATH] = _json_response(404, {"ok": False})
    transport.by_path[KNOWLEDGE_ROUTE_PATH] = S2sHttpResponse(
        status_code=404,
        headers={"content-type": "application/json"},
        body=b"{}",
    )
    after = await service.refresh()
    assert after.overall is ControlPlaneOverallReadiness.NOT_READY
    assert after.settings.usable is False
    assert after.knowledge.usable is False
    assert after.settings.error_code == "NOT_PUBLISHED"


@pytest.mark.asyncio
async def test_http_bare_409_never_uses_stale_via_real_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    transport = _FakeTransport()
    transport.by_path[SETTINGS_ROUTE_PATH] = _json_response(
        200, _settings_env()
    )
    transport.by_path[KNOWLEDGE_ROUTE_PATH] = _json_response(
        200, _knowledge_env()
    )
    client = ControlPlaneHttpClient(
        BookingEligibilityHttpConfig(
            base_url="https://example.test",
            bearer_token=_TOKEN,
        ),
        transport,
    )
    service = ControlPlaneSnapshotService(
        session_factory, remote=client, max_stale_seconds=300
    )
    await service.refresh()
    transport.by_path[SETTINGS_ROUTE_PATH] = _json_response(409, {"ok": False})
    transport.by_path[KNOWLEDGE_ROUTE_PATH] = _json_response(409, {"ok": False})
    after = await service.refresh()
    assert after.settings.readiness is ControlPlaneKindReadiness.INVALID
    assert after.settings.usable is False
    assert after.knowledge.usable is False


@pytest.mark.asyncio
async def test_http_client_end_to_end_persist(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    transport = _FakeTransport()
    transport.by_path[SETTINGS_ROUTE_PATH] = _json_response(
        200, _settings_env()
    )
    transport.by_path[KNOWLEDGE_ROUTE_PATH] = _json_response(
        200, _knowledge_env()
    )
    client = ControlPlaneHttpClient(
        BookingEligibilityHttpConfig(
            base_url="https://example.test",
            bearer_token=_TOKEN,
        ),
        transport,
    )
    service = ControlPlaneSnapshotService(
        session_factory, remote=client, max_stale_seconds=300
    )
    worker = ControlPlaneSnapshotWorker(service)
    await worker.tick()
    state = service.get_state()
    assert state.overall is ControlPlaneOverallReadiness.READY
    assert any(c.url.endswith(SETTINGS_ROUTE_PATH) for c in transport.calls)
    assert any(c.url.endswith(KNOWLEDGE_ROUTE_PATH) for c in transport.calls)


@pytest.mark.asyncio
async def test_alembic_upgrade_adds_control_plane_loop_and_table(
    pg_engine,  # noqa: ANN001
    pg_database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from pathlib import Path

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
            revision="20260828_36_amocrm_oauth_loop",
            database_url=pg_database_url,
        )
        async with session_factory() as session:
            async with session.begin():
                before = (
                    await session.execute(
                        text(
                            "SELECT loop_name FROM worker_heartbeats "
                            "ORDER BY loop_name"
                        )
                    )
                ).scalars().all()
                assert CONTROL_PLANE_SNAPSHOT_LOOP not in before
                exists_before = await session.scalar(
                    text(
                        "SELECT to_regclass('public.control_plane_snapshots')"
                    )
                )
                assert exists_before is None
                await session.execute(
                    text(
                        "INSERT INTO worker_heartbeats "
                        "(loop_name, generation_id, worker_id, started_at, "
                        "consecutive_failures, updated_at) "
                        "VALUES ('ingress', gen_random_uuid(), 'pre-existing', "
                        "now(), 0, now()) "
                        "ON CONFLICT (loop_name) DO UPDATE "
                        "SET worker_id = 'pre-existing'"
                    )
                )

        await run_alembic_command_async(
            alembic_ini=alembic_ini,
            command_name="upgrade",
            revision="20260829_37_control_plane",
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
            assert CONTROL_PLANE_SNAPSHOT_LOOP in after_upgrade
            assert "ingress" in after_upgrade
            exists_after = await session.scalar(
                text("SELECT to_regclass('public.control_plane_snapshots')")
            )
            assert exists_after is not None
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
    finally:
        await pg_engine.dispose()
        await run_alembic_command_async(
            alembic_ini=alembic_ini,
            command_name="upgrade",
            revision="head",
            database_url=pg_database_url,
        )
        await pg_engine.dispose()