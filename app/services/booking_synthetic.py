"""Bridge synthetic reply-plan booking fixtures to BookingFlowService (CURSOR-20/23).

Pure mapping + durable resolution helpers for ReplyPlanWorker.
No channel/outbound/HTTP I/O. Remote resolve must run outside DB locks.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Final

from app.core.booking_availability_remote import (
    format_canonical_booking_starts_at,
    require_calendar_date,
    require_canonical_booking_starts_at,
)
from app.core.booking_types import (
    AvailableDaysOfferDecision,
    AvailableSlot,
    BookingClientMessageKind,
    BookingDialogAction,
    BookingDomainError,
    BookingInternalReasonCode,
    ManagerHandoffDecision,
    MAX_OFFERED_SLOTS,
    SelectedMaster,
    SelectedService,
    ServiceUnavailableDecision,
    SlotOfferDecision,
)
from app.schemas.booking_input import (
    SyntheticAvailableDaysQuery,
    SyntheticAvailableSlotsQuery,
    SyntheticBookingInput,
)
from app.services.booking_flow import BookingFlowService
from app.services.outbound_reply_text import (
    OutboundReplyTextError,
    render_text_for_booking_fields,
)

BOOKING_RESOLUTION_STARTED_KEY: Final[str] = "booking_resolution_started"
BOOKING_RESOLUTION_RESULT_KEY: Final[str] = "booking_resolution_result"

_MAX_OFFER_DAYS: Final[int] = 31
_MAX_SLOT_ID_LENGTH: Final[int] = 128

_OFFER_DAYS_KEYS: Final[frozenset[str]] = frozenset(
    {
        "booking_action",
        "booking_reason",
        "booking_available_date_keys",
        "booking_studio_today",
    }
)
_OFFER_SLOTS_LEGACY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "booking_action",
        "booking_reason",
        "booking_offered_slot_ids",
    }
)
_OFFER_SLOTS_LEGACY_KEYS_WITH_KIND: Final[frozenset[str]] = frozenset(
    {
        "booking_action",
        "booking_reason",
        "booking_offered_slot_ids",
        "client_message_kind",
    }
)
_OFFER_SLOTS_NEW_KEYS: Final[frozenset[str]] = frozenset(
    {
        "booking_action",
        "booking_reason",
        "booking_offered_slot_ids",
        "booking_offered_slots",
    }
)
_OFFER_SLOTS_NEW_KEYS_WITH_KIND: Final[frozenset[str]] = frozenset(
    {
        "booking_action",
        "booking_reason",
        "booking_offered_slot_ids",
        "booking_offered_slots",
        "client_message_kind",
    }
)
_OFFERED_SLOT_OBJECT_KEYS: Final[frozenset[str]] = frozenset(
    {"slot_id", "starts_at"}
)
_HANDOFF_MESSAGE_KINDS: Final[frozenset[str]] = frozenset(
    {
        BookingClientMessageKind.HANDOFF_DURING_MANAGER_HOURS.value,
        BookingClientMessageKind.HANDOFF_OUTSIDE_MANAGER_HOURS.value,
    }
)

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
        "REQUEST_INVALID",
        "UNAUTHORIZED",
        "RATE_LIMITED",
        "VALIDATION_ERROR",
        "SERVICE_UNAVAILABLE",
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


def _require_safe_slot_id(value: object) -> str | None:
    if type(value) is not str or not value:
        return None
    if len(value) > _MAX_SLOT_ID_LENGTH:
        return None
    if any(ch.isspace() for ch in value):
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    return value


def _sanitize_offered_slots_new(
    slot_ids: list[str],
    offered: object,
) -> list[dict[str, str]] | None:
    if type(offered) is not list:
        return None
    if len(offered) != len(slot_ids):
        return None
    if not offered or len(offered) > MAX_OFFERED_SLOTS:
        return None

    sanitized: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_starts: set[str] = set()
    previous_start: str | None = None

    for index, item in enumerate(offered):
        if type(item) is not dict:
            return None
        if set(item) != _OFFERED_SLOT_OBJECT_KEYS:
            return None
        slot_id = _require_safe_slot_id(item.get("slot_id"))
        if slot_id is None:
            return None
        if slot_id != slot_ids[index]:
            return None
        if slot_id in seen_ids:
            return None
        try:
            starts_at = require_canonical_booking_starts_at(item.get("starts_at"))
        except ValueError:
            return None
        if starts_at in seen_starts:
            return None
        # Canonical studio timestamps are lexicographically ordered by wall time.
        if previous_start is not None and starts_at <= previous_start:
            return None
        seen_ids.add(slot_id)
        seen_starts.add(starts_at)
        previous_start = starts_at
        sanitized.append({"slot_id": slot_id, "starts_at": starts_at})
    return sanitized


def _sanitize_offer_days_fields(raw: dict[str, Any]) -> dict[str, Any] | None:
    if set(raw) != _OFFER_DAYS_KEYS:
        return None
    if raw.get("booking_action") != BookingDialogAction.OFFER_DAYS.value:
        return None
    if raw.get("booking_reason") is not None:
        return None

    date_keys_raw = raw.get("booking_available_date_keys")
    if type(date_keys_raw) is not list:
        return None
    if not date_keys_raw or len(date_keys_raw) > _MAX_OFFER_DAYS:
        return None

    keys: list[str] = []
    seen: set[str] = set()
    previous: str | None = None
    for item in date_keys_raw:
        try:
            key = require_calendar_date(item)
        except Exception:
            return None
        if key in seen:
            return None
        if previous is not None and key <= previous:
            return None
        seen.add(key)
        previous = key
        keys.append(key)

    studio_raw = raw.get("booking_studio_today")
    try:
        studio_today = require_calendar_date(studio_raw)
    except Exception:
        return None

    return {
        "booking_action": BookingDialogAction.OFFER_DAYS.value,
        "booking_reason": None,
        "booking_available_date_keys": list(keys),
        "booking_studio_today": studio_today,
    }


def _sanitize_offer_slots_fields(raw: dict[str, Any]) -> dict[str, Any] | None:
    keys = set(raw)
    if keys in {_OFFER_SLOTS_LEGACY_KEYS, _OFFER_SLOTS_LEGACY_KEYS_WITH_KIND}:
        return _sanitize_offer_slots_legacy(raw)
    if keys in {_OFFER_SLOTS_NEW_KEYS, _OFFER_SLOTS_NEW_KEYS_WITH_KIND}:
        return _sanitize_offer_slots_new_shape(raw)
    return None


def _optional_offer_slots_kind(raw: dict[str, Any]) -> str | None:
    kind = raw.get("client_message_kind")
    if kind is None:
        return None
    if kind != BookingClientMessageKind.OFFER_SLOTS.value:
        return None
    return kind


def _sanitize_slot_ids_list(raw: object) -> list[str] | None:
    if type(raw) is not list:
        return None
    if not raw or len(raw) > MAX_OFFERED_SLOTS:
        return None
    ids: list[str] = []
    seen: set[str] = set()
    for item in raw:
        slot_id = _require_safe_slot_id(item)
        if slot_id is None:
            return None
        if slot_id in seen:
            return None
        seen.add(slot_id)
        ids.append(slot_id)
    return ids


def _sanitize_offer_slots_legacy(raw: dict[str, Any]) -> dict[str, Any] | None:
    if raw.get("booking_action") != BookingDialogAction.OFFER_SLOTS.value:
        return None
    if raw.get("booking_reason") is not None:
        return None
    slot_ids = _sanitize_slot_ids_list(raw.get("booking_offered_slot_ids"))
    if slot_ids is None:
        return None
    fields: dict[str, Any] = {
        "booking_action": BookingDialogAction.OFFER_SLOTS.value,
        "booking_reason": None,
        "booking_offered_slot_ids": list(slot_ids),
    }
    kind = _optional_offer_slots_kind(raw)
    if "client_message_kind" in raw and kind is None:
        return None
    if kind is not None:
        fields["client_message_kind"] = kind
    return fields


def _sanitize_offer_slots_new_shape(raw: dict[str, Any]) -> dict[str, Any] | None:
    if raw.get("booking_action") != BookingDialogAction.OFFER_SLOTS.value:
        return None
    if raw.get("booking_reason") is not None:
        return None
    slot_ids = _sanitize_slot_ids_list(raw.get("booking_offered_slot_ids"))
    if slot_ids is None:
        return None
    offered = _sanitize_offered_slots_new(slot_ids, raw.get("booking_offered_slots"))
    if offered is None:
        return None
    fields: dict[str, Any] = {
        "booking_action": BookingDialogAction.OFFER_SLOTS.value,
        "booking_reason": None,
        "booking_offered_slot_ids": list(slot_ids),
        "booking_offered_slots": [
            {"slot_id": item["slot_id"], "starts_at": item["starts_at"]}
            for item in offered
        ],
    }
    kind = _optional_offer_slots_kind(raw)
    if "client_message_kind" in raw and kind is None:
        return None
    if kind is not None:
        fields["client_message_kind"] = kind
    return fields


def decision_to_outbound_fields(decision: object) -> dict[str, Any]:
    """Map a booking decision to safe synthetic outbound fields.

    ``client_message_kind`` is persisted for MANAGER_HANDOFF so outbound text
    can be re-derived deterministically before INSERT without re-evaluating
    manager hours.
    """

    if type(decision) is SlotOfferDecision:
        offered = [
            {
                "slot_id": slot.slot_id,
                "starts_at": format_canonical_booking_starts_at(slot.starts_at),
            }
            for slot in decision.offered_slots
        ]
        return {
            "booking_action": BookingDialogAction.OFFER_SLOTS.value,
            "booking_reason": None,
            "booking_offered_slot_ids": [slot.slot_id for slot in decision.offered_slots],
            "booking_offered_slots": offered,
            "client_message_kind": decision.client_message_kind.value,
        }
    if type(decision) is AvailableDaysOfferDecision:
        return {
            "booking_action": BookingDialogAction.OFFER_DAYS.value,
            "booking_reason": None,
            "booking_available_date_keys": list(decision.date_keys),
            "booking_studio_today": decision.studio_today,
        }
    if type(decision) is ManagerHandoffDecision:
        return {
            "booking_action": BookingDialogAction.MANAGER_HANDOFF.value,
            "booking_reason": _allowlisted_reason(decision.internal_reason_code),
            "client_message_kind": decision.client_message_kind.value,
        }
    if type(decision) is ServiceUnavailableDecision:
        return {
            "booking_action": BookingDialogAction.SERVICE_UNAVAILABLE.value,
            "booking_reason": _allowlisted_reason(decision.internal_reason_code),
            "client_message_kind": decision.client_message_kind.value,
        }
    return {
        "booking_action": BookingDialogAction.SERVICE_UNAVAILABLE.value,
        "booking_reason": BookingInternalReasonCode.UNKNOWN_OUTCOME.value,
        "client_message_kind": (
            BookingClientMessageKind.SERVICE_TEMPORARILY_UNAVAILABLE.value
        ),
    }


def interrupted_booking_fields() -> dict[str, Any]:
    """Fail-closed fields when a started attempt has no persisted result."""

    return {
        "booking_action": BookingDialogAction.SERVICE_UNAVAILABLE.value,
        "booking_reason": (
            BookingInternalReasonCode.BOOKING_RESOLUTION_INTERRUPTED.value
        ),
        "client_message_kind": (
            BookingClientMessageKind.SERVICE_TEMPORARILY_UNAVAILABLE.value
        ),
    }


def sanitize_booking_result_fields(raw: object) -> dict[str, Any]:
    """Keep only allowlisted action/reason/slot/day fields for durable persistence."""

    if type(raw) is not dict:
        return interrupted_booking_fields()
    action = raw.get("booking_action")
    if type(action) is not str or action not in _ALLOWED_RESULT_ACTIONS:
        return interrupted_booking_fields()

    if action == BookingDialogAction.OFFER_DAYS.value:
        sanitized = _sanitize_offer_days_fields(raw)
        if sanitized is None:
            return interrupted_booking_fields()
        return sanitized

    if action == BookingDialogAction.OFFER_SLOTS.value:
        sanitized = _sanitize_offer_slots_fields(raw)
        if sanitized is None:
            return interrupted_booking_fields()
        return sanitized

    fields: dict[str, Any] = {
        "booking_action": action,
        "booking_reason": _allowlisted_reason(raw.get("booking_reason")),
    }
    kind = raw.get("client_message_kind")
    if action == BookingDialogAction.MANAGER_HANDOFF.value:
        if type(kind) is not str or kind not in _HANDOFF_MESSAGE_KINDS:
            return interrupted_booking_fields()
        fields["client_message_kind"] = kind
    elif action == BookingDialogAction.SERVICE_UNAVAILABLE.value:
        if kind is None:
            fields["client_message_kind"] = (
                BookingClientMessageKind.SERVICE_TEMPORARILY_UNAVAILABLE.value
            )
        elif (
            kind
            == BookingClientMessageKind.SERVICE_TEMPORARILY_UNAVAILABLE.value
        ):
            fields["client_message_kind"] = kind
        else:
            return interrupted_booking_fields()
    elif kind is not None:
        return interrupted_booking_fields()
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
    """Call BookingFlowService once for a plan payload booking block.

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
            "client_message_kind": (
                BookingClientMessageKind.SERVICE_TEMPORARILY_UNAVAILABLE.value
            ),
        }
    if fixture is None:
        return {
            "booking_action": BookingDialogAction.SERVICE_UNAVAILABLE.value,
            "booking_reason": BookingInternalReasonCode.MALFORMED_ELIGIBILITY.value,
            "client_message_kind": (
                BookingClientMessageKind.SERVICE_TEMPORARILY_UNAVAILABLE.value
            ),
        }

    try:
        service = SelectedService(fixture.service_id)
        master = (
            SelectedMaster(fixture.master_id) if fixture.master_id is not None else None
        )
        query = fixture.availability_query
        if type(query) is SyntheticAvailableDaysQuery:
            decision = booking_flow.resolve_available_days(
                service,
                master,
                query.month,
                now=fixture.decision_at,
                include_alternatives=fixture.include_alternatives,
                alternate_master_consent=fixture.alternate_master_consent,
            )
        elif type(query) is SyntheticAvailableSlotsQuery:
            decision = booking_flow.resolve_available_slots(
                service,
                master,
                query.date,
                now=fixture.decision_at,
                include_alternatives=fixture.include_alternatives,
                alternate_master_consent=fixture.alternate_master_consent,
            )
        else:
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
            "client_message_kind": (
                BookingClientMessageKind.SERVICE_TEMPORARILY_UNAVAILABLE.value
            ),
        }

    return decision_to_outbound_fields(decision)


def base_synthetic_outbound_payload(plan_payload: dict[str, Any]) -> dict[str, Any]:
    """Token metadata outbound envelope (non-booking shape).

    ``synthetic_token`` remains technical metadata only — never user-facing body.
    """

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
    """Build synthetic.outbound.v1 payload with authoritative ``text`` when renderable.

    Prefer explicit ``booking_fields`` (durable result). Legacy ``booking_flow``
    path resolves synchronously and is reserved for unit helpers — workers must
    resolve off-transaction via ``resolve_booking_outbound_fields``.

    User-facing ``text`` is rendered from the booking domain path before INSERT.
    Machine-only ``OFFER_DAYS`` omits ``text`` (no invented copy). Non-booking
    plans raise ``OutboundReplyTextError`` rather than manufacturing a body or
    falling back to inbound/draft/token content.
    """

    payload = base_synthetic_outbound_payload(plan_payload)
    resolved_fields: dict[str, Any]
    if booking_fields is not None:
        resolved_fields = sanitize_booking_result_fields(booking_fields)
    elif booking_flow is not None:
        resolved_fields = resolve_booking_outbound_fields(
            plan_payload, booking_flow=booking_flow
        )
        if not resolved_fields:
            raise OutboundReplyTextError("OUTBOUND_REPLY_TEXT_MISSING")
        resolved_fields = sanitize_booking_result_fields(resolved_fields)
    else:
        raise OutboundReplyTextError("OUTBOUND_REPLY_TEXT_MISSING")

    payload.update(resolved_fields)
    try:
        payload["text"] = render_text_for_booking_fields(resolved_fields)
    except OutboundReplyTextError as exc:
        if (
            exc.code == "OUTBOUND_REPLY_TEXT_NOT_RENDERABLE"
            and resolved_fields.get("booking_action")
            == BookingDialogAction.OFFER_DAYS.value
        ):
            # Machine-only durable wire: keep booking fields, no client copy.
            return payload
        raise
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
