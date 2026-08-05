"""Remote DTOs for booking availability S2S reads (CURSOR-22).

Wire contract (online-zapis-tv):
- ``POST /api/internal/bot/v1/available-days``
- ``POST /api/internal/bot/v1/slots``

Request JSON uses camelCase IDs only. No client clock, URL, token, or PII.
Repr never prints IDs, dates, or slot payloads.

Route path constants live here as the single source of truth so config and
HTTP adapter share them without circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.core.booking_types import AvailableSlot

AVAILABLE_DAYS_ROUTE_PATH: Final[str] = "/api/internal/bot/v1/available-days"
SLOTS_ROUTE_PATH: Final[str] = "/api/internal/bot/v1/slots"


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
