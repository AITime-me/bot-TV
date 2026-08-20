"""Self-booking confirmed-create pending types (SELF-BOOKING-COMMAND-01).

Durable foundation only: confirmation admission, lease, fence cancel.
No dialog wiring, Booking HTTP, PII plaintext read, or CRM.
"""

from __future__ import annotations

import enum
import re
import uuid
from dataclasses import dataclass
from typing import Final, Literal

from app.core.booking_availability_remote import require_canonical_booking_starts_at
from app.core.booking_create_remote import (
    parse_bot_slot_id,
    require_canonical_idempotency_key,
)

__all__ = (
    "ACTIVE_SELF_BOOKING_CREATE_STATES",
    "CONFIRM_EXTERNAL_MESSAGE_ID_MAX_LENGTH",
    "DEFAULT_MAX_ATTEMPTS",
    "EXECUTION_LEASE_SECONDS",
    "TERMINAL_SELF_BOOKING_CREATE_STATES",
    "SelfBookingCreateAdmitOutcome",
    "SelfBookingCreateAdmitResult",
    "SelfBookingCreatePendingState",
    "SelfBookingCreateSafeSelection",
    "normalize_confirm_external_message_id",
    "require_opaque_pii_ref_token",
    "require_self_booking_channel",
)

CONFIRM_EXTERNAL_MESSAGE_ID_MAX_LENGTH: Final[int] = 128
EXECUTION_LEASE_SECONDS: Final[int] = 60
DEFAULT_MAX_ATTEMPTS: Final[int] = 3

_CONFIRM_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[\x21-\x7E]+$")
_REF_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^[\x21-\x7E]{1,64}$")
_ALLOWED_CHANNELS: Final[frozenset[str]] = frozenset({"synthetic"})


class SelfBookingCreatePendingState(str, enum.Enum):
    """Post-confirmation command states. Admission == confirmed."""

    READY = "READY"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


ACTIVE_SELF_BOOKING_CREATE_STATES: Final[
    frozenset[SelfBookingCreatePendingState]
] = frozenset(
    {
        SelfBookingCreatePendingState.READY,
        SelfBookingCreatePendingState.EXECUTING,
    }
)

TERMINAL_SELF_BOOKING_CREATE_STATES: Final[
    frozenset[SelfBookingCreatePendingState]
] = frozenset(
    {
        SelfBookingCreatePendingState.SUCCEEDED,
        SelfBookingCreatePendingState.FAILED,
        SelfBookingCreatePendingState.CANCELLED,
        SelfBookingCreatePendingState.EXPIRED,
    }
)


class SelfBookingCreateAdmitOutcome(str, enum.Enum):
    ADMITTED = "ADMITTED"
    DUPLICATE = "DUPLICATE"
    ACTIVE_EXISTS = "ACTIVE_EXISTS"
    INVALID_INPUT = "INVALID_INPUT"
    CONVERSATION_MISSING = "CONVERSATION_MISSING"
    FENCE_STALE = "FENCE_STALE"
    HANDOFF_BLOCKED = "HANDOFF_BLOCKED"


@dataclass(frozen=True, slots=True, repr=False)
class SelfBookingCreateSafeSelection:
    """Non-PII selected slot identity for durable persistence."""

    slot_id: str
    starts_at: str

    def __post_init__(self) -> None:
        parse_bot_slot_id(self.slot_id)
        require_canonical_booking_starts_at(self.starts_at)

    def __repr__(self) -> str:
        return (
            "SelfBookingCreateSafeSelection("
            "slot_id=<redacted>, "
            "starts_at=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SelfBookingCreateAdmitResult:
    outcome: SelfBookingCreateAdmitOutcome
    pending_id: uuid.UUID | None = None
    idempotency_key: str | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.outcome is SelfBookingCreateAdmitOutcome.ADMITTED:
            if not isinstance(self.pending_id, uuid.UUID):
                raise TypeError("ADMITTED requires pending_id") from None
            if type(self.idempotency_key) is not str or not self.idempotency_key:
                raise TypeError("ADMITTED requires idempotency_key") from None
            return
        if self.outcome is SelfBookingCreateAdmitOutcome.DUPLICATE:
            if not isinstance(self.pending_id, uuid.UUID):
                raise TypeError("DUPLICATE requires pending_id") from None
            return
        if self.outcome is SelfBookingCreateAdmitOutcome.ACTIVE_EXISTS:
            if not isinstance(self.pending_id, uuid.UUID):
                raise TypeError("ACTIVE_EXISTS requires pending_id") from None
            return

    def __repr__(self) -> str:
        return (
            "SelfBookingCreateAdmitResult("
            f"outcome={self.outcome.value!r}, "
            "pending_id=<redacted>, "
            "idempotency_key=<redacted>, "
            f"reason_code={self.reason_code!r})"
        )


def require_self_booking_channel(value: object) -> str:
    if type(value) is not str or value not in _ALLOWED_CHANNELS:
        raise ValueError("SELF_BOOKING_CHANNEL_INVALID") from None
    return value


def normalize_confirm_external_message_id(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("SELF_BOOKING_CONFIRM_MESSAGE_ID_INVALID") from None
    if len(value) > CONFIRM_EXTERNAL_MESSAGE_ID_MAX_LENGTH:
        raise ValueError("SELF_BOOKING_CONFIRM_MESSAGE_ID_INVALID") from None
    if _CONFIRM_ID_RE.fullmatch(value) is None:
        raise ValueError("SELF_BOOKING_CONFIRM_MESSAGE_ID_INVALID") from None
    return value


def require_opaque_pii_ref_token(value: object) -> str:
    """Opaque EphemeralPiiReference token. Never echoes value."""

    if type(value) is not str or not value:
        raise ValueError("SELF_BOOKING_PII_REF_INVALID") from None
    if _REF_TOKEN_RE.fullmatch(value) is None:
        raise ValueError("SELF_BOOKING_PII_REF_INVALID") from None
    return value


def require_true_consent(value: object, *, field: Literal["consent", "offer"]) -> Literal[True]:
    if value is not True:
        raise ValueError("SELF_BOOKING_CONSENT_INVALID") from None
    return True


def require_nonnegative_int(value: object, *, code: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise ValueError(code) from None
    return value


def require_positive_max_attempts(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 1:
        raise ValueError("SELF_BOOKING_MAX_ATTEMPTS_INVALID") from None
    return value


def require_caller_idempotency_key(value: object) -> str:
    return require_canonical_idempotency_key(value)
