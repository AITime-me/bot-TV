"""Read-only amoCRM business Deal (Lead) discovery result types.

Classifies linked Leads without treating them as Buyer Cards (Customers).
Only amoCRM system status 143 (closed/unrealized) is a reanimation candidate.
Status 142 (successfully realized) is successful history, not reanimation.
No auto-reanimation. No CRM writes.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final

__all__ = (
    "AMOCRM_SYSTEM_LEAD_STATUS_SUCCESS",
    "AMOCRM_SYSTEM_LEAD_STATUS_UNREALIZED",
    "AmoCrmDealDiscoveryOutcome",
    "AmoCrmDealDiscoveryResult",
)

# amoCRM system pipeline statuses (Lead).
AMOCRM_SYSTEM_LEAD_STATUS_SUCCESS: Final[int] = 142
AMOCRM_SYSTEM_LEAD_STATUS_UNREALIZED: Final[int] = 143


class AmoCrmDealDiscoveryOutcome(str, enum.Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INCOMPLETE = "INCOMPLETE"
    INVALID_INPUT = "INVALID_INPUT"
    DISABLED = "DISABLED"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    PERMANENT_ERROR = "PERMANENT_ERROR"


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmDealDiscoveryResult:
    """Lead classification receipt. Technical amoCRM ids only."""

    outcome: AmoCrmDealDiscoveryOutcome
    contact_id: str | None = None
    business_active_lead_ids: tuple[str, ...] = ()
    reanimation_candidate_lead_ids: tuple[str, ...] = ()
    successfully_closed_lead_ids: tuple[str, ...] = ()
    technical_lead_ids: tuple[str, ...] = ()
    known_technical_deal_ids: tuple[str, ...] = ()
    error_code: str | None = None
    http_calls: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "AmoCrmDealDiscoveryResult("
            f"outcome={self.outcome.value!r}, "
            f"contact_id={self.contact_id!r}, "
            f"business_active_lead_ids={self.business_active_lead_ids!r}, "
            f"reanimation_candidate_lead_ids={self.reanimation_candidate_lead_ids!r}, "
            f"successfully_closed_lead_ids={self.successfully_closed_lead_ids!r}, "
            f"technical_lead_ids={self.technical_lead_ids!r}, "
            f"known_technical_deal_ids={self.known_technical_deal_ids!r}, "
            f"error_code={self.error_code!r}, "
            f"http_calls={self.http_calls!r})"
        )
