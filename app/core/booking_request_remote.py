"""Remote DTOs for BookingRequest S2S (TEYA_REQUEST_ORCHESTRATOR Phase 1).

Wire contract (online-zapis-tv):
- POST /api/internal/bot/v1/booking-requests/feed
- POST /api/internal/bot/v1/booking-requests/get
- POST /api/internal/bot/v1/booking-requests/availability
- POST /api/internal/bot/v1/booking-requests/appointments-lookup
- POST /api/internal/bot/v1/booking-requests/book

Fail-closed parse. Repr never prints phone/name/PII.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Mapping

from app.core.booking_eligibility_http import (
    BookingEligibilityHttpError,
    require_canonical_backend_uuid,
)

BOOKING_REQUESTS_FEED_PATH: Final[str] = (
    "/api/internal/bot/v1/booking-requests/feed"
)
BOOKING_REQUESTS_GET_PATH: Final[str] = (
    "/api/internal/bot/v1/booking-requests/get"
)
BOOKING_REQUESTS_AVAILABILITY_PATH: Final[str] = (
    "/api/internal/bot/v1/booking-requests/availability"
)
BOOKING_REQUESTS_APPOINTMENTS_LOOKUP_PATH: Final[str] = (
    "/api/internal/bot/v1/booking-requests/appointments-lookup"
)
BOOKING_REQUESTS_BOOK_PATH: Final[str] = (
    "/api/internal/bot/v1/booking-requests/book"
)

_CANONICAL_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

BOOKING_REQUEST_REMOTE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "VALIDATION_ERROR",
        "PAYLOAD_TOO_LARGE",
        "UNAUTHORIZED",
        "RATE_LIMITED",
        "NOT_FOUND",
        "BOOKING_REQUEST_INVALID",
        "BOOKING_REQUEST_CONFLICT",
        "CONSULTATION_SERVICE_REQUIRED",
        "SLOT_NO_LONGER_AVAILABLE",
        "SERVICE_UNAVAILABLE",
        "MASTER_UNAVAILABLE",
        "SERVICE_MASTER_MISMATCH",
        "IDEMPOTENCY_CONFLICT",
        "IDEMPOTENCY_IN_PROGRESS",
        "RECONCILIATION_REQUIRED",
        "BOOKING_CONFLICT",
        "INTERNAL_ERROR",
    }
)

REMOTE_ERROR_CODE_BY_STATUS: Final[dict[int, frozenset[str]]] = {
    400: frozenset(
        {
            "VALIDATION_ERROR",
            "BOOKING_REQUEST_INVALID",
            "CONSULTATION_SERVICE_REQUIRED",
            "SERVICE_UNAVAILABLE",
            "MASTER_UNAVAILABLE",
            "SERVICE_MASTER_MISMATCH",
        }
    ),
    401: frozenset({"UNAUTHORIZED"}),
    404: frozenset({"NOT_FOUND"}),
    409: frozenset(
        {
            "BOOKING_REQUEST_CONFLICT",
            "SLOT_NO_LONGER_AVAILABLE",
            "IDEMPOTENCY_CONFLICT",
            "IDEMPOTENCY_IN_PROGRESS",
            "RECONCILIATION_REQUIRED",
            "BOOKING_CONFLICT",
        }
    ),
    413: frozenset({"PAYLOAD_TOO_LARGE"}),
    429: frozenset({"RATE_LIMITED"}),
    500: frozenset({"INTERNAL_ERROR"}),
}


class AppointmentsLookupOutcome(StrEnum):
    NONE = "NONE"
    UNIQUE = "UNIQUE"
    AMBIGUOUS = "AMBIGUOUS"


def _contains_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def require_canonical_request_id(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("BOOKING_REQUEST_ID_INVALID") from None
    if any(ch.isspace() for ch in value) or _contains_control_chars(value):
        raise ValueError("BOOKING_REQUEST_ID_INVALID") from None
    if value != value.lower():
        raise ValueError("BOOKING_REQUEST_ID_INVALID") from None
    if len(value) != 36 or _CANONICAL_UUID_RE.fullmatch(value) is None:
        raise ValueError("BOOKING_REQUEST_ID_INVALID") from None
    try:
        return require_canonical_backend_uuid(value)
    except BookingEligibilityHttpError:
        raise ValueError("BOOKING_REQUEST_ID_INVALID") from None


def require_optional_canonical_uuid(value: object, *, code: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value:
        raise ValueError(code) from None
    if value != value.lower() or _CANONICAL_UUID_RE.fullmatch(value) is None:
        raise ValueError(code) from None
    try:
        return require_canonical_backend_uuid(value)
    except BookingEligibilityHttpError:
        raise ValueError(code) from None


@dataclass(frozen=True, slots=True, repr=False)
class BotBookingRequestGameContext:
    gift: str | None = None
    procedure: str | None = None
    game_title: str | None = None

    def __repr__(self) -> str:
        return (
            "BotBookingRequestGameContext("
            f"gift={'set' if self.gift else None}, "
            f"procedure={'set' if self.procedure else None}, "
            f"game_title={'set' if self.game_title else None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BotBookingRequestDto:
    """Opaque BookingRequest snapshot from online-zapis. Phone never in repr."""

    request_id: str
    status: str
    request_type: str
    service_id: str | None = None
    master_id: str | None = None
    client_name: str | None = None
    phone_e164: str | None = None
    game_context: BotBookingRequestGameContext | None = None
    appointment_id: str | None = None

    def __repr__(self) -> str:
        return (
            "BotBookingRequestDto("
            "request_id=<redacted>, "
            f"status={self.status!r}, "
            f"request_type={self.request_type!r}, "
            f"service_id={'set' if self.service_id else None}, "
            f"master_id={'set' if self.master_id else None}, "
            "client_name=<redacted>, "
            "phone_e164=<redacted>, "
            f"game_context={self.game_context!r}, "
            f"appointment_id={'set' if self.appointment_id else None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BookingRequestFeedCursor:
    """Stable poll cursor matching online-zapis ``{createdAt, id}``."""

    created_at: str
    id: str

    def __repr__(self) -> str:
        return (
            "BookingRequestFeedCursor("
            f"created_at={'set' if self.created_at else None}, "
            "id=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BookingRequestFeedPage:
    items: tuple[BotBookingRequestDto, ...]
    next_cursor: BookingRequestFeedCursor | None = None

    def __repr__(self) -> str:
        return (
            "BookingRequestFeedPage("
            f"items_count={len(self.items)}, "
            f"next_cursor={'set' if self.next_cursor else None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BookingRequestAppointmentsLookupResult:
    outcome: AppointmentsLookupOutcome
    appointment_id: str | None = None
    appointment_ids: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "BookingRequestAppointmentsLookupResult("
            f"outcome={self.outcome.value!r}, "
            f"appointment_id={'set' if self.appointment_id else None}, "
            f"appointment_ids_count={len(self.appointment_ids)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BookingRequestBookSuccess:
    appointment_id: str
    request_id: str
    starts_at: str
    idempotent_replay: bool = False

    def __repr__(self) -> str:
        return (
            "BookingRequestBookSuccess("
            "appointment_id=<redacted>, "
            "request_id=<redacted>, "
            "starts_at=<redacted>, "
            f"idempotent_replay={self.idempotent_replay!r})"
        )


def _parse_feed_cursor(raw: object) -> BookingRequestFeedCursor:
    if type(raw) is not dict:
        raise ValueError("BOOKING_REQUEST_FEED_CURSOR_INVALID") from None
    created_at = raw.get("createdAt")
    cursor_id = raw.get("id")
    if type(created_at) is not str or not created_at or len(created_at) > 64:
        raise ValueError("BOOKING_REQUEST_FEED_CURSOR_INVALID") from None
    require_canonical_request_id(cursor_id)
    return BookingRequestFeedCursor(created_at=created_at, id=str(cursor_id))


def parse_bot_booking_request_dto(payload: object) -> BotBookingRequestDto:
    """Parse online-zapis BotBookingRequestDto (``id`` / ``type`` / ``clientPhone``)."""

    if type(payload) is not dict:
        raise ValueError("BOOKING_REQUEST_DTO_INVALID") from None
    # online-zapis uses ``id``; accept legacy ``requestId`` for test fixtures.
    request_id = require_canonical_request_id(
        payload.get("id") if payload.get("id") is not None else payload.get("requestId")
    )
    status = payload.get("status")
    request_type = payload.get("type")
    if request_type is None:
        request_type = payload.get("requestType")
    if type(status) is not str or not status or len(status) > 64:
        raise ValueError("BOOKING_REQUEST_DTO_INVALID") from None
    if type(request_type) is not str or not request_type or len(request_type) > 64:
        raise ValueError("BOOKING_REQUEST_DTO_INVALID") from None
    service_id = require_optional_canonical_uuid(
        payload.get("serviceId"), code="BOOKING_REQUEST_DTO_INVALID"
    )
    master_id = require_optional_canonical_uuid(
        payload.get("masterId"), code="BOOKING_REQUEST_DTO_INVALID"
    )
    appointment_id = require_optional_canonical_uuid(
        payload.get("appointmentId"), code="BOOKING_REQUEST_DTO_INVALID"
    )
    client_name = payload.get("clientName")
    if client_name is not None and (
        type(client_name) is not str or len(client_name) > 256
    ):
        raise ValueError("BOOKING_REQUEST_DTO_INVALID") from None
    phone = payload.get("clientPhone")
    if phone is None:
        phone = payload.get("phone")
    if phone is not None and (type(phone) is not str or len(phone) > 32):
        raise ValueError("BOOKING_REQUEST_DTO_INVALID") from None
    game_raw = payload.get("gameContext")
    game: BotBookingRequestGameContext | None = None
    if game_raw is not None:
        if type(game_raw) is not dict:
            raise ValueError("BOOKING_REQUEST_DTO_INVALID") from None
        gift = game_raw.get("giftName")
        if gift is None:
            gift = game_raw.get("gift")
        procedure = game_raw.get("procedure")
        title = game_raw.get("gameTitle") or game_raw.get("title")
        for field in (gift, procedure, title):
            if field is not None and (type(field) is not str or len(field) > 256):
                raise ValueError("BOOKING_REQUEST_DTO_INVALID") from None
        game = BotBookingRequestGameContext(
            gift=gift if type(gift) is str else None,
            procedure=procedure if type(procedure) is str else None,
            game_title=title if type(title) is str else None,
        )
    return BotBookingRequestDto(
        request_id=request_id,
        status=status,
        request_type=request_type,
        service_id=service_id,
        master_id=master_id,
        client_name=client_name if type(client_name) is str else None,
        phone_e164=phone if type(phone) is str else None,
        game_context=game,
        appointment_id=appointment_id,
    )


def parse_booking_request_feed_payload(payload: object) -> BookingRequestFeedPage:
    if type(payload) is not dict:
        raise ValueError("BOOKING_REQUEST_FEED_INVALID") from None
    if payload.get("ok") is not True:
        raise ValueError("BOOKING_REQUEST_FEED_INVALID") from None
    items_raw = payload.get("items")
    if type(items_raw) is not list:
        raise ValueError("BOOKING_REQUEST_FEED_INVALID") from None
    items = tuple(parse_bot_booking_request_dto(item) for item in items_raw)
    next_cursor_raw = payload.get("nextCursor")
    next_cursor: BookingRequestFeedCursor | None = None
    if next_cursor_raw is not None:
        next_cursor = _parse_feed_cursor(next_cursor_raw)
    return BookingRequestFeedPage(items=items, next_cursor=next_cursor)


def parse_appointments_lookup_payload(
    payload: object,
) -> BookingRequestAppointmentsLookupResult:
    """Map online-zapis ``clientOutcome`` + ``appointments[]`` to orchestrator outcome.

    UNIQUE client + 0 upcoming appointments → NONE (no self-booking).
    UNIQUE client + 1 appointment → UNIQUE.
    UNIQUE client + >1 appointments → AMBIGUOUS.
    """

    if type(payload) is not dict:
        raise ValueError("BOOKING_REQUEST_LOOKUP_INVALID") from None
    if payload.get("ok") is not True:
        raise ValueError("BOOKING_REQUEST_LOOKUP_INVALID") from None
    client_outcome = payload.get("clientOutcome")
    if type(client_outcome) is not str:
        # Legacy flat ``outcome`` (unit fixtures).
        client_outcome = payload.get("outcome")
    if type(client_outcome) is not str:
        raise ValueError("BOOKING_REQUEST_LOOKUP_INVALID") from None

    appointments_raw = payload.get("appointments")
    ids: list[str] = []
    if appointments_raw is not None:
        if type(appointments_raw) is not list:
            raise ValueError("BOOKING_REQUEST_LOOKUP_INVALID") from None
        for item in appointments_raw:
            if type(item) is dict:
                aid = require_optional_canonical_uuid(
                    item.get("id"), code="BOOKING_REQUEST_LOOKUP_INVALID"
                )
            else:
                aid = require_optional_canonical_uuid(
                    item, code="BOOKING_REQUEST_LOOKUP_INVALID"
                )
            if aid is None:
                raise ValueError("BOOKING_REQUEST_LOOKUP_INVALID") from None
            ids.append(aid)
    elif payload.get("appointmentIds") is not None:
        ids_raw = payload.get("appointmentIds")
        if type(ids_raw) is not list:
            raise ValueError("BOOKING_REQUEST_LOOKUP_INVALID") from None
        for item in ids_raw:
            aid = require_optional_canonical_uuid(
                item, code="BOOKING_REQUEST_LOOKUP_INVALID"
            )
            if aid is None:
                raise ValueError("BOOKING_REQUEST_LOOKUP_INVALID") from None
            ids.append(aid)

    legacy_appointment_id = require_optional_canonical_uuid(
        payload.get("appointmentId"), code="BOOKING_REQUEST_LOOKUP_INVALID"
    )
    if legacy_appointment_id is not None and legacy_appointment_id not in ids:
        ids.insert(0, legacy_appointment_id)

    if client_outcome in {"AMBIGUOUS", AppointmentsLookupOutcome.AMBIGUOUS.value}:
        return BookingRequestAppointmentsLookupResult(
            outcome=AppointmentsLookupOutcome.AMBIGUOUS,
            appointment_id=None,
            appointment_ids=tuple(ids),
        )
    if client_outcome in {"NONE", AppointmentsLookupOutcome.NONE.value}:
        return BookingRequestAppointmentsLookupResult(
            outcome=AppointmentsLookupOutcome.NONE,
            appointment_id=None,
            appointment_ids=(),
        )
    if client_outcome in {"UNIQUE", AppointmentsLookupOutcome.UNIQUE.value}:
        if len(ids) == 0:
            return BookingRequestAppointmentsLookupResult(
                outcome=AppointmentsLookupOutcome.NONE,
                appointment_id=None,
                appointment_ids=(),
            )
        if len(ids) == 1:
            return BookingRequestAppointmentsLookupResult(
                outcome=AppointmentsLookupOutcome.UNIQUE,
                appointment_id=ids[0],
                appointment_ids=(ids[0],),
            )
        return BookingRequestAppointmentsLookupResult(
            outcome=AppointmentsLookupOutcome.AMBIGUOUS,
            appointment_id=None,
            appointment_ids=tuple(ids),
        )
    raise ValueError("BOOKING_REQUEST_LOOKUP_INVALID") from None


def parse_book_success_payload(payload: object) -> BookingRequestBookSuccess:
    if type(payload) is not dict:
        raise ValueError("BOOKING_REQUEST_BOOK_INVALID") from None
    if payload.get("ok") is not True:
        raise ValueError("BOOKING_REQUEST_BOOK_INVALID") from None
    appointment_id = require_optional_canonical_uuid(
        payload.get("appointmentId"), code="BOOKING_REQUEST_BOOK_INVALID"
    )
    request_id = require_canonical_request_id(payload.get("requestId"))
    starts_at = payload.get("startsAt")
    if appointment_id is None or type(starts_at) is not str or not starts_at:
        raise ValueError("BOOKING_REQUEST_BOOK_INVALID") from None
    replay = payload.get("idempotentReplay", False)
    if type(replay) is not bool:
        raise ValueError("BOOKING_REQUEST_BOOK_INVALID") from None
    return BookingRequestBookSuccess(
        appointment_id=appointment_id,
        request_id=request_id,
        starts_at=starts_at,
        idempotent_replay=replay,
    )


def build_feed_request_body(
    *,
    limit: int,
    cursor: BookingRequestFeedCursor | None = None,
) -> dict[str, object]:
    if type(limit) is not int or isinstance(limit, bool) or limit < 1 or limit > 50:
        raise ValueError("BOOKING_REQUEST_FEED_LIMIT_INVALID") from None
    body: dict[str, object] = {"limit": limit}
    if cursor is not None:
        if type(cursor) is not BookingRequestFeedCursor:
            raise ValueError("BOOKING_REQUEST_FEED_CURSOR_INVALID") from None
        require_canonical_request_id(cursor.id)
        if type(cursor.created_at) is not str or not cursor.created_at:
            raise ValueError("BOOKING_REQUEST_FEED_CURSOR_INVALID") from None
        body["cursor"] = {"createdAt": cursor.created_at, "id": cursor.id}
    return body


def build_get_request_body(*, request_id: object) -> dict[str, object]:
    """online-zapis get body uses ``id`` (not requestId)."""

    return {"id": require_canonical_request_id(request_id)}


def build_appointments_lookup_body(
    *,
    phone: object = None,
    client_id: object = None,
) -> dict[str, object]:
    has_phone = phone is not None
    has_client = client_id is not None
    if has_phone == has_client:
        raise ValueError("BOOKING_REQUEST_LOOKUP_INPUT_INVALID") from None
    if has_phone:
        if type(phone) is not str or not phone or len(phone) > 32:
            raise ValueError("BOOKING_REQUEST_LOOKUP_INPUT_INVALID") from None
        # online-zapis normalizePhone accepts formatted phones; reject controls only.
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in phone):
            raise ValueError("BOOKING_REQUEST_LOOKUP_INPUT_INVALID") from None
        return {"phone": phone}
    cid = require_optional_canonical_uuid(
        client_id, code="BOOKING_REQUEST_LOOKUP_INPUT_INVALID"
    )
    if cid is None:
        raise ValueError("BOOKING_REQUEST_LOOKUP_INPUT_INVALID") from None
    return {"clientId": cid}


def build_book_request_body(
    *,
    request_id: object,
    starts_at: object,
    idempotency_key: object,
    service_id: object = None,
) -> dict[str, object]:
    rid = require_canonical_request_id(request_id)
    if type(starts_at) is not str or not starts_at or len(starts_at) > 64:
        raise ValueError("BOOKING_REQUEST_STARTS_AT_INVALID") from None
    key = require_canonical_request_id(idempotency_key)
    body: dict[str, object] = {
        "requestId": rid,
        "startsAt": starts_at,
        "idempotencyKey": key,
    }
    sid = require_optional_canonical_uuid(
        service_id, code="BOOKING_REQUEST_SERVICE_ID_INVALID"
    )
    if sid is not None:
        body["serviceId"] = sid
    return body


def require_error_code_from_envelope(
    status_code: int, payload: Mapping[str, object]
) -> str:
    allowed = REMOTE_ERROR_CODE_BY_STATUS.get(status_code)
    if allowed is None:
        raise ValueError("BOOKING_REQUEST_REMOTE_REJECTED") from None
    if payload.get("ok") is not False:
        raise ValueError("BOOKING_REQUEST_REMOTE_REJECTED") from None
    code = payload.get("code")
    if type(code) is not str or code not in allowed:
        raise ValueError("BOOKING_REQUEST_REMOTE_REJECTED") from None
    if code not in BOOKING_REQUEST_REMOTE_ERROR_CODES:
        raise ValueError("BOOKING_REQUEST_REMOTE_REJECTED") from None
    return code
