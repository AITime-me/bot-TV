"""BOT-REPLY-DURABLE-01 PostgreSQL proofs (M1/M3).

Real session_factory / workers / outbox rows — no mocked repo shortcuts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.booking_types import (
    BookingClientMessageKind,
    BookingDialogAction,
    BookingEligibilityOutcome,
    BookingEligibilityResult,
    SelectedMaster,
    SelectedService,
    render_client_message,
)
from app.db.session import session_scope
from app.models.outbox import DeliveryStatus, DestinationType, OutboxMessage
from app.models.reply_plan import ReplyPlan, ReplyPlanStatus
from app.schemas.booking_input import SyntheticBookingInput, SyntheticBookingSlot
from app.schemas.inbound import SyntheticInboundEvent
from app.services.booking_eligibility_flow import BookingEligibilityFlowService
from app.services.booking_flow import BookingFlowService
from app.services.inbound import InboundService
from app.services.outbound_arbiter import OutboundArbiter
from app.services.outbound_reply_text import OutboundReplyTextError
from app.services.reply_outbound import OutboundWorker, ReplyPlanWorker
from app.services.synthetic_outbound import SyntheticOutboundAdapter
from tests.pg_harness import truncate_foundation_tables

_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_SLOT_START = datetime(2026, 8, 6, 5, 0, tzinfo=timezone.utc)
_DECISION_AT = datetime(2026, 8, 5, 12, 0, tzinfo=timezone(timedelta(hours=5)))


@pytest_asyncio.fixture(autouse=True)
async def outbound_reply_text_pg_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


class _FakeEligibilityClient:
    def __init__(self, result: BookingEligibilityResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def check_eligibility(
        self,
        service: SelectedService,
        master: SelectedMaster | None = None,
        *,
        include_alternatives: bool = False,
    ) -> BookingEligibilityResult:
        self.calls.append(
            {
                "service": service,
                "master": master,
                "include_alternatives": include_alternatives,
            }
        )
        return self.result


def _booking_inbound(event_id: str, conv: str = "durable-text-conv") -> SyntheticInboundEvent:
    return SyntheticInboundEvent(
        external_conversation_id=conv,
        external_message_id=event_id,
        text="client-inbound-must-not-become-body",
        booking=SyntheticBookingInput(
            service_id=_SERVICE,
            master_id=_MASTER,
            include_alternatives=False,
            alternate_master_consent=False,
            slots=(
                SyntheticBookingSlot(
                    slot_id="durable-s1",
                    starts_at=_SLOT_START,
                    master_id=_MASTER,
                    service_id=_SERVICE,
                ),
            ),
            decision_at=_DECISION_AT,
        ),
    )


def _plain_inbound(event_id: str, conv: str = "non-booking-conv") -> SyntheticInboundEvent:
    return SyntheticInboundEvent(
        external_conversation_id=conv,
        external_message_id=event_id,
        text="plain-client-text",
    )


@pytest.mark.asyncio
async def test_non_booking_dispatch_fails_closed_without_outbound(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """M3: unrenderable non-booking reply → no SYNTHETIC_OUTBOUND, plan fail-closed."""

    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(_plain_inbound("nb-1"))
        assert accepted.reply_plan is not None
        plan_id = accepted.reply_plan.id
        due = accepted.reply_plan.not_before + timedelta(seconds=1)

    worker = ReplyPlanWorker(session_factory, worker_id="nb-plan")
    claim = await worker.claim_one(now=due)
    assert claim is not None
    with pytest.raises(OutboundReplyTextError):
        await worker.dispatch_claimed(claim)

    async with session_scope(session_factory) as session:
        plan = await session.get(ReplyPlan, plan_id)
        assert plan is not None
        assert plan.status in {
            ReplyPlanStatus.FAILED.value,
            ReplyPlanStatus.DEAD.value,
        }
        synth = await session.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .where(
                OutboxMessage.destination_type
                == DestinationType.SYNTHETIC_OUTBOUND.value
            )
        )
        assert synth == 0


@pytest.mark.asyncio
async def test_booking_outbound_persists_exact_text_and_reclaim_uses_db_only(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1: real PG persist of domain text + ADMITTED reclaim without re-render."""

    expected_text = render_client_message(BookingClientMessageKind.OFFER_SLOTS)
    eligibility = _FakeEligibilityClient(
        BookingEligibilityResult(
            outcome=BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
            selected_service=SelectedService(_SERVICE),
            selected_master=SelectedMaster(_MASTER),
            other_online_master_ids=(),
            internal_reason_code=None,
        )
    )
    flow = BookingFlowService(BookingEligibilityFlowService(eligibility))

    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            _booking_inbound("durable-offer-1")
        )
        assert accepted.reply_plan is not None
        due = accepted.reply_plan.not_before + timedelta(seconds=1)
        plan_id = accepted.reply_plan.id

    plan_worker = ReplyPlanWorker(
        session_factory,
        worker_id="durable-plan",
        booking_flow=flow,
    )
    claim = await plan_worker.claim_one(now=due)
    assert claim is not None
    dispatched = await plan_worker.dispatch_claimed(claim)
    assert dispatched.plan_status == ReplyPlanStatus.DISPATCHED.value
    assert dispatched.outbound_created is True

    async with session_scope(session_factory) as session:
        outbound = await session.get(OutboxMessage, dispatched.outbound_id)
        assert outbound is not None
        payload = dict(outbound.payload_json)
        assert payload.get("text") == expected_text
        assert payload.get("booking_action") == BookingDialogAction.OFFER_SLOTS.value
        assert payload.get("booking_offered_slot_ids") == ["durable-s1"]
        assert "booking_offered_slots" in payload
        assert payload.get("text") != "client-inbound-must-not-become-body"
        assert payload.get("text") != payload.get("synthetic_token")
        persisted_text = payload["text"]
        outbound_id = outbound.id

    sink = SyntheticOutboundAdapter()
    arbiter = OutboundArbiter(session_factory, sink=sink)
    out_worker = OutboundWorker(
        session_factory,
        worker_id="durable-out-crash",
        arbiter=arbiter,
    )
    first_claim = await out_worker.claim_one(now=due)
    assert first_claim is not None
    async with session_scope(session_factory) as session:
        request = await arbiter._admit_in_session(session, first_claim, now=due)
    assert request._text == persisted_text
    assert sink.deliver(request).outcome.value == "SUCCESS"

    async with session_scope(session_factory) as session:
        row = await session.get(OutboxMessage, outbound_id)
        assert row is not None
        assert row.delivery_status == DeliveryStatus.ADMITTED.value
        await session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == outbound_id)
            .values(lease_until=due - timedelta(seconds=1))
        )

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("delivery must not re-render after persistence")

    monkeypatch.setattr(
        "app.services.outbound_reply_text.render_client_message",
        _boom,
    )
    monkeypatch.setattr(
        "app.core.booking_types.render_client_message",
        _boom,
    )
    monkeypatch.setattr(
        "app.services.outbound_reply_text.render_text_for_booking_fields",
        _boom,
    )

    recovery = OutboundWorker(
        session_factory,
        worker_id="durable-out-recover",
        arbiter=arbiter,
    )
    recovered = await recovery.claim_one(now=due)
    assert recovered is not None
    assert recovered.delivery_status == DeliveryStatus.ADMITTED.value
    assert recovered.outbound_id == outbound_id
    # Claim payload must already carry the DB text (no re-render).
    assert recovered.payload_json.get("text") == persisted_text

    result = await recovery.process_claimed(recovered, now=due)
    assert result.delivery_status == DeliveryStatus.DELIVERED.value
    assert len(sink.calls) == 1
    assert sink.calls[0]._text == persisted_text

    async with session_scope(session_factory) as session:
        final = await session.get(OutboxMessage, outbound_id)
        assert final is not None
        assert final.delivery_status == DeliveryStatus.DELIVERED.value
        assert final.payload_json.get("text") == persisted_text
        plan = await session.get(ReplyPlan, plan_id)
        assert plan is not None
        assert plan.status == ReplyPlanStatus.DISPATCHED.value
