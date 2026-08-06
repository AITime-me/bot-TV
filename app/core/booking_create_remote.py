"""Remote DTOs for booking create S2S write (CURSOR-25).

Wire contract (online-zapis-tv):
- ``POST /api/internal/bot/v1/bookings``

Request JSON uses exact camelCase keys only. Caller owns idempotencyKey.
No automatic UUID generation. Repr never prints PII, slot, or booking IDs.

Route path constant lives here as the single source of truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal

from app.core.booking_availability_remote import (
    format_canonical_booking_starts_at,
    require_calendar_date,
    require_canonical_booking_starts_at,
)
from app.core.booking_eligibility_http import (
    BookingEligibilityHttpError,
    require_canonical_backend_uuid,
)
from app.core.booking_types import AvailableSlot

BOOKINGS_ROUTE_PATH: Final[str] = "/api/internal/bot/v1/bookings"

BOT_SLOT_ID_PREFIX: Final[str] = "bs1"
_MAX_SLOT_ID_LENGTH: Final[int] = 128
_MAX_CLIENT_NAME_LENGTH: Final[int] = 256
_MIN_CLIENT_NAME_LENGTH: Final[int] = 2
_MIN_PHONE_DIGITS: Final[int] = 10
_MAX_PHONE_DIGITS: Final[int] = 15

_CANONICAL_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_STRICT_HHMM_RE: Final[re.Pattern[str]] = re.compile(r"^([01]\d|2[0-3])([0-5]\d)$")
_PHONE_E164_RE: Final[re.Pattern[str]] = re.compile(r"^\+\d+$")

_REQUEST_JSON_KEYS: Final[frozenset[str]] = frozenset(
    {
        "idempotencyKey",
        "slotId",
        "clientName",
        "phone",
        "personalDataConsent",
        "offerAcknowledgement",
    }
)
_SUCCESS_JSON_KEYS: Final[frozenset[str]] = frozenset(
    {
        "ok",
        "bookingId",
        "slotId",
        "status",
        "startsAt",
        "idempotentReplay",
    }
)

BOOKING_CREATE_REMOTE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "VALIDATION_ERROR",
        "PAYLOAD_TOO_LARGE",
        "UNAUTHORIZED",
        "RATE_LIMITED",
        "IDEMPOTENCY_CONFLICT",
        "IDEMPOTENCY_IN_PROGRESS",
        "SLOT_INVALID",
        "SLOT_NO_LONGER_AVAILABLE",
        "SERVICE_UNAVAILABLE",
        "MASTER_UNAVAILABLE",
        "SERVICE_MASTER_MISMATCH",
        "CLIENT_AMBIGUOUS",
        "BOOKING_REQUEST_INVALID",
        "BOOKING_REQUEST_CONFLICT",
        "BOOKING_CONFLICT",
        "INTERNAL_ERROR",
    }
)

# Exact HTTP status ↔ remote code pairs from online-zapis-tv BotBookingCreateService.
REMOTE_ERROR_CODE_BY_STATUS: Final[dict[int, frozenset[str]]] = {
    400: frozenset(
        {
            "VALIDATION_ERROR",
            "SLOT_INVALID",
            "SERVICE_UNAVAILABLE",
            "MASTER_UNAVAILABLE",
            "SERVICE_MASTER_MISMATCH",
            "BOOKING_REQUEST_INVALID",
        }
    ),
    401: frozenset({"UNAUTHORIZED"}),
    409: frozenset(
        {
            "IDEMPOTENCY_CONFLICT",
            "IDEMPOTENCY_IN_PROGRESS",
            "SLOT_NO_LONGER_AVAILABLE",
            "CLIENT_AMBIGUOUS",
            "BOOKING_REQUEST_CONFLICT",
            "BOOKING_CONFLICT",
        }
    ),
    413: frozenset({"PAYLOAD_TOO_LARGE"}),
    429: frozenset({"RATE_LIMITED"}),
    500: frozenset({"INTERNAL_ERROR"}),
}


class BookingCreateMachineOutcome(StrEnum):
    """Closed application outcomes for confirmed-slot create (CURSOR-25)."""

    CONFIRMED = "CONFIRMED"
    SLOT_RESELECT_REQUIRED = "SLOT_RESELECT_REQUIRED"
    RETRY_LATER = "RETRY_LATER"
    MANAGER_HANDOFF = "MANAGER_HANDOFF"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    FAIL_CLOSED = "FAIL_CLOSED"


def _contains_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def require_canonical_idempotency_key(value: object) -> str:
    """Require caller-owned lowercase UUID idempotency key. Never echoes value."""

    if type(value) is not str or not value:
        raise ValueError("BOOKING_CREATE_IDEMPOTENCY_KEY_INVALID") from None
    if any(ch.isspace() for ch in value) or _contains_control_chars(value):
        raise ValueError("BOOKING_CREATE_IDEMPOTENCY_KEY_INVALID") from None
    if value != value.lower():
        raise ValueError("BOOKING_CREATE_IDEMPOTENCY_KEY_INVALID") from None
    if len(value) != 36 or _CANONICAL_UUID_RE.fullmatch(value) is None:
        raise ValueError("BOOKING_CREATE_IDEMPOTENCY_KEY_INVALID") from None
    try:
        return require_canonical_backend_uuid(value)
    except BookingEligibilityHttpError:
        raise ValueError("BOOKING_CREATE_IDEMPOTENCY_KEY_INVALID") from None


def require_canonical_booking_id(value: object) -> str:
    """Require backend bookingId as canonical lowercase UUID. Never echoes."""

    if type(value) is not str or not value:
        raise ValueError("BOOKING_CREATE_BOOKING_ID_INVALID") from None
    if value != value.lower():
        raise ValueError("BOOKING_CREATE_BOOKING_ID_INVALID") from None
    try:
        return require_canonical_backend_uuid(value)
    except BookingEligibilityHttpError:
        raise ValueError("BOOKING_CREATE_BOOKING_ID_INVALID") from None


@dataclass(frozen=True, slots=True, repr=False)
class BotSlotIdParts:
    """Parsed backend-issued bot slot id (unsigned reference only)."""

    service_id: str
    master_id: str
    date_key: str
    start_time: str  # HH:MM


def parse_bot_slot_id(raw: object) -> BotSlotIdParts:
    """Strict bot slot id parser aligned with online-zapis-tv parseBotSlotId."""

    if type(raw) is not str or not raw:
        raise ValueError("BOOKING_CREATE_SLOT_ID_INVALID") from None
    if len(raw) > _MAX_SLOT_ID_LENGTH:
        raise ValueError("BOOKING_CREATE_SLOT_ID_INVALID") from None
    if any(ch.isspace() for ch in raw) or _contains_control_chars(raw):
        raise ValueError("BOOKING_CREATE_SLOT_ID_INVALID") from None
    parts = raw.split(".")
    if len(parts) != 5:
        raise ValueError("BOOKING_CREATE_SLOT_ID_INVALID") from None
    version, service_id, master_id, date_key, hhmm = parts
    if version != BOT_SLOT_ID_PREFIX:
        raise ValueError("BOOKING_CREATE_SLOT_ID_INVALID") from None
    if _CANONICAL_UUID_RE.fullmatch(service_id) is None:
        raise ValueError("BOOKING_CREATE_SLOT_ID_INVALID") from None
    if _CANONICAL_UUID_RE.fullmatch(master_id) is None:
        raise ValueError("BOOKING_CREATE_SLOT_ID_INVALID") from None
    try:
        canonical_service = require_canonical_backend_uuid(service_id)
        canonical_master = require_canonical_backend_uuid(master_id)
        canonical_date = require_calendar_date(date_key)
    except (BookingEligibilityHttpError, ValueError):
        raise ValueError("BOOKING_CREATE_SLOT_ID_INVALID") from None
    if _STRICT_HHMM_RE.fullmatch(hhmm) is None:
        raise ValueError("BOOKING_CREATE_SLOT_ID_INVALID") from None
    start_time = f"{hhmm[0:2]}:{hhmm[2:4]}"
    return BotSlotIdParts(
        service_id=canonical_service,
        master_id=canonical_master,
        date_key=canonical_date,
        start_time=start_time,
    )


def expected_canonical_starts_at_from_slot_parts(parts: BotSlotIdParts) -> str:
    """Derive studio ``YYYY-MM-DDTHH:MM:00+05:00`` from parsed bot slot id parts.

    Never logs or echoes IDs. Output always passes ``require_canonical_booking_starts_at``.
    """

    if type(parts) is not BotSlotIdParts:
        raise ValueError("BOOKING_CREATE_SLOT_ID_INVALID") from None
    candidate = f"{parts.date_key}T{parts.start_time}:00+05:00"
    return require_canonical_booking_starts_at(candidate)


def require_confirmed_client_name(value: object) -> str:
    """Normalize and validate confirmed client name. Never echoes value."""

    if type(value) is not str:
        raise ValueError("BOOKING_CREATE_CLIENT_NAME_INVALID") from None
    if _contains_control_chars(value):
        raise ValueError("BOOKING_CREATE_CLIENT_NAME_INVALID") from None
    normalized = " ".join(value.split())
    if (
        len(normalized) < _MIN_CLIENT_NAME_LENGTH
        or len(normalized) > _MAX_CLIENT_NAME_LENGTH
    ):
        raise ValueError("BOOKING_CREATE_CLIENT_NAME_INVALID") from None
    return normalized


def _phone_digit_count(phone: str) -> int:
    return sum(1 for ch in phone if ch.isdigit())


def require_confirmed_phone(value: object) -> str:
    """Validate confirmed E.164-like phone (+digits). Never echoes value."""

    if type(value) is not str:
        raise ValueError("BOOKING_CREATE_PHONE_INVALID") from None
    if _contains_control_chars(value):
        raise ValueError("BOOKING_CREATE_PHONE_INVALID") from None
    phone = value.strip()
    if not phone or any(ch.isspace() for ch in phone):
        raise ValueError("BOOKING_CREATE_PHONE_INVALID") from None
    digits = _phone_digit_count(phone)
    if digits < _MIN_PHONE_DIGITS or digits > _MAX_PHONE_DIGITS:
        raise ValueError("BOOKING_CREATE_PHONE_INVALID") from None
    if _PHONE_E164_RE.fullmatch(phone) is None:
        raise ValueError("BOOKING_CREATE_PHONE_INVALID") from None
    return phone


@dataclass(frozen=True, slots=True, repr=False)
class BookingCreateRemoteRequest:
    """Bounded JSON body for booking create. Fields are pre-validated."""

    idempotency_key: str
    slot_id: str
    client_name: str
    phone: str
    personal_data_consent: Literal[True]
    offer_acknowledgement: Literal[True]

    def to_json_object(self) -> dict[str, object]:
        return {
            "idempotencyKey": self.idempotency_key,
            "slotId": self.slot_id,
            "clientName": self.client_name,
            "phone": self.phone,
            "personalDataConsent": True,
            "offerAcknowledgement": True,
        }

    def __repr__(self) -> str:
        return (
            "BookingCreateRemoteRequest("
            "idempotency_key=<redacted>, "
            "slot_id=<redacted>, "
            "client_name=<redacted>, "
            "phone=<redacted>, "
            "personal_data_consent=True, "
            "offer_acknowledgement=True)"
        )


def build_booking_create_remote_request(
    *,
    idempotency_key: object,
    slot_id: object,
    client_name: object,
    phone: object,
    personal_data_consent: object,
    offer_acknowledgement: object,
) -> BookingCreateRemoteRequest:
    """Validate and build a wire request. Fail closed before any HTTP I/O."""

    if personal_data_consent is not True:
        raise ValueError("BOOKING_CREATE_CONSENT_INVALID") from None
    if offer_acknowledgement is not True:
        raise ValueError("BOOKING_CREATE_CONSENT_INVALID") from None
    key = require_canonical_idempotency_key(idempotency_key)
    if type(slot_id) is not str:
        raise ValueError("BOOKING_CREATE_SLOT_ID_INVALID") from None
    parse_bot_slot_id(slot_id)
    name = require_confirmed_client_name(client_name)
    phone_value = require_confirmed_phone(phone)
    return BookingCreateRemoteRequest(
        idempotency_key=key,
        slot_id=slot_id,
        client_name=name,
        phone=phone_value,
        personal_data_consent=True,
        offer_acknowledgement=True,
    )


@dataclass(frozen=True, slots=True, repr=False)
class BookingCreateRemoteSuccess:
    """Fail-closed success DTO for booking create."""

    booking_id: str
    slot_id: str
    starts_at: str
    idempotent_replay: bool

    def __repr__(self) -> str:
        return (
            "BookingCreateRemoteSuccess("
            "booking_id=<redacted>, "
            "slot_id=<redacted>, "
            "starts_at=<redacted>, "
            f"idempotent_replay={self.idempotent_replay!r})"
        )


def parse_booking_create_success_payload(
    raw: object,
    *,
    request: BookingCreateRemoteRequest,
) -> BookingCreateRemoteSuccess | None:
    """Strict success JSON parser. Returns None on any contract violation.

    ``startsAt`` must semantically match the date/time encoded in ``slotId``
    (studio ``+05:00``, zero seconds). Mismatched minute/hour/date/offset fails.
    """

    if type(raw) is not dict:
        return None
    if set(raw) != _SUCCESS_JSON_KEYS:
        return None
    if raw.get("ok") is not True:
        return None
    if raw.get("status") != "SCHEDULED":
        return None
    idempotent_replay = raw.get("idempotentReplay")
    if type(idempotent_replay) is not bool:
        return None
    try:
        booking_id = require_canonical_booking_id(raw.get("bookingId"))
        starts_at = require_canonical_booking_starts_at(raw.get("startsAt"))
        slot_id = raw.get("slotId")
        if type(slot_id) is not str:
            return None
        slot_parts = parse_bot_slot_id(slot_id)
        expected_starts = expected_canonical_starts_at_from_slot_parts(slot_parts)
    except ValueError:
        return None
    if slot_id != request.slot_id:
        return None
    if starts_at != expected_starts:
        return None
    return BookingCreateRemoteSuccess(
        booking_id=booking_id,
        slot_id=slot_id,
        starts_at=starts_at,
        idempotent_replay=idempotent_replay,
    )


def assert_success_matches_available_slot(
    *,
    success: BookingCreateRemoteSuccess,
    slot: AvailableSlot,
) -> None:
    """Application-level binding: success must match the selected AvailableSlot."""

    if type(slot) is not AvailableSlot:
        raise ValueError("BOOKING_CREATE_SLOT_MISMATCH") from None
    if success.slot_id != slot.slot_id:
        raise ValueError("BOOKING_CREATE_SLOT_MISMATCH") from None
    try:
        expected_starts = format_canonical_booking_starts_at(slot.starts_at)
    except ValueError:
        raise ValueError("BOOKING_CREATE_SLOT_MISMATCH") from None
    if success.starts_at != expected_starts:
        raise ValueError("BOOKING_CREATE_SLOT_MISMATCH") from None
    parsed = parse_bot_slot_id(slot.slot_id)
    if parsed.service_id != slot.service_id or parsed.master_id != slot.master_id:
        raise ValueError("BOOKING_CREATE_SLOT_MISMATCH") from None


@dataclass(frozen=True, slots=True, repr=False)
class BookingCreateConfirmedResult:
    """Confirmed booking after validated Booking Service success."""

    outcome: Literal[BookingCreateMachineOutcome.CONFIRMED]
    booking_id: str
    slot_id: str
    starts_at: datetime
    idempotent_replay: bool
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.outcome is not BookingCreateMachineOutcome.CONFIRMED:
            raise TypeError("CONFIRMED outcome required") from None
        if type(self.booking_id) is not str or not self.booking_id:
            raise TypeError("booking_id required") from None
        if type(self.idempotent_replay) is not bool:
            raise TypeError("idempotent_replay must be bool") from None

    def __repr__(self) -> str:
        return (
            "BookingCreateConfirmedResult("
            f"outcome={self.outcome!r}, "
            "booking_id=<redacted>, "
            "slot_id=<redacted>, "
            "starts_at=<redacted>, "
            f"idempotent_replay={self.idempotent_replay!r}, "
            "idempotency_key=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BookingCreateRejectedResult:
    """Non-confirmed machine outcome. Never claims a booking exists."""

    outcome: BookingCreateMachineOutcome
    internal_reason_code: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.outcome is BookingCreateMachineOutcome.CONFIRMED:
            raise TypeError("CONFIRMED must use BookingCreateConfirmedResult") from None
        if type(self.internal_reason_code) is not str or not self.internal_reason_code:
            raise TypeError("internal_reason_code required") from None
        if type(self.idempotency_key) is not str or not self.idempotency_key:
            raise TypeError("idempotency_key required") from None

    def __repr__(self) -> str:
        return (
            "BookingCreateRejectedResult("
            f"outcome={self.outcome!r}, "
            f"internal_reason_code={self.internal_reason_code!r}, "
            "idempotency_key=<redacted>)"
        )


BookingCreateApplicationResult = BookingCreateConfirmedResult | BookingCreateRejectedResult
