"""Unit tests for CURSOR-19 booking flow consumer.

Injected fake eligibility-flow only. No live network, channels, outbound,
worker, DB, or production env. Consumer must not call decide_booking_dialog.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.core.booking_types import (
    AvailableSlot,
    BookingClientMessageKind,
    BookingDialogAction,
    BookingDomainError,
    BookingEligibilityOutcome,
    BookingEligibilityResult,
    BookingInternalReasonCode,
    ManagerHandoffDecision,
    SelectedMaster,
    SelectedService,
    ServiceUnavailableDecision,
    SlotOfferDecision,
    client_message_for_decision,
)
from app.services.booking_eligibility_flow import BookingEligibilityFlowService
from app.services.booking_flow import BookingFlowService

_SERVICE_UUID = "11111111-1111-4111-8111-111111111111"
_MASTER_UUID = "22222222-2222-4222-8222-222222222222"
_ALT_UUID = "33333333-3333-4333-8333-333333333333"
_SERVICE = SelectedService(_SERVICE_UUID)
_MASTER = SelectedMaster(_MASTER_UUID)
_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone(timedelta(hours=5)))
_CONSUMER_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "services" / "booking_flow.py"
)


def _slot(*, slot_id: str, master_id: str, minute: int = 0) -> AvailableSlot:
    return AvailableSlot(
        slot_id=slot_id,
        starts_at=datetime(2026, 8, 6, 5, minute, tzinfo=timezone.utc),
        master_id=master_id,
        service_id=_SERVICE_UUID,
    )


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
        if self.result is None:
            raise AssertionError("FakeEligibilityClient result not configured")
        return self.result


@dataclass
class RecordingFlow:
    """Fake BookingEligibilityFlowPort that records resolve calls."""

    decision: object
    calls: list[dict[str, Any]] = field(default_factory=list)
    error: BaseException | None = None

    def resolve(
        self,
        service: SelectedService,
        master: SelectedMaster | None,
        raw_slots: object,
        *,
        now: datetime,
        include_alternatives: bool,
        alternate_master_consent: bool = False,
    ) -> object:
        self.calls.append(
            {
                "service": service,
                "master": master,
                "raw_slots": raw_slots,
                "now": now,
                "include_alternatives": include_alternatives,
                "alternate_master_consent": alternate_master_consent,
            }
        )
        if self.error is not None:
            raise self.error
        return self.decision


def _allowed_result(*, other_ids: tuple[str, ...] = ()) -> BookingEligibilityResult:
    return BookingEligibilityResult(
        outcome=BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
        selected_service=_SERVICE,
        selected_master=_MASTER,
        other_online_master_ids=other_ids,
        internal_reason_code=None,
    )


# ---------------------------------------------------------------------------
# Consumer outcomes
# ---------------------------------------------------------------------------


def test_offer_slots_continues_booking_flow() -> None:
    client = FakeEligibilityClient(result=_allowed_result())
    flow = BookingEligibilityFlowService(client)
    consumer = BookingFlowService(flow)
    slots = (_slot(slot_id="s1", master_id=_MASTER_UUID),)
    decision = consumer.resolve(
        _SERVICE,
        _MASTER,
        slots,
        now=_NOW,
        include_alternatives=False,
    )
    assert isinstance(decision, SlotOfferDecision)
    assert decision.action is BookingDialogAction.OFFER_SLOTS
    assert [slot.master_id for slot in decision.offered_slots] == [_MASTER_UUID]
    assert len(client.calls) == 1


def test_manager_handoff_preserves_action_and_manager_only_reason() -> None:
    client = FakeEligibilityClient(
        result=BookingEligibilityResult(
            outcome=BookingEligibilityOutcome.MANAGER_HANDOFF,
            selected_service=_SERVICE,
            selected_master=_MASTER,
            other_online_master_ids=(),
            internal_reason_code="MANAGER_ONLY",
        )
    )
    consumer = BookingFlowService(BookingEligibilityFlowService(client))
    decision = consumer.resolve(
        _SERVICE,
        _MASTER,
        (_slot(slot_id="ignored", master_id=_MASTER_UUID),),
        now=_NOW,
        include_alternatives=False,
    )
    assert isinstance(decision, ManagerHandoffDecision)
    assert decision.action is BookingDialogAction.MANAGER_HANDOFF
    assert decision.internal_reason_code == "MANAGER_ONLY"
    assert "MANAGER_ONLY" not in client_message_for_decision(decision)
    assert len(client.calls) == 1


def test_service_unavailable_is_manager_bound_passthrough() -> None:
    client = FakeEligibilityClient(
        result=BookingEligibilityResult(
            outcome=BookingEligibilityOutcome.SERVICE_UNAVAILABLE,
            selected_service=_SERVICE,
            selected_master=_MASTER,
            other_online_master_ids=(),
            internal_reason_code="TIMEOUT",
        )
    )
    consumer = BookingFlowService(BookingEligibilityFlowService(client))
    decision = consumer.resolve(
        _SERVICE,
        _MASTER,
        (),
        now=_NOW,
        include_alternatives=False,
    )
    assert isinstance(decision, ServiceUnavailableDecision)
    assert decision.action is BookingDialogAction.SERVICE_UNAVAILABLE
    assert decision.internal_reason_code == "TIMEOUT"
    assert (
        decision.client_message_kind
        is BookingClientMessageKind.SERVICE_TEMPORARILY_UNAVAILABLE
    )
    assert len(client.calls) == 1


def test_missing_flow_fail_closed_without_eligibility_call() -> None:
    consumer = BookingFlowService(None)
    decision = consumer.resolve(
        _SERVICE,
        _MASTER,
        (_slot(slot_id="s1", master_id=_MASTER_UUID),),
        now=_NOW,
        include_alternatives=False,
    )
    assert isinstance(decision, ServiceUnavailableDecision)
    assert decision.action is BookingDialogAction.SERVICE_UNAVAILABLE
    assert (
        decision.internal_reason_code
        == BookingInternalReasonCode.BOOKING_FLOW_UNAVAILABLE.value
    )


def test_missing_eligibility_config_via_flow_fail_closed() -> None:
    """Flow present but client unset → CLIENT_UNAVAILABLE, still one consumer path."""

    consumer = BookingFlowService(BookingEligibilityFlowService(None))
    decision = consumer.resolve(
        _SERVICE,
        _MASTER,
        (_slot(slot_id="s1", master_id=_MASTER_UUID),),
        now=_NOW,
        include_alternatives=False,
    )
    assert isinstance(decision, ServiceUnavailableDecision)
    assert (
        decision.internal_reason_code
        == BookingInternalReasonCode.ELIGIBILITY_CLIENT_UNAVAILABLE.value
    )


def test_exactly_one_eligibility_call_per_consumer_decision() -> None:
    client = FakeEligibilityClient(result=_allowed_result())
    consumer = BookingFlowService(BookingEligibilityFlowService(client))
    slots = (_slot(slot_id="s1", master_id=_MASTER_UUID),)
    consumer.resolve(
        _SERVICE, _MASTER, slots, now=_NOW, include_alternatives=False
    )
    consumer.resolve(
        _SERVICE, _MASTER, slots, now=_NOW, include_alternatives=False
    )
    assert len(client.calls) == 2


def test_consumer_forwards_explicit_include_alternatives_and_consent() -> None:
    offer = SlotOfferDecision(
        action=BookingDialogAction.OFFER_SLOTS,
        offered_slots=(_slot(slot_id="alt", master_id=_ALT_UUID),),
        client_message_kind=BookingClientMessageKind.OFFER_SLOTS,
    )
    flow = RecordingFlow(decision=offer)
    consumer = BookingFlowService(flow)  # type: ignore[arg-type]
    slots = (_slot(slot_id="alt", master_id=_ALT_UUID),)
    decision = consumer.resolve(
        _SERVICE,
        _MASTER,
        slots,
        now=_NOW,
        include_alternatives=True,
        alternate_master_consent=True,
    )
    assert decision is offer
    assert len(flow.calls) == 1
    assert flow.calls[0]["include_alternatives"] is True
    assert flow.calls[0]["alternate_master_consent"] is True
    assert flow.calls[0]["service"] is _SERVICE
    assert flow.calls[0]["master"] is _MASTER


def test_include_alternatives_must_be_explicit_bool() -> None:
    flow = RecordingFlow(
        decision=ServiceUnavailableDecision(
            action=BookingDialogAction.SERVICE_UNAVAILABLE,
            client_message_kind=BookingClientMessageKind.SERVICE_TEMPORARILY_UNAVAILABLE,
            internal_reason_code="TIMEOUT",
        )
    )
    with pytest.raises(BookingDomainError):
        BookingFlowService(flow).resolve(  # type: ignore[arg-type]
            _SERVICE,
            _MASTER,
            (),
            now=_NOW,
            include_alternatives="yes",  # type: ignore[arg-type]
        )
    assert flow.calls == []


def test_flow_exception_fail_closed_single_call() -> None:
    flow = RecordingFlow(decision=None, error=RuntimeError("boom"))
    decision = BookingFlowService(flow).resolve(  # type: ignore[arg-type]
        _SERVICE,
        _MASTER,
        (),
        now=_NOW,
        include_alternatives=False,
    )
    assert isinstance(decision, ServiceUnavailableDecision)
    assert (
        decision.internal_reason_code
        == BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE.value
    )
    assert len(flow.calls) == 1


def test_unknown_flow_result_fail_closed() -> None:
    flow = RecordingFlow(decision={"action": "OFFER_SLOTS"})
    decision = BookingFlowService(flow).resolve(  # type: ignore[arg-type]
        _SERVICE,
        _MASTER,
        (_slot(slot_id="s1", master_id=_MASTER_UUID),),
        now=_NOW,
        include_alternatives=False,
    )
    assert isinstance(decision, ServiceUnavailableDecision)
    assert (
        decision.internal_reason_code
        == BookingInternalReasonCode.UNKNOWN_OUTCOME.value
    )
    assert len(flow.calls) == 1


# ---------------------------------------------------------------------------
# No policy bypass / redaction
# ---------------------------------------------------------------------------


def test_consumer_module_does_not_import_or_call_decide_booking_dialog() -> None:
    source = _CONSUMER_PATH.read_text(encoding="utf-8")
    assert "booking_dialog_policy" not in source
    assert "decide_booking_dialog" not in source
    assert "app.state" not in source
    assert "application.state" not in source
    # Executable surface: no policy import binding on the module.
    import app.services.booking_flow as consumer_mod

    assert not hasattr(consumer_mod, "decide_booking_dialog")
    assert "booking_dialog_policy" not in getattr(consumer_mod, "__dict__", {})


def test_consumer_does_not_bypass_flow_when_policy_monkeypatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if policy is patched to always offer slots, consumer must use flow."""

    import app.core.booking_dialog_policy as policy

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("consumer must not call decide_booking_dialog")

    monkeypatch.setattr(policy, "decide_booking_dialog", _boom)

    handoff = ManagerHandoffDecision(
        action=BookingDialogAction.MANAGER_HANDOFF,
        client_message_kind=BookingClientMessageKind.HANDOFF_DURING_MANAGER_HOURS,
        during_manager_hours=True,
        internal_reason_code="MANAGER_ONLY",
    )
    flow = RecordingFlow(decision=handoff)
    decision = BookingFlowService(flow).resolve(  # type: ignore[arg-type]
        _SERVICE,
        _MASTER,
        (_slot(slot_id="s1", master_id=_MASTER_UUID),),
        now=_NOW,
        include_alternatives=False,
    )
    assert decision is handoff
    assert decision.action is BookingDialogAction.MANAGER_HANDOFF
    assert len(flow.calls) == 1


def test_alternatives_rules_preserved_through_consumer() -> None:
    client = FakeEligibilityClient(result=_allowed_result(other_ids=(_ALT_UUID,)))
    consumer = BookingFlowService(BookingEligibilityFlowService(client))
    decision = consumer.resolve(
        _SERVICE,
        _MASTER,
        (_slot(slot_id="alt", master_id=_ALT_UUID),),
        now=_NOW,
        include_alternatives=True,
        alternate_master_consent=True,
    )
    assert isinstance(decision, SlotOfferDecision)
    assert [slot.master_id for slot in decision.offered_slots] == [_ALT_UUID]
    assert client.calls[0]["include_alternatives"] is True


def test_consumer_logs_never_leak_secrets(caplog: pytest.LogCaptureFixture) -> None:
    secret = "T" * 40
    flow = RecordingFlow(
        decision=None,
        error=RuntimeError(f"Authorization Bearer {secret}"),
    )
    with caplog.at_level(logging.INFO):
        decision = BookingFlowService(flow).resolve(  # type: ignore[arg-type]
            _SERVICE,
            _MASTER,
            (),
            now=_NOW,
            include_alternatives=False,
        )
    assert isinstance(decision, ServiceUnavailableDecision)
    blob = "\n".join(
        f"{record.getMessage()}{record.exc_text or ''}"
        for record in caplog.records
    )
    assert secret not in blob
    assert _SERVICE_UUID not in blob
    assert "Authorization" not in blob
    assert "booking_flow_consumer_fail_closed" in blob
