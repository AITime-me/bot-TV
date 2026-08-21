"""Confirm → pending orchestration outcomes (SELF-BOOKING-COMMAND-03K1).

Internal orchestration only. No ingress wiring, CREATE HTTP, or reply-plan edits.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

__all__ = (
    "SelfBookingConfirmAdmissionOutcome",
    "SelfBookingConfirmAdmissionResult",
)


class SelfBookingConfirmAdmissionOutcome(str, enum.Enum):
    ADMITTED = "ADMITTED"
    DUPLICATE = "DUPLICATE"
    OFFER_NOT_ACTIVE = "OFFER_NOT_ACTIVE"
    PII_NOT_FOUND = "PII_NOT_FOUND"
    PII_EXPIRED = "PII_EXPIRED"
    HANDOFF_BLOCKED = "HANDOFF_BLOCKED"
    FAIL_CLOSED = "FAIL_CLOSED"


@dataclass(frozen=True, slots=True, repr=False)
class SelfBookingConfirmAdmissionResult:
    outcome: SelfBookingConfirmAdmissionOutcome
    pending_id: uuid.UUID | None = None
    idempotency_key: str | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.outcome is SelfBookingConfirmAdmissionOutcome.ADMITTED:
            if not isinstance(self.pending_id, uuid.UUID):
                raise TypeError("ADMITTED requires pending_id") from None
            if type(self.idempotency_key) is not str or not self.idempotency_key:
                raise TypeError("ADMITTED requires idempotency_key") from None
            return
        if self.outcome is SelfBookingConfirmAdmissionOutcome.DUPLICATE:
            if not isinstance(self.pending_id, uuid.UUID):
                raise TypeError("DUPLICATE requires pending_id") from None
            return

    def __repr__(self) -> str:
        return (
            "SelfBookingConfirmAdmissionResult("
            f"outcome={self.outcome.value!r}, "
            "pending_id=<redacted>, "
            "idempotency_key=<redacted>, "
            f"reason_code={self.reason_code!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()
