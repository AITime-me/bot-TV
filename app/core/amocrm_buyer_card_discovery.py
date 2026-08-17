"""amoCRM Buyer Card discovery result types (IR-3).

Read-only lead-candidate evidence for IdentityResolutionService.reconcile_buyer_card.
No attach/create. No PII in repr.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

__all__ = (
    "AmoCrmBuyerCardDiscoveryOutcome",
    "AmoCrmBuyerCardDiscoveryResult",
    "BuyerCardReconcileCandidates",
    "buyer_card_reconcile_candidates_from_discovery",
)


class AmoCrmBuyerCardDiscoveryOutcome(str, enum.Enum):
    FOUND_CANDIDATE = "FOUND_CANDIDATE"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    INCOMPLETE = "INCOMPLETE"
    INVALID_INPUT = "INVALID_INPUT"
    DISABLED = "DISABLED"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    PERMANENT_ERROR = "PERMANENT_ERROR"


_COMPLETE_OUTCOMES = frozenset(
    {
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE,
        AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND,
        AmoCrmBuyerCardDiscoveryOutcome.AMBIGUOUS,
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmBuyerCardDiscoveryResult:
    """Discovery receipt. Lead/contact ids are technical amoCRM ids only."""

    outcome: AmoCrmBuyerCardDiscoveryOutcome
    contact_id: str | None = None
    eligible_lead_ids: tuple[str, ...] = ()
    known_technical_deal_ids: tuple[str, ...] = ()
    error_code: str | None = None
    http_calls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome is AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE:
            if type(self.contact_id) is not str or not self.contact_id:
                raise TypeError("FOUND_CANDIDATE requires contact_id") from None
            if len(self.eligible_lead_ids) != 1:
                raise TypeError("FOUND_CANDIDATE requires exactly 1 eligible id") from None
        elif self.outcome is AmoCrmBuyerCardDiscoveryOutcome.AMBIGUOUS:
            if type(self.contact_id) is not str or not self.contact_id:
                raise TypeError("AMBIGUOUS requires contact_id") from None
            if len(self.eligible_lead_ids) < 2:
                raise TypeError("AMBIGUOUS requires >=2 eligible ids") from None
        elif self.outcome is AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND:
            if len(self.eligible_lead_ids) != 0:
                raise TypeError("NOT_FOUND must not carry eligible ids") from None
        elif self.eligible_lead_ids:
            raise TypeError(
                f"{self.outcome.value} must not carry eligible_lead_ids"
            ) from None

    def __repr__(self) -> str:
        return (
            "AmoCrmBuyerCardDiscoveryResult("
            f"outcome={self.outcome.value!r}, "
            f"contact_id={self.contact_id!r}, "
            f"eligible_lead_ids={self.eligible_lead_ids!r}, "
            f"known_technical_deal_ids={self.known_technical_deal_ids!r}, "
            f"error_code={self.error_code!r}, "
            f"http_calls={self.http_calls!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BuyerCardReconcileCandidates:
    """Inputs for IdentityResolutionService.reconcile_buyer_card. Not an attach."""

    candidate_buyer_card_ids: tuple[str, ...]
    candidate_technical_deal_ids: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            "BuyerCardReconcileCandidates("
            f"candidate_buyer_card_ids={self.candidate_buyer_card_ids!r}, "
            f"candidate_technical_deal_ids={self.candidate_technical_deal_ids!r})"
        )


def buyer_card_reconcile_candidates_from_discovery(
    result: AmoCrmBuyerCardDiscoveryResult,
) -> BuyerCardReconcileCandidates | None:
    """Map a complete discovery into reconcile kwargs. Never attaches."""

    if result.outcome not in _COMPLETE_OUTCOMES:
        return None
    return BuyerCardReconcileCandidates(
        candidate_buyer_card_ids=result.eligible_lead_ids,
        candidate_technical_deal_ids=result.known_technical_deal_ids,
    )
