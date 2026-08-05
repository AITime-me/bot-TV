"""Remote DTOs for POST /api/internal/bot/v1/eligibility.

Separated from dialog/domain DTOs. No client phone, client name, channel ids,
or conversation text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EligibilityRemoteOutcome(StrEnum):
    SELF_BOOKING_ALLOWED = "SELF_BOOKING_ALLOWED"
    MANAGER_HANDOFF = "MANAGER_HANDOFF"


@dataclass(frozen=True, slots=True)
class EligibilityRemoteRequest:
    """Bounded JSON body for the eligibility endpoint."""

    service_id: str
    master_id: str | None
    include_alternatives: bool

    def to_json_object(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "serviceId": self.service_id,
            "includeAlternatives": self.include_alternatives,
        }
        if self.master_id is not None:
            payload["masterId"] = self.master_id
        return payload


@dataclass(frozen=True, slots=True)
class EligibilityRemoteAlternativeMaster:
    """Backend alternative master. public_name is remote-only and never mapped to dialog DTO."""

    id: str
    public_name: str


@dataclass(frozen=True, slots=True)
class EligibilityRemoteSuccess:
    """Strict success payload for HTTP 200."""

    outcome: EligibilityRemoteOutcome
    reason_code: str | None
    selected_pair_allowed: bool | None
    service_online_in_general: bool
    other_online_master_count: int
    other_online_masters: tuple[EligibilityRemoteAlternativeMaster, ...] | None
