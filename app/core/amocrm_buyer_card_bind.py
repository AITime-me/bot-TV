"""Manual Buyer Card bind result types (IR-5).

Offline/ops approval after live IR-2 + IR-3. Local identity DB write only.
No CRM writes. No PII or external ids in repr.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

__all__ = (
    "AMOCRM_BUYER_CARD_BIND_SOURCE",
    "AmoCrmBuyerCardBindOutcome",
    "AmoCrmBuyerCardBindResult",
    "BuyerCardBindApproval",
)

AMOCRM_BUYER_CARD_BIND_SOURCE = "IR5_OPS_APPROVED"


class AmoCrmBuyerCardBindOutcome(str, enum.Enum):
    BOUND = "BOUND"
    ALREADY_BOUND = "ALREADY_BOUND"
    NOT_FOUND = "NOT_FOUND"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    INVALID_INPUT = "INVALID_INPUT"
    INCOMPLETE = "INCOMPLETE"
    DISABLED = "DISABLED"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    PERMANENT_ERROR = "PERMANENT_ERROR"


@dataclass(frozen=True, slots=True, repr=False)
class BuyerCardBindApproval:
    """Operator-approved contact + Buyer Card ids from stdin JSON."""

    contact_id: str
    buyer_card_id: str

    def __repr__(self) -> str:
        return (
            "BuyerCardBindApproval("
            "contact_id=<redacted>, "
            "buyer_card_id=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmBuyerCardBindResult:
    """Manual bind receipt. Technical amoCRM ids stay in controlled fields."""

    outcome: AmoCrmBuyerCardBindOutcome
    canonical_identity_id: uuid.UUID | None = None
    contact_id: str | None = None
    buyer_card_id: str | None = None
    reason: str | None = None
    error_code: str | None = None
    http_calls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome in {
            AmoCrmBuyerCardBindOutcome.BOUND,
            AmoCrmBuyerCardBindOutcome.ALREADY_BOUND,
        }:
            if self.canonical_identity_id is None:
                raise TypeError(f"{self.outcome.value} requires canonical_identity_id") from None
            if type(self.buyer_card_id) is not str or not self.buyer_card_id:
                raise TypeError(f"{self.outcome.value} requires buyer_card_id") from None
        elif self.buyer_card_id is not None:
            raise TypeError(
                f"{self.outcome.value} must not carry buyer_card_id"
            ) from None

    def __repr__(self) -> str:
        return (
            "AmoCrmBuyerCardBindResult("
            f"outcome={self.outcome.value!r}, "
            "canonical_identity_id=<redacted>, "
            "contact_id=<redacted>, "
            "buyer_card_id=<redacted>, "
            f"reason={self.reason!r}, "
            f"error_code={self.error_code!r}, "
            f"http_calls={self.http_calls!r})"
        )
