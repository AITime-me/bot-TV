"""PostgreSQL durability / CAS / lease races for CURSOR-20 booking wiring."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.booking_types import (
    BookingDialogAction,
    BookingEligibilityOutcome,
    BookingEligibilityResult,
    BookingInternalReasonCode,
    SelectedMaster,
    SelectedService,
)
from app.db.session import session_scope
from app.models.outbox import OutboxMessage
from app.models.reply_plan import ReplyPlan, ReplyPlanStatus
from app.repositories import reply_plans as reply_plan_repo
from app.repositories.reply_plans import StaleReplyPlanLeaseError
from app.schemas.booking_input import SyntheticBookingInput, SyntheticBookingSlot
from app.schemas.inbound import SyntheticInboundEvent
from app.services.booking_eligibility_flow import BookingEligibilityFlowService
from app.services.booking_flow import BookingFlowService
from app.services.booking_synthetic import (
    BOOKING_RESOLUTION_RESULT_KEY,
    BOOKING_RESOLUTION_STARTED_KEY,
)
from app.services.inbound import InboundService
from app.services.reply_outbound import ReplyPlanWorker
from tests.pg_harness import truncate_foundation_tables

_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_SLOT_START = datetime(2026, 8, 6, 5, 0, tzinfo=timezone.utc)
_DECISION_AT = datetime(2026, 8, 5, 12, 0, tzinfo=timezone(timedelta(hours=5)))


@pytest_asyncio.fixture(autouse=True)
async def booking_inbound_pg_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


def _booking_inbound(
    *,
    event_id: str,
    conv: str = "booking-pg-conv",
) -> SyntheticInboundEvent:
    return SyntheticInboundEvent(
        external_conversation_id=conv,
        external_message_id=event_id,
        text="booking-fixture-placeholder",
        booking=SyntheticBookingInput(
            service_id=_SERVICE,
            master_id=_MASTER,
            include_alternatives=False,
            alternate_master_consent=False,
            slots=(
                SyntheticBookingSlot(
                    slot_id="s1",
                    starts_at=_SLOT_START,
                    master_id=_MASTER,
                    service_id=_SERVICE,
                ),
            ),
            decision_at=_DECISION_AT,
        ),
    )


def _allowed_result() -> BookingEligibilityResult:
    return BookingEligibilityResult(
        outcome=BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
        selected_service=SelectedService(_SERVICE),
        selected_master=SelectedMaster(_MASTER),
        other_online_master_ids=(),
        internal_reason_code=None,
    )


async def _create_due_booking_plan(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event_id: str,
    conv: str,
) -> tuple[object, datetime]:
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            _booking_inbound(event_id=event_id, conv=conv)
        )
        assert accepted.reply_plan is not None
        plan = accepted.reply_plan
        assert "booking" in plan.payload_json
        due = plan.not_before
        plan_id = plan.id
    return plan_id, due


@pytest.mark.asyncio
async def test_booking_resolution_marker_survives_commit_new_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CAS marker must remain visible after commit in a fresh session."""

    plan_id, due = await _create_due_booking_plan(
        session_factory, event_id="mark-dur-1", conv="mark-dur-conv"
    )
    worker = ReplyPlanWorker(session_factory, worker_id="mark-dur")
    claim = await worker.claim_one(now=due)
    assert claim is not None
    assert claim.plan_id == plan_id

    async with session_scope(session_factory) as session:
        # Load into identity map with pre-marker payload (wipe risk surface).
        loaded = await reply_plan_repo.get_by_id(session, plan_id=plan_id)
        assert loaded is not None
        assert BOOKING_RESOLUTION_STARTED_KEY not in loaded.payload_json
        acquired = await reply_plan_repo.try_mark_booking_resolution_started(
            session,
            plan_id=claim.plan_id,
            lease_token=claim.lease_token,
            lease_version=claim.lease_version,
        )
        assert acquired is True
        # Same session must see merged payload without dirty write-back.
        assert loaded.payload_json.get(BOOKING_RESOLUTION_STARTED_KEY) is True

    async with session_scope(session_factory) as session:
        refreshed = await reply_plan_repo.get_by_id(session, plan_id=plan_id)
        assert refreshed is not None
        assert refreshed.payload_json.get(BOOKING_RESOLUTION_STARTED_KEY) is True
        assert BOOKING_RESOLUTION_RESULT_KEY not in refreshed.payload_json


@pytest.mark.asyncio
async def test_booking_resolution_marker_cas_single_winner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two concurrent CAS attempts under the same lease: exactly one winner."""

    plan_id, due = await _create_due_booking_plan(
        session_factory, event_id="mark-cas-1", conv="mark-cas-conv"
    )
    worker = ReplyPlanWorker(session_factory, worker_id="mark-cas")
    claim = await worker.claim_one(now=due)
    assert claim is not None
    assert claim.plan_id == plan_id

    barrier = asyncio.Barrier(2)
    results: list[bool] = []

    async def _attempt() -> bool:
        await barrier.wait()
        async with session_scope(session_factory) as session:
            return await reply_plan_repo.try_mark_booking_resolution_started(
                session,
                plan_id=claim.plan_id,
                lease_token=claim.lease_token,
                lease_version=claim.lease_version,
            )

    first, second = await asyncio.gather(_attempt(), _attempt())
    results.extend([first, second])
    assert sorted(results) == [False, True]

    async with session_scope(session_factory) as session:
        row = await reply_plan_repo.get_by_id(session, plan_id=plan_id)
        assert row is not None
        assert row.payload_json.get(BOOKING_RESOLUTION_STARTED_KEY) is True


@pytest.mark.asyncio
async def test_lease_expiry_during_resolve_no_second_remote_no_stale_offer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Lease reclaim during off-txn resolve: no second remote; no stale OFFER."""

    resolve_entered = threading.Event()
    allow_resolve_finish = threading.Event()
    calls: list[dict[str, Any]] = []

    class BlockingClient:
        def check_eligibility(
            self,
            service: SelectedService,
            master: SelectedMaster | None = None,
            *,
            include_alternatives: bool = False,
        ) -> BookingEligibilityResult:
            calls.append(
                {
                    "service": service,
                    "master": master,
                    "include_alternatives": include_alternatives,
                }
            )
            resolve_entered.set()
            if not allow_resolve_finish.wait(timeout=10.0):
                raise TimeoutError("first resolve barrier timed out")
            return _allowed_result()

    flow = BookingFlowService(BookingEligibilityFlowService(BlockingClient()))
    plan_id, due = await _create_due_booking_plan(
        session_factory, event_id="lease-race-1", conv="lease-race-conv"
    )

    first_worker = ReplyPlanWorker(
        session_factory,
        worker_id="lease-first",
        booking_flow=flow,
        lease_seconds=30,
    )
    claim_a = await first_worker.claim_one(now=due)
    assert claim_a is not None
    assert claim_a.plan_id == plan_id

    dispatch_a = asyncio.create_task(first_worker.dispatch_claimed(claim_a))

    for _ in range(200):
        if resolve_entered.is_set():
            break
        await asyncio.sleep(0.05)
    assert resolve_entered.is_set(), "first worker never reached remote resolve"
    assert not dispatch_a.done()

    async with session_scope(session_factory) as session:
        marked = await reply_plan_repo.get_by_id(session, plan_id=plan_id)
        assert marked is not None
        assert marked.payload_json.get(BOOKING_RESOLUTION_STARTED_KEY) is True
        await session.execute(
            update(ReplyPlan)
            .where(ReplyPlan.id == plan_id)
            .values(lease_until=due - timedelta(seconds=1))
        )

    class RefuseClient:
        def check_eligibility(
            self,
            service: SelectedService,
            master: SelectedMaster | None = None,
            *,
            include_alternatives: bool = False,
        ) -> BookingEligibilityResult:
            raise AssertionError("second worker must not call eligibility")

    second_flow = BookingFlowService(BookingEligibilityFlowService(RefuseClient()))
    second_worker = ReplyPlanWorker(
        session_factory,
        worker_id="lease-second",
        booking_flow=second_flow,
    )
    claim_b = await second_worker.claim_one(now=due)
    assert claim_b is not None
    assert claim_b.plan_id == plan_id
    assert claim_b.lease_token != claim_a.lease_token

    result_b = await second_worker.dispatch_claimed(claim_b)
    assert result_b.outbound_created is True

    async with session_scope(session_factory) as session:
        outbound = await session.scalar(
            select(OutboxMessage).where(OutboxMessage.reply_plan_id == plan_id)
        )
        assert outbound is not None
        assert outbound.payload_json.get("booking_action") == (
            BookingDialogAction.SERVICE_UNAVAILABLE.value
        )
        assert outbound.payload_json.get("booking_reason") == (
            BookingInternalReasonCode.BOOKING_RESOLUTION_INTERRUPTED.value
        )
        assert outbound.payload_json.get("text") == (
            "Сейчас не могу завершить запись самостоятельно. "
            "Передаю ваш запрос менеджеру."
        )
        assert outbound.payload_json.get("text") != "booking-fixture-placeholder"
        plan = await reply_plan_repo.get_by_id(session, plan_id=plan_id)
        assert plan is not None
        assert plan.status == ReplyPlanStatus.DISPATCHED.value
        assert isinstance(plan.payload_json.get(BOOKING_RESOLUTION_RESULT_KEY), dict)

    allow_resolve_finish.set()
    with pytest.raises(StaleReplyPlanLeaseError):
        await asyncio.wait_for(dispatch_a, timeout=10.0)

    assert len(calls) == 1

    async with session_scope(session_factory) as session:
        rows = (
            await session.execute(
                select(OutboxMessage).where(OutboxMessage.reply_plan_id == plan_id)
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].payload_json.get("booking_action") == (
            BookingDialogAction.SERVICE_UNAVAILABLE.value
        )
        assert rows[0].payload_json.get("booking_action") != (
            BookingDialogAction.OFFER_SLOTS.value
        )
