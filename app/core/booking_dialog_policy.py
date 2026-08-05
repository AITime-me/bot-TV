"""Pure booking dialog policy (CURSOR-15).

No AI, HTTP, channel adapters, or production pipeline wiring.
Slots must come from a backend-provided collection; the bot never invents times.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from app.core.booking_types import (
    MAX_OFFERED_SLOTS,
    AvailableSlot,
    BookingClientMessageKind,
    BookingDialogAction,
    BookingDomainError,
    BookingEligibilityOutcome,
    BookingEligibilityResult,
    BookingInternalReasonCode,
    BookingPolicyDecision,
    ManagerHandoffDecision,
    SelectedMaster,
    SelectedService,
    ServiceUnavailableDecision,
    SlotOfferDecision,
)
from app.core.manager_working_hours import is_manager_working_time


def normalize_eligibility_outcome(
    outcome: object,
    *,
    selected_service: SelectedService | None = None,
    selected_master: SelectedMaster | None = None,
    other_online_master_ids: Sequence[object] = (),
    internal_reason_code: object = None,
) -> BookingEligibilityResult:
    """Normalize a raw eligibility outcome. Unknown values fail closed.

    Unknown / malformed outcomes never become SELF_BOOKING_ALLOWED.
    """

    if selected_service is not None and type(selected_service) is not SelectedService:
        raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
    if selected_master is not None and type(selected_master) is not SelectedMaster:
        raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None

    master_ids: list[str] = []
    if type(other_online_master_ids) not in (tuple, list):
        return BookingEligibilityResult(
            outcome=BookingEligibilityOutcome.SERVICE_UNAVAILABLE,
            selected_service=selected_service,
            selected_master=selected_master,
            other_online_master_ids=(),
            internal_reason_code=BookingInternalReasonCode.MALFORMED_ELIGIBILITY,
        )
    for item in other_online_master_ids:
        if type(item) is not str or not item or any(ch.isspace() for ch in item):
            return BookingEligibilityResult(
                outcome=BookingEligibilityOutcome.SERVICE_UNAVAILABLE,
                selected_service=selected_service,
                selected_master=selected_master,
                other_online_master_ids=(),
                internal_reason_code=BookingInternalReasonCode.MALFORMED_ELIGIBILITY,
            )
        master_ids.append(item)

    reason: str | BookingInternalReasonCode | None
    if internal_reason_code is None:
        reason = None
    elif type(internal_reason_code) is BookingInternalReasonCode:
        reason = internal_reason_code
    elif type(internal_reason_code) is str and internal_reason_code and not any(
        ch.isspace() for ch in internal_reason_code
    ):
        reason = internal_reason_code
    else:
        return BookingEligibilityResult(
            outcome=BookingEligibilityOutcome.SERVICE_UNAVAILABLE,
            selected_service=selected_service,
            selected_master=selected_master,
            other_online_master_ids=tuple(master_ids),
            internal_reason_code=BookingInternalReasonCode.MALFORMED_ELIGIBILITY,
        )

    if outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED:
        return BookingEligibilityResult(
            outcome=BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
            selected_service=selected_service,
            selected_master=selected_master,
            other_online_master_ids=tuple(master_ids),
            internal_reason_code=reason,
        )
    if outcome is BookingEligibilityOutcome.MANAGER_HANDOFF:
        return BookingEligibilityResult(
            outcome=BookingEligibilityOutcome.MANAGER_HANDOFF,
            selected_service=selected_service,
            selected_master=selected_master,
            other_online_master_ids=tuple(master_ids),
            internal_reason_code=(
                reason
                if reason is not None
                else BookingInternalReasonCode.ELIGIBILITY_MANAGER_HANDOFF
            ),
        )
    if outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE:
        return BookingEligibilityResult(
            outcome=BookingEligibilityOutcome.SERVICE_UNAVAILABLE,
            selected_service=selected_service,
            selected_master=selected_master,
            other_online_master_ids=tuple(master_ids),
            internal_reason_code=(
                reason
                if reason is not None
                else BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE
            ),
        )

    # Fail closed: strings that match known outcomes are accepted; anything else
    # never grants self-booking.
    if type(outcome) is str:
        if outcome == BookingEligibilityOutcome.SELF_BOOKING_ALLOWED.value:
            return BookingEligibilityResult(
                outcome=BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
                selected_service=selected_service,
                selected_master=selected_master,
                other_online_master_ids=tuple(master_ids),
                internal_reason_code=reason,
            )
        if outcome == BookingEligibilityOutcome.MANAGER_HANDOFF.value:
            return BookingEligibilityResult(
                outcome=BookingEligibilityOutcome.MANAGER_HANDOFF,
                selected_service=selected_service,
                selected_master=selected_master,
                other_online_master_ids=tuple(master_ids),
                internal_reason_code=(
                    reason
                    if reason is not None
                    else BookingInternalReasonCode.ELIGIBILITY_MANAGER_HANDOFF
                ),
            )
        if outcome == BookingEligibilityOutcome.SERVICE_UNAVAILABLE.value:
            return BookingEligibilityResult(
                outcome=BookingEligibilityOutcome.SERVICE_UNAVAILABLE,
                selected_service=selected_service,
                selected_master=selected_master,
                other_online_master_ids=tuple(master_ids),
                internal_reason_code=(
                    reason
                    if reason is not None
                    else BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE
                ),
            )

    return BookingEligibilityResult(
        outcome=BookingEligibilityOutcome.SERVICE_UNAVAILABLE,
        selected_service=selected_service,
        selected_master=selected_master,
        other_online_master_ids=tuple(master_ids),
        internal_reason_code=BookingInternalReasonCode.UNKNOWN_OUTCOME,
    )


def parse_available_slot(raw: object) -> AvailableSlot | None:
    """Accept only well-formed AvailableSlot values. Malformed input is dropped."""

    if type(raw) is AvailableSlot:
        return raw
    return None


def collect_valid_slots(raw_slots: object) -> tuple[AvailableSlot, ...]:
    """Filter a backend slot collection. Never invents replacement slots."""

    if raw_slots is None:
        return ()
    if type(raw_slots) not in (tuple, list):
        return ()
    valid: list[AvailableSlot] = []
    for item in raw_slots:
        slot = parse_available_slot(item)
        if slot is not None:
            valid.append(slot)
    return tuple(valid)


def _handoff_decision(
    *,
    now: datetime,
    internal_reason_code: str | BookingInternalReasonCode | None,
) -> ManagerHandoffDecision:
    during_hours = is_manager_working_time(now)
    if during_hours:
        kind = BookingClientMessageKind.HANDOFF_DURING_MANAGER_HOURS
    else:
        kind = BookingClientMessageKind.HANDOFF_OUTSIDE_MANAGER_HOURS
    return ManagerHandoffDecision(
        action=BookingDialogAction.MANAGER_HANDOFF,
        client_message_kind=kind,
        during_manager_hours=during_hours,
        internal_reason_code=internal_reason_code,
    )


def _service_unavailable_decision(
    *,
    internal_reason_code: str | BookingInternalReasonCode | None,
) -> ServiceUnavailableDecision:
    return ServiceUnavailableDecision(
        action=BookingDialogAction.SERVICE_UNAVAILABLE,
        client_message_kind=BookingClientMessageKind.SERVICE_TEMPORARILY_UNAVAILABLE,
        internal_reason_code=internal_reason_code,
    )


def _select_nearest_slots(slots: Iterable[AvailableSlot]) -> tuple[AvailableSlot, ...]:
    ordered = sorted(slots, key=lambda slot: (slot.starts_at, slot.slot_id))
    return tuple(ordered[:MAX_OFFERED_SLOTS])


def decide_booking_dialog(
    eligibility: BookingEligibilityResult,
    raw_slots: object,
    *,
    now: datetime,
    alternate_master_consent: bool = False,
) -> BookingPolicyDecision:
    """Apply pure booking dialog policy without AI or HTTP.

    Rules:
    - offer only backend-provided valid slots;
    - never invent times;
    - offer at most three nearest suitable slots;
    - no slots → manager handoff;
    - MANAGER_HANDOFF → no slot offers;
    - SERVICE_UNAVAILABLE → unavailable path (no closed-master claims);
    - other ONLINE master slots require explicit client consent;
    - without consent → manager handoff;
    - handoff copy depends on manager working hours.
    """

    if type(eligibility) is not BookingEligibilityResult:
        raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
    if type(alternate_master_consent) is not bool:
        raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
    if not isinstance(now, datetime):
        raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
    if now.tzinfo is None or now.utcoffset() is None:
        raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None

    if eligibility.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE:
        return _service_unavailable_decision(
            internal_reason_code=(
                eligibility.internal_reason_code
                or BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE
            )
        )

    if eligibility.outcome is BookingEligibilityOutcome.MANAGER_HANDOFF:
        return _handoff_decision(
            now=now,
            internal_reason_code=(
                eligibility.internal_reason_code
                or BookingInternalReasonCode.ELIGIBILITY_MANAGER_HANDOFF
            ),
        )

    if eligibility.outcome is not BookingEligibilityOutcome.SELF_BOOKING_ALLOWED:
        # Fail closed: never treat an unexpected enum member as self-booking.
        return _service_unavailable_decision(
            internal_reason_code=BookingInternalReasonCode.UNKNOWN_OUTCOME
        )

    valid_slots = collect_valid_slots(raw_slots)
    if not valid_slots:
        return _handoff_decision(
            now=now,
            internal_reason_code=BookingInternalReasonCode.NO_VALID_SLOTS,
        )

    selected_master = eligibility.selected_master
    if selected_master is None:
        offered = _select_nearest_slots(valid_slots)
        return SlotOfferDecision(
            action=BookingDialogAction.OFFER_SLOTS,
            offered_slots=offered,
            client_message_kind=BookingClientMessageKind.OFFER_SLOTS,
        )

    selected_id = selected_master.master_id
    matching = tuple(slot for slot in valid_slots if slot.master_id == selected_id)
    if matching:
        offered = _select_nearest_slots(matching)
        return SlotOfferDecision(
            action=BookingDialogAction.OFFER_SLOTS,
            offered_slots=offered,
            client_message_kind=BookingClientMessageKind.OFFER_SLOTS,
        )

    # Only other-master slots remain.
    if not alternate_master_consent:
        return _handoff_decision(
            now=now,
            internal_reason_code=BookingInternalReasonCode.ALTERNATE_MASTER_WITHOUT_CONSENT,
        )

    offered = _select_nearest_slots(valid_slots)
    return SlotOfferDecision(
        action=BookingDialogAction.OFFER_SLOTS,
        offered_slots=offered,
        client_message_kind=BookingClientMessageKind.OFFER_SLOTS,
    )
