"""Real PostgreSQL proofs for AI-DIALOGUE-01 conversation history in runtime context."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import BotMode, Settings
from app.core.control_plane_types import (
    ControlPlaneSnapshotKind,
    parse_knowledge_publication_v1,
    parse_settings_publication_v1,
    settings_publication_to_payload_dict,
    knowledge_publication_to_payload_dict,
)
from app.core.live_facts_http import LiveFactsFetchCode, LiveFactsFetchResult
from app.core.live_facts_types import parse_live_facts_response_v1
from app.core.runtime_context_types import (
    HARD_MAX_HISTORY_TURNS,
    RuntimeContextReadiness,
    RuntimeContextReason,
    TrustBoundary,
)
from app.repositories import control_plane_snapshots as snapshot_repo
from app.schemas.inbound import SyntheticInboundEvent
from app.schemas.manager_message import SyntheticManagerMessageEvent
from app.services.control_plane_snapshot_service import ControlPlaneSnapshotService
from app.services.inbound import InboundService
from app.services.manager_messages import apply_manager_message_in_session
from app.services.runtime_context_builder import RuntimeContextBuilder
from tests.fixtures.online_zapis_live_facts_v1 import (
    ONLINE_ZAPIS_LIVE_FACTS_V1_REPRESENTATIVE,
)
from tests.pg_harness import truncate_foundation_tables

_CHECKSUM = "a" * 64
_PUB_ID = "11111111-1111-4111-8111-111111111111"


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    yield
    await truncate_foundation_tables(session_factory)


def _settings_envelope() -> dict[str, Any]:
    return {
        "ok": True,
        "schemaVersion": 1,
        "publicationId": _PUB_ID,
        "version": 3,
        "checksum": _CHECKSUM,
        "publishedAt": "2026-08-01T12:00:00.000Z",
        "sourceUpdatedAt": "2026-08-01T11:00:00.000Z",
        "settings": {
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
        },
    }


def _knowledge_envelope() -> dict[str, Any]:
    return {
        "ok": True,
        "schemaVersion": 1,
        "knowledgePublicationId": _PUB_ID,
        "version": 3,
        "checksum": _CHECKSUM,
        "publishedAt": "2026-08-01T12:00:00.000Z",
        "entries": [
            {
                "key": "faq-general",
                "category": "FAQ",
                "title": "Общее",
                "content": "FAQ prose only",
                "tags": ["general"],
                "serviceId": None,
            }
        ],
    }


class _StaticLiveFacts:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self._payload = (
            payload
            if payload is not None
            else copy.deepcopy(ONLINE_ZAPIS_LIVE_FACTS_V1_REPRESENTATIVE)
        )
        self.fetch_count = 0

    def fetch(self) -> LiveFactsFetchResult:
        self.fetch_count += 1
        return LiveFactsFetchResult(
            code=LiveFactsFetchCode.OK,
            payload=parse_live_facts_response_v1(self._payload),
        )


class _FailLiveFacts:
    def __init__(self, code: LiveFactsFetchCode) -> None:
        self._code = code

    def fetch(self) -> LiveFactsFetchResult:
        return LiveFactsFetchResult(code=self._code, payload=None)


async def _seed_control_plane(
    session_factory: async_sessionmaker[AsyncSession],
) -> ControlPlaneSnapshotService:
    settings = parse_settings_publication_v1(_settings_envelope())
    knowledge = parse_knowledge_publication_v1(_knowledge_envelope())
    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        async with session.begin():
            await snapshot_repo.upsert_verified(
                session,
                kind=ControlPlaneSnapshotKind.SETTINGS,
                schema_version=1,
                publication_id=settings.publication_id,
                version=settings.version,
                checksum=settings.checksum,
                payload=settings_publication_to_payload_dict(settings),
                published_at=settings.published_at,
                verified_at=now,
                fetched_at=now,
            )
            await snapshot_repo.upsert_verified(
                session,
                kind=ControlPlaneSnapshotKind.KNOWLEDGE,
                schema_version=1,
                publication_id=knowledge.knowledge_publication_id,
                version=knowledge.version,
                checksum=knowledge.checksum,
                payload=knowledge_publication_to_payload_dict(knowledge),
                published_at=knowledge.published_at,
                verified_at=now,
                fetched_at=now,
            )
    service = ControlPlaneSnapshotService(
        session_factory=session_factory,
        remote=None,
        max_stale_seconds=300,
    )
    await service.load_state_from_cache()
    return service


def _local_settings(*, emergency_lock: bool = False) -> Settings:
    return Settings(bot_mode=BotMode.OFF, emergency_lock=emergency_lock)


@pytest.mark.asyncio
async def test_runtime_context_reads_real_history_ordered_and_isolated(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cp = await _seed_control_plane(session_factory)
    live = _StaticLiveFacts()

    async with session_factory() as session:
        async with session.begin():
            first = await InboundService(session).accept(
                SyntheticInboundEvent(
                    external_conversation_id="ctx-a",
                    external_message_id="c1",
                    text="Привет из диалога A",
                )
            )
            conv_a = first.conversation.id
            await apply_manager_message_in_session(
                session,
                event=SyntheticManagerMessageEvent(
                    external_conversation_id="ctx-a",
                    external_message_id="m1",
                    provider_sequence=1,
                    text="Ответ менеджера A",
                ),
            )
            other = await InboundService(session).accept(
                SyntheticInboundEvent(
                    external_conversation_id="ctx-b",
                    external_message_id="c2",
                    text="Чужой диалог B — не должен попасть",
                )
            )
            conv_b = other.conversation.id

    builder = RuntimeContextBuilder(
        session_factory=session_factory,
        local_settings=_local_settings(emergency_lock=False),
        control_plane=cp,
        live_facts_remote=live,
    )
    result_a = await builder.build_for_conversation(conv_a)
    result_b = await builder.build_for_conversation(conv_b)

    assert result_a.generation_allowed is False
    assert result_a.context is not None
    assert result_a.context.conversation is not None
    texts_a = [t.text for t in result_a.context.conversation.turns]
    assert texts_a == ["Привет из диалога A", "Ответ менеджера A"]
    seqs = [
        t.conversation_event_seq for t in result_a.context.conversation.turns
    ]
    assert seqs == sorted(seqs)
    assert result_a.context.conversation.turns[0].trust is TrustBoundary.UNTRUSTED_CONVERSATION
    assert result_a.context.conversation.turns[1].trust is TrustBoundary.MANAGER_AUTHORED

    assert result_b.context is not None
    assert result_b.context.conversation is not None
    texts_b = [t.text for t in result_b.context.conversation.turns]
    assert texts_b == ["Чужой диалог B — не должен попасть"]
    assert "Привет из диалога A" not in texts_b

    # Live-facts fetched fresh each build — no durable cache row.
    assert live.fetch_count == 2
    async with session_factory() as session:
        async with session.begin():
            kinds = [
                row.kind
                for row in await snapshot_repo.list_all(session)
            ]
    assert "LIVE_FACTS" not in kinds
    assert set(kinds) <= {"SETTINGS", "KNOWLEDGE"}


@pytest.mark.asyncio
async def test_runtime_context_history_bounded_and_durable_across_rebuild(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cp = await _seed_control_plane(session_factory)
    live = _StaticLiveFacts()
    builder = RuntimeContextBuilder(
        session_factory=session_factory,
        local_settings=_local_settings(emergency_lock=False),
        control_plane=cp,
        live_facts_remote=live,
    )

    async with session_factory() as session:
        async with session.begin():
            first = await InboundService(session).accept(
                SyntheticInboundEvent(
                    external_conversation_id="ctx-bound",
                    external_message_id="seed",
                    text="seed",
                )
            )
            conv_id = first.conversation.id
            for i in range(45):
                await InboundService(session).accept(
                    SyntheticInboundEvent(
                        external_conversation_id="ctx-bound",
                        external_message_id=f"msg-{i}",
                        text=f"turn-{i}",
                    )
                )

    result = await builder.build_for_conversation(conv_id)
    assert result.context is not None
    assert result.context.conversation is not None
    assert result.context.conversation.turn_count <= HARD_MAX_HISTORY_TURNS
    texts = [t.text for t in result.context.conversation.turns]
    # Newest window retained; early seed / early turns dropped under overflow.
    assert "seed" not in texts
    assert "turn-0" not in texts
    assert texts[-1] == "turn-44"
    assert all(t.startswith("turn-") for t in texts)

    # Restart-style: new builder, same durable history.
    cp2 = await _seed_control_plane(session_factory)
    builder2 = RuntimeContextBuilder(
        session_factory=session_factory,
        local_settings=_local_settings(emergency_lock=False),
        control_plane=cp2,
        live_facts_remote=_StaticLiveFacts(),
    )
    result2 = await builder2.build_for_conversation(conv_id)
    assert result2.context is not None
    assert result2.context.conversation is not None
    assert (
        result2.context.conversation.turn_count
        == result.context.conversation.turn_count
    )
    assert [t.text for t in result2.context.conversation.turns] == texts


@pytest.mark.asyncio
async def test_runtime_context_live_facts_failure_fail_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cp = await _seed_control_plane(session_factory)
    async with session_factory() as session:
        async with session.begin():
            first = await InboundService(session).accept(
                SyntheticInboundEvent(
                    external_conversation_id="ctx-fail",
                    external_message_id="c1",
                    text="hello",
                )
            )
            conv_id = first.conversation.id

    builder = RuntimeContextBuilder(
        session_factory=session_factory,
        local_settings=_local_settings(emergency_lock=False),
        control_plane=cp,
        live_facts_remote=_FailLiveFacts(LiveFactsFetchCode.UNAVAILABLE),
    )
    result = await builder.build_for_conversation(conv_id)
    assert result.readiness is RuntimeContextReadiness.NOT_READY
    assert RuntimeContextReason.LIVE_FACTS_UNAVAILABLE in result.reasons
    assert result.generation_allowed is False
    assert result.context is not None
    assert result.context.live_facts is None


@pytest.mark.asyncio
async def test_runtime_context_emergency_lock_blocks_readiness_keeps_context(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cp = await _seed_control_plane(session_factory)
    async with session_factory() as session:
        async with session.begin():
            first = await InboundService(session).accept(
                SyntheticInboundEvent(
                    external_conversation_id="ctx-lock",
                    external_message_id="c1",
                    text="hello",
                )
            )
            conv_id = first.conversation.id

    builder = RuntimeContextBuilder(
        session_factory=session_factory,
        local_settings=_local_settings(emergency_lock=True),
        control_plane=cp,
        live_facts_remote=_StaticLiveFacts(),
    )
    result = await builder.build_for_conversation(conv_id)
    assert result.readiness is RuntimeContextReadiness.NOT_READY
    assert RuntimeContextReason.EMERGENCY_LOCK_ACTIVE in result.reasons
    assert result.generation_allowed is False
    assert result.context is not None
    assert result.context.live_facts is not None
