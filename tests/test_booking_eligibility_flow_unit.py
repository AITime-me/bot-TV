"""Unit tests for CURSOR-18 booking eligibility flow wiring.

Injected fake eligibility clients only. No live network, channels, outbound,
or production env.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.core.booking_types import (
    AvailableSlot,
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

_SERVICE_UUID = "11111111-1111-4111-8111-111111111111"
_MASTER_UUID = "22222222-2222-4222-8222-222222222222"
_ALT_UUID = "33333333-3333-4333-8333-333333333333"
_SERVICE = SelectedService(_SERVICE_UUID)
_MASTER = SelectedMaster(_MASTER_UUID)
_NOW = datetime(2026, 8, 5, 7, 0, tzinfo=timezone(timedelta(hours=5)))


def _slot(*, slot_id: str, master_id: str, minute: int = 0) -> AvailableSlot:
    return AvailableSlot(
        slot_id=slot_id,
        starts_at=datetime(2026, 8, 6, 5, minute, tzinfo=timezone.utc),
        master_id=master_id,
        service_id=_SERVICE_UUID,
    )


@dataclass
class FakeEligibilityClient:
    """Recording fake. Returns a fixed result or raises."""

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


def _allowed(
    *,
    master: SelectedMaster | None = _MASTER,
    other_ids: tuple[str, ...] = (),
) -> BookingEligibilityResult:
    return BookingEligibilityResult(
        outcome=BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
        selected_service=_SERVICE,
        selected_master=master,
        other_online_master_ids=other_ids,
        internal_reason_code=None,
    )


def _handoff(*, reason: str = "MANAGER_ONLY") -> BookingEligibilityResult:
    return BookingEligibilityResult(
        outcome=BookingEligibilityOutcome.MANAGER_HANDOFF,
        selected_service=_SERVICE,
        selected_master=_MASTER,
        other_online_master_ids=(),
        internal_reason_code=reason,
    )


def _unavailable(*, reason: str = "TIMEOUT") -> BookingEligibilityResult:
    return BookingEligibilityResult(
        outcome=BookingEligibilityOutcome.SERVICE_UNAVAILABLE,
        selected_service=_SERVICE,
        selected_master=_MASTER,
        other_online_master_ids=(),
        internal_reason_code=reason,
    )


# ---------------------------------------------------------------------------
# Allowed / handoff / missing client / remote failure / unknown
# ---------------------------------------------------------------------------


def test_self_booking_allowed_continues_to_slot_offer() -> None:
    fake = FakeEligibilityClient(result=_allowed())
    slots = (_slot(slot_id="s1", master_id=_MASTER_UUID),)
    decision = BookingEligibilityFlowService(fake).resolve(
        _SERVICE,
        _MASTER,
        slots,
        now=_NOW,
        include_alternatives=False,
    )
    assert isinstance(decision, SlotOfferDecision)
    assert decision.action is BookingDialogAction.OFFER_SLOTS
    assert [slot.master_id for slot in decision.offered_slots] == [_MASTER_UUID]
    assert len(fake.calls) == 1
    assert fake.calls[0]["include_alternatives"] is False
    assert fake.calls[0]["service"] is _SERVICE
    assert fake.calls[0]["master"] is _MASTER


def test_manager_handoff_preserves_manager_only_reason() -> None:
    fake = FakeEligibilityClient(result=_handoff(reason="MANAGER_ONLY"))
    slots = (_slot(slot_id="ignored", master_id=_MASTER_UUID),)
    decision = BookingEligibilityFlowService(fake).resolve(
        _SERVICE,
        _MASTER,
        slots,
        now=_NOW,
        include_alternatives=False,
    )
    assert isinstance(decision, ManagerHandoffDecision)
    assert decision.action is BookingDialogAction.MANAGER_HANDOFF
    assert decision.internal_reason_code == "MANAGER_ONLY"
    assert "MANAGER_ONLY" not in client_message_for_decision(decision)
    assert len(fake.calls) == 1


def test_missing_client_fail_closed_without_eligibility_call() -> None:
    """None client: no eligibility call and exact CLIENT_UNAVAILABLE reason."""

    slots = (_slot(slot_id="s1", master_id=_MASTER_UUID),)
    flow = BookingEligibilityFlowService(None)
    assert flow._client is None  # noqa: SLF001
    decision = flow.resolve(
        _SERVICE,
        _MASTER,
        slots,
        now=_NOW,
        include_alternatives=False,
    )
    assert isinstance(decision, ServiceUnavailableDecision)
    assert decision.action is BookingDialogAction.SERVICE_UNAVAILABLE
    assert (
        decision.internal_reason_code
        == BookingInternalReasonCode.ELIGIBILITY_CLIENT_UNAVAILABLE.value
    )
    text = client_message_for_decision(decision)
    assert "менеджер" in text.lower()
    assert _SERVICE_UUID not in text


def test_remote_failure_fail_closed_single_call() -> None:
    fake = FakeEligibilityClient(result=_unavailable(reason="TIMEOUT"))
    decision = BookingEligibilityFlowService(fake).resolve(
        _SERVICE,
        _MASTER,
        (_slot(slot_id="s1", master_id=_MASTER_UUID),),
        now=_NOW,
        include_alternatives=False,
    )
    assert isinstance(decision, ServiceUnavailableDecision)
    assert decision.internal_reason_code == "TIMEOUT"
    assert len(fake.calls) == 1


def test_client_exception_fail_closed_without_retry() -> None:
    fake = FakeEligibilityClient(error=RuntimeError("boom"))
    decision = BookingEligibilityFlowService(fake).resolve(
        _SERVICE,
        _MASTER,
        (_slot(slot_id="s1", master_id=_MASTER_UUID),),
        now=_NOW,
        include_alternatives=True,
    )
    assert isinstance(decision, ServiceUnavailableDecision)
    assert (
        decision.internal_reason_code
        == BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE.value
    )
    assert len(fake.calls) == 1


def test_unknown_result_type_fail_closed() -> None:
    @dataclass
    class _BadClient:
        calls: int = 0

        def check_eligibility(
            self,
            service: SelectedService,
            master: SelectedMaster | None = None,
            *,
            include_alternatives: bool = False,
        ) -> object:
            self.calls += 1
            return {"outcome": "SELF_BOOKING_ALLOWED"}  # not a domain result

    bad = _BadClient()
    decision = BookingEligibilityFlowService(bad).resolve(  # type: ignore[arg-type]
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
    assert bad.calls == 1


def test_unknown_eligibility_outcome_never_self_books() -> None:
    """Adapter-style fail-closed UNKNOWN_OUTCOME must not open slots."""

    fake = FakeEligibilityClient(
        result=BookingEligibilityResult(
            outcome=BookingEligibilityOutcome.SERVICE_UNAVAILABLE,
            selected_service=_SERVICE,
            selected_master=_MASTER,
            other_online_master_ids=(),
            internal_reason_code=BookingInternalReasonCode.UNKNOWN_OUTCOME,
        )
    )
    decision = BookingEligibilityFlowService(fake).resolve(
        _SERVICE,
        _MASTER,
        (_slot(slot_id="s1", master_id=_MASTER_UUID),),
        now=_NOW,
        include_alternatives=False,
    )
    assert isinstance(decision, ServiceUnavailableDecision)
    assert decision.action is not BookingDialogAction.OFFER_SLOTS
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# Alternatives / consent / call count / redaction
# ---------------------------------------------------------------------------


def test_alternatives_require_explicit_include_and_consent() -> None:
    fake = FakeEligibilityClient(
        result=_allowed(other_ids=(_ALT_UUID,)),
    )
    slots = (_slot(slot_id="alt", master_id=_ALT_UUID),)
    decision = BookingEligibilityFlowService(fake).resolve(
        _SERVICE,
        _MASTER,
        slots,
        now=_NOW,
        include_alternatives=True,
        alternate_master_consent=True,
    )
    assert isinstance(decision, SlotOfferDecision)
    assert [slot.master_id for slot in decision.offered_slots] == [_ALT_UUID]
    assert fake.calls[0]["include_alternatives"] is True


def test_without_consent_alternate_slots_handoff() -> None:
    fake = FakeEligibilityClient(result=_allowed(other_ids=(_ALT_UUID,)))
    decision = BookingEligibilityFlowService(fake).resolve(
        _SERVICE,
        _MASTER,
        (_slot(slot_id="alt", master_id=_ALT_UUID),),
        now=_NOW,
        include_alternatives=True,
        alternate_master_consent=False,
    )
    assert isinstance(decision, ManagerHandoffDecision)
    assert (
        decision.internal_reason_code
        == BookingInternalReasonCode.ALTERNATE_MASTER_WITHOUT_CONSENT.value
    )


def test_include_alternatives_must_be_bool() -> None:
    fake = FakeEligibilityClient(result=_allowed())
    with pytest.raises(BookingDomainError):
        BookingEligibilityFlowService(fake).resolve(
            _SERVICE,
            _MASTER,
            (),
            now=_NOW,
            include_alternatives="yes",  # type: ignore[arg-type]
        )
    assert fake.calls == []


def test_exactly_one_eligibility_call_per_resolve() -> None:
    fake = FakeEligibilityClient(result=_allowed())
    flow = BookingEligibilityFlowService(fake)
    slots = (_slot(slot_id="s1", master_id=_MASTER_UUID),)
    flow.resolve(
        _SERVICE, _MASTER, slots, now=_NOW, include_alternatives=False
    )
    flow.resolve(
        _SERVICE, _MASTER, slots, now=_NOW, include_alternatives=False
    )
    assert len(fake.calls) == 2  # one per resolve, never doubled inside resolve


def test_omitted_master_passed_through() -> None:
    fake = FakeEligibilityClient(result=_allowed(master=None))
    decision = BookingEligibilityFlowService(fake).resolve(
        _SERVICE,
        None,
        (_slot(slot_id="s1", master_id=_MASTER_UUID),),
        now=_NOW,
        include_alternatives=False,
    )
    assert isinstance(decision, SlotOfferDecision)
    assert fake.calls[0]["master"] is None


def test_fail_closed_logs_allowlisted_code_not_exception_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exception payload may contain secrets; flow must not log them."""

    secret_token = "T" * 40
    secret_url = "https://eligibility.internal.example"
    fake = FakeEligibilityClient(
        error=RuntimeError(
            f"Authorization Bearer {secret_token} url={secret_url}"
        )
    )
    with caplog.at_level(logging.INFO):
        decision = BookingEligibilityFlowService(fake).resolve(
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
    assert len(fake.calls) == 1
    blob = "\n".join(
        f"{record.getMessage()}{record.exc_text or ''}"
        for record in caplog.records
    )
    assert secret_token not in blob
    assert secret_url not in blob
    assert _SERVICE_UUID not in blob
    assert _MASTER_UUID not in blob
    assert "Authorization" not in blob
    assert "booking_eligibility_flow_fail_closed" in blob
    assert (
        BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE.value in blob
    )


def test_missing_client_logs_only_allowlisted_ids_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        BookingEligibilityFlowService(None).resolve(
            _SERVICE,
            _MASTER,
            (),
            now=_NOW,
            include_alternatives=False,
        )
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert _SERVICE_UUID not in blob
    assert _MASTER_UUID not in blob
    assert "booking_eligibility_flow_fail_closed" in blob
    assert BookingInternalReasonCode.ELIGIBILITY_CLIENT_UNAVAILABLE.value in blob


def test_deleting_missing_client_guard_would_be_caught() -> None:
    """Mutation canary: None must yield CLIENT_UNAVAILABLE, not SERVICE_UNAVAILABLE."""

    decision = BookingEligibilityFlowService(None).resolve(
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
    # Injected exploding client is a different path (exception → SERVICE_UNAVAILABLE).
    exploding = FakeEligibilityClient(error=AssertionError("injected boom"))
    fail_closed = BookingEligibilityFlowService(exploding).resolve(
        _SERVICE,
        _MASTER,
        (),
        now=_NOW,
        include_alternatives=False,
    )
    assert isinstance(fail_closed, ServiceUnavailableDecision)
    assert (
        fail_closed.internal_reason_code
        == BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE.value
    )
    assert len(exploding.calls) == 1


# ---------------------------------------------------------------------------
# CURSOR-23 primitives: check_eligibility / decide_from_eligibility / resolve
# ---------------------------------------------------------------------------


def test_check_eligibility_one_call_and_fail_closed_paths() -> None:
    fake = FakeEligibilityClient(result=_allowed())
    flow = BookingEligibilityFlowService(fake)
    result = flow.check_eligibility(_SERVICE, _MASTER, include_alternatives=False)
    assert type(result) is BookingEligibilityResult
    assert result.outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    assert len(fake.calls) == 1

    missing = BookingEligibilityFlowService(None).check_eligibility(
        _SERVICE, _MASTER, include_alternatives=False
    )
    assert missing.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert (
        missing.internal_reason_code
        == BookingInternalReasonCode.ELIGIBILITY_CLIENT_UNAVAILABLE.value
    )

    boom = FakeEligibilityClient(error=RuntimeError("boom"))
    exploded = BookingEligibilityFlowService(boom).check_eligibility(
        _SERVICE, _MASTER, include_alternatives=False
    )
    assert exploded.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert (
        exploded.internal_reason_code
        == BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE.value
    )
    assert len(boom.calls) == 1

    class BadClient:
        def check_eligibility(self, *args: object, **kwargs: object) -> object:
            return {"outcome": "SELF_BOOKING_ALLOWED"}

    unknown = BookingEligibilityFlowService(BadClient()).check_eligibility(  # type: ignore[arg-type]
        _SERVICE, _MASTER, include_alternatives=False
    )
    assert unknown.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert (
        unknown.internal_reason_code
        == BookingInternalReasonCode.UNKNOWN_OUTCOME.value
    )


def test_decide_from_eligibility_never_calls_client() -> None:
    fake = FakeEligibilityClient(result=_allowed())
    flow = BookingEligibilityFlowService(fake)
    eligibility = _allowed()
    decision = flow.decide_from_eligibility(
        eligibility,
        (_slot(slot_id="s1", master_id=_MASTER_UUID),),
        now=_NOW,
        alternate_master_consent=False,
    )
    assert isinstance(decision, SlotOfferDecision)
    assert fake.calls == []

    handoff = flow.decide_from_eligibility(
        _handoff(),
        (_slot(slot_id="s1", master_id=_MASTER_UUID),),
        now=_NOW,
    )
    assert isinstance(handoff, ManagerHandoffDecision)
    assert fake.calls == []

    empty = flow.decide_from_eligibility(eligibility, (), now=_NOW)
    assert isinstance(empty, ManagerHandoffDecision)
    assert empty.internal_reason_code == BookingInternalReasonCode.NO_VALID_SLOTS.value

    alt = flow.decide_from_eligibility(
        _allowed(other_ids=(_ALT_UUID,)),
        (_slot(slot_id="alt", master_id=_ALT_UUID),),
        now=_NOW,
        alternate_master_consent=False,
    )
    assert isinstance(alt, ManagerHandoffDecision)
    assert (
        alt.internal_reason_code
        == BookingInternalReasonCode.ALTERNATE_MASTER_WITHOUT_CONSENT.value
    )
    assert fake.calls == []


def test_resolve_uses_check_once_then_decide_no_nested_double_call() -> None:
    fake = FakeEligibilityClient(result=_allowed())
    flow = BookingEligibilityFlowService(fake)
    check_calls: list[int] = []
    original_check = flow.check_eligibility

    def _counting_check(*args: object, **kwargs: object) -> BookingEligibilityResult:
        check_calls.append(1)
        return original_check(*args, **kwargs)  # type: ignore[arg-type]

    flow.check_eligibility = _counting_check  # type: ignore[method-assign]
    decision = flow.resolve(
        _SERVICE,
        _MASTER,
        (_slot(slot_id="s1", master_id=_MASTER_UUID),),
        now=_NOW,
        include_alternatives=False,
    )
    assert isinstance(decision, SlotOfferDecision)
    assert len(fake.calls) == 1
    assert len(check_calls) == 1
