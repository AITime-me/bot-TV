"""Remote DTOs for booking availability S2S reads (CURSOR-22).

Wire contract (online-zapis-tv):
- ``POST /api/internal/bot/v1/available-days``
- ``POST /api/internal/bot/v1/slots``

Request JSON uses camelCase IDs only. No client clock, URL, token, or PII.
Repr never prints IDs, dates, or slot payloads.

Route path constants live here as the single source of truth so config and
HTTP adapter share them without circular imports.

Canonical calendar / studio-timestamp validators also live here so the HTTP
adapter and durable synthetic sanitizer share one contract without coupling
the service layer to the HTTP adapter module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Final

from app.core.booking_types import AvailableSlot

AVAILABLE_DAYS_ROUTE_PATH: Final[str] = "/api/internal/bot/v1/available-days"
SLOTS_ROUTE_PATH: Final[str] = "/api/internal/bot/v1/slots"

_STUDIO_UTC_OFFSET: Final[timedelta] = timedelta(hours=5)
_STUDIO_TZ: Final[timezone] = timezone(_STUDIO_UTC_OFFSET)

_MONTH_RE: Final[re.Pattern[str]] = re.compile(r"^([0-9]{4})-([0-9]{2})$")
_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$"
)
# Studio wall clock: exact minutes, zero seconds, fixed UTC+5 offset only.
_STARTS_AT_RE: Final[re.Pattern[str]] = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):00\+05:00$"
)


def _contains_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def require_calendar_month(value: object) -> str:
    """Require a real calendar month ``YYYY-MM``. Never echoes the value."""

    if type(value) is not str or not value:
        raise ValueError("BOOKING_CALENDAR_MONTH_INVALID") from None
    if any(ch.isspace() for ch in value) or _contains_control_chars(value):
        raise ValueError("BOOKING_CALENDAR_MONTH_INVALID") from None
    match = _MONTH_RE.fullmatch(value)
    if match is None:
        raise ValueError("BOOKING_CALENDAR_MONTH_INVALID") from None
    year = int(match.group(1))
    month = int(match.group(2))
    if year < 1 or month < 1 or month > 12:
        raise ValueError("BOOKING_CALENDAR_MONTH_INVALID") from None
    try:
        date(year, month, 1)
    except ValueError:
        raise ValueError("BOOKING_CALENDAR_MONTH_INVALID") from None
    return f"{year:04d}-{month:02d}"


def require_calendar_date(value: object) -> str:
    """Require a real calendar day ``YYYY-MM-DD``. Never echoes the value."""

    if type(value) is not str or not value:
        raise ValueError("BOOKING_CALENDAR_DATE_INVALID") from None
    if any(ch.isspace() for ch in value) or _contains_control_chars(value):
        raise ValueError("BOOKING_CALENDAR_DATE_INVALID") from None
    match = _DATE_RE.fullmatch(value)
    if match is None:
        raise ValueError("BOOKING_CALENDAR_DATE_INVALID") from None
    year = int(match.group(1))
    month = int(match.group(2))
    day = int(match.group(3))
    try:
        parsed = date(year, month, day)
    except ValueError:
        raise ValueError("BOOKING_CALENDAR_DATE_INVALID") from None
    canonical = parsed.isoformat()
    if canonical != value:
        raise ValueError("BOOKING_CALENDAR_DATE_INVALID") from None
    return canonical


def require_canonical_booking_starts_at(value: object) -> str:
    """Require studio slot timestamp ``YYYY-MM-DDTHH:MM:00+05:00``.

    Returns the original canonical string unchanged. Never normalizes ``Z``,
    other offsets, fractional seconds, or nonzero seconds.
    """

    if type(value) is not str or not value:
        raise ValueError("BOOKING_STARTS_AT_INVALID") from None
    if any(ch.isspace() for ch in value) or _contains_control_chars(value):
        raise ValueError("BOOKING_STARTS_AT_INVALID") from None
    match = _STARTS_AT_RE.fullmatch(value)
    if match is None:
        raise ValueError("BOOKING_STARTS_AT_INVALID") from None
    year = int(match.group(1))
    month = int(match.group(2))
    day = int(match.group(3))
    hour = int(match.group(4))
    minute = int(match.group(5))
    try:
        wall = date(year, month, day)
    except ValueError:
        raise ValueError("BOOKING_STARTS_AT_INVALID") from None
    if wall.isoformat() != f"{year:04d}-{month:02d}-{day:02d}":
        raise ValueError("BOOKING_STARTS_AT_INVALID") from None
    if hour > 23 or minute > 59:
        raise ValueError("BOOKING_STARTS_AT_INVALID") from None
    return value


def format_canonical_booking_starts_at(value: object) -> str:
    """Serialize an aware datetime to studio ``YYYY-MM-DDTHH:MM:00+05:00``.

    Converts to the studio offset without inventing minutes. Nonzero seconds
    or naive values fail closed. Output always passes
    ``require_canonical_booking_starts_at``.
    """

    if not isinstance(value, datetime):
        raise ValueError("BOOKING_STARTS_AT_INVALID") from None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("BOOKING_STARTS_AT_INVALID") from None
    studio = value.astimezone(_STUDIO_TZ)
    if studio.second != 0 or studio.microsecond != 0:
        raise ValueError("BOOKING_STARTS_AT_INVALID") from None
    formatted = (
        f"{studio.year:04d}-{studio.month:02d}-{studio.day:02d}"
        f"T{studio.hour:02d}:{studio.minute:02d}:00+05:00"
    )
    return require_canonical_booking_starts_at(formatted)


@dataclass(frozen=True, slots=True, repr=False)
class AvailableDaysRemoteRequest:
    """Bounded JSON body for available-days. Fields are pre-validated canonical values."""

    service_id: str
    master_id: str
    month: str

    def to_json_object(self) -> dict[str, object]:
        return {
            "serviceId": self.service_id,
            "masterId": self.master_id,
            "month": self.month,
        }

    def __repr__(self) -> str:
        return (
            "AvailableDaysRemoteRequest("
            "service_id=<redacted>, "
            "master_id=<redacted>, "
            "month=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AvailableSlotsRemoteRequest:
    """Bounded JSON body for slots. Fields are pre-validated canonical values."""

    service_id: str
    master_id: str
    date: str

    def to_json_object(self) -> dict[str, object]:
        return {
            "serviceId": self.service_id,
            "masterId": self.master_id,
            "date": self.date,
        }

    def __repr__(self) -> str:
        return (
            "AvailableSlotsRemoteRequest("
            "service_id=<redacted>, "
            "master_id=<redacted>, "
            "date=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AvailableDaysResult:
    """Fail-closed success DTO for available-days. Collections are immutable."""

    service_id: str
    master_id: str
    month: str
    studio_today: str
    date_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.date_keys) is not tuple:
            raise TypeError("date_keys must be a tuple") from None

    def __repr__(self) -> str:
        return (
            "AvailableDaysResult("
            "service_id=<redacted>, "
            "master_id=<redacted>, "
            f"month=<redacted>, "
            f"studio_today=<redacted>, "
            f"date_keys_len={len(self.date_keys)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AvailableSlotsResult:
    """Fail-closed success DTO for slots. Projects remote slots to domain AvailableSlot."""

    service_id: str
    master_id: str
    date: str
    studio_today: str
    slots: tuple[AvailableSlot, ...]

    def __post_init__(self) -> None:
        if type(self.slots) is not tuple:
            raise TypeError("slots must be a tuple") from None
        for item in self.slots:
            if type(item) is not AvailableSlot:
                raise TypeError("slots must contain AvailableSlot only") from None

    def __repr__(self) -> str:
        return (
            "AvailableSlotsResult("
            "service_id=<redacted>, "
            "master_id=<redacted>, "
            f"date=<redacted>, "
            f"studio_today=<redacted>, "
            f"slots_len={len(self.slots)!r})"
        )
