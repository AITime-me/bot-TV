"""AMO-01A PostgreSQL: durable amoCRM manager ingress → existing FSM."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.amocrm_chat_webhook import AMOCRM_CHAT_WEBHOOK_PATH, build_amocrm_chat_router
from app.core.amocrm_chat_config import AmoCrmChatConfig
from app.core.amocrm_manager_ids import amocrm_manager_namespaced_id
from app.db.clock import db_statement_now
from app.db.session import session_scope
from app.models.amocrm_chat_binding import AmocrmChatBindingStatus
from app.models.conversation import (
    Conversation,
    HandoffState,
    conversation_allows_automatic_reply,
)
from app.models.inbox import InboxMessage
from app.models.ingress import IngressEvent, IngressStatus
from app.models.manager_message import ManagerMessage, ManagerMessageStatus
from app.models.outbox import DeliveryStatus, OutboxMessage
from app.models.reply_plan import ReplyPlan, ReplyPlanStatus
from app.repositories import amocrm_chat_bindings as binding_repo
from app.schemas.amocrm_manager_ingress import AmoCrmManagerIngressEvent
from app.schemas.inbound import SyntheticInboundEvent
from app.schemas.manager_message import SyntheticManagerMessageEvent
from app.services.amocrm_manager_ingress import (
    AmoCrmManagerIngressAdapter,
    IngressIdempotencyConflict,
)
from app.services.handoff_expiry import HandoffExpiryWorker
from app.services.inbound import InboundService
from app.services.ingress import IngressWorker
from app.services.manager_messages import SyntheticManagerMessageService
from tests.pg_harness import truncate_foundation_tables

_SECRET = "t" * 32


@pytest_asyncio.fixture(autouse=True)
async def amo_ingress_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


def _sign(body: bytes) -> str:
    return hmac.new(_SECRET.encode("utf-8"), body, hashlib.sha1).hexdigest()


def _mgr_event(
    *,
    chat_id: str = "amo-chat-pg-1",
    message_id: str = "amo-mgr-1",
    provider_sequence: int = 10,
    text: str = "manager takeover text",
) -> AmoCrmManagerIngressEvent:
    namespaced = amocrm_manager_namespaced_id(
        amocrm_chat_id=chat_id,
        amocrm_message_id=message_id,
    )
    return AmoCrmManagerIngressEvent(
        amocrm_chat_id=chat_id,
        amocrm_message_id=message_id,
        external_message_id=namespaced,
        provider_sequence=provider_sequence,
        text=text,
    )


async def _seed_bound_conversation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    chat_id: str = "amo-chat-pg-1",
    external_conversation_id: str = "synth-amo-conv-1",
) -> Conversation:
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=external_conversation_id,
                external_message_id=f"client-{uuid4().hex[:12]}",
                text="client seed",
            )
        )
        conversation = accepted.conversation
        await binding_repo.insert_active_if_absent(
            session,
            conversation_id=conversation.id,
            amocrm_chat_id=chat_id,
        )
        return conversation


@pytest.mark.asyncio
async def test_duplicate_webhook_one_durable_one_applied(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_bound_conversation(session_factory)
    adapter = AmoCrmManagerIngressAdapter(session_factory)
    event = _mgr_event(message_id="amo-mgr-dup-1")
    first = await adapter.accept(event)
    second = await adapter.accept(event)
    assert first.event_id == second.event_id
    assert first.duplicate is False
    assert second.duplicate is True

    worker = IngressWorker(session_factory, worker_id="amo-worker-1")
    claim = await worker.claim_one()
    assert claim is not None
    result = await worker.process_claimed(claim)
    assert result.status == IngressStatus.PROCESSED.value
    assert result.duplicate_business is False

    claim2 = await worker.claim_one()
    assert claim2 is None

    namespaced = event.external_message_id
    async with session_factory() as session:
        async with session.begin():
            ingress_count = await session.scalar(
                select(func.count()).select_from(IngressEvent).where(
                    IngressEvent.channel == "amocrm",
                    IngressEvent.external_event_id == namespaced,
                )
            )
            assert ingress_count == 1
            applied = await session.scalar(
                select(func.count()).select_from(ManagerMessage).where(
                    ManagerMessage.conversation_id == conversation.id,
                    ManagerMessage.status == ManagerMessageStatus.APPLIED.value,
                    ManagerMessage.external_message_id == namespaced,
                )
            )
            assert applied == 1
            conv = await session.get(Conversation, conversation.id)
            assert conv is not None
            assert conv.handoff_state == HandoffState.HUMAN_ACTIVE.value
            assert conv.manager_epoch == 1
            assert conv.handoff_deadline_at is not None


@pytest.mark.asyncio
async def test_same_message_id_different_chat_are_distinct(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_bound_conversation(session_factory, chat_id="amo-chat-a")
    await _seed_bound_conversation(
        session_factory,
        chat_id="amo-chat-b",
        external_conversation_id="synth-amo-conv-b",
    )
    adapter = AmoCrmManagerIngressAdapter(session_factory)
    first = await adapter.accept(
        _mgr_event(chat_id="amo-chat-a", message_id="shared-msg")
    )
    second = await adapter.accept(
        _mgr_event(chat_id="amo-chat-b", message_id="shared-msg")
    )
    assert first.event_id != second.event_id
    assert first.duplicate is False
    assert second.duplicate is False

    async with session_factory() as session:
        async with session.begin():
            keys = set(
                await session.scalars(
                    select(IngressEvent.external_event_id).where(
                        IngressEvent.channel == "amocrm"
                    )
                )
            )
            assert keys == {
                "amo:amo-chat-a:shared-msg",
                "amo:amo-chat-b:shared-msg",
            }


@pytest.mark.asyncio
async def test_same_key_altered_body_conflicts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_bound_conversation(session_factory)
    adapter = AmoCrmManagerIngressAdapter(session_factory)
    await adapter.accept(_mgr_event(message_id="amo-mgr-body", text="original-body"))
    with pytest.raises(IngressIdempotencyConflict):
        await adapter.accept(
            _mgr_event(message_id="amo-mgr-body", text="mutated-body")
        )

    async with session_factory() as session:
        async with session.begin():
            count = await session.scalar(
                select(func.count()).select_from(IngressEvent).where(
                    IngressEvent.channel == "amocrm"
                )
            )
            assert count == 1
            row = (
                await session.scalars(
                    select(IngressEvent).where(IngressEvent.channel == "amocrm")
                )
            ).one()
            assert row.envelope_json["text"] == "original-body"


@pytest.mark.asyncio
async def test_synthetic_manager_raw_id_does_not_collide_with_amo(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_bound_conversation(session_factory)
    raw_id = "amo-raw-collision"
    synth = SyntheticManagerMessageService(session_factory)
    applied = await synth.apply(
        SyntheticManagerMessageEvent(
            external_conversation_id=conversation.external_conversation_id,
            external_message_id=raw_id,
            provider_sequence=1,
            text="synthetic manager first",
        )
    )
    assert applied.duplicate is False

    adapter = AmoCrmManagerIngressAdapter(session_factory)
    await adapter.accept(_mgr_event(message_id=raw_id, provider_sequence=2))
    worker = IngressWorker(session_factory, worker_id="amo-worker-collision")
    claim = await worker.claim_one()
    assert claim is not None
    result = await worker.process_claimed(claim)
    assert result.duplicate_business is False

    async with session_factory() as session:
        async with session.begin():
            rows = (
                await session.scalars(
                    select(ManagerMessage).where(
                        ManagerMessage.conversation_id == conversation.id
                    )
                )
            ).all()
            ids = {row.external_message_id for row in rows}
            assert raw_id in ids
            assert f"amo:amo-chat-pg-1:{raw_id}" in ids
            assert len(ids) == 2


@pytest.mark.asyncio
async def test_invalid_amocrm_synthetic_message_pairing_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    before_conversations = 0
    async with session_factory() as session:
        async with session.begin():
            before_conversations = await session.scalar(
                select(func.count()).select_from(Conversation)
            )

    async with session_factory() as session:
        async with session.begin():
            with pytest.raises((IntegrityError, DBAPIError)):
                await session.execute(
                    text(
                        "INSERT INTO ingress_events ("
                        "id, channel, external_event_id, external_conversation_id, "
                        "event_type, status, attempt_count, max_attempts, "
                        "lease_version, correlation_id, envelope_json"
                        ") VALUES ("
                        "gen_random_uuid(), 'amocrm', 'bad-pair-1', 'chat-x', "
                        "'SYNTHETIC_MESSAGE', 'RECEIVED', 0, 5, 0, "
                        "gen_random_uuid(), CAST(:env AS jsonb)"
                        ")"
                    ),
                    {"env": json.dumps({"text": "should-not-land"})},
                )

    async with session_factory() as session:
        async with session.begin():
            ingress_count = await session.scalar(
                select(func.count()).select_from(IngressEvent)
            )
            inbox_count = await session.scalar(
                select(func.count()).select_from(InboxMessage)
            )
            conversation_count = await session.scalar(
                select(func.count()).select_from(Conversation)
            )
            assert ingress_count == 0
            assert inbox_count == 0
            assert conversation_count == before_conversations


@pytest.mark.asyncio
async def test_unknown_binding_fail_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_bound_conversation(session_factory, chat_id="amo-chat-known")
    adapter = AmoCrmManagerIngressAdapter(session_factory)
    await adapter.accept(
        _mgr_event(chat_id="amo-chat-unknown", message_id="amo-mgr-unknown-1")
    )
    worker = IngressWorker(session_factory, worker_id="amo-worker-2")
    claim = await worker.claim_one()
    assert claim is not None
    with pytest.raises(ValueError, match="BINDING_UNKNOWN"):
        await worker.process_claimed(claim)

    async with session_factory() as session:
        async with session.begin():
            row = await session.get(IngressEvent, claim.event_id)
            assert row is not None
            assert row.status in {
                IngressStatus.FAILED.value,
                IngressStatus.RECEIVED.value,
                IngressStatus.DEAD.value,
            }
            assert row.error_code == "BINDING_UNKNOWN"
            applied = await session.scalar(
                select(func.count()).select_from(ManagerMessage)
            )
            assert applied == 0


@pytest.mark.asyncio
async def test_manager_takeover_cancels_bot_work_and_arbiter_denies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_bound_conversation(session_factory)

    async with session_factory() as session:
        async with session.begin():
            open_plans = (
                await session.scalars(
                    select(ReplyPlan).where(
                        ReplyPlan.conversation_id == conversation.id,
                        ReplyPlan.status.in_(
                            [
                                ReplyPlanStatus.PENDING.value,
                                ReplyPlanStatus.READY.value,
                            ]
                        ),
                    )
                )
            ).all()
            assert open_plans

    adapter = AmoCrmManagerIngressAdapter(session_factory)
    await adapter.accept(_mgr_event(message_id="amo-mgr-takeover-1", provider_sequence=5))
    worker = IngressWorker(session_factory, worker_id="amo-worker-3")
    claim = await worker.claim_one()
    assert claim is not None
    await worker.process_claimed(claim)

    async with session_factory() as session:
        async with session.begin():
            conv = await session.get(Conversation, conversation.id)
            assert conv is not None
            assert conv.handoff_state == HandoffState.HUMAN_ACTIVE.value
            assert conversation_allows_automatic_reply(conv) is False
            cancelled_plans = await session.scalar(
                select(func.count()).select_from(ReplyPlan).where(
                    ReplyPlan.conversation_id == conversation.id,
                    ReplyPlan.status == ReplyPlanStatus.CANCELLED.value,
                )
            )
            assert cancelled_plans >= 1
            cancelled_outbound = await session.scalar(
                select(func.count()).select_from(OutboxMessage).where(
                    OutboxMessage.conversation_id == conversation.id,
                    OutboxMessage.delivery_status == DeliveryStatus.CANCELLED.value,
                )
            )
            pending = await session.scalar(
                select(func.count()).select_from(OutboxMessage).where(
                    OutboxMessage.conversation_id == conversation.id,
                    OutboxMessage.delivery_status.in_(
                        [
                            DeliveryStatus.PENDING.value,
                            DeliveryStatus.PROCESSING.value,
                        ]
                    ),
                )
            )
            assert pending == 0
            assert cancelled_outbound is not None


@pytest.mark.asyncio
async def test_fifteen_minute_expiry_same_conversation_bot_active(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_bound_conversation(session_factory)
    adapter = AmoCrmManagerIngressAdapter(session_factory)
    await adapter.accept(_mgr_event(message_id="amo-mgr-expiry-1", provider_sequence=7))
    worker = IngressWorker(session_factory, worker_id="amo-worker-4")
    claim = await worker.claim_one()
    assert claim is not None
    await worker.process_claimed(claim)

    async with session_factory() as session:
        async with session.begin():
            conv = await session.get(Conversation, conversation.id)
            assert conv is not None
            assert conv.handoff_state == HandoffState.HUMAN_ACTIVE.value
            assert conv.handoff_deadline_at is not None
            moment = await db_statement_now(session)
            due = moment - timedelta(seconds=1)
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation.id)
                .values(handoff_deadline_at=due)
            )

    expired = await HandoffExpiryWorker(session_factory).expire_one()
    assert expired is not None
    assert expired.conversation_id == conversation.id

    async with session_factory() as session:
        async with session.begin():
            conv = await session.get(Conversation, conversation.id)
            assert conv is not None
            assert conv.id == conversation.id
            assert conv.handoff_state == HandoffState.BOT_ACTIVE.value
            assert conv.handoff_deadline_at is None


@pytest.mark.asyncio
async def test_webhook_http_ack_after_commit_and_signature(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_bound_conversation(session_factory)
    config = AmoCrmChatConfig(enabled=True, channel_secret=_SECRET)
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(
        build_amocrm_chat_router(config=config, session_factory=session_factory)
    )
    body = json.dumps(
        {
            "amocrm_chat_id": "amo-chat-pg-1",
            "message_id": "amo-http-1",
            "provider_sequence": 3,
            "text": "via http",
        }
    ).encode("utf-8")
    namespaced = "amo:amo-chat-pg-1:amo-http-1"

    with TestClient(app) as client:
        unauthorized = client.post(
            AMOCRM_CHAT_WEBHOOK_PATH,
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert unauthorized.status_code == 401
        async with session_factory() as session:
            async with session.begin():
                before = await session.scalar(
                    select(func.count()).select_from(IngressEvent)
                )
        assert before == 0

        ok = client.post(
            AMOCRM_CHAT_WEBHOOK_PATH,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Signature": _sign(body),
            },
        )
        assert ok.status_code == 200
        assert ok.text == "ok"

        dup = client.post(
            AMOCRM_CHAT_WEBHOOK_PATH,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Signature": _sign(body),
            },
        )
        assert dup.status_code == 200

        mutated = json.dumps(
            {
                "amocrm_chat_id": "amo-chat-pg-1",
                "message_id": "amo-http-1",
                "provider_sequence": 3,
                "text": "via http mutated",
            }
        ).encode("utf-8")
        conflict = client.post(
            AMOCRM_CHAT_WEBHOOK_PATH,
            content=mutated,
            headers={
                "Content-Type": "application/json",
                "X-Signature": _sign(mutated),
            },
        )
        assert conflict.status_code == 409

    async with session_factory() as session:
        async with session.begin():
            count = await session.scalar(
                select(func.count()).select_from(IngressEvent).where(
                    IngressEvent.external_event_id == namespaced
                )
            )
            assert count == 1
            row = (
                await session.scalars(
                    select(IngressEvent).where(
                        IngressEvent.external_event_id == namespaced
                    )
                )
            ).one()
            assert row.status == IngressStatus.RECEIVED.value
            assert row.channel == "amocrm"
            assert row.envelope_json["amocrm_message_id"] == "amo-http-1"


@pytest.mark.asyncio
async def test_revoked_binding_fail_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_bound_conversation(
        session_factory,
        chat_id="amo-chat-revoked",
    )
    async with session_scope(session_factory) as session:
        binding = await binding_repo.get_active_by_amocrm_chat_id(
            session,
            amocrm_chat_id="amo-chat-revoked",
        )
        assert binding is not None
        binding.status = AmocrmChatBindingStatus.REVOKED.value

    adapter = AmoCrmManagerIngressAdapter(session_factory)
    await adapter.accept(
        _mgr_event(chat_id="amo-chat-revoked", message_id="amo-mgr-revoked-1")
    )
    worker = IngressWorker(session_factory, worker_id="amo-worker-5")
    claim = await worker.claim_one()
    assert claim is not None
    with pytest.raises(ValueError, match="BINDING_UNKNOWN"):
        await worker.process_claimed(claim)
    assert conversation.id is not None
