"""PostgreSQL durability / CAS / lease races for CURSOR-23 availability query."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.booking_availability_remote import (
    AvailableDaysResult,
    AvailableSlotsResult,
)
from app.core.booking_types import (
    AvailableSlot,
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
from app.schemas.booking_input import (
    SyntheticAvailableDaysQuery,
    SyntheticAvailableSlotsQuery,
    SyntheticBookingInput,
)
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
_DECISION_AT = datetime(2026, 8, 5, 12, 0, tzinfo=timezone(timedelta(hours=5)))
_SLOT_START = datetime(2026, 8, 6, 10, 0, tzinfo=timezone(timedelta(hours=5)))

QueryKind = Literal["AVAILABLE_DAYS", "SLOTS"]


@pytest_asyncio.fixture(autouse=True)
async def booking_availability_pg_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


def _availability_booking(*, kind: QueryKind) -> SyntheticBookingInput:
    if kind == "AVAILABLE_DAYS":
        query: SyntheticAvailableDaysQuery | SyntheticAvailableSlotsQuery = (
            SyntheticAvailableDaysQuery(kind="AVAILABLE_DAYS", month="2026-08")
        )
    else:
        query = SyntheticAvailableSlotsQuery(kind="SLOTS", date="2026-08-06")
    return SyntheticBookingInput(
        service_id=_SERVICE,
        master_id=_MASTER,
        include_alternatives=False,
        alternate_master_consent=False,
        availability_query=query,
        decision_at=_DECISION_AT,
    )


def _availability_inbound(
    *,
    event_id: str,
    conv: str,
    kind: QueryKind,
) -> SyntheticInboundEvent:
    return SyntheticInboundEvent(
        external_conversation_id=conv,
        external_message_id=event_id,
        text="availability-query-placeholder",
        booking=_availability_booking(kind=kind),
    )


def _allowed_result() -> BookingEligibilityResult:
    return BookingEligibilityResult(
        outcome=BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
        selected_service=SelectedService(_SERVICE),
        selected_master=SelectedMaster(_MASTER),
        other_online_master_ids=(),
        internal_reason_code=None,
    )


async def _create_due_availability_plan(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event_id: str,
    conv: str,
    kind: QueryKind,
) -> tuple[object, datetime, dict[str, Any]]:
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            _availability_inbound(event_id=event_id, conv=conv, kind=kind)
        )
        assert accepted.reply_plan is not None
        plan = accepted.reply_plan
        assert "booking" in plan.payload_json
        assert "availability_query" in plan.payload_json["booking"]
        due = plan.not_before
        plan_id = plan.id
        payload = dict(plan.payload_json)
    return plan_id, due, payload


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["AVAILABLE_DAYS", "SLOTS"])
async def test_availability_query_marker_survives_commit_new_session(
    session_factory: async_sessionmaker[AsyncSession],
    kind: QueryKind,
) -> None:
    plan_id, due, original = await _create_due_availability_plan(
        session_factory,
        event_id=f"avail-mark-{kind}",
        conv=f"avail-mark-{kind}-conv",
        kind=kind,
    )
    assert original["booking"]["availability_query"]["kind"] == kind

    worker = ReplyPlanWorker(session_factory, worker_id=f"avail-mark-{kind}")
    claim = await worker.claim_one(now=due)
    assert claim is not None
    assert claim.plan_id == plan_id

    async with session_scope(session_factory) as session:
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
        assert loaded.payload_json.get(BOOKING_RESOLUTION_STARTED_KEY) is True

    async with session_scope(session_factory) as session:
        refreshed = await reply_plan_repo.get_by_id(session, plan_id=plan_id)
        assert refreshed is not None
        assert refreshed.payload_json.get(BOOKING_RESOLUTION_STARTED_KEY) is True
        assert BOOKING_RESOLUTION_RESULT_KEY not in refreshed.payload_json
        assert refreshed.payload_json["booking"]["availability_query"] == (
            original["booking"]["availability_query"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["AVAILABLE_DAYS", "SLOTS"])
async def test_availability_query_marker_cas_single_winner(
    session_factory: async_sessionmaker[AsyncSession],
    kind: QueryKind,
) -> None:
    plan_id, due, _ = await _create_due_availability_plan(
        session_factory,
        event_id=f"avail-cas-{kind}",
        conv=f"avail-cas-{kind}-conv",
        kind=kind,
    )
    worker = ReplyPlanWorker(session_factory, worker_id=f"avail-cas-{kind}")
    claim = await worker.claim_one(now=due)
    assert claim is not None

    barrier = asyncio.Barrier(2)

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
    assert sorted([first, second]) == [False, True]

    async with session_scope(session_factory) as session:
        row = await reply_plan_repo.get_by_id(session, plan_id=plan_id)
        assert row is not None
        assert row.payload_json.get(BOOKING_RESOLUTION_STARTED_KEY) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["AVAILABLE_DAYS", "SLOTS"])
async def test_lease_expiry_during_availability_no_second_remote_no_stale_offer(
    session_factory: async_sessionmaker[AsyncSession],
    kind: QueryKind,
) -> None:
    """Lease reclaim while availability is in-flight: no second remote; no stale offer."""

    eligibility_calls: list[dict[str, Any]] = []
    day_calls: list[dict[str, Any]] = []
    slot_calls: list[dict[str, Any]] = []
    availability_entered = threading.Event()
    allow_availability_finish = threading.Event()

    class RecordingEligibility:
        def check_eligibility(
            self,
            service: SelectedService,
            master: SelectedMaster | None = None,
            *,
            include_alternatives: bool = False,
        ) -> BookingEligibilityResult:
            eligibility_calls.append(
                {
                    "service": service,
                    "master": master,
                    "include_alternatives": include_alternatives,
                }
            )
            return _allowed_result()

    class BlockingAvailability:
        def get_available_days(
            self,
            *,
            service_id: object,
            master_id: object,
            month: object,
        ) -> AvailableDaysResult:
            day_calls.append(
                {
                    "service_id": service_id,
                    "master_id": master_id,
                    "month": month,
                }
            )
            availability_entered.set()
            if not allow_availability_finish.wait(timeout=10.0):
                raise TimeoutError("availability days barrier timed out")
            return AvailableDaysResult(
                service_id=_SERVICE,
                master_id=_MASTER,
                month="2026-08",
                studio_today="2026-08-05",
                date_keys=("2026-08-06", "2026-08-07"),
            )

        def get_available_slots(
            self,
            *,
            service_id: object,
            master_id: object,
            date: object,
        ) -> AvailableSlotsResult:
            slot_calls.append(
                {
                    "service_id": service_id,
                    "master_id": master_id,
                    "date": date,
                }
            )
            availability_entered.set()
            if not allow_availability_finish.wait(timeout=10.0):
                raise TimeoutError("availability slots barrier timed out")
            return AvailableSlotsResult(
                service_id=_SERVICE,
                master_id=_MASTER,
                date="2026-08-06",
                studio_today="2026-08-05",
                slots=(
                    AvailableSlot(
                        slot_id="stale-s1",
                        starts_at=_SLOT_START,
                        master_id=_MASTER,
                        service_id=_SERVICE,
                    ),
                ),
            )

    flow = BookingFlowService(
        BookingEligibilityFlowService(RecordingEligibility()),
        BlockingAvailability(),  # type: ignore[arg-type]
    )
    plan_id, due, _ = await _create_due_availability_plan(
        session_factory,
        event_id=f"avail-lease-{kind}",
        conv=f"avail-lease-{kind}-conv",
        kind=kind,
    )

    first_worker = ReplyPlanWorker(
        session_factory,
        worker_id=f"avail-lease-first-{kind}",
        booking_flow=flow,
        lease_seconds=30,
    )
    claim_a = await first_worker.claim_one(now=due)
    assert claim_a is not None
    assert claim_a.plan_id == plan_id

    dispatch_a = asyncio.create_task(first_worker.dispatch_claimed(claim_a))

    for _ in range(200):
        if availability_entered.is_set():
            break
        await asyncio.sleep(0.05)
    assert availability_entered.is_set(), "first worker never reached availability"
    assert not dispatch_a.done()
    assert len(eligibility_calls) == 1

    async with session_scope(session_factory) as session:
        marked = await reply_plan_repo.get_by_id(session, plan_id=plan_id)
        assert marked is not None
        assert marked.payload_json.get(BOOKING_RESOLUTION_STARTED_KEY) is True
        await session.execute(
            update(ReplyPlan)
            .where(ReplyPlan.id == plan_id)
            .values(lease_until=due - timedelta(seconds=1))
        )

    class RefuseEligibility:
        def check_eligibility(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("second worker must not call eligibility")

    class RefuseAvailability:
        def get_available_days(self, **kwargs: object) -> object:
            raise AssertionError("second worker must not call available-days")

        def get_available_slots(self, **kwargs: object) -> object:
            raise AssertionError("second worker must not call slots")

    second_flow = BookingFlowService(
        BookingEligibilityFlowService(RefuseEligibility()),  # type: ignore[arg-type]
        RefuseAvailability(),  # type: ignore[arg-type]
    )
    second_worker = ReplyPlanWorker(
        session_factory,
        worker_id=f"avail-lease-second-{kind}",
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
        assert outbound.payload_json.get("booking_action") != (
            BookingDialogAction.OFFER_DAYS.value
        )
        assert outbound.payload_json.get("booking_action") != (
            BookingDialogAction.OFFER_SLOTS.value
        )
        plan = await reply_plan_repo.get_by_id(session, plan_id=plan_id)
        assert plan is not None
        assert plan.status == ReplyPlanStatus.DISPATCHED.value
        persisted = plan.payload_json.get(BOOKING_RESOLUTION_RESULT_KEY)
        assert isinstance(persisted, dict)
        assert persisted.get("booking_action") == (
            BookingDialogAction.SERVICE_UNAVAILABLE.value
        )
        assert persisted.get("booking_reason") == (
            BookingInternalReasonCode.BOOKING_RESOLUTION_INTERRUPTED.value
        )

    allow_availability_finish.set()
    with pytest.raises(StaleReplyPlanLeaseError):
        await asyncio.wait_for(dispatch_a, timeout=10.0)

    assert len(eligibility_calls) == 1
    if kind == "AVAILABLE_DAYS":
        assert len(day_calls) == 1
        assert slot_calls == []
    else:
        assert len(slot_calls) == 1
        assert day_calls == []

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
        assert "booking_available_date_keys" not in rows[0].payload_json
        assert "booking_offered_slot_ids" not in rows[0].payload_json
