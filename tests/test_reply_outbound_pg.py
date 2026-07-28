from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.outbound_policy import OutboundAction, is_automatic_outbound_allowed
from app.db.clock import db_now
from app.db.session import session_scope
from app.models.conversation import Conversation, ConversationOwnership
from app.models.outbox import DeliveryStatus, DestinationType, OutboxMessage
from app.models.reply_plan import (
    BOT_RESPONSE_DELAY_MS,
    ReplyPlan,
    ReplyPlanStatus,
)
from app.repositories import conversations as conversation_repo
from app.repositories import outbound as outbound_repo
from app.repositories import reply_plans as reply_plan_repo
from app.repositories.outbound import StaleOutboundLeaseError
from app.repositories.reply_plans import StaleReplyPlanLeaseError
from app.schemas.inbound import SyntheticInboundEvent
from app.services.inbound import InboundService
from app.services.outbound_arbiter import OutboundArbiter, OutboundArbiterDenied
from app.services.reply_outbound import OutboundWorker, ReplyPlanWorker
from app.services.synthetic_outbound import (
    SyntheticOutboundAdapter,
    SyntheticOutboundOutcome,
    SyntheticOutboundRequest,
)
from app.services.takeover import ManagerTakeoverService
from tests.pg_harness import truncate_foundation_tables


def _inbound(
    event_id: str,
    conv: str = "reply-conv",
    text: str = "synth-text",
    received_at: datetime | None = None,
) -> SyntheticInboundEvent:
    return SyntheticInboundEvent(
        external_conversation_id=conv,
        external_message_id=event_id,
        text=text,
        received_at=received_at,
    )


@pytest_asyncio.fixture(autouse=True)
async def reply_outbound_row_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


@pytest.mark.asyncio
async def test_first_message_bumps_context_and_creates_delayed_plan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound("msg-1"))
        assert result.created_inbox is True
        assert result.context_version == 1
        assert result.reply_plan_created is True
        assert result.reply_plan is not None
        assert result.reply_plan.bot_response_delay_ms == BOT_RESPONSE_DELAY_MS
        assert result.reply_plan.status == ReplyPlanStatus.PENDING.value
        delta_ms = int(
            (result.reply_plan.not_before - result.reply_plan.created_at).total_seconds()
            * 1000
        )
        # created_at and not_before share one PostgreSQL instant, so the stored
        # delay is exact rather than latency- or skew-dependent.
        assert delta_ms == BOT_RESPONSE_DELAY_MS
        assert result.reply_plan.not_before > await db_now(session)
        assert result.conversation.context_version == 1
        assert result.conversation.active_reply_plan_id == result.reply_plan.id


@pytest.mark.asyncio
async def test_delay_ignores_skewed_application_and_provider_clock(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A host clock hours away from PostgreSQL must not move not_before."""
    async with session_scope(session_factory) as session:
        server_now = await db_now(session)
        skewed = server_now - timedelta(hours=3)
        result = await InboundService(session).accept(
            _inbound("msg-skew", received_at=skewed)
        )
        assert result.reply_plan is not None
        plan = result.reply_plan
        assert plan.not_before - plan.created_at == timedelta(
            milliseconds=BOT_RESPONSE_DELAY_MS
        )
        # The skewed instant is recorded as client activity but never scheduled on.
        assert result.conversation.last_client_activity_at == skewed
        assert plan.not_before > server_now

    worker = ReplyPlanWorker(session_factory, worker_id="w-skew")
    assert await worker.claim_one(now=plan.not_before - timedelta(milliseconds=1)) is None
    claim = await worker.claim_one(now=plan.not_before)
    assert claim is not None


@pytest.mark.asyncio
async def test_duplicate_message_does_not_bump_context(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event = _inbound("msg-dup")
    async with session_scope(session_factory) as session:
        first = await InboundService(session).accept(event)
    async with session_scope(session_factory) as session:
        second = await InboundService(session).accept(event)
        assert second.duplicate is True
        assert second.context_version == first.context_version
        assert second.reply_plan_created is False
        count = await session.scalar(select(func.count()).select_from(ReplyPlan))
        assert count == 1


@pytest.mark.asyncio
async def test_new_message_supersedes_previous_plan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        first = await InboundService(session).accept(_inbound("msg-a"))
        first_plan_id = first.reply_plan.id if first.reply_plan else None
    async with session_scope(session_factory) as session:
        second = await InboundService(session).accept(_inbound("msg-b"))
        assert second.context_version == 2
        old = await session.get(ReplyPlan, first_plan_id)
        assert old is not None
        assert old.status == ReplyPlanStatus.SUPERSEDED.value
        assert second.reply_plan is not None
        assert second.reply_plan.context_version == 2


async def _accept_versions(
    session_factory: async_sessionmaker[AsyncSession],
    conv: str,
    message_ids: tuple[str, ...],
) -> list[int]:
    """Accept several messages of one dialog concurrently, return their versions."""

    async def _one(msg_id: str) -> int:
        async with session_scope(session_factory) as session:
            result = await InboundService(session).accept(_inbound(msg_id, conv=conv))
            return result.context_version

    return list(await asyncio.gather(*(_one(msg_id) for msg_id in message_ids)))


@pytest.mark.asyncio
async def test_concurrent_messages_preserve_context_monotonicity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    versions = await _accept_versions(
        session_factory,
        "reply-conv",
        ("c-1", "c-2", "c-3"),
    )
    assert sorted(versions) == [1, 2, 3]
    async with session_factory() as session:
        async with session.begin():
            conv = await session.scalar(select(Conversation))
            assert conv is not None
            assert conv.context_version == 3
            plans = await session.scalar(select(func.count()).select_from(ReplyPlan))
            assert plans == 3
            plan_versions = sorted(
                await session.scalars(select(ReplyPlan.context_version))
            )
            assert plan_versions == [1, 2, 3]


@pytest.mark.asyncio
async def test_repeated_concurrent_first_messages_never_deadlock(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Repeat the race that used to escalate FOR KEY SHARE into FOR UPDATE."""
    for round_index in range(3):
        conv = f"race-first-{round_index}"
        versions = await _accept_versions(
            session_factory,
            conv,
            tuple(f"race-first-{round_index}-{n}" for n in range(3)),
        )
        assert sorted(versions) == [1, 2, 3]


@pytest.mark.asyncio
async def test_repeated_concurrent_messages_on_existing_conversation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Minimal deadlock shape: inbox FK takes KEY SHARE, the bump wants FOR UPDATE."""
    conv = "race-existing"
    async with session_scope(session_factory) as session:
        first = await InboundService(session).accept(_inbound("exist-base", conv=conv))
        assert first.context_version == 1

    for round_index in range(3):
        base = 1 + round_index * 3
        versions = await _accept_versions(
            session_factory,
            conv,
            tuple(f"exist-{round_index}-{n}" for n in range(3)),
        )
        assert sorted(versions) == [base + 1, base + 2, base + 3]

    async with session_factory() as session:
        async with session.begin():
            conv_row = await session.scalar(select(Conversation))
            assert conv_row is not None
            assert conv_row.context_version == 10
            plan_versions = sorted(
                await session.scalars(select(ReplyPlan.context_version))
            )
            assert plan_versions == list(range(1, 11))


@pytest.mark.asyncio
async def test_plan_unavailable_before_not_before_without_sleep(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound("msg-delay"))
        assert result.reply_plan is not None
        not_before = result.reply_plan.not_before
    worker = ReplyPlanWorker(session_factory, worker_id="w-delay")
    # not_before is 5s ahead on the PostgreSQL clock; claim returns None without
    # sleeping and without consulting the application host clock.
    assert await worker.claim_one() is None
    assert await worker.claim_one(now=not_before - timedelta(milliseconds=1)) is None


@pytest.mark.asyncio
async def test_plan_claimable_after_not_before_by_clock_injection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound("msg-ready"))
        assert result.reply_plan is not None
        future = result.reply_plan.not_before + timedelta(seconds=1)
    worker = ReplyPlanWorker(session_factory, worker_id="w-ready")
    claim = await worker.claim_one(now=future)
    assert claim is not None
    assert claim.status == ReplyPlanStatus.PROCESSING.value


@pytest.mark.asyncio
async def test_reply_plan_lease_fencing_and_exclusive_claim(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound("msg-lease"))
        assert result.reply_plan is not None
        now = result.reply_plan.not_before + timedelta(seconds=1)
    worker_a = ReplyPlanWorker(session_factory, worker_id="a")
    worker_b = ReplyPlanWorker(session_factory, worker_id="b")
    claim_a = await worker_a.claim_one(now=now)
    claim_b = await worker_b.claim_one(now=now)
    assert claim_a is not None
    assert claim_b is None

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(ReplyPlan)
                .where(ReplyPlan.id == claim_a.plan_id)
                .values(lease_until=now - timedelta(seconds=1))
            )
    claim_b2 = await worker_b.claim_one(now=now)
    assert claim_b2 is not None
    assert claim_b2.lease_version == claim_a.lease_version + 1

    with pytest.raises(StaleReplyPlanLeaseError):
        async with session_scope(session_factory) as session:
            await reply_plan_repo.complete_dispatched_with_lease(
                session,
                plan_id=claim_a.plan_id,
                lease_token=claim_a.lease_token,
                lease_version=claim_a.lease_version,
            )


@pytest.mark.asyncio
async def test_reply_plan_retry_then_dead(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound("msg-dead"))
        assert result.reply_plan is not None
        now = result.reply_plan.not_before + timedelta(seconds=1)
    worker = ReplyPlanWorker(
        session_factory,
        worker_id="dead-w",
        max_attempts=2,
        retry_delay_seconds=0,
    )
    first = await worker.claim_one(now=now)
    assert first is not None
    failed = await worker.fail_claimed(first, error_code="boom")
    assert failed.status == ReplyPlanStatus.FAILED.value
    # fail_claimed resolves next not_before on the PostgreSQL clock (it does
    # not reuse the test's injected claim time), so retry must follow the
    # value actually stored on the row.
    retry_at = failed.not_before
    assert retry_at is not None
    assert await worker.claim_one(now=retry_at - timedelta(milliseconds=1)) is None
    second = await worker.claim_one(now=retry_at)
    assert second is not None
    dead = await worker.fail_claimed(second, error_code="boom2")
    assert dead.status == ReplyPlanStatus.DEAD.value


@pytest.mark.asyncio
async def test_manager_takeover_cancels_and_blocks_new_plans(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        first = await InboundService(session).accept(_inbound("msg-takeover"))
        conversation_id = first.conversation.id
        plan_id = first.reply_plan.id if first.reply_plan else None

    takeover = ManagerTakeoverService(session_factory)
    result = await takeover.apply(conversation_id)
    assert result.changed is True
    assert result.cancelled_plans >= 1
    again = await takeover.apply(conversation_id)
    assert again.changed is False

    async with session_factory() as session:
        async with session.begin():
            plan = await session.get(ReplyPlan, plan_id)
            assert plan is not None
            assert plan.status == ReplyPlanStatus.CANCELLED.value
            conv = await session.get(Conversation, conversation_id)
            assert conv is not None
            assert conv.ownership == ConversationOwnership.MANAGER.value

    async with session_scope(session_factory) as session:
        second = await InboundService(session).accept(_inbound("msg-after-takeover"))
        assert second.reply_plan_created is False
        assert second.automatic_reply_allowed is False


@pytest.mark.asyncio
async def test_one_reply_plan_one_outbound_and_arbiter_success(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound("msg-out"))
        assert result.reply_plan is not None
        not_before = result.reply_plan.not_before
        now = not_before + timedelta(seconds=1)

    plan_worker = ReplyPlanWorker(session_factory, worker_id="plan-w")
    claim = await plan_worker.claim_one(now=now)
    assert claim is not None
    dispatched = await plan_worker.dispatch_claimed(claim)
    assert dispatched.plan_status == ReplyPlanStatus.DISPATCHED.value

    # Concurrent duplicate insert must not create a second outbound.
    async with session_scope(session_factory) as session:
        row, created = await outbound_repo.insert_synthetic_outbound_if_absent(
            session,
            conversation_id=claim.conversation_id,
            reply_plan_id=claim.plan_id,
            context_version=claim.context_version,
            payload_json={"schema": "synthetic.outbound.v1", "synthetic_token": "X"},
            correlation_id=claim.correlation_id,
            not_before=claim.not_before,
        )
        assert created is False
        assert row.id == dispatched.outbound_id

    sink = SyntheticOutboundAdapter()
    arbiter = OutboundArbiter(session_factory, sink=sink)
    out_worker = OutboundWorker(session_factory, worker_id="out-w", arbiter=arbiter)
    # The outbound row inherits the plan's not_before, so one millisecond early
    # on the PostgreSQL timeline is still not claimable.
    early = not_before - timedelta(milliseconds=1)
    assert await out_worker.claim_one(now=early) is None
    out_claim = await out_worker.claim_one(now=now)
    assert out_claim is not None
    admit = await out_worker.process_claimed(out_claim, now=now)
    assert admit.admitted is True
    assert admit.delivery_status == DeliveryStatus.DELIVERED.value
    assert len(sink.calls) == 1
    assert is_automatic_outbound_allowed(Settings(), OutboundAction.SEND_MESSAGE) is False


@pytest.mark.asyncio
async def test_arbiter_rejects_stale_context_and_manager_takeover(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        first = await InboundService(session).accept(_inbound("msg-arb-1"))
        assert first.reply_plan is not None
        now = first.reply_plan.not_before + timedelta(seconds=1)
        conversation_id = first.conversation.id

    plan_worker = ReplyPlanWorker(session_factory, worker_id="plan-arb")
    claim = await plan_worker.claim_one(now=now)
    assert claim is not None
    dispatched = await plan_worker.dispatch_claimed(claim)

    # New message bumps context while outbound still carries old version.
    async with session_scope(session_factory) as session:
        await InboundService(session).accept(_inbound("msg-arb-2"))

    arbiter = OutboundArbiter(session_factory)
    out_worker = OutboundWorker(session_factory, worker_id="out-arb", arbiter=arbiter)
    out_claim = await out_worker.claim_one(now=now)
    assert out_claim is not None
    assert out_claim.outbound_id == dispatched.outbound_id
    with pytest.raises(OutboundArbiterDenied, match="STALE_CONTEXT"):
        await out_worker.process_claimed(out_claim, now=now)


@pytest.mark.asyncio
async def test_arbiter_process_claimed_denies_manager_owned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound("msg-mgr-owned"))
        assert result.reply_plan is not None
        due = result.reply_plan.not_before + timedelta(seconds=1)
        conversation_id = result.conversation.id

    plan_worker = ReplyPlanWorker(session_factory, worker_id="plan-mgr")
    claim = await plan_worker.claim_one(now=due)
    assert claim is not None
    await plan_worker.dispatch_claimed(claim)

    await ManagerTakeoverService(session_factory).apply(conversation_id)

    arbiter = OutboundArbiter(session_factory)
    out_worker = OutboundWorker(session_factory, worker_id="out-mgr", arbiter=arbiter)
    out_claim = await out_worker.claim_one(now=due)
    assert out_claim is not None
    with pytest.raises(OutboundArbiterDenied, match="^MANAGER_OWNED$"):
        await out_worker.process_claimed(out_claim, now=due)


@pytest.mark.asyncio
async def test_arbiter_process_claimed_denies_manager_takeover_timestamp(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ownership stays BOT while manager_takeover_at is set — hits MANAGER_TAKEOVER."""
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound("msg-mgr-ts"))
        assert result.reply_plan is not None
        due = result.reply_plan.not_before + timedelta(seconds=1)
        conversation_id = result.conversation.id

    plan_worker = ReplyPlanWorker(session_factory, worker_id="plan-mgr-ts")
    claim = await plan_worker.claim_one(now=due)
    assert claim is not None
    await plan_worker.dispatch_claimed(claim)

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(
                    manager_takeover_at=due,
                    ownership=ConversationOwnership.BOT.value,
                )
            )

    arbiter = OutboundArbiter(session_factory)
    out_worker = OutboundWorker(session_factory, worker_id="out-mgr-ts", arbiter=arbiter)
    out_claim = await out_worker.claim_one(now=due)
    assert out_claim is not None
    with pytest.raises(OutboundArbiterDenied, match="^MANAGER_TAKEOVER$"):
        await out_worker.process_claimed(out_claim, now=due)


@pytest.mark.asyncio
async def test_arbiter_process_claimed_denies_non_dispatched_plan_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound("msg-plan-status"))
        assert result.reply_plan is not None
        due = result.reply_plan.not_before + timedelta(seconds=1)

    plan_worker = ReplyPlanWorker(session_factory, worker_id="plan-status")
    claim = await plan_worker.claim_one(now=due)
    assert claim is not None
    dispatched = await plan_worker.dispatch_claimed(claim)

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(ReplyPlan)
                .where(ReplyPlan.id == claim.plan_id)
                .values(status=ReplyPlanStatus.CANCELLED.value)
            )

    arbiter = OutboundArbiter(session_factory)
    out_worker = OutboundWorker(
        session_factory, worker_id="out-plan-status", arbiter=arbiter
    )
    out_claim = await out_worker.claim_one(now=due)
    assert out_claim is not None
    assert out_claim.outbound_id == dispatched.outbound_id
    with pytest.raises(OutboundArbiterDenied, match="^REPLY_PLAN_CANCELLED$"):
        await out_worker.process_claimed(out_claim, now=due)

@pytest.mark.asyncio
async def test_outbound_fencing_and_early_not_before(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound("msg-fence"))
        assert result.reply_plan is not None
        not_before = result.reply_plan.not_before
        due = not_before + timedelta(seconds=1)
    plan_worker = ReplyPlanWorker(session_factory, worker_id="plan-f")
    claim = await plan_worker.claim_one(now=due)
    assert claim is not None
    await plan_worker.dispatch_claimed(claim)

    arbiter = OutboundArbiter(session_factory)
    worker_a = OutboundWorker(session_factory, worker_id="oa", arbiter=arbiter)
    worker_b = OutboundWorker(session_factory, worker_id="ob", arbiter=arbiter)
    # The outbound row inherits the plan's not_before, so no worker can claim it
    # before that instant: early delivery stays impossible.
    assert await worker_a.claim_one(now=not_before - timedelta(milliseconds=1)) is None

    claim_a = await worker_a.claim_one(now=due)
    claim_b = await worker_b.claim_one(now=due)
    assert claim_a is not None
    assert claim_b is None

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(OutboxMessage)
                .where(OutboxMessage.id == claim_a.outbound_id)
                .values(lease_until=due - timedelta(seconds=1))
            )
    claim_b2 = await worker_b.claim_one(now=due)
    assert claim_b2 is not None
    with pytest.raises(StaleOutboundLeaseError):
        async with session_scope(session_factory) as session:
            await outbound_repo.mark_delivered_with_lease(
                session,
                outbound_id=claim_a.outbound_id,
                lease_token=claim_a.lease_token,
                lease_version=claim_a.lease_version,
            )

    # Early send: force not_before into the future under the new lease.
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(OutboxMessage)
                .where(OutboxMessage.id == claim_b2.outbound_id)
                .values(
                    not_before=due + timedelta(hours=1),
                    delivery_status=DeliveryStatus.PROCESSING.value,
                )
            )
    with pytest.raises(OutboundArbiterDenied, match="NOT_BEFORE"):
        await worker_b.process_claimed(claim_b2, now=due)


@pytest.mark.asyncio
async def test_db_rejects_sent_and_duplicate_idempotency(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound("msg-db"))
        assert result.reply_plan is not None
        due = result.reply_plan.not_before + timedelta(seconds=1)
    worker = ReplyPlanWorker(session_factory, worker_id="db-w")
    claim = await worker.claim_one(now=due)
    assert claim is not None
    dispatched = await worker.dispatch_claimed(claim)

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE outbox_messages SET delivery_status = 'SENT' "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(dispatched.outbound_id)},
                )

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                session.add(
                    OutboxMessage(
                        conversation_id=claim.conversation_id,
                        reply_plan_id=claim.plan_id,
                        idempotency_key=f"synthetic-outbound:reply-plan:{claim.plan_id}",
                        context_version=claim.context_version,
                        destination_type=DestinationType.SYNTHETIC_OUTBOUND.value,
                        payload_json={"schema": "synthetic.outbound.v1"},
                        delivery_status=DeliveryStatus.PENDING.value,
                    )
                )
                await session.flush()


@pytest.mark.asyncio
async def test_no_payload_leak_in_repr_and_no_external_side_effects(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    secret = f"secret-client-{uuid4()}"
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(
            _inbound("msg-redact", text=secret)
        )
        assert result.reply_plan is not None
        assert secret not in repr(result.reply_plan)
        assert secret not in repr(result.outbox)
    assert is_automatic_outbound_allowed(Settings(), OutboundAction.SEND_MESSAGE) is False
    failing = SyntheticOutboundAdapter(
        forced_outcome=SyntheticOutboundOutcome.PERMANENT_ERROR,
    )
    denied = failing.deliver(
        SyntheticOutboundRequest(
            outbound_id="o",
            conversation_id="c",
            reply_plan_id=None,
            context_version=1,
            correlation_id=None,
            _payload_schema="synthetic.outbound.v1",
        )
    )
    assert denied.outcome is SyntheticOutboundOutcome.PERMANENT_ERROR


@pytest.mark.asyncio
async def test_dispatch_locks_conversation_before_outbox_no_deadlock_with_inbound(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inbound waits on Conversation while dispatch holds it; no deadlock.

    Deterministic barriers prove dispatch takes Conversation FOR UPDATE before
    the outbox INSERT. Removing that lock from dispatch_claimed makes
    ``dispatch_holds_conversation`` never fire and fails this test.
    """
    conv_key = "race-dispatch-inbound"
    async with session_scope(session_factory) as session:
        first = await InboundService(session).accept(
            _inbound("race-base", conv=conv_key)
        )
        assert first.reply_plan is not None
        due = first.reply_plan.not_before + timedelta(seconds=1)
        old_plan_id = first.reply_plan.id
        conversation_id = first.conversation.id

    plan_worker = ReplyPlanWorker(session_factory, worker_id="race-dispatch")
    claim = await plan_worker.claim_one(now=due)
    assert claim is not None
    assert claim.plan_id == old_plan_id

    original_lock = conversation_repo.lock_for_update
    original_insert = outbound_repo.insert_synthetic_outbound_if_absent

    lock_calls = {"n": 0}
    dispatch_holds_conversation = asyncio.Event()
    allow_dispatch_past_lock = asyncio.Event()
    inbound_reached_lock = asyncio.Event()
    insert_started = asyncio.Event()
    insert_after_lock = {"ok": False}

    async def gated_lock(session, *, conversation_id):  # type: ignore[no-untyped-def]
        lock_calls["n"] += 1
        if lock_calls["n"] == 1:
            conversation = await original_lock(
                session, conversation_id=conversation_id
            )
            dispatch_holds_conversation.set()
            await allow_dispatch_past_lock.wait()
            return conversation
        inbound_reached_lock.set()
        return await original_lock(session, conversation_id=conversation_id)

    async def gated_insert(*args, **kwargs):  # type: ignore[no-untyped-def]
        if not dispatch_holds_conversation.is_set():
            raise AssertionError(
                "outbox INSERT ran before Conversation FOR UPDATE in dispatch"
            )
        insert_after_lock["ok"] = True
        insert_started.set()
        return await original_insert(*args, **kwargs)

    monkeypatch.setattr(conversation_repo, "lock_for_update", gated_lock)
    monkeypatch.setattr(
        "app.services.reply_outbound.conversation_repo.lock_for_update",
        gated_lock,
    )
    monkeypatch.setattr(
        outbound_repo,
        "insert_synthetic_outbound_if_absent",
        gated_insert,
    )
    monkeypatch.setattr(
        "app.services.reply_outbound.outbound_repo.insert_synthetic_outbound_if_absent",
        gated_insert,
    )

    dispatch_task = asyncio.create_task(plan_worker.dispatch_claimed(claim))

    await asyncio.wait_for(dispatch_holds_conversation.wait(), timeout=10)

    async def _inbound_accept() -> int:
        async with session_scope(session_factory) as session:
            accepted = await InboundService(session).accept(
                _inbound("race-concurrent", conv=conv_key)
            )
            return accepted.context_version

    inbound_task = asyncio.create_task(_inbound_accept())
    await asyncio.wait_for(inbound_reached_lock.wait(), timeout=10)
    assert not inbound_task.done()
    assert not insert_started.is_set()

    allow_dispatch_past_lock.set()
    dispatched, inbound_version = await asyncio.wait_for(
        asyncio.gather(dispatch_task, inbound_task),
        timeout=10,
    )

    assert insert_after_lock["ok"] is True
    assert dispatched.plan_status == ReplyPlanStatus.DISPATCHED.value
    assert dispatched.outbound_created is True
    assert inbound_version == 2

    async with session_factory() as session:
        async with session.begin():
            conv = await session.get(Conversation, conversation_id)
            assert conv is not None
            assert conv.context_version == 2
            assert conv.active_reply_plan_id is not None
            assert conv.active_reply_plan_id != old_plan_id

            old_plan = await session.get(ReplyPlan, old_plan_id)
            assert old_plan is not None
            assert old_plan.status == ReplyPlanStatus.DISPATCHED.value

            new_plan = await session.get(ReplyPlan, conv.active_reply_plan_id)
            assert new_plan is not None
            assert new_plan.context_version == 2
            assert new_plan.status == ReplyPlanStatus.PENDING.value

            outbound_rows = list(
                await session.scalars(
                    select(OutboxMessage).where(
                        OutboxMessage.destination_type
                        == DestinationType.SYNTHETIC_OUTBOUND.value
                    )
                )
            )
            assert len(outbound_rows) == 1
            assert outbound_rows[0].reply_plan_id == old_plan_id
            assert outbound_rows[0].id == dispatched.outbound_id
