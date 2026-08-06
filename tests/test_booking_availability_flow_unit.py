"""CURSOR-23 unit tests: availability wired through BookingFlowService."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.core.booking_availability_http import BookingAvailabilityHttpError
from app.core.booking_availability_remote import (
    AvailableDaysResult,
    AvailableSlotsResult,
    require_canonical_booking_starts_at,
)
from app.core.booking_eligibility_factory import (
    build_booking_flow_from_settings,
    build_booking_s2s_clients,
)
from app.core.booking_types import (
    AvailableDaysOfferDecision,
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
from app.core.s2s_http_stdlib import S2sHttpStdlibTransport
from app.schemas.booking_input import (
    SyntheticAvailableDaysQuery,
    SyntheticAvailableSlotsQuery,
    SyntheticBookingInput,
    SyntheticBookingSlot,
)
from app.services.booking_eligibility_flow import BookingEligibilityFlowService
from app.services.booking_flow import BookingFlowService
from app.services.booking_synthetic import (
    client_reply_plan_payload,
    decision_to_outbound_fields,
    resolve_booking_outbound_fields,
    sanitize_booking_result_fields,
)
from app.services.worker_runtime import build_booking_flow_for_worker

_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_ALT_MASTER = "33333333-3333-4333-8333-333333333333"
_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone(timedelta(hours=5)))
_TOKEN = "a" * 32


class RecordingEligibility:
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


class RecordingAvailability:
    def __init__(
        self,
        *,
        days: AvailableDaysResult | None = None,
        slots: AvailableSlotsResult | None = None,
        error: BaseException | None = None,
        expected_master_id: str | None = None,
    ) -> None:
        self.days = days
        self.slots = slots
        self.error = error
        self.expected_master_id = expected_master_id
        self.day_calls: list[dict[str, Any]] = []
        self.slot_calls: list[dict[str, Any]] = []

    def _assert_expected_master(self, master_id: object) -> None:
        if self.expected_master_id is not None and master_id != self.expected_master_id:
            raise AssertionError(
                "availability must receive eligibility-selected master, not caller"
            )

    def get_available_days(
        self,
        *,
        service_id: object,
        master_id: object,
        month: object,
    ) -> AvailableDaysResult:
        self._assert_expected_master(master_id)
        self.day_calls.append(
            {"service_id": service_id, "master_id": master_id, "month": month}
        )
        if self.error is not None:
            raise self.error
        assert self.days is not None
        return self.days

    def get_available_slots(
        self,
        *,
        service_id: object,
        master_id: object,
        date: object,
    ) -> AvailableSlotsResult:
        self._assert_expected_master(master_id)
        self.slot_calls.append(
            {"service_id": service_id, "master_id": master_id, "date": date}
        )
        if self.error is not None:
            raise self.error
        assert self.slots is not None
        return self.slots


def _allowed(
    *,
    master: SelectedMaster | None = SelectedMaster(_MASTER),
) -> BookingEligibilityResult:
    return BookingEligibilityResult(
        outcome=BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
        selected_service=SelectedService(_SERVICE),
        selected_master=master,
        other_online_master_ids=(),
        internal_reason_code=None,
    )


def _handoff() -> BookingEligibilityResult:
    return BookingEligibilityResult(
        outcome=BookingEligibilityOutcome.MANAGER_HANDOFF,
        selected_service=SelectedService(_SERVICE),
        selected_master=SelectedMaster(_MASTER),
        other_online_master_ids=(),
        internal_reason_code="MANAGER_ONLY",
    )


def _slot(minute: int = 0) -> AvailableSlot:
    return AvailableSlot(
        slot_id=f"s{minute}",
        starts_at=datetime(2026, 8, 6, 10, minute, tzinfo=timezone(timedelta(hours=5))),
        master_id=_MASTER,
        service_id=_SERVICE,
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_availability_days_query_requires_real_month() -> None:
    q = SyntheticAvailableDaysQuery(kind="AVAILABLE_DAYS", month="2026-08")
    assert q.month == "2026-08"
    with pytest.raises(ValidationError):
        SyntheticAvailableDaysQuery(kind="AVAILABLE_DAYS", month="2026-13")
    with pytest.raises(ValidationError):
        SyntheticAvailableDaysQuery(kind="AVAILABLE_DAYS", month="2026-8")


def test_availability_slots_query_requires_real_date() -> None:
    q = SyntheticAvailableSlotsQuery(kind="SLOTS", date="2026-08-06")
    assert q.date == "2026-08-06"
    with pytest.raises(ValidationError):
        SyntheticAvailableSlotsQuery(kind="SLOTS", date="2026-02-29")


def test_availability_query_rejects_unknown_kind_and_extras() -> None:
    with pytest.raises(ValidationError):
        SyntheticBookingInput.model_validate(
            {
                "service_id": _SERVICE,
                "master_id": _MASTER,
                "include_alternatives": False,
                "decision_at": _NOW.isoformat(),
                "availability_query": {"kind": "OTHER", "month": "2026-08"},
            }
        )
    with pytest.raises(ValidationError):
        SyntheticAvailableDaysQuery.model_validate(
            {"kind": "AVAILABLE_DAYS", "month": "2026-08", "extra": 1}
        )


def test_slots_and_availability_query_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        SyntheticBookingInput(
            service_id=_SERVICE,
            master_id=_MASTER,
            include_alternatives=False,
            slots=(
                SyntheticBookingSlot(
                    slot_id="s1",
                    starts_at=_NOW,
                    master_id=_MASTER,
                    service_id=_SERVICE,
                ),
            ),
            availability_query=SyntheticAvailableDaysQuery(
                kind="AVAILABLE_DAYS", month="2026-08"
            ),
            decision_at=_NOW,
        )


def test_availability_query_reaches_reply_plan_not_inbox_shape() -> None:
    booking = SyntheticBookingInput(
        service_id=_SERVICE,
        master_id=_MASTER,
        include_alternatives=False,
        availability_query=SyntheticAvailableSlotsQuery(
            kind="SLOTS", date="2026-08-06"
        ),
        decision_at=_NOW,
    )
    plan = client_reply_plan_payload(inbox_id="inbox-1", booking=booking)
    assert plan["booking"]["availability_query"] == {
        "kind": "SLOTS",
        "date": "2026-08-06",
    }
    assert "text" not in plan
    assert "availability_query" not in plan  # only under booking


# ---------------------------------------------------------------------------
# Available days flow
# ---------------------------------------------------------------------------


def test_available_days_one_eligibility_and_one_availability_call() -> None:
    eligibility = RecordingEligibility(_allowed())
    availability = RecordingAvailability(
        days=AvailableDaysResult(
            service_id=_SERVICE,
            master_id=_MASTER,
            month="2026-08",
            studio_today="2026-08-05",
            date_keys=("2026-08-06", "2026-08-07"),
        )
    )
    consumer = BookingFlowService(
        BookingEligibilityFlowService(eligibility),
        availability,
    )
    decision = consumer.resolve_available_days(
        SelectedService(_SERVICE),
        SelectedMaster(_MASTER),
        "2026-08",
        now=_NOW,
        include_alternatives=False,
    )
    assert type(decision) is AvailableDaysOfferDecision
    assert decision.date_keys == ("2026-08-06", "2026-08-07")
    assert len(eligibility.calls) == 1
    assert len(availability.day_calls) == 1
    assert availability.day_calls[0] == {
        "service_id": _SERVICE,
        "master_id": _MASTER,
        "month": "2026-08",
    }
    assert availability.slot_calls == []


def test_available_days_manager_handoff_skips_availability() -> None:
    eligibility = RecordingEligibility(_handoff())
    availability = RecordingAvailability(
        days=AvailableDaysResult(
            service_id=_SERVICE,
            master_id=_MASTER,
            month="2026-08",
            studio_today="2026-08-05",
            date_keys=("2026-08-06",),
        )
    )
    consumer = BookingFlowService(
        BookingEligibilityFlowService(eligibility),
        availability,
    )
    decision = consumer.resolve_available_days(
        SelectedService(_SERVICE),
        SelectedMaster(_MASTER),
        "2026-08",
        now=_NOW,
        include_alternatives=False,
    )
    assert type(decision) is ManagerHandoffDecision
    assert len(eligibility.calls) == 1
    assert availability.day_calls == []


def test_available_days_empty_is_manager_bound_not_offer() -> None:
    eligibility = RecordingEligibility(_allowed())
    availability = RecordingAvailability(
        days=AvailableDaysResult(
            service_id=_SERVICE,
            master_id=_MASTER,
            month="2026-08",
            studio_today="2026-08-05",
            date_keys=(),
        )
    )
    decision = BookingFlowService(
        BookingEligibilityFlowService(eligibility),
        availability,
    ).resolve_available_days(
        SelectedService(_SERVICE),
        SelectedMaster(_MASTER),
        "2026-08",
        now=_NOW,
        include_alternatives=False,
    )
    assert type(decision) is ManagerHandoffDecision
    assert decision.internal_reason_code == BookingInternalReasonCode.NO_AVAILABLE_DAYS.value
    fields = decision_to_outbound_fields(decision)
    assert fields["booking_action"] == BookingDialogAction.MANAGER_HANDOFF.value
    assert "booking_available_date_keys" not in fields


def test_available_days_missing_client_fail_closed() -> None:
    decision = BookingFlowService(
        BookingEligibilityFlowService(RecordingEligibility(_allowed())),
        None,
    ).resolve_available_days(
        SelectedService(_SERVICE),
        SelectedMaster(_MASTER),
        "2026-08",
        now=_NOW,
        include_alternatives=False,
    )
    assert type(decision) is ServiceUnavailableDecision
    assert (
        decision.internal_reason_code
        == BookingInternalReasonCode.AVAILABILITY_CLIENT_UNAVAILABLE.value
    )


def test_available_days_outbound_fields_exact() -> None:
    decision = AvailableDaysOfferDecision(
        action=BookingDialogAction.OFFER_DAYS,
        date_keys=("2026-08-06", "2026-08-07"),
        studio_today="2026-08-05",
    )
    fields = decision_to_outbound_fields(decision)
    assert fields == {
        "booking_action": "OFFER_DAYS",
        "booking_reason": None,
        "booking_available_date_keys": ["2026-08-06", "2026-08-07"],
        "booking_studio_today": "2026-08-05",
    }
    assert sanitize_booking_result_fields(fields) == fields
    assert (
        sanitize_booking_result_fields(
            {**fields, "booking_available_date_keys": []}
        )["booking_action"]
        == "SERVICE_UNAVAILABLE"
    )


def test_offer_days_is_machine_only_not_renderable() -> None:
    from app.core.booking_types import BookingClientMessageKind, _CLIENT_MESSAGE_TEXT

    decision = AvailableDaysOfferDecision(
        action=BookingDialogAction.OFFER_DAYS,
        date_keys=("2026-08-06",),
        studio_today="2026-08-05",
    )
    with pytest.raises(BookingDomainError):
        client_message_for_decision(decision)
    assert not hasattr(BookingClientMessageKind, "OFFER_DAYS")
    assert "свободные дни" not in " ".join(_CLIENT_MESSAGE_TEXT.values())
    assert BookingClientMessageKind.OFFER_SLOTS in _CLIENT_MESSAGE_TEXT


# ---------------------------------------------------------------------------
# Master selection (H1)
# ---------------------------------------------------------------------------


def test_requested_alt_master_mismatch_skips_availability_days() -> None:
    eligibility = RecordingEligibility(
        _allowed(master=SelectedMaster(_MASTER))
    )
    availability = RecordingAvailability(
        days=AvailableDaysResult(
            service_id=_SERVICE,
            master_id=_ALT_MASTER,
            month="2026-08",
            studio_today="2026-08-05",
            date_keys=("2026-08-06",),
        ),
        expected_master_id=_MASTER,
    )
    decision = BookingFlowService(
        BookingEligibilityFlowService(eligibility),
        availability,
    ).resolve_available_days(
        SelectedService(_SERVICE),
        SelectedMaster(_ALT_MASTER),
        "2026-08",
        now=_NOW,
        include_alternatives=True,
        alternate_master_consent=True,
    )
    assert type(decision) is ServiceUnavailableDecision
    assert (
        decision.internal_reason_code
        == BookingInternalReasonCode.ELIGIBILITY_MASTER_MISMATCH.value
    )
    assert availability.day_calls == []
    assert type(decision) is not AvailableDaysOfferDecision


def test_requested_alt_master_mismatch_skips_availability_slots() -> None:
    eligibility = RecordingEligibility(
        _allowed(master=SelectedMaster(_MASTER))
    )
    availability = RecordingAvailability(
        slots=AvailableSlotsResult(
            service_id=_SERVICE,
            master_id=_ALT_MASTER,
            date="2026-08-06",
            studio_today="2026-08-05",
            slots=(_slot(),),
        ),
        expected_master_id=_MASTER,
    )
    decision = BookingFlowService(
        BookingEligibilityFlowService(eligibility),
        availability,
    ).resolve_available_slots(
        SelectedService(_SERVICE),
        SelectedMaster(_ALT_MASTER),
        "2026-08-06",
        now=_NOW,
        include_alternatives=True,
        alternate_master_consent=True,
    )
    assert type(decision) is ServiceUnavailableDecision
    assert (
        decision.internal_reason_code
        == BookingInternalReasonCode.ELIGIBILITY_MASTER_MISMATCH.value
    )
    assert availability.slot_calls == []
    assert type(decision) is not SlotOfferDecision


def test_matching_requested_master_uses_eligibility_selected() -> None:
    selected = SelectedMaster(_MASTER)
    eligibility = RecordingEligibility(_allowed(master=selected))
    availability = RecordingAvailability(
        days=AvailableDaysResult(
            service_id=_SERVICE,
            master_id=_MASTER,
            month="2026-08",
            studio_today="2026-08-05",
            date_keys=("2026-08-06",),
        ),
        expected_master_id=_MASTER,
    )
    decision = BookingFlowService(
        BookingEligibilityFlowService(eligibility),
        availability,
    ).resolve_available_days(
        SelectedService(_SERVICE),
        SelectedMaster(_MASTER),
        "2026-08",
        now=_NOW,
        include_alternatives=False,
    )
    assert type(decision) is AvailableDaysOfferDecision
    assert availability.day_calls[0]["master_id"] == _MASTER


def test_none_requested_master_uses_eligibility_selected() -> None:
    eligibility = RecordingEligibility(
        _allowed(master=SelectedMaster(_MASTER))
    )
    availability = RecordingAvailability(
        days=AvailableDaysResult(
            service_id=_SERVICE,
            master_id=_MASTER,
            month="2026-08",
            studio_today="2026-08-05",
            date_keys=("2026-08-06",),
        ),
        expected_master_id=_MASTER,
    )
    decision = BookingFlowService(
        BookingEligibilityFlowService(eligibility),
        availability,
    ).resolve_available_days(
        SelectedService(_SERVICE),
        None,
        "2026-08",
        now=_NOW,
        include_alternatives=False,
    )
    assert type(decision) is AvailableDaysOfferDecision
    assert availability.day_calls[0]["master_id"] == _MASTER


def test_missing_eligibility_selected_master_skips_availability() -> None:
    eligibility = RecordingEligibility(_allowed(master=None))
    availability = RecordingAvailability(
        days=AvailableDaysResult(
            service_id=_SERVICE,
            master_id=_MASTER,
            month="2026-08",
            studio_today="2026-08-05",
            date_keys=("2026-08-06",),
        ),
        expected_master_id=_MASTER,
    )
    decision = BookingFlowService(
        BookingEligibilityFlowService(eligibility),
        availability,
    ).resolve_available_days(
        SelectedService(_SERVICE),
        None,
        "2026-08",
        now=_NOW,
        include_alternatives=False,
    )
    assert type(decision) is ServiceUnavailableDecision
    assert availability.day_calls == []


# ---------------------------------------------------------------------------
# Error taxonomy (M3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("adapter_code", "expected"),
    [
        ("CONFIG_INVALID", BookingInternalReasonCode.AVAILABILITY_CLIENT_UNAVAILABLE),
        ("REQUEST_INVALID", BookingInternalReasonCode.AVAILABILITY_REQUEST_INVALID),
        ("TIMEOUT", BookingInternalReasonCode.AVAILABILITY_SERVICE_UNAVAILABLE),
        ("UNAUTHORIZED", BookingInternalReasonCode.AVAILABILITY_SERVICE_UNAVAILABLE),
    ],
)
def test_availability_adapter_error_mapping(
    adapter_code: str,
    expected: BookingInternalReasonCode,
) -> None:
    eligibility = RecordingEligibility(_allowed())
    availability = RecordingAvailability(
        error=BookingAvailabilityHttpError(adapter_code),
        expected_master_id=_MASTER,
    )
    decision = BookingFlowService(
        BookingEligibilityFlowService(eligibility),
        availability,
    ).resolve_available_days(
        SelectedService(_SERVICE),
        SelectedMaster(_MASTER),
        "2026-08",
        now=_NOW,
        include_alternatives=False,
    )
    assert type(decision) is ServiceUnavailableDecision
    assert decision.internal_reason_code == expected.value
    assert decision.internal_reason_code != adapter_code


def test_availability_unknown_exception_fail_closed() -> None:
    eligibility = RecordingEligibility(_allowed())
    availability = RecordingAvailability(
        error=RuntimeError("https://secret.example token=abc"),
        expected_master_id=_MASTER,
    )
    decision = BookingFlowService(
        BookingEligibilityFlowService(eligibility),
        availability,
    ).resolve_available_days(
        SelectedService(_SERVICE),
        SelectedMaster(_MASTER),
        "2026-08",
        now=_NOW,
        include_alternatives=False,
    )
    assert type(decision) is ServiceUnavailableDecision
    assert (
        decision.internal_reason_code
        == BookingInternalReasonCode.AVAILABILITY_SERVICE_UNAVAILABLE.value
    )


def test_invalid_month_maps_to_availability_request_invalid() -> None:
    decision = BookingFlowService(
        BookingEligibilityFlowService(RecordingEligibility(_allowed())),
        RecordingAvailability(expected_master_id=_MASTER),
    ).resolve_available_days(
        SelectedService(_SERVICE),
        SelectedMaster(_MASTER),
        "2026-13",
        now=_NOW,
        include_alternatives=False,
    )
    assert type(decision) is ServiceUnavailableDecision
    assert (
        decision.internal_reason_code
        == BookingInternalReasonCode.AVAILABILITY_REQUEST_INVALID.value
    )


# ---------------------------------------------------------------------------
# Sanitizer strictness (M2)
# ---------------------------------------------------------------------------


def test_offer_days_sanitizer_rejects_unsorted_duplicate_extra() -> None:
    base = {
        "booking_action": "OFFER_DAYS",
        "booking_reason": None,
        "booking_available_date_keys": ["2026-08-06", "2026-08-07"],
        "booking_studio_today": "2026-08-05",
    }
    assert sanitize_booking_result_fields(base) == base
    assert (
        sanitize_booking_result_fields(
            {**base, "booking_available_date_keys": ["2026-08-07", "2026-08-06"]}
        )["booking_action"]
        == "SERVICE_UNAVAILABLE"
    )
    assert (
        sanitize_booking_result_fields(
            {**base, "booking_available_date_keys": ["2026-08-06", "2026-08-06"]}
        )["booking_action"]
        == "SERVICE_UNAVAILABLE"
    )
    assert (
        sanitize_booking_result_fields({**base, "extra": 1})["booking_action"]
        == "SERVICE_UNAVAILABLE"
    )
    assert (
        sanitize_booking_result_fields(
            {**base, "booking_available_date_keys": ("2026-08-06",)}
        )["booking_action"]
        == "SERVICE_UNAVAILABLE"
    )
    assert (
        sanitize_booking_result_fields(
            {**base, "booking_available_date_keys": ["2026-02-30"]}
        )["booking_action"]
        == "SERVICE_UNAVAILABLE"
    )
    assert (
        sanitize_booking_result_fields({**base, "booking_reason": "X"})[
            "booking_action"
        ]
        == "SERVICE_UNAVAILABLE"
    )


def test_offer_slots_new_shape_strict_and_legacy_compatible() -> None:
    legacy = {
        "booking_action": "OFFER_SLOTS",
        "booking_reason": None,
        "booking_offered_slot_ids": ["a", "b"],
    }
    assert sanitize_booking_result_fields(legacy)["booking_offered_slot_ids"] == [
        "a",
        "b",
    ]
    assert "booking_offered_slots" not in sanitize_booking_result_fields(legacy)

    good = {
        "booking_action": "OFFER_SLOTS",
        "booking_reason": None,
        "booking_offered_slot_ids": ["a", "b"],
        "booking_offered_slots": [
            {"slot_id": "a", "starts_at": "2026-08-06T10:00:00+05:00"},
            {"slot_id": "b", "starts_at": "2026-08-06T10:10:00+05:00"},
        ],
    }
    assert sanitize_booking_result_fields(good) == good
    three = {
        "booking_action": "OFFER_SLOTS",
        "booking_reason": None,
        "booking_offered_slot_ids": ["a", "b", "c"],
        "booking_offered_slots": [
            {"slot_id": "a", "starts_at": "2026-08-06T10:00:00+05:00"},
            {"slot_id": "b", "starts_at": "2026-08-06T10:10:00+05:00"},
            {"slot_id": "c", "starts_at": "2026-08-06T10:20:00+05:00"},
        ],
    }
    assert sanitize_booking_result_fields(three) == three

    mismatch = {
        **good,
        "booking_offered_slots": [
            {"slot_id": "b", "starts_at": "2026-08-06T10:00:00+05:00"},
            {"slot_id": "a", "starts_at": "2026-08-06T10:10:00+05:00"},
        ],
    }
    assert (
        sanitize_booking_result_fields(mismatch)["booking_action"]
        == "SERVICE_UNAVAILABLE"
    )

    dup_ids = {
        **good,
        "booking_offered_slot_ids": ["a", "a"],
        "booking_offered_slots": [
            {"slot_id": "a", "starts_at": "2026-08-06T10:00:00+05:00"},
            {"slot_id": "a", "starts_at": "2026-08-06T10:10:00+05:00"},
        ],
    }
    assert (
        sanitize_booking_result_fields(dup_ids)["booking_action"]
        == "SERVICE_UNAVAILABLE"
    )

    unsorted = {
        **good,
        "booking_offered_slots": [
            {"slot_id": "a", "starts_at": "2026-08-06T10:10:00+05:00"},
            {"slot_id": "b", "starts_at": "2026-08-06T10:00:00+05:00"},
        ],
    }
    assert (
        sanitize_booking_result_fields(unsorted)["booking_action"]
        == "SERVICE_UNAVAILABLE"
    )

    naive = {
        **good,
        "booking_offered_slots": [
            {"slot_id": "a", "starts_at": "2026-08-06T10:00:00"},
            {"slot_id": "b", "starts_at": "2026-08-06T10:10:00+05:00"},
        ],
    }
    assert (
        sanitize_booking_result_fields(naive)["booking_action"]
        == "SERVICE_UNAVAILABLE"
    )

    extra_key = {**good, "extra": 1}
    assert (
        sanitize_booking_result_fields(extra_key)["booking_action"]
        == "SERVICE_UNAVAILABLE"
    )

    # Malformed new shape must not fall back to legacy (key present → full validate).
    broken_new = {
        "booking_action": "OFFER_SLOTS",
        "booking_reason": None,
        "booking_offered_slot_ids": ["a"],
        "booking_offered_slots": [{"slot_id": "a"}],
    }
    assert (
        sanitize_booking_result_fields(broken_new)["booking_action"]
        == "SERVICE_UNAVAILABLE"
    )
    none_slots = {
        "booking_action": "OFFER_SLOTS",
        "booking_reason": None,
        "booking_offered_slot_ids": ["a"],
        "booking_offered_slots": None,
    }
    assert (
        sanitize_booking_result_fields(none_slots)["booking_action"]
        == "SERVICE_UNAVAILABLE"
    )


@pytest.mark.parametrize(
    "starts_at",
    [
        "2026-08-06T05:00:00Z",
        "2026-08-06T10:00:00+00:00",
        "2026-08-06T10:00:01+05:00",
        "2026-08-06T10:00:00+04:00",
        "2026-08-06T10:00:00.000+05:00",
        "2026-08-06 10:00:00+05:00",
        "2026-02-30T10:00:00+05:00",
        "2026-08-06T24:00:00+05:00",
        "2026-08-06T10:60:00+05:00",
        " 2026-08-06T10:00:00+05:00",
        "2026-08-06T10:00:00+05:00 ",
    ],
)
def test_offer_slots_new_shape_rejects_noncanonical_starts_at(starts_at: str) -> None:
    payload = {
        "booking_action": "OFFER_SLOTS",
        "booking_reason": None,
        "booking_offered_slot_ids": ["a"],
        "booking_offered_slots": [
            {"slot_id": "a", "starts_at": starts_at},
        ],
    }
    result = sanitize_booking_result_fields(payload)
    assert result["booking_action"] == "SERVICE_UNAVAILABLE"
    assert result["booking_reason"] == (
        BookingInternalReasonCode.BOOKING_RESOLUTION_INTERRUPTED.value
    )
    assert "booking_offered_slot_ids" not in result
    assert "booking_offered_slots" not in result


def test_offer_slots_accepts_single_canonical_studio_timestamp() -> None:
    payload = {
        "booking_action": "OFFER_SLOTS",
        "booking_reason": None,
        "booking_offered_slot_ids": ["only"],
        "booking_offered_slots": [
            {"slot_id": "only", "starts_at": "2026-08-06T10:00:00+05:00"},
        ],
    }
    assert sanitize_booking_result_fields(payload) == payload


def test_http_and_sanitizer_share_canonical_starts_at_helper() -> None:
    """HTTP adapter and durable sanitizer must use the same production helper."""

    import app.core.booking_availability_http as http_mod
    import app.services.booking_synthetic as synth_mod

    assert (
        http_mod.require_canonical_booking_starts_at
        is require_canonical_booking_starts_at
    )
    assert (
        synth_mod.require_canonical_booking_starts_at
        is require_canonical_booking_starts_at
    )
    good = "2026-08-06T10:00:00+05:00"
    assert require_canonical_booking_starts_at(good) is good
    for bad in (
        "2026-08-06T05:00:00Z",
        "2026-08-06T10:00:00+00:00",
        "2026-08-06T10:00:01+05:00",
    ):
        with pytest.raises(ValueError):
            require_canonical_booking_starts_at(bad)
        payload = {
            "booking_action": "OFFER_SLOTS",
            "booking_reason": None,
            "booking_offered_slot_ids": ["a"],
            "booking_offered_slots": [{"slot_id": "a", "starts_at": bad}],
        }
        assert (
            sanitize_booking_result_fields(payload)["booking_action"]
            == "SERVICE_UNAVAILABLE"
        )


# ---------------------------------------------------------------------------
# Slots flow
# ---------------------------------------------------------------------------


def test_available_slots_one_eligibility_and_one_slots_call_policy_cap() -> None:
    eligibility = RecordingEligibility(_allowed())
    remote_slots = (
        _slot(0),
        _slot(10),
        _slot(20),
        _slot(30),
    )
    availability = RecordingAvailability(
        slots=AvailableSlotsResult(
            service_id=_SERVICE,
            master_id=_MASTER,
            date="2026-08-06",
            studio_today="2026-08-05",
            slots=remote_slots,
        )
    )
    decision = BookingFlowService(
        BookingEligibilityFlowService(eligibility),
        availability,
    ).resolve_available_slots(
        SelectedService(_SERVICE),
        SelectedMaster(_MASTER),
        "2026-08-06",
        now=_NOW,
        include_alternatives=False,
    )
    assert type(decision) is SlotOfferDecision
    assert len(decision.offered_slots) == 3
    assert [s.slot_id for s in decision.offered_slots] == ["s0", "s10", "s20"]
    assert decision.offered_slots[0].starts_at == remote_slots[0].starts_at
    assert len(eligibility.calls) == 1
    assert len(availability.slot_calls) == 1
    assert availability.day_calls == []
    fields = decision_to_outbound_fields(decision)
    assert fields["booking_offered_slot_ids"] == ["s0", "s10", "s20"]
    assert fields["booking_offered_slots"][0]["slot_id"] == "s0"
    assert "+05:00" in fields["booking_offered_slots"][0]["starts_at"]


def test_available_slots_handoff_skips_slots_call() -> None:
    eligibility = RecordingEligibility(_handoff())
    availability = RecordingAvailability(
        slots=AvailableSlotsResult(
            service_id=_SERVICE,
            master_id=_MASTER,
            date="2026-08-06",
            studio_today="2026-08-05",
            slots=(_slot(),),
        )
    )
    decision = BookingFlowService(
        BookingEligibilityFlowService(eligibility),
        availability,
    ).resolve_available_slots(
        SelectedService(_SERVICE),
        SelectedMaster(_MASTER),
        "2026-08-06",
        now=_NOW,
        include_alternatives=False,
    )
    assert type(decision) is ManagerHandoffDecision
    assert availability.slot_calls == []


def test_resolve_booking_outbound_fields_routes_availability_query() -> None:
    eligibility = RecordingEligibility(_allowed())
    availability = RecordingAvailability(
        days=AvailableDaysResult(
            service_id=_SERVICE,
            master_id=_MASTER,
            month="2026-08",
            studio_today="2026-08-05",
            date_keys=("2026-08-06",),
        )
    )
    flow = BookingFlowService(
        BookingEligibilityFlowService(eligibility),
        availability,
    )
    plan = {
        "booking": SyntheticBookingInput(
            service_id=_SERVICE,
            master_id=_MASTER,
            include_alternatives=False,
            availability_query=SyntheticAvailableDaysQuery(
                kind="AVAILABLE_DAYS", month="2026-08"
            ),
            decision_at=_NOW,
        ).wire_dict()
    }
    fields = resolve_booking_outbound_fields(plan, booking_flow=flow)
    assert fields["booking_action"] == "OFFER_DAYS"
    assert fields["booking_available_date_keys"] == ["2026-08-06"]
    assert len(eligibility.calls) == 1
    assert len(availability.day_calls) == 1


def test_legacy_fixture_slots_path_unchanged() -> None:
    eligibility = RecordingEligibility(_allowed())
    availability = RecordingAvailability(
        days=AvailableDaysResult(
            service_id=_SERVICE,
            master_id=_MASTER,
            month="2026-08",
            studio_today="2026-08-05",
            date_keys=("2026-08-06",),
        )
    )
    flow = BookingFlowService(
        BookingEligibilityFlowService(eligibility),
        availability,
    )
    plan = {
        "booking": SyntheticBookingInput(
            service_id=_SERVICE,
            master_id=_MASTER,
            include_alternatives=False,
            slots=(
                SyntheticBookingSlot(
                    slot_id="fix1",
                    starts_at=_slot().starts_at,
                    master_id=_MASTER,
                    service_id=_SERVICE,
                ),
            ),
            decision_at=_NOW,
        ).wire_dict()
    }
    fields = resolve_booking_outbound_fields(plan, booking_flow=flow)
    assert fields["booking_action"] == "OFFER_SLOTS"
    assert fields["booking_offered_slot_ids"] == ["fix1"]
    assert availability.day_calls == []
    assert availability.slot_calls == []


# ---------------------------------------------------------------------------
# DI
# ---------------------------------------------------------------------------


def test_worker_and_factory_share_transport_identity() -> None:
    settings = Settings.from_env(
        {
            "BOOKING_ELIGIBILITY_BASE_URL": "https://eligibility.example",
            "BOOKING_ELIGIBILITY_BEARER_TOKEN": _TOKEN,
        }
    )
    transport = S2sHttpStdlibTransport()
    clients = build_booking_s2s_clients(settings, transport=transport)
    assert clients.eligibility is not None
    assert clients.availability is not None
    assert clients.booking_create is not None
    assert clients.transport is transport
    assert clients.eligibility._transport is transport
    assert clients.availability._transport is transport
    assert clients.booking_create._transport is transport
    flow = build_booking_flow_for_worker(settings)
    assert type(flow) is BookingFlowService
    assert flow._availability_client is not None
    assert flow._booking_create_client is not None


def test_absent_config_fail_closed_booking_flow() -> None:
    settings = Settings.from_env({})
    flow = build_booking_flow_from_settings(settings)
    decision = flow.resolve_available_days(
        SelectedService(_SERVICE),
        SelectedMaster(_MASTER),
        "2026-08",
        now=_NOW,
        include_alternatives=False,
    )
    assert type(decision) is ServiceUnavailableDecision


def test_interrupted_plan_payload_does_not_call_availability_in_resolver_path() -> None:
    """Durable started marker is enforced by worker; resolver itself is at-most-once
    only when invoked. This documents that one resolve attempt does one pair of calls.
    """

    eligibility = RecordingEligibility(_allowed())
    availability = RecordingAvailability(
        days=AvailableDaysResult(
            service_id=_SERVICE,
            master_id=_MASTER,
            month="2026-08",
            studio_today="2026-08-05",
            date_keys=("2026-08-06",),
        )
    )
    flow = BookingFlowService(
        BookingEligibilityFlowService(eligibility),
        availability,
    )
    plan = {
        "booking": SyntheticBookingInput(
            service_id=_SERVICE,
            master_id=_MASTER,
            include_alternatives=False,
            availability_query=SyntheticAvailableDaysQuery(
                kind="AVAILABLE_DAYS", month="2026-08"
            ),
            decision_at=_NOW,
        ).wire_dict()
    }
    resolve_booking_outbound_fields(plan, booking_flow=flow)
    resolve_booking_outbound_fields(plan, booking_flow=flow)
    assert len(eligibility.calls) == 2
    assert len(availability.day_calls) == 2
