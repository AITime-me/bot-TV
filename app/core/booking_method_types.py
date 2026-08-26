"""A2.2 booking-method analytics durable sync types.

Poll-only contour for SELF_SERVICE|MANAGER|MASTER appointments.
Never enqueues TEYA. Never creates deals. No phone in durable pending.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Final

__all__ = (
    "BOOKING_METHOD_ANALYTICS_LOOP",
    "BOOKING_METHOD_PURPOSE",
    "CREATOR_KIND_TO_ENUM_ID",
    "DEFAULT_MAX_ATTEMPTS",
    "EXECUTION_LEASE_SECONDS",
    "FEED_CURSOR_ID",
    "PURPOSE",
    "TERMINAL_BOOKING_METHOD_STATES",
    "BookingMethodAnalyticsOutcome",
    "BookingMethodAnalyticsResult",
    "BookingMethodCreatorKind",
    "BookingMethodPendingState",
    "enum_id_for_creator_kind",
)

BOOKING_METHOD_ANALYTICS_LOOP: Final[str] = "booking_method_analytics"
PURPOSE: Final[str] = "BOOKING_CREATION_METHOD"
BOOKING_METHOD_PURPOSE: Final[str] = PURPOSE
FEED_CURSOR_ID: Final[str] = "booking_method"
EXECUTION_LEASE_SECONDS: Final[int] = 90
DEFAULT_MAX_ATTEMPTS: Final[int] = 8


class BookingMethodPendingState(str, enum.Enum):
    """Workflow states for a booking-method analytics pending row."""

    DISCOVERED = "DISCOVERED"
    RESOLVING = "RESOLVING"
    APPLYING = "APPLYING"
    DONE = "DONE"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    SKIPPED = "SKIPPED"


TERMINAL_BOOKING_METHOD_STATES: Final[frozenset[BookingMethodPendingState]] = (
    frozenset(
        {
            BookingMethodPendingState.DONE,
            BookingMethodPendingState.MANUAL_REVIEW,
            BookingMethodPendingState.SKIPPED,
        }
    )
)


class BookingMethodCreatorKind(str, enum.Enum):
    """Feed-eligible creator kinds (TEYA excluded by design)."""

    SELF_SERVICE = "SELF_SERVICE"
    MANAGER = "MANAGER"
    MASTER = "MASTER"


# Live amoCRM enum ids for field 1321305 (BOOKING_CREATION_METHOD).
CREATOR_KIND_TO_ENUM_ID: Final[dict[BookingMethodCreatorKind, int]] = {
    BookingMethodCreatorKind.SELF_SERVICE: 851489,
    BookingMethodCreatorKind.MANAGER: 851493,
    BookingMethodCreatorKind.MASTER: 851495,
}


def enum_id_for_creator_kind(kind: BookingMethodCreatorKind | str) -> int:
    """Map creator kind → analytics enum id. Reject TEYA/OTHER/unknown."""

    if isinstance(kind, BookingMethodCreatorKind):
        return CREATOR_KIND_TO_ENUM_ID[kind]
    if type(kind) is not str:
        raise ValueError("BOOKING_METHOD_CREATOR_KIND_INVALID")
    try:
        parsed = BookingMethodCreatorKind(kind)
    except ValueError as exc:
        raise ValueError("BOOKING_METHOD_CREATOR_KIND_INVALID") from exc
    return CREATOR_KIND_TO_ENUM_ID[parsed]


class BookingMethodAnalyticsOutcome(str, enum.Enum):
    ADVANCED = "ADVANCED"
    TERMINAL = "TERMINAL"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    CLAIM_DENIED = "CLAIM_DENIED"
    IDLE = "IDLE"
    FEED_UNAVAILABLE = "FEED_UNAVAILABLE"


@dataclass(frozen=True, slots=True, repr=False)
class BookingMethodAnalyticsResult:
    outcome: BookingMethodAnalyticsOutcome
    pending_id: uuid.UUID | None = None
    pending_state: BookingMethodPendingState | None = None
    result_code: str | None = None

    def __repr__(self) -> str:
        return (
            "BookingMethodAnalyticsResult("
            f"outcome={self.outcome.value!r}, "
            "pending_id=<redacted>, "
            f"pending_state="
            f"{None if self.pending_state is None else self.pending_state.value!r}, "
            f"result_code={self.result_code!r})"
        )
