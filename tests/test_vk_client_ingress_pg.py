"""VK CLIENT shadow ingress PostgreSQL integration proof."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.vk_client_config import VkClientCallbackConfig
from app.channels.vk_client_http import handle_vk_client_callback
from app.models.amocrm_mirror import AmoCrmMirrorJob
from app.models.amocrm_message_projection import AmocrmMessageProjection
from app.models.conversation import Conversation
from app.models.inbox import InboxMessage
from app.models.ingress import IngressEvent, IngressStatus
from app.models.outbox import OutboxMessage
from app.models.reply_plan import ReplyPlan
from app.repositories.ingress import IngressClaim
from app.schemas.vk_client_ingress import VkClientIngressEvent
from app.services.ingress import IngressWorker
from app.services.vk_client_ingress import VkClientIngressAdapter
from tests.pg_harness import truncate_foundation_tables

_GROUP = 303030
_SECRET = "vk-client-pg-secret-xx"
_CONFIRM = "vk-client-pg-confirm"
_USER = 7002002
_CMID = 501
_NOW_TS = 1723200000


def _cfg() -> VkClientCallbackConfig:
    return VkClientCallbackConfig(
        enabled=True,
        group_id=_GROUP,
        callback_secret=_SECRET,
        confirmation=_CONFIRM,
    )


def _payload(*, cmid: int = _CMID, text: str = "Хочу записаться на стрижку") -> dict[str, Any]:
    return {
        "type": "message_new",
        "group_id": _GROUP,
        "secret": _SECRET,
        "object": {
            "message": {
                "id": 11,
                "date": _NOW_TS,
                "from_id": _USER,
                "peer_id": _USER,
                "out": 0,
                "text": text,
                "conversation_message_id": cmid,
            }
        },
    }


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
async def test_vk_client_callback_durable_ingress_and_worker_shadow(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = VkClientIngressAdapter(session_factory)
    http = await handle_vk_client_callback(
        _payload(),
        config=_cfg(),
        adapter=adapter,
    )
    assert http.body == "ok"
    assert http.status_code == 200

    async with session_factory() as session:
        row = await session.scalar(select(IngressEvent))
        assert row is not None
        assert row.channel == "vk"
        assert row.event_type == "VK_CLIENT_MESSAGE"
        assert row.external_event_id == f"vk-{_GROUP}-{_USER}-{_CMID}"
        assert row.external_conversation_id == f"vk-{_GROUP}-{_USER}"
        assert row.status == IngressStatus.RECEIVED.value
        assert "Хочу" not in repr(row)

    # Idempotent Callback retry → same row, still ACK.
    http2 = await handle_vk_client_callback(
        _payload(),
        config=_cfg(),
        adapter=adapter,
    )
    assert http2.body == "ok"
    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(IngressEvent))
        assert count == 1

    worker = IngressWorker(session_factory, worker_id="vk-client-pg-worker")
    shadow_calls: list[object] = []

    async def _shadow(**kwargs: object) -> None:
        shadow_calls.append(kwargs)

    # First claim/process → new conversation/inbox, no CRM artifacts.
    claim1 = await worker.claim_one()
    assert claim1 is not None
    assert isinstance(claim1, IngressClaim)
    assert claim1.channel == "vk"
    result1 = await worker.process_claimed(claim1)
    assert result1.duplicate_business is False
    assert result1.conversation_id is not None
    assert result1.inbox_id is not None
    assert result1.outbox_id is None

    # Simulate post-ingress worker condition.
    if (
        result1.conversation_id is not None
        and result1.inbox_id is not None
        and not result1.duplicate_business
    ):
        await _shadow(conversation_id=result1.conversation_id)

    async with session_factory() as session:
        conv = await session.get(Conversation, result1.conversation_id)
        assert conv is not None
        assert conv.channel == "vk"
        inbox = await session.get(InboxMessage, result1.inbox_id)
        assert inbox is not None
        assert inbox.channel == "vk"
        assert inbox.payload_json.get("text") == "Хочу записаться на стрижку"

        mirror_n = await session.scalar(
            select(func.count()).select_from(AmoCrmMirrorJob)
        )
        proj_n = await session.scalar(
            select(func.count()).select_from(AmocrmMessageProjection)
        )
        outbox_n = await session.scalar(select(func.count()).select_from(OutboxMessage))
        plan_n = await session.scalar(select(func.count()).select_from(ReplyPlan))
        assert mirror_n == 0
        assert proj_n == 0
        assert outbox_n == 0
        assert plan_n == 0

    # Re-accept same message (already PROCESSED ingress) — no second row.
    # Insert a fresh duplicate-business scenario via second process is N/A once
    # PROCESSED; instead re-run inbound accept path by creating a second claim
    # is impossible. Prove duplicate_business via direct second inbound accept.
    from app.services.vk_client_inbound import VkClientInboundService
    from app.db.session import session_scope

    async with session_scope(session_factory) as session:
        again = await VkClientInboundService(session).accept(
            VkClientIngressEvent(
                external_event_id=f"vk-{_GROUP}-{_USER}-{_CMID}",
                external_conversation_id=f"vk-{_GROUP}-{_USER}",
                text="Хочу записаться на стрижку",
            ).to_inbound()
        )
        assert again.duplicate is True
        if (
            again.conversation.id is not None
            and again.inbox.id is not None
            and not again.duplicate
        ):
            await _shadow(conversation_id=again.conversation.id)

    assert len(shadow_calls) == 1


@pytest.mark.asyncio
async def test_vk_client_no_master_command_side_effects(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = VkClientIngressAdapter(session_factory)
    with patch(
        "app.services.vk_master_adapter.VkMasterAdapterService.handle_callback",
        new_callable=AsyncMock,
    ) as master_handle:
        await handle_vk_client_callback(
            _payload(cmid=502),
            config=_cfg(),
            adapter=adapter,
        )
        master_handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_text_no_durable_ingress_no_pii(
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.channels.vk_client_types import VK_CLIENT_TEXT_MAX_LEN

    marker = "PII_MARKER_PG_OVERSIZE_qq11"
    oversized = marker + ("Z" * VK_CLIENT_TEXT_MAX_LEN)
    adapter = VkClientIngressAdapter(session_factory)
    with caplog.at_level("DEBUG"):
        http = await handle_vk_client_callback(
            _payload(cmid=601, text=oversized),
            config=_cfg(),
            adapter=adapter,
        )
    assert http.body == "ok"
    assert http.status_code == 200
    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(IngressEvent))
        assert count == 0
    joined = "\n".join(
        f"{rec.getMessage()}\n{rec.exc_text or ''}" for rec in caplog.records
    )
    assert marker not in joined


@pytest.mark.asyncio
async def test_altered_body_conflict_preserves_envelope_no_second_shadow(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = VkClientIngressAdapter(session_factory)
    original = "original durable body"
    http1 = await handle_vk_client_callback(
        _payload(cmid=701, text=original),
        config=_cfg(),
        adapter=adapter,
    )
    assert http1.body == "ok"

    async with session_factory() as session:
        row = await session.scalar(select(IngressEvent))
        assert row is not None
        assert row.envelope_json.get("text") == original
        event_id = row.id

    http2 = await handle_vk_client_callback(
        _payload(cmid=701, text="mutated durable body"),
        config=_cfg(),
        adapter=adapter,
    )
    assert http2.status_code == 409
    assert http2.body == "conflict"

    async with session_factory() as session:
        rows = (await session.scalars(select(IngressEvent))).all()
        assert len(rows) == 1
        assert rows[0].id == event_id
        assert rows[0].envelope_json.get("text") == original

    # Conflict leaves a single RECEIVED row; one worker process → one shadow.
    worker = IngressWorker(session_factory, worker_id="vk-client-pg-conflict")
    shadow_calls: list[object] = []
    claim = await worker.claim_one()
    assert claim is not None
    result = await worker.process_claimed(claim)
    if (
        result.conversation_id is not None
        and result.inbox_id is not None
        and not result.duplicate_business
    ):
        shadow_calls.append({"conversation_id": result.conversation_id})

    # No second claimable event from the conflicted Callback.
    assert await worker.claim_one() is None
    assert len(shadow_calls) == 1

    async with session_factory() as session:
        inbox = await session.get(InboxMessage, result.inbox_id)
        assert inbox is not None
        assert inbox.payload_json.get("text") == original
