"""Bridge synthetic reply-plan booking fixtures to BookingFlowService (CURSOR-20).

Pure mapping + durable resolution helpers for ReplyPlanWorker.
No channel/outbound/HTTP I/O. Remote resolve must run outside DB locks.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Final

from app.core.booking_types import (
    AvailableSlot,
    BookingDialogAction,
    BookingDomainError,
    BookingInternalReasonCode,
    ManagerHandoffDecision,
    SelectedMaster,
    SelectedService,
    ServiceUnavailableDecision,
    SlotOfferDecision,
)
from app.schemas.booking_input import SyntheticBookingInput
from app.services.booking_flow import BookingFlowService

BOOKING_RESOLUTION_STARTED_KEY: Final[str] = "booking_resolution_started"
BOOKING_RESOLUTION_RESULT_KEY: Final[str] = "booking_resolution_result"

_ALLOWED_BOOKING_REASONS: Final[frozenset[str]] = frozenset(
    {
        *(code.value for code in BookingInternalReasonCode),
        "STUDIO_ONLINE_DISABLED",
        "SERVICE_INACTIVE",
        "MASTER_INACTIVE",
        "ONLINE_DISABLED",
        "MASTER_SERVICE_UNAVAILABLE",
        "MANAGER_ONLY",
        "REMOTE_REJECTED",
        "TIMEOUT",
        "TRANSPORT_ERROR",
        "RESPONSE_TOO_LARGE",
        "RESPONSE_INVALID",
        "CONFIG_INVALID",
    }
)

_ALLOWED_RESULT_ACTIONS: Final[frozenset[str]] = frozenset(
    action.value for action in BookingDialogAction
)


class BookingResolutionPhase(str, Enum):
    """Durable booking state derived from reply-plan payload_json."""

    NON_BOOKING = "NON_BOOKING"
    NEEDS_REMOTE = "NEEDS_REMOTE"
    INTERRUPTED = "INTERRUPTED"
    HAS_RESULT = "HAS_RESULT"


def parse_booking_fixture(raw: object) -> SyntheticBookingInput | None:
    """Parse optional booking block. Invalid shapes raise ValueError."""

    if raw is None:
        return None
    if type(raw) is not dict:
        raise ValueError("BOOKING_INPUT_INVALID")
    try:
        return SyntheticBookingInput.model_validate(raw)
    except Exception as exc:
        raise ValueError("BOOKING_INPUT_INVALID") from exc


def plan_has_booking_fixture(plan_payload: dict[str, Any]) -> bool:
    return "booking" in plan_payload


def booking_resolution_phase(plan_payload: dict[str, Any]) -> BookingResolutionPhase:
    """Classify durable booking state. Non-booking plans skip remote work."""

    if not plan_has_booking_fixture(plan_payload):
        return BookingResolutionPhase.NON_BOOKING
    result = plan_payload.get(BOOKING_RESOLUTION_RESULT_KEY)
    if isinstance(result, dict):
        return BookingResolutionPhase.HAS_RESULT
    if plan_payload.get(BOOKING_RESOLUTION_STARTED_KEY) is True:
        return BookingResolutionPhase.INTERRUPTED
    return BookingResolutionPhase.NEEDS_REMOTE


def _allowlisted_reason(raw: object) -> str | None:
    if raw is None:
        return None
    if type(raw) is not str or raw not in _ALLOWED_BOOKING_REASONS:
        return BookingInternalReasonCode.UNKNOWN_OUTCOME.value
    return raw


def decision_to_outbound_fields(decision: object) -> dict[str, Any]:
    """Map a booking decision to safe synthetic outbound fields."""

    if type(decision) is SlotOfferDecision:
        return {
            "booking_action": BookingDialogAction.OFFER_SLOTS.value,
            "booking_reason": None,
            "booking_offered_slot_ids": [slot.slot_id for slot in decision.offered_slots],
        }
    if type(decision) is ManagerHandoffDecision:
        return {
            "booking_action": BookingDialogAction.MANAGER_HANDOFF.value,
            "booking_reason": _allowlisted_reason(decision.internal_reason_code),
        }
    if type(decision) is ServiceUnavailableDecision:
        return {
            "booking_action": BookingDialogAction.SERVICE_UNAVAILABLE.value,
            "booking_reason": _allowlisted_reason(decision.internal_reason_code),
        }
    return {
        "booking_action": BookingDialogAction.SERVICE_UNAVAILABLE.value,
        "booking_reason": BookingInternalReasonCode.UNKNOWN_OUTCOME.value,
    }


def interrupted_booking_fields() -> dict[str, Any]:
    """Fail-closed fields when a started attempt has no persisted result."""

    return {
        "booking_action": BookingDialogAction.SERVICE_UNAVAILABLE.value,
        "booking_reason": (
            BookingInternalReasonCode.BOOKING_RESOLUTION_INTERRUPTED.value
        ),
    }


def sanitize_booking_result_fields(raw: object) -> dict[str, Any]:
    """Keep only allowlisted action/reason/slot_ids for durable persistence."""

    if type(raw) is not dict:
        return interrupted_booking_fields()
    action = raw.get("booking_action")
    if type(action) is not str or action not in _ALLOWED_RESULT_ACTIONS:
        return interrupted_booking_fields()
    fields: dict[str, Any] = {
        "booking_action": action,
        "booking_reason": _allowlisted_reason(raw.get("booking_reason")),
    }
    if action == BookingDialogAction.OFFER_SLOTS.value:
        slot_ids = raw.get("booking_offered_slot_ids")
        if type(slot_ids) is not list or not all(type(x) is str for x in slot_ids):
            return interrupted_booking_fields()
        fields["booking_offered_slot_ids"] = list(slot_ids)
    return fields


def read_booking_resolution_result(
    plan_payload: dict[str, Any],
) -> dict[str, Any] | None:
    raw = plan_payload.get(BOOKING_RESOLUTION_RESULT_KEY)
    if raw is None:
        return None
    return sanitize_booking_result_fields(raw)


def with_booking_resolution_started(plan_payload: dict[str, Any]) -> dict[str, Any]:
    updated = dict(plan_payload)
    updated[BOOKING_RESOLUTION_STARTED_KEY] = True
    return updated


def with_booking_resolution_result(
    plan_payload: dict[str, Any],
    fields: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(plan_payload)
    updated[BOOKING_RESOLUTION_STARTED_KEY] = True
    updated[BOOKING_RESOLUTION_RESULT_KEY] = sanitize_booking_result_fields(fields)
    return updated


def resolve_booking_outbound_fields(
    plan_payload: dict[str, Any],
    *,
    booking_flow: BookingFlowService,
) -> dict[str, Any]:
    """Call booking_flow.resolve once for a plan payload booking block.

    Must run outside DB transactions/locks (typically via asyncio.to_thread).
    Missing booking → empty dict (non-booking path unchanged).
    Malformed booking / domain errors → fail-closed SERVICE_UNAVAILABLE fields.
    """

    if "booking" not in plan_payload:
        return {}

    try:
        fixture = parse_booking_fixture(plan_payload.get("booking"))
    except ValueError:
        return {
            "booking_action": BookingDialogAction.SERVICE_UNAVAILABLE.value,
            "booking_reason": BookingInternalReasonCode.MALFORMED_ELIGIBILITY.value,
        }
    if fixture is None:
        return {
            "booking_action": BookingDialogAction.SERVICE_UNAVAILABLE.value,
            "booking_reason": BookingInternalReasonCode.MALFORMED_ELIGIBILITY.value,
        }

    try:
        service = SelectedService(fixture.service_id)
        master = (
            SelectedMaster(fixture.master_id) if fixture.master_id is not None else None
        )
        slots = tuple(
            AvailableSlot(
                slot_id=slot.slot_id,
                starts_at=slot.starts_at,
                master_id=slot.master_id,
                service_id=slot.service_id,
            )
            for slot in fixture.slots
        )
        decision = booking_flow.resolve(
            service,
            master,
            slots,
            now=fixture.decision_at,
            include_alternatives=fixture.include_alternatives,
            alternate_master_consent=fixture.alternate_master_consent,
        )
    except (BookingDomainError, ValueError, TypeError):
        return {
            "booking_action": BookingDialogAction.SERVICE_UNAVAILABLE.value,
            "booking_reason": BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE.value,
        }

    return decision_to_outbound_fields(decision)


def base_synthetic_outbound_payload(plan_payload: dict[str, Any]) -> dict[str, Any]:
    """Token-only outbound envelope (non-booking shape)."""

    return {
        "schema": "synthetic.outbound.v1",
        "source_schema": plan_payload.get("schema"),
        "plan_type": plan_payload.get("plan_type"),
        "synthetic_token": plan_payload.get("synthetic_token", "SYNTHETIC_OK"),
    }


def build_synthetic_outbound_payload(
    plan_payload: dict[str, Any],
    *,
    booking_fields: dict[str, Any] | None = None,
    booking_flow: BookingFlowService | None = None,
) -> dict[str, Any]:
    """Build synthetic.outbound.v1 payload.

    Prefer explicit ``booking_fields`` (durable result). Legacy ``booking_flow``
    path resolves synchronously and is reserved for unit helpers — workers must
    resolve off-transaction via ``resolve_booking_outbound_fields``.
    """

    payload = base_synthetic_outbound_payload(plan_payload)
    if booking_fields is not None:
        payload.update(sanitize_booking_result_fields(booking_fields))
        return payload
    if booking_flow is None:
        return payload
    payload.update(
        resolve_booking_outbound_fields(plan_payload, booking_flow=booking_flow)
    )
    return payload


def client_reply_plan_payload(
    *,
    inbox_id: str,
    booking: SyntheticBookingInput | None = None,
    deferred_for_handoff: bool = False,
) -> dict[str, Any]:
    """CLIENT_REPLY reply-plan payload. Booking wire only — no message text."""

    payload: dict[str, Any] = {
        "schema": "synthetic.reply_plan.v1",
        "plan_type": "CLIENT_REPLY",
        "synthetic_token": "SYNTHETIC_OK",
        "inbox_id": inbox_id,
    }
    if deferred_for_handoff:
        payload["deferred_for_handoff"] = True
    if booking is not None:
        payload["booking"] = booking.wire_dict()
    return payload
