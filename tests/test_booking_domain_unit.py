"""Unit tests for CURSOR-15 booking domain foundation.

Exercises real public behaviour of working hours, eligibility normalization,
and dialog policy. No HTTP, Docker, or production pipeline wiring.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from app.core.booking_dialog_policy import (
    collect_valid_slots,
    decide_booking_dialog,
    normalize_eligibility_outcome,
    parse_available_slot,
)
from app.core.booking_types import (
    MAX_OFFERED_SLOTS,
    AvailableSlot,
    BookingClientMessageKind,
    BookingDialogAction,
    BookingEligibilityOutcome,
    BookingEligibilityResult,
    BookingInternalReasonCode,
    ManagerHandoffDecision,
    SelectedMaster,
    SelectedService,
    ServiceUnavailableDecision,
    SlotOfferDecision,
    client_message_for_decision,
    render_client_message,
)
from app.core.manager_working_hours import (
    MANAGER_TIMEZONE_NAME,
    MANAGER_WORKDAY_END,
    MANAGER_WORKDAY_START,
    is_manager_working_time,
    manager_timezone,
    to_manager_local,
)
from app.core.outbound_policy import OutboundAction, is_automatic_outbound_allowed
from app.config import BotMode, Settings


def _utc(*parts: int) -> datetime:
    year, month, day, hour, minute = parts
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _try_zoneinfo():
    try:
        return ZoneInfo(MANAGER_TIMEZONE_NAME)
    except ZoneInfoNotFoundError:
        return None


def _local_boundary(hour: int, minute: int) -> datetime:
    """Build an aware UTC instant that is hour:minute in Asia/Yekaterinburg."""

    # YEKT is permanent UTC+5.
    return datetime(2026, 8, 5, hour, minute, tzinfo=timezone(timedelta(hours=5))).astimezone(
        timezone.utc
    )


def _slot(
    *,
    slot_id: str,
    starts_at: datetime,
    master_id: str = "master-a",
    service_id: str = "service-1",
) -> AvailableSlot:
    return AvailableSlot(
        slot_id=slot_id,
        starts_at=starts_at,
        master_id=master_id,
        service_id=service_id,
    )


# ---------------------------------------------------------------------------
# Manager working hours
# ---------------------------------------------------------------------------


def test_manager_timezone_is_asia_yekaterinburg() -> None:
    tz = manager_timezone()
    assert MANAGER_TIMEZONE_NAME == "Asia/Yekaterinburg"
    zoneinfo_tz = _try_zoneinfo()
    if zoneinfo_tz is not None:
        assert getattr(tz, "key", None) == MANAGER_TIMEZONE_NAME
    else:
        assert tz.tzname(None) == MANAGER_TIMEZONE_NAME
    # 04:00 UTC == 09:00 YEKT
    local = to_manager_local(_utc(2026, 8, 5, 4, 0))
    assert local.hour == 9
    assert local.minute == 0


@pytest.mark.parametrize(
    ("local_hour", "local_minute", "expected"),
    [
        (8, 59, False),
        (9, 0, True),
        (17, 59, True),
        (18, 0, False),
        (18, 1, False),
    ],
)
def test_manager_working_time_boundaries(
    local_hour: int,
    local_minute: int,
    expected: bool,
) -> None:
    moment = _local_boundary(local_hour, local_minute)
    local = to_manager_local(moment)
    assert local.hour == local_hour
    assert local.minute == local_minute
    assert is_manager_working_time(moment) is expected


def test_manager_hours_constants_are_single_source() -> None:
    assert MANAGER_WORKDAY_START.hour == 9
    assert MANAGER_WORKDAY_START.minute == 0
    assert MANAGER_WORKDAY_END.hour == 18
    assert MANAGER_WORKDAY_END.minute == 0


def test_naive_datetime_rejected_for_working_hours() -> None:
    with pytest.raises(ValueError):
        is_manager_working_time(datetime(2026, 8, 5, 9, 0))


# ---------------------------------------------------------------------------
# Eligibility outcomes
# ---------------------------------------------------------------------------


def test_normalize_self_booking_allowed() -> None:
    result = normalize_eligibility_outcome(
        BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
        selected_service=SelectedService("service-1"),
        selected_master=SelectedMaster("master-a"),
    )
    assert result.outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    assert result.selected_service is not None
    assert result.selected_service.service_id == "service-1"
    assert result.selected_master is not None
    assert result.selected_master.master_id == "master-a"


def test_normalize_manager_handoff() -> None:
    result = normalize_eligibility_outcome(
        BookingEligibilityOutcome.MANAGER_HANDOFF,
        internal_reason_code="MANAGER_ONLY",
    )
    assert result.outcome is BookingEligibilityOutcome.MANAGER_HANDOFF
    assert result.internal_reason_code == "MANAGER_ONLY"


def test_normalize_service_unavailable() -> None:
    result = normalize_eligibility_outcome(
        BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    )
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert (
        result.internal_reason_code
        == BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE.value
    )


@pytest.mark.parametrize(
    "unknown",
    [
        "TOTALLY_UNKNOWN",
        "self_booking_allowed",
        "",
        42,
        None,
        object(),
        ["SELF_BOOKING_ALLOWED"],
    ],
)
def test_unknown_eligibility_outcome_fails_closed(unknown: object) -> None:
    result = normalize_eligibility_outcome(unknown)
    assert result.outcome is not BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert result.internal_reason_code == BookingInternalReasonCode.UNKNOWN_OUTCOME.value


def test_string_outcomes_are_accepted() -> None:
    allowed = normalize_eligibility_outcome("SELF_BOOKING_ALLOWED")
    handoff = normalize_eligibility_outcome("MANAGER_HANDOFF")
    unavailable = normalize_eligibility_outcome("SERVICE_UNAVAILABLE")
    assert allowed.outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    assert handoff.outcome is BookingEligibilityOutcome.MANAGER_HANDOFF
    assert unavailable.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE


def test_eligibility_dto_has_no_phone_or_name_fields() -> None:
    fields = BookingEligibilityResult.__dataclass_fields__
    assert "phone" not in fields
    assert "name" not in fields
    assert "client_name" not in fields
    assert "client_phone" not in fields
    slot_fields = AvailableSlot.__dataclass_fields__
    assert "phone" not in slot_fields
    assert "name" not in slot_fields


# ---------------------------------------------------------------------------
# Slot collection / malformed / invent prohibition
# ---------------------------------------------------------------------------


def test_parse_available_slot_accepts_only_typed_slots() -> None:
    good = _slot(slot_id="s1", starts_at=_utc(2026, 8, 6, 5, 0))
    assert parse_available_slot(good) is good
    assert parse_available_slot(None) is None
    assert parse_available_slot({"slot_id": "s1", "starts_at": _utc(2026, 8, 6, 5, 0)}) is None
    assert parse_available_slot("09:00") is None
    assert parse_available_slot(object()) is None


def test_collect_valid_slots_drops_malformed_and_never_invents() -> None:
    good = _slot(slot_id="s1", starts_at=_utc(2026, 8, 6, 5, 0))
    assert collect_valid_slots([good, None, {"starts_at": "soon"}, "10:00", 1]) == (good,)
    assert collect_valid_slots(None) == ()
    assert collect_valid_slots("not-a-list") == ()
    assert collect_valid_slots([]) == ()
    assert collect_valid_slots([None, {}, "invented"]) == ()


# ---------------------------------------------------------------------------
# Dialog policy: eligibility branches
# ---------------------------------------------------------------------------


def test_manager_handoff_eligibility_never_offers_slots() -> None:
    eligibility = normalize_eligibility_outcome(
        BookingEligibilityOutcome.MANAGER_HANDOFF,
        internal_reason_code="ONLINE_DISABLED",
    )
    slots = (
        _slot(slot_id="s1", starts_at=_utc(2026, 8, 6, 5, 0)),
        _slot(slot_id="s2", starts_at=_utc(2026, 8, 6, 6, 0)),
    )
    decision = decide_booking_dialog(
        eligibility,
        slots,
        now=_local_boundary(12, 0),
    )
    assert isinstance(decision, ManagerHandoffDecision)
    assert decision.action is BookingDialogAction.MANAGER_HANDOFF
    text = client_message_for_decision(decision)
    assert "ONLINE_DISABLED" not in text
    assert decision.internal_reason_code == "ONLINE_DISABLED"


def test_service_unavailable_does_not_claim_closed() -> None:
    eligibility = normalize_eligibility_outcome(
        BookingEligibilityOutcome.SERVICE_UNAVAILABLE,
        internal_reason_code="STUDIO_ONLINE_DISABLED",
    )
    decision = decide_booking_dialog(
        eligibility,
        (_slot(slot_id="s1", starts_at=_utc(2026, 8, 6, 5, 0)),),
        now=_local_boundary(12, 0),
    )
    assert isinstance(decision, ServiceUnavailableDecision)
    text = client_message_for_decision(decision)
    assert "STUDIO_ONLINE_DISABLED" not in text
    lowered = text.lower()
    assert "закрыт" not in lowered
    assert "closed" not in lowered
    assert "недоступен мастер" not in lowered
    assert "услуга закрыт" not in lowered


# ---------------------------------------------------------------------------
# Slot counts 0..3+
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4, 5])
def test_slot_offer_counts_and_cap(count: int) -> None:
    eligibility = normalize_eligibility_outcome(
        BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
        selected_master=SelectedMaster("master-a"),
    )
    slots = tuple(
        _slot(
            slot_id=f"s{i}",
            starts_at=_utc(2026, 8, 6, 5, i),
            master_id="master-a",
        )
        for i in range(count)
    )
    decision = decide_booking_dialog(
        eligibility,
        slots,
        now=_local_boundary(10, 0),
    )
    if count == 0:
        assert isinstance(decision, ManagerHandoffDecision)
        assert (
            decision.internal_reason_code
            == BookingInternalReasonCode.NO_VALID_SLOTS.value
        )
        return
    assert isinstance(decision, SlotOfferDecision)
    assert len(decision.offered_slots) == min(count, MAX_OFFERED_SLOTS)
    assert MAX_OFFERED_SLOTS == 3


def test_slots_are_sorted_nearest_first_and_capped_at_three() -> None:
    eligibility = normalize_eligibility_outcome(
        BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
        selected_master=SelectedMaster("master-a"),
    )
    slots = (
        _slot(slot_id="late", starts_at=_utc(2026, 8, 6, 9, 0)),
        _slot(slot_id="mid", starts_at=_utc(2026, 8, 6, 7, 0)),
        _slot(slot_id="early", starts_at=_utc(2026, 8, 6, 5, 0)),
        _slot(slot_id="latest", starts_at=_utc(2026, 8, 6, 11, 0)),
    )
    decision = decide_booking_dialog(
        eligibility,
        slots,
        now=_local_boundary(10, 0),
    )
    assert isinstance(decision, SlotOfferDecision)
    assert [slot.slot_id for slot in decision.offered_slots] == [
        "early",
        "mid",
        "late",
    ]


def test_malformed_slots_do_not_create_invented_offers() -> None:
    eligibility = normalize_eligibility_outcome(
        BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
        selected_master=SelectedMaster("master-a"),
    )
    decision = decide_booking_dialog(
        eligibility,
        [None, {"slot_id": "x"}, "завтра в 10", object()],
        now=_local_boundary(10, 0),
    )
    assert isinstance(decision, ManagerHandoffDecision)
    assert (
        decision.internal_reason_code
        == BookingInternalReasonCode.NO_VALID_SLOTS.value
    )


def test_policy_never_invents_slots_when_backend_empty() -> None:
    eligibility = normalize_eligibility_outcome(
        BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    )
    decision = decide_booking_dialog(eligibility, (), now=_local_boundary(10, 0))
    assert not isinstance(decision, SlotOfferDecision)
    assert isinstance(decision, ManagerHandoffDecision)


# ---------------------------------------------------------------------------
# Alternate master consent
# ---------------------------------------------------------------------------


def test_other_master_slots_without_consent_handoff() -> None:
    eligibility = normalize_eligibility_outcome(
        BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
        selected_master=SelectedMaster("master-a"),
        other_online_master_ids=("master-b",),
    )
    slots = (
        _slot(
            slot_id="b1",
            starts_at=_utc(2026, 8, 6, 5, 0),
            master_id="master-b",
        ),
    )
    decision = decide_booking_dialog(
        eligibility,
        slots,
        now=_local_boundary(10, 0),
        alternate_master_consent=False,
    )
    assert isinstance(decision, ManagerHandoffDecision)
    assert (
        decision.internal_reason_code
        == BookingInternalReasonCode.ALTERNATE_MASTER_WITHOUT_CONSENT.value
    )


def test_other_master_slots_with_consent_offers() -> None:
    eligibility = normalize_eligibility_outcome(
        BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
        selected_master=SelectedMaster("master-a"),
        other_online_master_ids=("master-b",),
    )
    slots = (
        _slot(
            slot_id="b1",
            starts_at=_utc(2026, 8, 6, 5, 0),
            master_id="master-b",
        ),
        _slot(
            slot_id="b2",
            starts_at=_utc(2026, 8, 6, 6, 0),
            master_id="master-b",
        ),
    )
    decision = decide_booking_dialog(
        eligibility,
        slots,
        now=_local_boundary(10, 0),
        alternate_master_consent=True,
    )
    assert isinstance(decision, SlotOfferDecision)
    assert [slot.master_id for slot in decision.offered_slots] == [
        "master-b",
        "master-b",
    ]


def test_selected_master_slots_preferred_without_needing_consent() -> None:
    eligibility = normalize_eligibility_outcome(
        BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
        selected_master=SelectedMaster("master-a"),
    )
    slots = (
        _slot(
            slot_id="b1",
            starts_at=_utc(2026, 8, 6, 4, 0),
            master_id="master-b",
        ),
        _slot(
            slot_id="a1",
            starts_at=_utc(2026, 8, 6, 8, 0),
            master_id="master-a",
        ),
    )
    decision = decide_booking_dialog(
        eligibility,
        slots,
        now=_local_boundary(10, 0),
        alternate_master_consent=False,
    )
    assert isinstance(decision, SlotOfferDecision)
    assert [slot.slot_id for slot in decision.offered_slots] == ["a1"]


# ---------------------------------------------------------------------------
# Manager hours wording
# ---------------------------------------------------------------------------


def test_handoff_during_manager_hours_uses_neutral_copy() -> None:
    eligibility = normalize_eligibility_outcome(
        BookingEligibilityOutcome.MANAGER_HANDOFF,
        internal_reason_code="MANAGER_ONLY",
    )
    decision = decide_booking_dialog(
        eligibility,
        (),
        now=_local_boundary(9, 0),
    )
    assert isinstance(decision, ManagerHandoffDecision)
    assert decision.during_manager_hours is True
    assert (
        decision.client_message_kind
        is BookingClientMessageKind.HANDOFF_DURING_MANAGER_HOURS
    )
    text = client_message_for_decision(decision)
    assert "ближайшее рабочее время" not in text
    assert "MANAGER_ONLY" not in text


def test_handoff_outside_manager_hours_mentions_next_working_time() -> None:
    eligibility = normalize_eligibility_outcome(
        BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    )
    decision = decide_booking_dialog(
        eligibility,
        (),
        now=_local_boundary(18, 0),
    )
    assert isinstance(decision, ManagerHandoffDecision)
    assert decision.during_manager_hours is False
    assert (
        decision.client_message_kind
        is BookingClientMessageKind.HANDOFF_OUTSIDE_MANAGER_HOURS
    )
    text = client_message_for_decision(decision)
    assert "ближайшее рабочее время" in text


@pytest.mark.parametrize(
    ("hour", "minute", "during"),
    [
        (8, 59, False),
        (9, 0, True),
        (17, 59, True),
        (18, 0, False),
        (18, 1, False),
    ],
)
def test_handoff_message_follows_working_hour_boundaries(
    hour: int,
    minute: int,
    during: bool,
) -> None:
    eligibility = normalize_eligibility_outcome(
        BookingEligibilityOutcome.MANAGER_HANDOFF
    )
    decision = decide_booking_dialog(
        eligibility,
        (),
        now=_local_boundary(hour, minute),
    )
    assert isinstance(decision, ManagerHandoffDecision)
    assert decision.during_manager_hours is during
    text = client_message_for_decision(decision)
    if during:
        assert "ближайшее рабочее время" not in text
    else:
        assert "ближайшее рабочее время" in text


# ---------------------------------------------------------------------------
# Internal reason never in client text / fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    list(BookingInternalReasonCode) + ["STUDIO_ONLINE_DISABLED", "MANAGER_ONLY"],
)
def test_client_messages_never_contain_internal_reason(reason: object) -> None:
    code = reason.value if isinstance(reason, BookingInternalReasonCode) else reason
    assert isinstance(code, str)
    for kind in BookingClientMessageKind:
        text = render_client_message(kind)
        assert code not in text
        assert "reason" not in text.lower()


def test_decisions_keep_internal_reason_out_of_client_text() -> None:
    for outcome in (
        BookingEligibilityOutcome.MANAGER_HANDOFF,
        BookingEligibilityOutcome.SERVICE_UNAVAILABLE,
    ):
        eligibility = normalize_eligibility_outcome(
            outcome,
            internal_reason_code="SECRET_REASON_CODE_XYZ",
        )
        decision = decide_booking_dialog(
            eligibility,
            (),
            now=_local_boundary(12, 0),
        )
        text = client_message_for_decision(decision)
        assert "SECRET_REASON_CODE_XYZ" not in text
        assert getattr(decision, "internal_reason_code") == "SECRET_REASON_CODE_XYZ"


def test_unknown_outcome_path_does_not_offer_slots() -> None:
    eligibility = normalize_eligibility_outcome("NOT_A_REAL_OUTCOME")
    decision = decide_booking_dialog(
        eligibility,
        (_slot(slot_id="s1", starts_at=_utc(2026, 8, 6, 5, 0)),),
        now=_local_boundary(12, 0),
    )
    assert isinstance(decision, ServiceUnavailableDecision)
    assert not isinstance(decision, SlotOfferDecision)


def test_booking_domain_does_not_weaken_outbound_deny() -> None:
    settings = Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=False)
    assert (
        is_automatic_outbound_allowed(settings, OutboundAction.SEND_MESSAGE) is False
    )


def test_decide_rejects_naive_now() -> None:
    eligibility = normalize_eligibility_outcome(
        BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    )
    with pytest.raises(Exception):
        decide_booking_dialog(
            eligibility,
            (),
            now=datetime(2026, 8, 5, 10, 0),
        )
