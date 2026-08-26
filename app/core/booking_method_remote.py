"""Remote DTOs for A2.2 booking-method feed + context S2S.

Wire contract (online-zapis-tv):
- POST /api/internal/bot/v1/booking-method/feed
- POST /api/internal/bot/v1/booking-method/context

Fail-closed parse. Repr never prints phone/PII.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from app.core.booking_eligibility_http import (
    BookingEligibilityHttpError,
    require_canonical_backend_uuid,
)
from app.core.booking_method_types import BookingMethodCreatorKind

BOOKING_METHOD_FEED_PATH: Final[str] = (
    "/api/internal/bot/v1/booking-method/feed"
)
BOOKING_METHOD_CONTEXT_PATH: Final[str] = (
    "/api/internal/bot/v1/booking-method/context"
)

_CANONICAL_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_PHONE_E164_RE: Final[re.Pattern[str]] = re.compile(r"^\+\d{8,15}$")

BOOKING_METHOD_REMOTE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "VALIDATION_ERROR",
        "PAYLOAD_TOO_LARGE",
        "UNAUTHORIZED",
        "RATE_LIMITED",
        "NOT_FOUND",
        "INTERNAL_ERROR",
        "UNAVAILABLE",
    }
)

REMOTE_ERROR_CODE_BY_STATUS: Final[dict[int, frozenset[str]]] = {
    400: frozenset({"VALIDATION_ERROR"}),
    401: frozenset({"UNAUTHORIZED"}),
    403: frozenset({"UNAUTHORIZED"}),
    404: frozenset({"NOT_FOUND", "UNAVAILABLE"}),
    413: frozenset({"PAYLOAD_TOO_LARGE"}),
    429: frozenset({"RATE_LIMITED"}),
    500: frozenset({"INTERNAL_ERROR"}),
    502: frozenset({"INTERNAL_ERROR"}),
    503: frozenset({"INTERNAL_ERROR"}),
    504: frozenset({"INTERNAL_ERROR"}),
}

_FEED_CREATOR_KINDS: Final[frozenset[str]] = frozenset(
    k.value for k in BookingMethodCreatorKind
)


def require_canonical_appointment_id(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("BOOKING_METHOD_APPOINTMENT_ID_INVALID") from None
    if any(ch.isspace() for ch in value):
        raise ValueError("BOOKING_METHOD_APPOINTMENT_ID_INVALID") from None
    if value != value.lower():
        raise ValueError("BOOKING_METHOD_APPOINTMENT_ID_INVALID") from None
    if len(value) != 36 or _CANONICAL_UUID_RE.fullmatch(value) is None:
        raise ValueError("BOOKING_METHOD_APPOINTMENT_ID_INVALID") from None
    try:
        return require_canonical_backend_uuid(value)
    except BookingEligibilityHttpError as exc:
        raise ValueError("BOOKING_METHOD_APPOINTMENT_ID_INVALID") from exc


def require_feed_creator_kind(value: object) -> BookingMethodCreatorKind:
    if type(value) is not str or value not in _FEED_CREATOR_KINDS:
        raise ValueError("BOOKING_METHOD_CREATOR_KIND_INVALID") from None
    return BookingMethodCreatorKind(value)


def require_phone_e164(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("BOOKING_METHOD_PHONE_INVALID") from None
    if len(value) > 32 or _PHONE_E164_RE.fullmatch(value) is None:
        raise ValueError("BOOKING_METHOD_PHONE_INVALID") from None
    return value


@dataclass(frozen=True, slots=True, repr=False)
class BookingMethodFeedCursor:
    """Stable poll cursor matching online-zapis ``{createdAt, id}``."""

    created_at: str
    id: str

    def __repr__(self) -> str:
        return (
            "BookingMethodFeedCursor("
            f"created_at={'set' if self.created_at else None}, "
            "id=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BookingMethodFeedItem:
    appointment_id: str
    creator_kind: BookingMethodCreatorKind
    created_at: str

    def __repr__(self) -> str:
        return (
            "BookingMethodFeedItem("
            "appointment_id=<redacted>, "
            f"creator_kind={self.creator_kind.value!r}, "
            f"created_at={'set' if self.created_at else None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BookingMethodFeedPage:
    items: tuple[BookingMethodFeedItem, ...]
    next_cursor: BookingMethodFeedCursor | None = None

    def __repr__(self) -> str:
        return (
            "BookingMethodFeedPage("
            f"items_count={len(self.items)}, "
            f"next_cursor={'set' if self.next_cursor else None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BookingMethodContextDto:
    appointment_id: str
    creator_kind: BookingMethodCreatorKind
    phone_e164: str

    def __repr__(self) -> str:
        return (
            "BookingMethodContextDto("
            "appointment_id=<redacted>, "
            f"creator_kind={self.creator_kind.value!r}, "
            "phone_e164=<redacted>)"
        )


def _parse_feed_cursor(raw: object) -> BookingMethodFeedCursor:
    if type(raw) is not dict:
        raise ValueError("BOOKING_METHOD_FEED_CURSOR_INVALID") from None
    created_at = raw.get("createdAt")
    cursor_id = raw.get("id")
    if type(created_at) is not str or not created_at or len(created_at) > 64:
        raise ValueError("BOOKING_METHOD_FEED_CURSOR_INVALID") from None
    require_canonical_appointment_id(cursor_id)
    return BookingMethodFeedCursor(created_at=created_at, id=str(cursor_id))


def parse_booking_method_feed_item(payload: object) -> BookingMethodFeedItem:
    if type(payload) is not dict:
        raise ValueError("BOOKING_METHOD_FEED_ITEM_INVALID") from None
    appointment_id = require_canonical_appointment_id(payload.get("appointmentId"))
    creator_kind = require_feed_creator_kind(payload.get("creatorKind"))
    created_at = payload.get("createdAt")
    if type(created_at) is not str or not created_at or len(created_at) > 64:
        raise ValueError("BOOKING_METHOD_FEED_ITEM_INVALID") from None
    return BookingMethodFeedItem(
        appointment_id=appointment_id,
        creator_kind=creator_kind,
        created_at=created_at,
    )


def parse_booking_method_feed_payload(payload: object) -> BookingMethodFeedPage:
    if type(payload) is not dict:
        raise ValueError("BOOKING_METHOD_FEED_INVALID") from None
    if payload.get("ok") is not True:
        raise ValueError("BOOKING_METHOD_FEED_INVALID") from None
    items_raw = payload.get("items")
    if type(items_raw) is not list:
        raise ValueError("BOOKING_METHOD_FEED_INVALID") from None
    items = tuple(parse_booking_method_feed_item(item) for item in items_raw)
    next_cursor_raw = payload.get("nextCursor")
    next_cursor: BookingMethodFeedCursor | None = None
    if next_cursor_raw is not None:
        next_cursor = _parse_feed_cursor(next_cursor_raw)
    return BookingMethodFeedPage(items=items, next_cursor=next_cursor)


def parse_booking_method_context_payload(
    payload: object,
) -> BookingMethodContextDto:
    if type(payload) is not dict:
        raise ValueError("BOOKING_METHOD_CONTEXT_INVALID") from None
    if payload.get("ok") is not True:
        raise ValueError("BOOKING_METHOD_CONTEXT_INVALID") from None
    appointment_id = require_canonical_appointment_id(payload.get("appointmentId"))
    creator_kind = require_feed_creator_kind(payload.get("creatorKind"))
    phone_e164 = require_phone_e164(payload.get("phoneE164"))
    return BookingMethodContextDto(
        appointment_id=appointment_id,
        creator_kind=creator_kind,
        phone_e164=phone_e164,
    )


def build_feed_request_body(
    *,
    limit: int = 20,
    cursor: BookingMethodFeedCursor | None = None,
) -> dict[str, object]:
    if type(limit) is not int or isinstance(limit, bool) or limit < 1 or limit > 50:
        raise ValueError("BOOKING_METHOD_FEED_LIMIT_INVALID") from None
    body: dict[str, object] = {"limit": limit}
    if cursor is not None:
        if type(cursor) is not BookingMethodFeedCursor:
            raise ValueError("BOOKING_METHOD_FEED_CURSOR_INVALID") from None
        body["cursor"] = {"createdAt": cursor.created_at, "id": cursor.id}
    return body


def build_context_request_body(*, appointment_id: object) -> dict[str, object]:
    aid = require_canonical_appointment_id(appointment_id)
    return {"appointmentId": aid}
