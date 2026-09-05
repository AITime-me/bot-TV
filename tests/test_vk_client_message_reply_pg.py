"""PostgreSQL: VK message_reply external takeover races and fences."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.vk_client_external_takeover_config import (
    VkClientExternalTakeoverConfig,
    VkClientExternalTakeoverMode,
)
from app.channels.vk_client_http import handle_vk_client_callback
from app.channels.vk_client_config import VkClientCallbackConfig
from app.channels.vk_client_outbound_provenance import (
    build_vk_outbound_provenance_payload,
)
from app.channels.vk_client_types import vk_client_external_conversation_id
from app.db.session import session_scope
from app.models.conversation import (
    Channel,
    ConversationOwnership,
    ConversationStatus,
    HandoffState,
)
from app.models.ingress import IngressEvent, IngressEventType, IngressStatus
from app.models.outbox import DeliveryStatus, DestinationType, OutboxMessage
from app.models.reply_plan import ReplyPlan, ReplyPlanStatus, ReplyPlanType
from app.repositories import conversations as conversation_repo
from app.repositories import outbound as outbound_repo
from app.services.ingress import IngressWorker
from app.services.vk_client_ingress import VkClientIngressAdapter
from app.services.vk_client_message_reply import (
    VkClientReplyClassification,
    apply_vk_client_message_reply_in_session,
)
from tests.pg_harness import truncate_foundation_tables

_GROUP = 154387737
_USER = 145508039
_CONV = vk_client_external_conversation_id(group_id=_GROUP, user_id=_USER)
_SECRET = "vk-reply-pg-secret-xx"
_CONFIRM = "vk-reply-pg-confirm"
_KEY = _SECRET
_NOW_TS = 1725530000


def _callback_cfg() -> VkClientCallbackConfig:
    return VkClientCallbackConfig(
        enabled=True,
        group_id=_GROUP,
        callback_secret=_SECRET,
        confirmation=_CONFIRM,
    )


def _takeover_all() -> VkClientExternalTakeoverConfig:
    return VkClientExternalTakeoverConfig(
        mode=VkClientExternalTakeoverMode.ALL,
        provenance_key=_KEY,
    )


def _reply_callback(
    *,
    cmid: int,
    provider_id: int,
    payload: object | None = None,
    random_id: int = 0,
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "id": provider_id,
        "date": _NOW_TS,
        "peer_id": _USER,
        "from_id": -_GROUP,
        "out": 1,
        "conversation_message_id": cmid,
        "random_id": random_id,
        "text": "SECRET_MANAGER_BODY",
    }
    if payload is not None:
        obj["payload"] = payload
    return {
        "type": "message_reply",
        "group_id": _GROUP,
        "secret": _SECRET,
        "object": obj,
    }


@pytest.mark.asyncio
async def test_pg_external_reply_takeover_and_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await truncate_foundation_tables(session_factory)
    monkeypatch.setenv("VK_CLIENT_EXTERNAL_TAKEOVER_MODE", "ALL")
    monkeypatch.setenv("VK_CLIENT_CALLBACK_SECRET", _KEY)

    async with session_scope(session_factory) as session:
        conversation, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.VK,
            external_conversation_id=_CONV,
        )
        plan = ReplyPlan(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            status=ReplyPlanStatus.PENDING.value,
            plan_type=ReplyPlanType.CLIENT_REPLY.value,
            context_version=conversation.context_version,
            manager_epoch=conversation.manager_epoch,
            event_seq_hwm=0,
            not_before=datetime.now(timezone.utc),
            payload_json={"schema": "test.v1"},
        )
        session.add(plan)
        out = OutboxMessage(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            destination_type=DestinationType.VK_CLIENT_OUTBOUND.value,
            delivery_status=DeliveryStatus.PENDING.value,
            payload_json={"schema": "vk.client.outbound.v1", "text": "x"},
            context_version=conversation.context_version,
            manager_epoch=conversation.manager_epoch,
            event_seq_hwm=0,
        )
        session.add(out)
        conv_id = conversation.id

    adapter = VkClientIngressAdapter(session_factory)
    http = await handle_vk_client_callback(
        _reply_callback(cmid=3639, provider_id=82727, payload={"known_event": True}),
        config=_callback_cfg(),
        adapter=adapter,
    )
    assert http.status_code == 200

    worker = IngressWorker(session_factory, worker_id="vk-reply-pg")
    claim = await worker.claim_one()
    assert claim is not None
    assert claim.event_type == IngressEventType.VK_CLIENT_MESSAGE_REPLY.value
    assert "SECRET_MANAGER_BODY" not in json.dumps(claim.envelope_json)
    await worker.process_claimed(claim)

    async with session_scope(session_factory) as session:
        conversation = await conversation_repo.get_by_id_for_update(
            session, conversation_id=conv_id
        )
        assert conversation is not None
        assert conversation.ownership == ConversationOwnership.MANAGER.value
        assert conversation.status == ConversationStatus.HANDOFF.value
        assert conversation.handoff_state == HandoffState.HUMAN_ACTIVE.value
        assert conversation.vk_client_external_reply_hwm == 3639
        assert conversation.manager_takeover_at is not None
        assert conversation.handoff_deadline_at is not None
        assert conversation.active_reply_plan_id is None
        open_plans = await session.scalar(
            select(func.count())
            .select_from(ReplyPlan)
            .where(
                ReplyPlan.conversation_id == conv_id,
                ReplyPlan.status == ReplyPlanStatus.PENDING.value,
            )
        )
        assert open_plans == 0
        pending = await session.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .where(
                OutboxMessage.conversation_id == conv_id,
                OutboxMessage.delivery_status == DeliveryStatus.PENDING.value,
            )
        )
        assert pending == 0
        cancelled = await session.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .where(
                OutboxMessage.conversation_id == conv_id,
                OutboxMessage.delivery_status == DeliveryStatus.CANCELLED.value,
            )
        )
        assert cancelled == 1

    # Duplicate callback → one logical effect.
    http2 = await handle_vk_client_callback(
        _reply_callback(cmid=3639, provider_id=82727, payload={"known_event": True}),
        config=_callback_cfg(),
        adapter=adapter,
    )
    assert http2.status_code == 200
    claim2 = await worker.claim_one()
    assert claim2 is None

    async with session_scope(session_factory) as session:
        conversation = await conversation_repo.lock_for_update(
            session, conversation_id=conv_id
        )
        epoch = conversation.manager_epoch
        hwm = conversation.vk_client_external_reply_hwm

    # Stale/reordered older cmid must not mutate.
    async with session_scope(session_factory) as session:
        result = await apply_vk_client_message_reply_in_session(
            session,
            envelope={
                "external_conversation_id": _CONV,
                "provider_message_id": 1,
                "conversation_message_id": 100,
                "payload": {"known_event": True},
            },
            handoff_pause_seconds=900,
            takeover_config=_takeover_all(),
        )
        assert result.classification is VkClientReplyClassification.STALE
        assert result.fsm_changed is False
        conversation = await conversation_repo.lock_for_update(
            session, conversation_id=conv_id
        )
        assert conversation.manager_epoch == epoch
        assert conversation.vk_client_external_reply_hwm == hwm

    # Newer external reply refreshes handoff.
    async with session_scope(session_factory) as session:
        result = await apply_vk_client_message_reply_in_session(
            session,
            envelope={
                "external_conversation_id": _CONV,
                "provider_message_id": 999001,
                "conversation_message_id": 3640,
                "payload": {"known_event": True},
            },
            handoff_pause_seconds=900,
            takeover_config=_takeover_all(),
        )
        assert result.classification is VkClientReplyClassification.EXTERNAL_ACTOR
        assert result.fsm_changed is True
        conversation = await conversation_repo.lock_for_update(
            session, conversation_id=conv_id
        )
        assert conversation.vk_client_external_reply_hwm == 3640
        assert conversation.manager_epoch == epoch + 1


@pytest.mark.asyncio
async def test_pg_own_echo_via_provenance_and_provider_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await truncate_foundation_tables(session_factory)
    outbound_id = uuid.uuid4()
    provider_id = 555001

    async with session_scope(session_factory) as session:
        conversation, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.VK,
            external_conversation_id=_CONV,
        )
        session.add(
            OutboxMessage(
                id=outbound_id,
                conversation_id=conversation.id,
                destination_type=DestinationType.VK_CLIENT_OUTBOUND.value,
                delivery_status=DeliveryStatus.DELIVERED.value,
                admitted_at=datetime.now(timezone.utc),
                provider_message_id=provider_id,
                payload_json={"schema": "vk.client.outbound.v1", "text": "x"},
                context_version=0,
                manager_epoch=0,
                event_seq_hwm=0,
            )
        )
        conv_id = conversation.id

    marker = json.loads(
        build_vk_outbound_provenance_payload(
            outbound_id=outbound_id,
            provenance_key=_KEY,
        )
    )
    async with session_scope(session_factory) as session:
        result = await apply_vk_client_message_reply_in_session(
            session,
            envelope={
                "external_conversation_id": _CONV,
                "provider_message_id": 1,
                "conversation_message_id": 5000,
                "payload": marker,
            },
            handoff_pause_seconds=900,
            takeover_config=_takeover_all(),
        )
        assert result.classification is VkClientReplyClassification.OWN_TEYA_ECHO
        conversation = await conversation_repo.lock_for_update(
            session, conversation_id=conv_id
        )
        assert conversation.ownership == ConversationOwnership.BOT.value

    async with session_scope(session_factory) as session:
        result = await apply_vk_client_message_reply_in_session(
            session,
            envelope={
                "external_conversation_id": _CONV,
                "provider_message_id": provider_id,
                "conversation_message_id": 5001,
                "payload": None,
            },
            handoff_pause_seconds=900,
            takeover_config=_takeover_all(),
        )
        assert result.classification is VkClientReplyClassification.OWN_TEYA_ECHO


@pytest.mark.asyncio
async def test_pg_closed_and_missing_and_feature_off(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await truncate_foundation_tables(session_factory)

    async with session_scope(session_factory) as session:
        conversation, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.VK,
            external_conversation_id=_CONV,
        )
        conversation.status = ConversationStatus.CLOSED.value
        await session.flush()
        conv_id = conversation.id

    async with session_scope(session_factory) as session:
        result = await apply_vk_client_message_reply_in_session(
            session,
            envelope={
                "external_conversation_id": _CONV,
                "provider_message_id": 10,
                "conversation_message_id": 10,
                "payload": {"known_event": True},
            },
            handoff_pause_seconds=900,
            takeover_config=_takeover_all(),
        )
        assert result.classification is VkClientReplyClassification.CONVERSATION_CLOSED
        conversation = await conversation_repo.lock_for_update(
            session, conversation_id=conv_id
        )
        assert conversation.status == ConversationStatus.CLOSED.value

    async with session_scope(session_factory) as session:
        result = await apply_vk_client_message_reply_in_session(
            session,
            envelope={
                "external_conversation_id": "vk-1-2",
                "provider_message_id": 10,
                "conversation_message_id": 10,
                "payload": {"known_event": True},
            },
            handoff_pause_seconds=900,
            takeover_config=_takeover_all(),
        )
        assert (
            result.classification
            is VkClientReplyClassification.UNRESOLVED_CONVERSATION
        )
        assert await session.scalar(select(func.count()).select_from(IngressEvent)) == 0

    off = VkClientExternalTakeoverConfig(mode=VkClientExternalTakeoverMode.OFF)
    async with session_scope(session_factory) as session:
        result = await apply_vk_client_message_reply_in_session(
            session,
            envelope={
                "external_conversation_id": _CONV,
                "provider_message_id": 11,
                "conversation_message_id": 11,
                "payload": {"known_event": True},
            },
            handoff_pause_seconds=900,
            takeover_config=off,
        )
        assert result.classification is VkClientReplyClassification.FEATURE_OFF
