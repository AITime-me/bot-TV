"""Read-only Buyer Card orchestration result types (IR-4).

Composes IR-2 lookup, IR-3 discovery, and IdentityResolutionService.reconcile_buyer_card.
No attach/create. No PII in repr.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

__all__ = (
    "AmoCrmBuyerCardReadOutcome",
    "AmoCrmBuyerCardReadResult",
    "BuyerCardContactSource",
)


class BuyerCardContactSource(str, enum.Enum):
    DURABLE_LINK = "DURABLE_LINK"
    PHONE_LOOKUP = "PHONE_LOOKUP"


class AmoCrmBuyerCardReadOutcome(str, enum.Enum):
    REUSED = "REUSED"
    NOT_FOUND = "NOT_FOUND"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    INVALID_INPUT = "INVALID_INPUT"
    INCOMPLETE = "INCOMPLETE"
    DISABLED = "DISABLED"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    PERMANENT_ERROR = "PERMANENT_ERROR"


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmBuyerCardReadResult:
    """Read-only orchestration receipt. Technical amoCRM ids are allowed."""

    outcome: AmoCrmBuyerCardReadOutcome
    canonical_identity_id: uuid.UUID | None = None
    contact_id: str | None = None
    buyer_card_external_id: str | None = None
    contact_source: BuyerCardContactSource | None = None
    reason: str | None = None
    error_code: str | None = None
    http_calls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome is AmoCrmBuyerCardReadOutcome.REUSED:
            if self.canonical_identity_id is None:
                raise TypeError("REUSED requires canonical_identity_id") from None
            if type(self.buyer_card_external_id) is not str or not self.buyer_card_external_id:
                raise TypeError("REUSED requires buyer_card_external_id") from None
        elif self.buyer_card_external_id is not None:
            raise TypeError(
                f"{self.outcome.value} must not carry buyer_card_external_id"
            ) from None

    def __repr__(self) -> str:
        source = None if self.contact_source is None else self.contact_source.value
        return (
            "AmoCrmBuyerCardReadResult("
            f"outcome={self.outcome.value!r}, "
            f"canonical_identity_id={self.canonical_identity_id!r}, "
            f"contact_id={self.contact_id!r}, "
            f"buyer_card_external_id={self.buyer_card_external_id!r}, "
            f"contact_source={source!r}, "
            f"reason={self.reason!r}, "
            f"error_code={self.error_code!r}, "
            f"http_calls={self.http_calls!r})"
        )
