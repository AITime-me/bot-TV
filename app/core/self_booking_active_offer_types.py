"""Self-booking active-offer read-model types (SELF-BOOKING-COMMAND-03C).

Durable binding: conversation → offered slots from a DELIVERED OFFER_SLOTS
outbound. No PII, confirm schema, admit, or CREATE.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Final

from app.core.booking_availability_remote import require_canonical_booking_starts_at
from app.core.booking_create_remote import parse_bot_slot_id

__all__ = (
    "ActiveOfferActivateOutcome",
    "ActiveOfferActivateResult",
    "ActiveOfferResolveOutcome",
    "ActiveOfferResolveResult",
    "ActiveOfferSlot",
    "MAX_ACTIVE_OFFER_SLOTS",
    "require_active_offer_slots",
)

MAX_ACTIVE_OFFER_SLOTS: Final[int] = 3


@dataclass(frozen=True, slots=True, repr=False)
class ActiveOfferSlot:
    """Non-PII offered slot identity."""

    slot_id: str
    starts_at: str

    def __post_init__(self) -> None:
        parse_bot_slot_id(self.slot_id)
        require_canonical_booking_starts_at(self.starts_at)

    def to_json_dict(self) -> dict[str, str]:
        return {"slot_id": self.slot_id, "starts_at": self.starts_at}

    def __repr__(self) -> str:
        return "ActiveOfferSlot(slot_id=<redacted>, starts_at=<redacted>)"


def require_active_offer_slots(value: object) -> tuple[ActiveOfferSlot, ...]:
    if type(value) is not tuple and type(value) is not list:
        raise ValueError("ACTIVE_OFFER_SLOTS_INVALID") from None
    if not value or len(value) > MAX_ACTIVE_OFFER_SLOTS:
        raise ValueError("ACTIVE_OFFER_SLOTS_INVALID") from None
    slots: list[ActiveOfferSlot] = []
    seen: set[str] = set()
    for item in value:
        if type(item) is ActiveOfferSlot:
            slot = item
        elif type(item) is dict:
            slot_id = item.get("slot_id")
            starts_at = item.get("starts_at")
            if type(slot_id) is not str or type(starts_at) is not str:
                raise ValueError("ACTIVE_OFFER_SLOTS_INVALID") from None
            slot = ActiveOfferSlot(slot_id=slot_id, starts_at=starts_at)
        else:
            raise ValueError("ACTIVE_OFFER_SLOTS_INVALID") from None
        if slot.slot_id in seen:
            raise ValueError("ACTIVE_OFFER_SLOTS_INVALID") from None
        seen.add(slot.slot_id)
        slots.append(slot)
    return tuple(slots)


class ActiveOfferActivateOutcome(str, enum.Enum):
    ACTIVATED = "ACTIVATED"
    REPLACED = "REPLACED"
    REPLAYED = "REPLAYED"
    IGNORED_STALE = "IGNORED_STALE"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True, repr=False)
class ActiveOfferActivateResult:
    outcome: ActiveOfferActivateOutcome
    conversation_id: uuid.UUID | None = None
    source_outbound_id: uuid.UUID | None = None
    reason_code: str | None = None

    def __repr__(self) -> str:
        return (
            "ActiveOfferActivateResult("
            f"outcome={self.outcome.value!r}, "
            "conversation_id=<redacted>, "
            "source_outbound_id=<redacted>, "
            f"reason_code={self.reason_code!r})"
        )


class ActiveOfferResolveOutcome(str, enum.Enum):
    FOUND = "FOUND"
    NOT_ACTIVE = "NOT_ACTIVE"


@dataclass(frozen=True, slots=True, repr=False)
class ActiveOfferResolveResult:
    outcome: ActiveOfferResolveOutcome
    starts_at: str | None = None
    source_outbound_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.outcome is ActiveOfferResolveOutcome.FOUND:
            if type(self.starts_at) is not str or not self.starts_at:
                raise TypeError("FOUND requires starts_at") from None
            if not isinstance(self.source_outbound_id, uuid.UUID):
                raise TypeError("FOUND requires source_outbound_id") from None
            return
        if self.starts_at is not None or self.source_outbound_id is not None:
            raise TypeError("NOT_ACTIVE must not carry starts_at/source") from None

    def __repr__(self) -> str:
        return (
            "ActiveOfferResolveResult("
            f"outcome={self.outcome.value!r}, "
            "starts_at=<redacted>, "
            "source_outbound_id=<redacted>)"
        )
