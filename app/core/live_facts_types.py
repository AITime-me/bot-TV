"""Strict immutable live-facts v1 contract (online-zapis-tv producer).

Exact keys. No coercion. Unknown fields reject. No durable cache semantics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final, Mapping, NoReturn

from app.core.live_facts_remote import (
    BOT_LIVE_FACTS_CURRENCY,
    BOT_LIVE_FACTS_SCHEMA_VERSION,
)

_CANONICAL_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_DECIMAL_RE: Final[re.Pattern[str]] = re.compile(r"^-?\d+(\.\d+)?$")

_MAX_SAFE_STRING: Final[int] = 500
_MAX_SAFE_LONG_STRING: Final[int] = 1000
_MAX_DURATION_MINUTES: Final[int] = 24 * 60
_MAX_SERVICES: Final[int] = 500
_MAX_MASTERS: Final[int] = 500
_MAX_SERVICE_IDS: Final[int] = 500

_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "ok",
        "schemaVersion",
        "generatedAt",
        "studio",
        "services",
        "masters",
    }
)
_STUDIO_KEYS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "phone",
        "email",
        "address",
        "workingHoursText",
        "isOnlineBookingEnabled",
    }
)
_SERVICE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "name",
        "category",
        "priceFrom",
        "priceTo",
        "currency",
        "durationMinutes",
        "bookingMode",
        "isActive",
        "isOnlineBookingEnabled",
    }
)
_MASTER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "name",
        "isActive",
        "isOnlineBookingEnabled",
        "serviceIds",
    }
)
_BOOKING_MODES: Final[frozenset[str]] = frozenset({"ONLINE", "MANAGER_ONLY"})

_FORBIDDEN_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "slots",
        "availableDays",
        "availability",
        "scheduleBlocks",
        "appointments",
        "appointmentState",
        "promotions",
        "gifts",
        "discounts",
        "priceLabel",
        "internalName",
        "clientDescription",
        "updatedByUserId",
        "publishedByUserId",
        "apiKey",
        "token",
        "secret",
        "password",
        "authorization",
        "botKnowledge",
        "botSettings",
    }
)


class LiveFactsBookingMode(StrEnum):
    ONLINE = "ONLINE"
    MANAGER_ONLY = "MANAGER_ONLY"


class LiveFactsParseError(ValueError):
    """Strict contract violation. Message is a fixed safe code only."""

    def __init__(self, code: str = "RESPONSE_INVALID") -> None:
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "RESPONSE_INVALID"

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return f"LiveFactsParseError({self.code!r})"


def _fail(code: str = "RESPONSE_INVALID") -> NoReturn:
    raise LiveFactsParseError(code) from None


def _require_mapping(value: object) -> Mapping[str, object]:
    if type(value) is not dict:
        _fail()
    return value


def _require_exact_keys(
    mapping: Mapping[str, object], allowed: frozenset[str]
) -> None:
    keys = frozenset(mapping.keys())
    if keys != allowed:
        _fail()


def _assert_no_forbidden_keys(value: object) -> None:
    if value is None or type(value) in {str, int, float, bool}:
        return
    if type(value) is list:
        for entry in value:
            _assert_no_forbidden_keys(entry)
        return
    if type(value) is not dict:
        _fail()
    for key, entry in value.items():
        if type(key) is not str:
            _fail()
        if key in _FORBIDDEN_PAYLOAD_KEYS:
            _fail()
        _assert_no_forbidden_keys(entry)


def _require_bool(value: object) -> bool:
    if type(value) is not bool:
        _fail()
    return value


def _require_bounded_string(value: object, *, max_len: int) -> str:
    if type(value) is not str or len(value) > max_len:
        _fail()
    return value


def _require_uuid(value: object) -> str:
    if type(value) is not str or not _CANONICAL_UUID_RE.fullmatch(value):
        _fail()
    return value


def _require_decimal_or_null(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not _DECIMAL_RE.fullmatch(value):
        _fail()
    return value


def _require_iso_timestamp(value: object) -> datetime:
    if type(value) is not str or not value:
        _fail()
    raw = value
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        _fail()
    if parsed.tzinfo is None:
        _fail()
    return parsed.astimezone(timezone.utc)


def _require_duration_minutes(value: object) -> int:
    if type(value) is not int or isinstance(value, bool):
        _fail()
    if value < 1 or value > _MAX_DURATION_MINUTES:
        _fail()
    return value


def _require_booking_mode(value: object) -> LiveFactsBookingMode:
    if type(value) is not str or value not in _BOOKING_MODES:
        _fail()
    return LiveFactsBookingMode(value)


@dataclass(frozen=True, slots=True, repr=False)
class LiveFactsStudioV1:
    name: str
    phone: str
    email: str
    address: str
    working_hours_text: str
    is_online_booking_enabled: bool

    def __repr__(self) -> str:
        return (
            "LiveFactsStudioV1("
            f"name=<redacted len={len(self.name)}>, "
            "phone=<redacted>, email=<redacted>, "
            f"address=<redacted len={len(self.address)}>, "
            f"working_hours_text=<redacted len={len(self.working_hours_text)}>, "
            f"is_online_booking_enabled={self.is_online_booking_enabled!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class LiveFactsServiceV1:
    id: str
    name: str
    category: str | None
    price_from: str | None
    price_to: str | None
    currency: str
    duration_minutes: int
    booking_mode: LiveFactsBookingMode
    is_active: bool
    is_online_booking_enabled: bool

    def __repr__(self) -> str:
        return (
            "LiveFactsServiceV1("
            f"id={self.id!r}, "
            f"name=<redacted len={len(self.name)}>, "
            f"category={'None' if self.category is None else '<redacted>'}, "
            f"price_from={self.price_from!r}, "
            f"price_to={self.price_to!r}, "
            f"currency={self.currency!r}, "
            f"duration_minutes={self.duration_minutes!r}, "
            f"booking_mode={self.booking_mode.value!r}, "
            f"is_active={self.is_active!r}, "
            f"is_online_booking_enabled={self.is_online_booking_enabled!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class LiveFactsMasterV1:
    id: str
    name: str
    is_active: bool
    is_online_booking_enabled: bool
    service_ids: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            "LiveFactsMasterV1("
            f"id={self.id!r}, "
            f"name=<redacted len={len(self.name)}>, "
            f"is_active={self.is_active!r}, "
            f"is_online_booking_enabled={self.is_online_booking_enabled!r}, "
            f"service_ids_count={len(self.service_ids)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class LiveFactsPayloadV1:
    schema_version: int
    generated_at: datetime
    studio: LiveFactsStudioV1
    services: tuple[LiveFactsServiceV1, ...]
    masters: tuple[LiveFactsMasterV1, ...]

    def __repr__(self) -> str:
        return (
            "LiveFactsPayloadV1("
            f"schema_version={self.schema_version!r}, "
            f"generated_at={self.generated_at.isoformat()!r}, "
            f"service_count={len(self.services)!r}, "
            f"master_count={len(self.masters)!r})"
        )

def _parse_studio(raw: object) -> LiveFactsStudioV1:
    mapping = _require_mapping(raw)
    _require_exact_keys(mapping, _STUDIO_KEYS)
    return LiveFactsStudioV1(
        name=_require_bounded_string(mapping["name"], max_len=_MAX_SAFE_STRING),
        phone=_require_bounded_string(mapping["phone"], max_len=_MAX_SAFE_STRING),
        email=_require_bounded_string(mapping["email"], max_len=_MAX_SAFE_STRING),
        address=_require_bounded_string(
            mapping["address"], max_len=_MAX_SAFE_LONG_STRING
        ),
        working_hours_text=_require_bounded_string(
            mapping["workingHoursText"], max_len=_MAX_SAFE_LONG_STRING
        ),
        is_online_booking_enabled=_require_bool(
            mapping["isOnlineBookingEnabled"]
        ),
    )


def _parse_service(raw: object) -> LiveFactsServiceV1:
    mapping = _require_mapping(raw)
    _require_exact_keys(mapping, _SERVICE_KEYS)
    category_raw = mapping["category"]
    category: str | None
    if category_raw is None:
        category = None
    else:
        category = _require_bounded_string(
            category_raw, max_len=_MAX_SAFE_STRING
        )
    currency = mapping["currency"]
    if type(currency) is not str or currency != BOT_LIVE_FACTS_CURRENCY:
        _fail()
    return LiveFactsServiceV1(
        id=_require_uuid(mapping["id"]),
        name=_require_bounded_string(mapping["name"], max_len=_MAX_SAFE_STRING),
        category=category,
        price_from=_require_decimal_or_null(mapping["priceFrom"]),
        price_to=_require_decimal_or_null(mapping["priceTo"]),
        currency=currency,
        duration_minutes=_require_duration_minutes(mapping["durationMinutes"]),
        booking_mode=_require_booking_mode(mapping["bookingMode"]),
        is_active=_require_bool(mapping["isActive"]),
        is_online_booking_enabled=_require_bool(
            mapping["isOnlineBookingEnabled"]
        ),
    )


def _parse_master(raw: object) -> LiveFactsMasterV1:
    mapping = _require_mapping(raw)
    _require_exact_keys(mapping, _MASTER_KEYS)
    service_ids_raw = mapping["serviceIds"]
    if type(service_ids_raw) is not list:
        _fail()
    if len(service_ids_raw) > _MAX_SERVICE_IDS:
        _fail()
    service_ids = tuple(_require_uuid(item) for item in service_ids_raw)
    return LiveFactsMasterV1(
        id=_require_uuid(mapping["id"]),
        name=_require_bounded_string(mapping["name"], max_len=_MAX_SAFE_STRING),
        is_active=_require_bool(mapping["isActive"]),
        is_online_booking_enabled=_require_bool(
            mapping["isOnlineBookingEnabled"]
        ),
        service_ids=service_ids,
    )


def parse_live_facts_response_v1(raw: object) -> LiveFactsPayloadV1:
    """Parse merged HTTP ``{ok:true, ...BotLiveFactsPayloadV1}``."""

    mapping = _require_mapping(raw)
    _assert_no_forbidden_keys(mapping)
    _require_exact_keys(mapping, _ENVELOPE_KEYS)
    if mapping["ok"] is not True:
        _fail()
    schema_version = mapping["schemaVersion"]
    if (
        type(schema_version) is not int
        or isinstance(schema_version, bool)
        or schema_version != BOT_LIVE_FACTS_SCHEMA_VERSION
    ):
        _fail()
    services_raw = mapping["services"]
    masters_raw = mapping["masters"]
    if type(services_raw) is not list or type(masters_raw) is not list:
        _fail()
    if len(services_raw) > _MAX_SERVICES or len(masters_raw) > _MAX_MASTERS:
        _fail()
    return LiveFactsPayloadV1(
        schema_version=schema_version,
        generated_at=_require_iso_timestamp(mapping["generatedAt"]),
        studio=_parse_studio(mapping["studio"]),
        services=tuple(_parse_service(item) for item in services_raw),
        masters=tuple(_parse_master(item) for item in masters_raw),
    )
