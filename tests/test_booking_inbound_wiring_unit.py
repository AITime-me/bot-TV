"""CURSOR-20 two-phase durable booking wiring unit tests.

Mutation-sensitive coverage without live channels, network, or PostgreSQL:
inbound → reply plan → phase1 marker → off-txn resolve → phase2 result/outbound.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.booking_types import (
    BookingClientMessageKind,
    BookingDialogAction,
    BookingEligibilityOutcome,
    BookingEligibilityResult,
    BookingInternalReasonCode,
    ManagerHandoffDecision,
    SelectedMaster,
    SelectedService,
)
from app.models.conversation import ConversationOwnership, HandoffState
from app.models.reply_plan import ReplyPlanStatus
from app.repositories.reply_plans import ReplyPlanClaim, StaleReplyPlanLeaseError
from app.schemas.booking_input import SyntheticBookingInput, SyntheticBookingSlot
from app.schemas.inbound import SyntheticInboundEvent
from app.schemas.ingress import SyntheticIngressEvent
from app.services.booking_eligibility_flow import BookingEligibilityFlowService
from app.services.booking_flow import BookingFlowService
from app.services.booking_synthetic import (
    BOOKING_RESOLUTION_RESULT_KEY,
    BOOKING_RESOLUTION_STARTED_KEY,
    booking_resolution_phase,
    build_synthetic_outbound_payload,
    client_reply_plan_payload,
    interrupted_booking_fields,
    resolve_booking_outbound_fields,
    BookingResolutionPhase,
)
from app.services.reply_outbound import ReplyPlanWorker

_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_ALT = "33333333-3333-4333-8333-333333333333"
_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone(timedelta(hours=5)))
_SLOT_START = datetime(2026, 8, 6, 5, 0, tzinfo=timezone.utc)


@dataclass
class FakeEligibilityClient:
    result: BookingEligibilityResult | None = None
    error: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

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
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _booking(
    *,
    master_id: str | None = _MASTER,
    include_alternatives: bool = False,
    consent: bool = False,
    slots: tuple[SyntheticBookingSlot, ...] | None = None,
) -> SyntheticBookingInput:
    if slots is None:
        slots = (
            SyntheticBookingSlot(
                slot_id="s1",
                starts_at=_SLOT_START,
                master_id=_MASTER,
                service_id=_SERVICE,
            ),
        )
    return SyntheticBookingInput(
        service_id=_SERVICE,
        master_id=master_id,
        include_alternatives=include_alternatives,
        alternate_master_consent=consent,
        slots=slots,
        decision_at=_NOW,
    )


def _allowed_result(**kwargs: Any) -> BookingEligibilityResult:
    return BookingEligibilityResult(
        outcome=BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
        selected_service=SelectedService(_SERVICE),
        selected_master=SelectedMaster(_MASTER),
        other_online_master_ids=(),
        internal_reason_code=None,
        **kwargs,
    )


def _claim_with_plan(plan: dict[str, Any]) -> ReplyPlanClaim:
    plan_id = uuid.uuid4()
    return ReplyPlanClaim(
        plan_id=plan_id,
        conversation_id=uuid.uuid4(),
        context_version=1,
        manager_epoch=0,
        event_seq_hwm=1,
        plan_type="CLIENT_REPLY",
        status=ReplyPlanStatus.PROCESSING.value,
        not_before=_NOW,
        bot_response_delay_ms=0,
        attempt_count=1,
        max_attempts=5,
        lease_owner="w",
        lease_token=uuid.uuid4(),
        lease_version=1,
        lease_until=_NOW + timedelta(minutes=1),
        correlation_id=uuid.uuid4(),
        payload_json=plan,
    )


@dataclass
class _TxnProbe:
    """Tracks whether a session_scope is open when resolve runs."""

    depth: int = 0
    resolve_seen_while_open: bool = False
    marker_before_resolve: bool = False
    resolve_calls: int = 0
    open_during_resolve: list[bool] = field(default_factory=list)


@dataclass
class _PlanStore:
    payload: dict[str, Any]
    max_attempts: int = 5
    marker_acquired_once: bool = False
    stale_on_phase2: bool = False
    existing_outbound: MagicMock | None = None
    inserted_payloads: list[dict[str, Any]] = field(default_factory=list)
    phase1_commits: int = 0
    phase2_commits: int = 0
    _phase: int = 0


def _conversation_for(claim: ReplyPlanClaim) -> MagicMock:
    conversation = MagicMock()
    conversation.context_version = claim.context_version
    conversation.ownership = ConversationOwnership.BOT.value
    conversation.handoff_state = HandoffState.BOT_ACTIVE.value
    conversation.manager_takeover_at = None
    conversation.manager_epoch = claim.manager_epoch
    conversation.current_event_seq = claim.event_seq_hwm
    return conversation


def _plan_row_for(claim: ReplyPlanClaim, store: _PlanStore) -> MagicMock:
    plan_row = MagicMock()
    plan_row.status = ReplyPlanStatus.PROCESSING.value
    plan_row.lease_token = claim.lease_token
    plan_row.lease_version = claim.lease_version
    plan_row.manager_epoch = claim.manager_epoch
    plan_row.event_seq_hwm = claim.event_seq_hwm
    plan_row.max_attempts = store.max_attempts
    plan_row.payload_json = store.payload
    return plan_row


def _completed_for(claim: ReplyPlanClaim) -> MagicMock:
    completed = MagicMock()
    completed.id = claim.plan_id
    completed.status = ReplyPlanStatus.DISPATCHED.value
    completed.conversation_id = claim.conversation_id
    completed.context_version = claim.context_version
    completed.correlation_id = claim.correlation_id
    return completed


async def _run_booking_dispatch(
    *,
    claim: ReplyPlanClaim,
    flow: BookingFlowService,
    store: _PlanStore,
    probe: _TxnProbe,
    resolve_wrapper: Any | None = None,
) -> Any:
    conversation = _conversation_for(claim)
    completed = _completed_for(claim)
    outbound_row = MagicMock()
    outbound_row.id = uuid.uuid4()

    @asynccontextmanager
    async def fake_scope(_factory: object):
        probe.depth += 1
        current_phase = store._phase + 1
        store._phase = current_phase
        try:
            yield AsyncMock()
        finally:
            if current_phase == 1:
                store.phase1_commits += 1
            else:
                store.phase2_commits += 1
            probe.depth -= 1

    async def get_by_id(_session: object, *, plan_id: uuid.UUID) -> MagicMock:
        if store.stale_on_phase2 and store._phase >= 2:
            raise StaleReplyPlanLeaseError("REPLY_PLAN_STALE_CONTEXT")
        return _plan_row_for(claim, store)

    async def try_mark(
        _session: object,
        *,
        plan_id: uuid.UUID,
        lease_token: uuid.UUID,
        lease_version: int,
    ) -> bool:
        assert probe.depth > 0
        if store.payload.get(BOOKING_RESOLUTION_STARTED_KEY) is True:
            return False
        if BOOKING_RESOLUTION_RESULT_KEY in store.payload:
            return False
        store.payload = dict(store.payload)
        store.payload[BOOKING_RESOLUTION_STARTED_KEY] = True
        store.marker_acquired_once = True
        return True

    async def persist_result(
        _session: object,
        *,
        plan_id: uuid.UUID,
        lease_token: uuid.UUID,
        lease_version: int,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        assert probe.depth > 0
        if isinstance(store.payload.get(BOOKING_RESOLUTION_RESULT_KEY), dict):
            return dict(store.payload)
        store.payload = dict(store.payload)
        store.payload[BOOKING_RESOLUTION_STARTED_KEY] = True
        store.payload[BOOKING_RESOLUTION_RESULT_KEY] = dict(result)
        return dict(store.payload)

    async def get_outbound(_session: object, *, idempotency_key: str) -> MagicMock | None:
        return store.existing_outbound

    async def insert_outbound(_session: object, **kwargs: Any):
        store.inserted_payloads.append(kwargs["payload_json"])
        return outbound_row, True

    async def tracked_to_thread(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        probe.resolve_calls += 1
        open_now = probe.depth > 0
        probe.open_during_resolve.append(open_now)
        if open_now:
            probe.resolve_seen_while_open = True
        if store.payload.get(BOOKING_RESOLUTION_STARTED_KEY) is True:
            probe.marker_before_resolve = True
        if resolve_wrapper is not None:
            return resolve_wrapper(*args, **kwargs)
        # Always invoke the real helper with worker kwargs (avoid MagicMock-in-thread).
        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: resolve_booking_outbound_fields(*args, **kwargs),
        )

    with (
        patch("app.services.reply_outbound.session_scope", fake_scope),
        patch(
            "app.services.reply_outbound.conversation_repo.lock_for_update",
            AsyncMock(return_value=conversation),
        ),
        patch(
            "app.services.reply_outbound.reply_plan_repo.get_by_id",
            side_effect=get_by_id,
        ),
        patch(
            "app.services.reply_outbound.reply_plan_repo.try_mark_booking_resolution_started",
            side_effect=try_mark,
        ),
        patch(
            "app.services.reply_outbound.reply_plan_repo.persist_booking_resolution_result",
            side_effect=persist_result,
        ),
        patch(
            "app.services.reply_outbound.outbound_repo.get_by_idempotency_key",
            side_effect=get_outbound,
        ),
        patch(
            "app.services.reply_outbound.outbound_repo.insert_synthetic_outbound_if_absent",
            side_effect=insert_outbound,
        ),
        patch(
            "app.services.reply_outbound.reply_plan_repo.complete_dispatched_with_lease",
            AsyncMock(return_value=completed),
        ),
        patch(
            "app.services.reply_outbound.enqueue_reply_plan_state_changed",
            AsyncMock(),
        ),
        patch(
            "app.services.reply_outbound.asyncio.to_thread",
            side_effect=tracked_to_thread,
        ),
    ):
        worker = ReplyPlanWorker(AsyncMock(), worker_id="unit", booking_flow=flow)
        return await worker.dispatch_claimed(claim)


# ---------------------------------------------------------------------------
# Schema / inbound payload
# ---------------------------------------------------------------------------


def test_booking_input_rejects_free_text_intent_fields() -> None:
    with pytest.raises(Exception):
        SyntheticBookingInput.model_validate(
            {
                "service_id": _SERVICE,
                "include_alternatives": False,
                "decision_at": _NOW.isoformat(),
                "intent": "хочу записаться",
            }
        )


def test_inbound_event_booking_not_copied_into_inbox_payload() -> None:
    event = SyntheticInboundEvent(
        external_conversation_id="c1",
        external_message_id="m1",
        text="fixture-placeholder",
        booking=_booking(),
    )
    inbox_payload = event.safe_payload()
    assert inbox_payload == {
        "schema": "synthetic.inbound.v1",
        "text": "fixture-placeholder",
    }
    assert "booking" not in inbox_payload
    plan = client_reply_plan_payload(inbox_id="inbox-1", booking=event.booking)
    assert plan["booking"]["service_id"] == _SERVICE
    assert "text" not in plan


def test_path_helpers_allowed_handoff_unavailable_malformed() -> None:
    client = FakeEligibilityClient(result=_allowed_result())
    flow = BookingFlowService(BookingEligibilityFlowService(client))
    plan = client_reply_plan_payload(inbox_id="i1", booking=_booking())
    outbound = build_synthetic_outbound_payload(plan, booking_flow=flow)
    assert outbound["booking_action"] == BookingDialogAction.OFFER_SLOTS.value
    assert len(client.calls) == 1

    client.calls.clear()
    client.result = BookingEligibilityResult(
        outcome=BookingEligibilityOutcome.MANAGER_HANDOFF,
        selected_service=SelectedService(_SERVICE),
        selected_master=SelectedMaster(_MASTER),
        other_online_master_ids=(),
        internal_reason_code="MANAGER_ONLY",
    )
    outbound = build_synthetic_outbound_payload(plan, booking_flow=flow)
    assert outbound["booking_action"] == BookingDialogAction.MANAGER_HANDOFF.value
    assert outbound["booking_reason"] == "MANAGER_ONLY"

    client.calls.clear()
    client.result = BookingEligibilityResult(
        outcome=BookingEligibilityOutcome.SERVICE_UNAVAILABLE,
        selected_service=SelectedService(_SERVICE),
        selected_master=SelectedMaster(_MASTER),
        other_online_master_ids=(),
        internal_reason_code="TIMEOUT",
    )
    outbound = build_synthetic_outbound_payload(plan, booking_flow=flow)
    assert outbound["booking_action"] == BookingDialogAction.SERVICE_UNAVAILABLE.value

    bad = {
        "schema": "synthetic.reply_plan.v1",
        "plan_type": "CLIENT_REPLY",
        "synthetic_token": "SYNTHETIC_OK",
        "inbox_id": "i1",
        "booking": {"service_id": "bad id with spaces", "include_alternatives": False},
    }
    outbound = build_synthetic_outbound_payload(
        bad, booking_flow=BookingFlowService(None)
    )
    assert outbound["booking_action"] == BookingDialogAction.SERVICE_UNAVAILABLE.value


def test_missing_flow_and_non_booking_helpers() -> None:
    plan = client_reply_plan_payload(inbox_id="i1", booking=_booking())
    outbound = build_synthetic_outbound_payload(
        plan, booking_flow=BookingFlowService(None)
    )
    assert (
        outbound["booking_reason"]
        == BookingInternalReasonCode.BOOKING_FLOW_UNAVAILABLE.value
    )
    plain = client_reply_plan_payload(inbox_id="i1", booking=None)
    assert build_synthetic_outbound_payload(plain) == {
        "schema": "synthetic.outbound.v1",
        "source_schema": "synthetic.reply_plan.v1",
        "plan_type": "CLIENT_REPLY",
        "synthetic_token": "SYNTHETIC_OK",
    }


def test_interrupted_fields_are_allowlisted() -> None:
    fields = interrupted_booking_fields()
    assert fields["booking_action"] == BookingDialogAction.SERVICE_UNAVAILABLE.value
    assert (
        fields["booking_reason"]
        == BookingInternalReasonCode.BOOKING_RESOLUTION_INTERRUPTED.value
    )
    assert (
        BookingInternalReasonCode.BOOKING_RESOLUTION_INTERRUPTED.value
        in {c.value for c in BookingInternalReasonCode}
    )


def test_resolution_phase_state_machine() -> None:
    base = client_reply_plan_payload(inbox_id="i1", booking=_booking())
    assert booking_resolution_phase(base) == BookingResolutionPhase.NEEDS_REMOTE
    started = dict(base)
    started[BOOKING_RESOLUTION_STARTED_KEY] = True
    assert booking_resolution_phase(started) == BookingResolutionPhase.INTERRUPTED
    done = dict(started)
    done[BOOKING_RESOLUTION_RESULT_KEY] = interrupted_booking_fields()
    assert booking_resolution_phase(done) == BookingResolutionPhase.HAS_RESULT
    assert (
        booking_resolution_phase({"schema": "synthetic.reply_plan.v1"})
        == BookingResolutionPhase.NON_BOOKING
    )


# ---------------------------------------------------------------------------
# Two-phase worker path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_runs_outside_db_transaction_and_after_marker() -> None:
    client = FakeEligibilityClient(result=_allowed_result())
    flow = BookingFlowService(BookingEligibilityFlowService(client))
    plan = client_reply_plan_payload(inbox_id="i1", booking=_booking())
    claim = _claim_with_plan(plan)
    store = _PlanStore(payload=dict(plan))
    probe = _TxnProbe()

    result = await _run_booking_dispatch(
        claim=claim, flow=flow, store=store, probe=probe
    )

    assert result.outbound_created is True
    assert probe.resolve_calls == 1
    assert probe.resolve_seen_while_open is False
    assert probe.open_during_resolve == [False]
    assert probe.marker_before_resolve is True
    assert store.marker_acquired_once is True
    assert store.phase1_commits == 1
    assert store.phase2_commits == 1
    assert isinstance(store.payload.get(BOOKING_RESOLUTION_RESULT_KEY), dict)
    assert store.inserted_payloads[0]["booking_action"] == (
        BookingDialogAction.OFFER_SLOTS.value
    )
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_event_loop_not_blocked_by_sync_fake_client() -> None:
    """Real asyncio.to_thread: probe progresses while resolve thread is blocked.

    Mutation-sensitive: a sync (non-threaded) resolve blocks the loop so the
    probe cannot observe an in-flight blocked resolve_task.
    """

    resolve_entered = threading.Event()
    allow_resolve_finish = threading.Event()

    class BlockingClient:
        calls: list[dict[str, Any]] = []

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
            resolve_entered.set()
            if not allow_resolve_finish.wait(timeout=5.0):
                raise TimeoutError("resolve barrier release timed out")
            return _allowed_result()

    client = BlockingClient()
    flow = BookingFlowService(BookingEligibilityFlowService(client))
    worker = ReplyPlanWorker(AsyncMock(), worker_id="unit", booking_flow=flow)
    plan = client_reply_plan_payload(inbox_id="i1", booking=_booking())

    loop_progressed = asyncio.Event()

    async def probe() -> None:
        # Busy-yield until the worker thread signals entry, then mark progress
        # while resolve is still blocked on allow_resolve_finish.
        for _ in range(500):
            if resolve_entered.is_set():
                break
            await asyncio.sleep(0)
        assert resolve_entered.is_set()
        loop_progressed.set()
        for _ in range(10):
            await asyncio.sleep(0)

    resolve_task = asyncio.create_task(
        worker._resolve_booking_off_transaction(plan)
    )
    probe_task = asyncio.create_task(probe())

    await asyncio.wait_for(loop_progressed.wait(), timeout=2.0)
    # Critical: resolve must still be in-flight (blocked in thread) when the
    # event loop already ran the probe coroutine.
    assert not resolve_task.done()
    assert resolve_entered.is_set()

    allow_resolve_finish.set()
    fields = await asyncio.wait_for(resolve_task, timeout=2.0)
    await probe_task

    assert len(client.calls) == 1
    assert fields["booking_action"] == BookingDialogAction.OFFER_SLOTS.value


@pytest.mark.asyncio
async def test_crash_after_marker_before_resolve_retries_without_remote() -> None:
    client = FakeEligibilityClient(result=_allowed_result())
    flow = BookingFlowService(BookingEligibilityFlowService(client))
    plan = client_reply_plan_payload(inbox_id="i1", booking=_booking())
    # Simulate durable marker left by a crashed attempt.
    plan = dict(plan)
    plan[BOOKING_RESOLUTION_STARTED_KEY] = True
    claim = _claim_with_plan(plan)
    store = _PlanStore(payload=dict(plan))
    probe = _TxnProbe()

    result = await _run_booking_dispatch(
        claim=claim, flow=flow, store=store, probe=probe
    )

    assert result.outbound_created is True
    assert probe.resolve_calls == 0
    assert client.calls == []
    assert store.inserted_payloads[0]["booking_action"] == (
        BookingDialogAction.SERVICE_UNAVAILABLE.value
    )
    assert store.inserted_payloads[0]["booking_reason"] == (
        BookingInternalReasonCode.BOOKING_RESOLUTION_INTERRUPTED.value
    )
    assert store.payload[BOOKING_RESOLUTION_RESULT_KEY]["booking_reason"] == (
        BookingInternalReasonCode.BOOKING_RESOLUTION_INTERRUPTED.value
    )


@pytest.mark.asyncio
async def test_crash_after_resolve_before_result_persistence_no_second_remote() -> None:
    """Marker present, no result → same interrupted path (lost successful HTTP)."""

    client = FakeEligibilityClient(result=_allowed_result())
    flow = BookingFlowService(BookingEligibilityFlowService(client))
    plan = client_reply_plan_payload(inbox_id="i1", booking=_booking())
    plan = dict(plan)
    plan[BOOKING_RESOLUTION_STARTED_KEY] = True
    claim = _claim_with_plan(plan)
    store = _PlanStore(payload=dict(plan))
    probe = _TxnProbe()

    await _run_booking_dispatch(claim=claim, flow=flow, store=store, probe=probe)
    assert probe.resolve_calls == 0
    assert client.calls == []


@pytest.mark.asyncio
async def test_successful_finalize_one_result_one_outbound_one_eligibility() -> None:
    client = FakeEligibilityClient(result=_allowed_result())
    flow = BookingFlowService(BookingEligibilityFlowService(client))
    plan = client_reply_plan_payload(inbox_id="i1", booking=_booking())
    claim = _claim_with_plan(plan)
    store = _PlanStore(payload=dict(plan))
    probe = _TxnProbe()

    result = await _run_booking_dispatch(
        claim=claim, flow=flow, store=store, probe=probe
    )
    assert result.outbound_created is True
    assert len(store.inserted_payloads) == 1
    assert len(client.calls) == 1
    assert probe.resolve_calls == 1
    assert store.payload[BOOKING_RESOLUTION_RESULT_KEY]["booking_action"] == (
        BookingDialogAction.OFFER_SLOTS.value
    )


@pytest.mark.asyncio
async def test_existing_outbound_skips_resolve_and_marker() -> None:
    client = FakeEligibilityClient(result=_allowed_result())
    flow = BookingFlowService(BookingEligibilityFlowService(client))
    plan = client_reply_plan_payload(inbox_id="i1", booking=_booking())
    claim = _claim_with_plan(plan)
    existing = MagicMock()
    existing.id = uuid.uuid4()
    store = _PlanStore(payload=dict(plan), existing_outbound=existing)
    probe = _TxnProbe()

    result = await _run_booking_dispatch(
        claim=claim, flow=flow, store=store, probe=probe
    )
    assert result.outbound_created is False
    assert result.outbound_id == existing.id
    assert probe.resolve_calls == 0
    assert client.calls == []
    assert store.marker_acquired_once is False
    assert store.inserted_payloads == []


@pytest.mark.asyncio
async def test_existing_saved_result_skips_resolve() -> None:
    client = FakeEligibilityClient(result=_allowed_result())
    flow = BookingFlowService(BookingEligibilityFlowService(client))
    plan = client_reply_plan_payload(inbox_id="i1", booking=_booking())
    plan = dict(plan)
    plan[BOOKING_RESOLUTION_STARTED_KEY] = True
    plan[BOOKING_RESOLUTION_RESULT_KEY] = {
        "booking_action": BookingDialogAction.MANAGER_HANDOFF.value,
        "booking_reason": "MANAGER_ONLY",
    }
    claim = _claim_with_plan(plan)
    store = _PlanStore(payload=dict(plan))
    probe = _TxnProbe()

    result = await _run_booking_dispatch(
        claim=claim, flow=flow, store=store, probe=probe
    )
    assert result.outbound_created is True
    assert probe.resolve_calls == 0
    assert client.calls == []
    assert store.inserted_payloads[0]["booking_action"] == (
        BookingDialogAction.MANAGER_HANDOFF.value
    )


@pytest.mark.asyncio
async def test_lease_race_on_phase2_does_not_publish_stale_result() -> None:
    client = FakeEligibilityClient(result=_allowed_result())
    flow = BookingFlowService(BookingEligibilityFlowService(client))
    plan = client_reply_plan_payload(inbox_id="i1", booking=_booking())
    claim = _claim_with_plan(plan)
    store = _PlanStore(payload=dict(plan), stale_on_phase2=True)
    probe = _TxnProbe()

    with pytest.raises(StaleReplyPlanLeaseError):
        await _run_booking_dispatch(claim=claim, flow=flow, store=store, probe=probe)

    assert store.inserted_payloads == []
    # Remote may have run after phase1, but stale phase2 must not publish.
    assert BOOKING_RESOLUTION_RESULT_KEY not in store.payload or (
        store.inserted_payloads == []
    )


@pytest.mark.asyncio
async def test_non_booking_path_single_transaction_unchanged() -> None:
    flow = BookingFlowService(BookingEligibilityFlowService(FakeEligibilityClient()))
    plan = client_reply_plan_payload(inbox_id="i1", booking=None)
    claim = _claim_with_plan(plan)
    conversation = _conversation_for(claim)
    completed = _completed_for(claim)
    outbound_row = MagicMock()
    outbound_row.id = uuid.uuid4()
    inserted: list[dict[str, Any]] = []
    scopes = 0

    @asynccontextmanager
    async def fake_scope(_factory: object):
        nonlocal scopes
        scopes += 1
        yield AsyncMock()

    async def capture_insert(_session: object, **kwargs: Any):
        inserted.append(kwargs["payload_json"])
        return outbound_row, True

    with (
        patch("app.services.reply_outbound.session_scope", fake_scope),
        patch(
            "app.services.reply_outbound.conversation_repo.lock_for_update",
            AsyncMock(return_value=conversation),
        ),
        patch(
            "app.services.reply_outbound.reply_plan_repo.get_by_id",
            AsyncMock(return_value=_plan_row_for(claim, _PlanStore(payload=dict(plan)))),
        ),
        patch(
            "app.services.reply_outbound.outbound_repo.get_by_idempotency_key",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.reply_outbound.outbound_repo.insert_synthetic_outbound_if_absent",
            side_effect=capture_insert,
        ),
        patch(
            "app.services.reply_outbound.reply_plan_repo.complete_dispatched_with_lease",
            AsyncMock(return_value=completed),
        ),
        patch(
            "app.services.reply_outbound.enqueue_reply_plan_state_changed",
            AsyncMock(),
        ),
        patch(
            "app.services.reply_outbound.resolve_booking_outbound_fields",
            side_effect=AssertionError("non-booking must not resolve"),
        ),
        patch(
            "app.services.reply_outbound.reply_plan_repo.try_mark_booking_resolution_started",
            side_effect=AssertionError("non-booking must not mark"),
        ),
    ):
        worker = ReplyPlanWorker(AsyncMock(), worker_id="unit", booking_flow=flow)
        result = await worker.dispatch_claimed(claim)

    assert result.outbound_created is True
    assert scopes == 1
    assert inserted == [
        {
            "schema": "synthetic.outbound.v1",
            "source_schema": "synthetic.reply_plan.v1",
            "plan_type": "CLIENT_REPLY",
            "synthetic_token": "SYNTHETIC_OK",
        }
    ]


def test_worker_source_guarantees_two_phase_and_to_thread() -> None:
    source = inspect.getsource(ReplyPlanWorker)
    assert "asyncio.to_thread" in source
    assert "_booking_phase1_prepare" in source
    assert "_booking_phase2_finalize" in source
    assert "try_mark_booking_resolution_started" in source
    assert "BOOKING_RESOLUTION_INTERRUPTED" in inspect.getsource(
        interrupted_booking_fields
    )
    # resolve helper is not invoked inside session_scope blocks of phase methods
    phase1 = inspect.getsource(ReplyPlanWorker._booking_phase1_prepare)
    phase2 = inspect.getsource(ReplyPlanWorker._booking_phase2_finalize)
    assert "resolve_booking_outbound_fields" not in phase1
    assert "resolve_booking_outbound_fields" not in phase2
    assert "booking_flow.resolve" not in phase1
    assert "booking_flow.resolve" not in phase2


def test_ingress_envelope_booking_reaches_client_reply_plan_without_text() -> None:
    booking = _booking()
    ingress = SyntheticIngressEvent(
        external_event_id="e1",
        external_conversation_id="c1",
        text="client-secret-text",
        booking=booking,
    )
    envelope = ingress.safe_envelope()
    inbound = SyntheticInboundEvent(
        external_conversation_id=ingress.external_conversation_id,
        external_message_id=ingress.external_event_id,
        text=envelope["text"],
        booking=SyntheticBookingInput.model_validate(envelope["booking"]),
    )
    plan = client_reply_plan_payload(inbox_id="inbox-x", booking=inbound.booking)
    assert "client-secret-text" not in str(plan)


def test_unknown_reason_scrubbed_to_allowlist() -> None:
    decision = ManagerHandoffDecision(
        action=BookingDialogAction.MANAGER_HANDOFF,
        client_message_kind=BookingClientMessageKind.HANDOFF_DURING_MANAGER_HOURS,
        during_manager_hours=True,
        internal_reason_code="NOT_A_REAL_REASON",
    )
    from app.services.booking_synthetic import decision_to_outbound_fields

    fields = decision_to_outbound_fields(decision)
    assert fields["booking_reason"] == BookingInternalReasonCode.UNKNOWN_OUTCOME.value


def test_alternatives_and_consent_forwarded() -> None:
    client = FakeEligibilityClient(
        result=BookingEligibilityResult(
            outcome=BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
            selected_service=SelectedService(_SERVICE),
            selected_master=SelectedMaster(_MASTER),
            other_online_master_ids=(_ALT,),
            internal_reason_code=None,
        )
    )
    flow = BookingFlowService(BookingEligibilityFlowService(client))
    slots = (
        SyntheticBookingSlot(
            slot_id="alt1",
            starts_at=_SLOT_START,
            master_id=_ALT,
            service_id=_SERVICE,
        ),
    )
    plan = client_reply_plan_payload(
        inbox_id="i1",
        booking=_booking(include_alternatives=True, consent=True, slots=slots),
    )
    outbound = build_synthetic_outbound_payload(plan, booking_flow=flow)
    assert outbound["booking_offered_slot_ids"] == ["alt1"]
    assert client.calls[0]["include_alternatives"] is True
