"""PostgreSQL proofs for VK_CLIENT_OUTBOUND closed single-conversation transport."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.vk_client_http import handle_vk_client_callback
from app.channels.vk_client_config import VkClientCallbackConfig
from app.channels.vk_client_outbound_config import VkClientOutboundConfig
from app.channels.vk_client_outbound_http import (
    VkClientSendOutcome,
    VkClientSendResult,
    vk_client_random_id_from_outbound_id,
)
from app.config import BotMode, Settings
from app.db.session import session_scope
from app.models.amocrm_message_projection import AmocrmMessageProjection
from app.models.amocrm_mirror import AmoCrmMirrorJob, AmoCrmMirrorJobType
from app.models.outbox import DeliveryStatus, DestinationType, OutboxMessage
from app.models.reply_plan import ReplyPlan, ReplyPlanStatus
from app.repositories import outbound as outbound_repo
from app.services.ingress import IngressWorker
from app.services.outbound_arbiter import OutboundArbiter, OutboundArbiterDenied
from app.services.reply_outbound import OutboundWorker, ReplyPlanWorker
from app.services.synthetic_outbound import SyntheticOutboundAdapter
from app.services.vk_client_ingress import VkClientIngressAdapter
from tests.pg_harness import truncate_foundation_tables

_GROUP = 404040
_SECRET = "vk-client-out-pg-secret"
_CONFIRM = "vk-client-out-pg-confirm"
_USER = 8003003
_ALLOW = f"vk-{_GROUP}-{_USER}"
_TRIGGER = "CLOSED_PROOF_TRIGGER"
_REPLY = "CLOSED_PROOF_REPLY_OK"


def _callback_cfg() -> VkClientCallbackConfig:
    return VkClientCallbackConfig(
        enabled=True,
        group_id=_GROUP,
        callback_secret=_SECRET,
        confirmation=_CONFIRM,
    )


def _outbound_cfg() -> VkClientOutboundConfig:
    return VkClientOutboundConfig.from_env(
        {
            "VK_CLIENT_OUTBOUND_ENABLED": "true",
            "VK_CLIENT_ACCESS_TOKEN": "y" * 20,
            "VK_CLIENT_OUTBOUND_ALLOW_CONVERSATION": _ALLOW,
            "VK_CLIENT_OUTBOUND_PROOF_ENABLED": "true",
            "VK_CLIENT_OUTBOUND_PROOF_TRIGGER": _TRIGGER,
            "VK_CLIENT_OUTBOUND_PROOF_REPLY": _REPLY,
            "VK_CLIENT_GROUP_ID": str(_GROUP),
        }
    )


def _settings() -> Settings:
    return Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=False)


def _payload(*, cmid: int, text: str) -> dict[str, Any]:
    return {
        "type": "message_new",
        "group_id": _GROUP,
        "secret": _SECRET,
        "object": {
            "message": {
                "id": cmid,
                "date": 1723200000,
                "from_id": _USER,
                "peer_id": _USER,
                "out": 0,
                "text": text,
                "conversation_message_id": cmid,
            }
        },
    }


class _RecordingVkSender:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.outcome = VkClientSendOutcome.SUCCESS
        self.error_code: str | None = None

    def send_text(self, *, peer_id: int, text: str, outbound_id: object) -> VkClientSendResult:
        self.calls.append(
            {
                "peer_id": peer_id,
                "text": text,
                "outbound_id": outbound_id,
                "random_id": vk_client_random_id_from_outbound_id(outbound_id),  # type: ignore[arg-type]
            }
        )
        return VkClientSendResult(outcome=self.outcome, error_code=self.error_code)


@pytest_asyncio.fixture(autouse=True)
async def cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


async def _ingest_and_process(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cmid: int,
    text: str,
    settings: Settings | None = None,
    vk_cfg: VkClientOutboundConfig | None = None,
) -> None:
    adapter = VkClientIngressAdapter(session_factory)
    http = await handle_vk_client_callback(
        _payload(cmid=cmid, text=text),
        config=_callback_cfg(),
        adapter=adapter,
    )
    assert http.body == "ok"
    worker = IngressWorker(
        session_factory,
        worker_id=f"vk-out-ingress-{cmid}",
        settings=settings if settings is not None else _settings(),
        vk_outbound_config=vk_cfg if vk_cfg is not None else _outbound_cfg(),
    )
    claim = await worker.claim_one()
    assert claim is not None
    await worker.process_claimed(claim)


@pytest.mark.asyncio
async def test_proof_trigger_creates_one_vk_outbound_and_delivers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _ingest_and_process(session_factory, cmid=701, text=_TRIGGER)

    async with session_scope(session_factory) as session:
        plans = (await session.scalars(select(ReplyPlan))).all()
        assert len(plans) == 1
        plan = plans[0]
        assert plan.payload_json["destination"] == "VK_CLIENT_OUTBOUND"
        due = plan.not_before + timedelta(seconds=1)

    plan_worker = ReplyPlanWorker(session_factory, worker_id="vk-plan")
    plan_claim = await plan_worker.claim_one(now=due)
    assert plan_claim is not None
    dispatched = await plan_worker.dispatch_claimed(plan_claim)
    assert dispatched.plan_status == ReplyPlanStatus.DISPATCHED.value

    async with session_scope(session_factory) as session:
        outs = (await session.scalars(select(OutboxMessage))).all()
        assert len(outs) == 1
        assert outs[0].destination_type == DestinationType.VK_CLIENT_OUTBOUND.value
        assert outs[0].payload_json["text"] == _REPLY
        outbound_id = outs[0].id

    sender = _RecordingVkSender()
    arbiter = OutboundArbiter(
        session_factory,
        settings=_settings(),
        sink=SyntheticOutboundAdapter(),
        vk_config=_outbound_cfg(),
        vk_sender=sender,  # type: ignore[arg-type]
    )
    out_worker = OutboundWorker(
        session_factory, worker_id="vk-out", arbiter=arbiter
    )
    out_claim = await out_worker.claim_one(now=due)
    assert out_claim is not None
    assert out_claim.destination_type == DestinationType.VK_CLIENT_OUTBOUND.value
    # Claim boundary must yield stdlib UUID (not asyncpg pgproto subclass).
    assert type(out_claim.outbound_id) is UUID
    admit = await out_worker.process_claimed(out_claim, now=due)
    assert admit.admitted is True
    assert admit.delivery_status == DeliveryStatus.DELIVERED.value
    assert len(sender.calls) == 1
    assert sender.calls[0]["peer_id"] == _USER
    assert sender.calls[0]["text"] == _REPLY
    assert sender.calls[0]["outbound_id"] == outbound_id
    # Expected random_id from claim boundary UUID (stdlib), never raw ORM id.
    assert sender.calls[0]["random_id"] == vk_client_random_id_from_outbound_id(
        out_claim.outbound_id
    )

    async with session_scope(session_factory) as session:
        proj_n = await session.scalar(
            select(func.count()).select_from(AmocrmMessageProjection)
        )
        assert proj_n == 0
        mirror_kinds = set(
            await session.scalars(select(AmoCrmMirrorJob.job_type))
        )
        assert AmoCrmMirrorJobType.OUTBOUND_DELIVERED_META.value in mirror_kinds
        bot_proj = await session.scalar(
            select(func.count())
            .select_from(AmocrmMessageProjection)
            .where(AmocrmMessageProjection.source_kind == "BOT_OUTBOUND")
        )
        assert bot_proj == 0


@pytest.mark.asyncio
async def test_wrong_text_or_disabled_creates_no_reply_plan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _ingest_and_process(session_factory, cmid=702, text="ordinary message")
    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(ReplyPlan)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(OutboxMessage)) == 0
        )

    await _ingest_and_process(
        session_factory,
        cmid=703,
        text=_TRIGGER,
        settings=Settings(bot_mode=BotMode.AUTO_READ, emergency_lock=False),
    )
    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(ReplyPlan)) == 0


@pytest.mark.asyncio
async def test_duplicate_callback_no_second_plan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = VkClientIngressAdapter(session_factory)
    for _ in range(2):
        await handle_vk_client_callback(
            _payload(cmid=704, text=_TRIGGER),
            config=_callback_cfg(),
            adapter=adapter,
        )
    worker = IngressWorker(
        session_factory,
        worker_id="vk-dup",
        settings=_settings(),
        vk_outbound_config=_outbound_cfg(),
    )
    claim = await worker.claim_one()
    assert claim is not None
    await worker.process_claimed(claim)
    assert await worker.claim_one() is None

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(ReplyPlan)) == 1


@pytest.mark.asyncio
async def test_vk_transient_then_reclaim_same_random_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _ingest_and_process(session_factory, cmid=705, text=_TRIGGER)
    async with session_scope(session_factory) as session:
        plan = (await session.scalars(select(ReplyPlan))).one()
        due = plan.not_before + timedelta(seconds=1)

    plan_worker = ReplyPlanWorker(session_factory, worker_id="vk-plan-t")
    plan_claim = await plan_worker.claim_one(now=due)
    assert plan_claim is not None
    await plan_worker.dispatch_claimed(plan_claim)

    sender = _RecordingVkSender()
    sender.outcome = VkClientSendOutcome.TRANSIENT_ERROR
    sender.error_code = "VK_CLIENT_SEND_TRANSIENT"
    arbiter = OutboundArbiter(
        session_factory,
        settings=_settings(),
        vk_config=_outbound_cfg(),
        vk_sender=sender,  # type: ignore[arg-type]
    )
    out_worker = OutboundWorker(
        session_factory, worker_id="vk-out-t", arbiter=arbiter
    )
    claim1 = await out_worker.claim_one(now=due)
    assert claim1 is not None
    with pytest.raises(OutboundArbiterDenied):
        await out_worker.process_claimed(claim1, now=due)
    assert len(sender.calls) == 1
    first_random = sender.calls[0]["random_id"]
    outbound_id = claim1.outbound_id

    async with session_scope(session_factory) as session:
        row = await session.get(OutboxMessage, outbound_id)
        assert row is not None
        assert row.delivery_status == DeliveryStatus.ADMITTED.value
        assert row.admitted_at is not None
        retry_at = row.not_before
        assert retry_at is not None

    sender.outcome = VkClientSendOutcome.SUCCESS
    sender.error_code = None
    claim2 = await out_worker.claim_one(now=retry_at)
    assert claim2 is not None
    assert claim2.outbound_id == outbound_id
    admit = await out_worker.process_claimed(claim2, now=retry_at)
    assert admit.delivery_status == DeliveryStatus.DELIVERED.value
    assert len(sender.calls) == 2
    assert sender.calls[1]["random_id"] == first_random
    assert sender.calls[1]["outbound_id"] == outbound_id


@pytest.mark.asyncio
async def test_vk_permanent_goes_dead(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _ingest_and_process(session_factory, cmid=706, text=_TRIGGER)
    async with session_scope(session_factory) as session:
        plan = (await session.scalars(select(ReplyPlan))).one()
        due = plan.not_before + timedelta(seconds=1)

    plan_worker = ReplyPlanWorker(session_factory, worker_id="vk-plan-p")
    plan_claim = await plan_worker.claim_one(now=due)
    assert plan_claim is not None
    await plan_worker.dispatch_claimed(plan_claim)

    sender = _RecordingVkSender()
    sender.outcome = VkClientSendOutcome.PERMANENT_ERROR
    sender.error_code = "VK_CLIENT_API_PERMANENT"
    arbiter = OutboundArbiter(
        session_factory,
        settings=_settings(),
        vk_config=_outbound_cfg(),
        vk_sender=sender,  # type: ignore[arg-type]
    )
    out_worker = OutboundWorker(
        session_factory, worker_id="vk-out-p", arbiter=arbiter
    )
    claim = await out_worker.claim_one(now=due)
    assert claim is not None
    with pytest.raises(OutboundArbiterDenied):
        await out_worker.process_claimed(claim, now=due)
    async with session_scope(session_factory) as session:
        row = await session.get(OutboxMessage, claim.outbound_id)
        assert row is not None
        assert row.delivery_status == DeliveryStatus.DEAD.value


@pytest.mark.asyncio
async def test_claim_next_outbound_id_is_stdlib_uuid(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PG/runtime claim must expose stdlib UUID for VK random_id contract."""

    await _ingest_and_process(session_factory, cmid=707, text=_TRIGGER)
    async with session_scope(session_factory) as session:
        plan = (await session.scalars(select(ReplyPlan))).one()
        due = plan.not_before + timedelta(seconds=1)

    plan_worker = ReplyPlanWorker(session_factory, worker_id="vk-plan-uuid")
    plan_claim = await plan_worker.claim_one(now=due)
    assert plan_claim is not None
    await plan_worker.dispatch_claimed(plan_claim)

    out_worker = OutboundWorker(
        session_factory,
        worker_id="vk-out-uuid",
        arbiter=OutboundArbiter(
            session_factory,
            settings=_settings(),
            vk_config=_outbound_cfg(),
            vk_sender=_RecordingVkSender(),  # type: ignore[arg-type]
        ),
    )
    claim = await out_worker.claim_one(now=due)
    assert claim is not None
    assert type(claim.outbound_id) is UUID
    assert type(claim.lease_token) is UUID
    assert type(claim.conversation_id) is UUID
    # Transport helper must accept claim.outbound_id without TypeError.
    _ = vk_client_random_id_from_outbound_id(claim.outbound_id)


@pytest.mark.asyncio
async def test_migration_constraints_accept_vk_legal_states(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CHECK constraints accept ADMITTED/DELIVERED for VK_CLIENT_OUTBOUND."""

    from app.models.conversation import Channel
    from app.repositories import conversations as conversation_repo
    from app.repositories import reply_plans as reply_plan_repo

    async with session_scope(session_factory) as session:
        conv, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.VK,
            external_conversation_id=_ALLOW,
        )
        plan = await reply_plan_repo.create_client_reply_plan(
            session,
            conversation_id=conv.id,
            context_version=conv.context_version,
            correlation_id=uuid4(),
            delay_ms=0,
            manager_epoch=conv.manager_epoch,
            event_seq_hwm=conv.current_event_seq,
            payload_json={
                "schema": "vk.client.proof.reply_plan.v1",
                "destination": "VK_CLIENT_OUTBOUND",
                "text": _REPLY,
            },
        )
        outbound, created = await outbound_repo.insert_vk_client_outbound_if_absent(
            session,
            conversation_id=conv.id,
            reply_plan_id=plan.id,
            context_version=conv.context_version,
            manager_epoch=conv.manager_epoch,
            event_seq_hwm=conv.current_event_seq,
            payload_json={"schema": "vk.client.outbound.v1", "text": _REPLY},
            correlation_id=uuid4(),
            not_before=plan.not_before,
        )
        assert created is True
        await session.execute(
            text(
                "UPDATE outbox_messages SET delivery_status='ADMITTED', "
                "admitted_at=now(), lease_owner='t', lease_token=gen_random_uuid(), "
                "lease_until=now() + interval '30 seconds' "
                "WHERE id = :id"
            ),
            {"id": outbound.id},
        )
        await session.execute(
            text(
                "UPDATE outbox_messages SET delivery_status='DELIVERED', "
                "lease_owner=NULL, lease_token=NULL, lease_until=NULL "
                "WHERE id = :id"
            ),
            {"id": outbound.id},
        )
        again, created2 = await outbound_repo.insert_vk_client_outbound_if_absent(
            session,
            conversation_id=conv.id,
            reply_plan_id=plan.id,
            context_version=conv.context_version,
            manager_epoch=conv.manager_epoch,
            event_seq_hwm=conv.current_event_seq,
            payload_json={"schema": "vk.client.outbound.v1", "text": _REPLY},
            correlation_id=uuid4(),
            not_before=plan.not_before,
        )
        assert created2 is False
        assert again.id == outbound.id

    # Illegal DELIVERED without admitted_at — separate session (CHECK abort).
    async with session_scope(session_factory) as session:
        conv = (
            await session.scalars(
                select(OutboxMessage).where(OutboxMessage.id == outbound.id)
            )
        ).one()
        cid = conv.conversation_id
        with pytest.raises(Exception):
            await session.execute(
                text(
                    "INSERT INTO outbox_messages ("
                    "id, conversation_id, reply_plan_id, idempotency_key, "
                    "context_version, manager_epoch, event_seq_hwm, "
                    "destination_type, payload_json, delivery_status, "
                    "not_before, attempt_count, max_attempts, lease_version"
                    ") VALUES ("
                    "gen_random_uuid(), :cid, NULL, :ikey, 0, 0, 0, "
                    "'VK_CLIENT_OUTBOUND', '{}'::jsonb, 'DELIVERED', "
                    "now(), 0, 5, 0"
                    ")"
                ),
                {"cid": cid, "ikey": f"illegal-{uuid4()}"},
            )
            await session.flush()
