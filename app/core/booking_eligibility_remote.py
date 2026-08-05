"""Remote DTOs for POST /api/internal/bot/v1/eligibility.

Separated from dialog/domain DTOs. No client phone, client name, channel ids,
or conversation text. Repr never prints publicName or body-like payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EligibilityRemoteOutcome(StrEnum):
    SELF_BOOKING_ALLOWED = "SELF_BOOKING_ALLOWED"
    MANAGER_HANDOFF = "MANAGER_HANDOFF"


@dataclass(frozen=True, slots=True, repr=False)
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

    def __repr__(self) -> str:
        return (
            "EligibilityRemoteRequest("
            "service_id=<redacted>, "
            f"master_present={self.master_id is not None!r}, "
            f"include_alternatives={self.include_alternatives!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class EligibilityRemoteAlternativeMaster:
    """Backend alternative master. public_name is remote-only and never mapped to dialog DTO."""

    id: str
    public_name: str

    def __repr__(self) -> str:
        return "EligibilityRemoteAlternativeMaster(id=<redacted>, public_name=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class EligibilityRemoteSuccess:
    """Strict success payload for HTTP 200."""

    outcome: EligibilityRemoteOutcome
    reason_code: str | None
    selected_pair_allowed: bool | None
    service_online_in_general: bool
    other_online_master_count: int
    other_online_masters: tuple[EligibilityRemoteAlternativeMaster, ...] | None

    def __repr__(self) -> str:
        alt_len = (
            None
            if self.other_online_masters is None
            else len(self.other_online_masters)
        )
        return (
            "EligibilityRemoteSuccess("
            f"outcome={self.outcome!r}, "
            f"reason_code={self.reason_code!r}, "
            f"selected_pair_allowed={self.selected_pair_allowed!r}, "
            f"service_online_in_general={self.service_online_in_general!r}, "
            f"other_online_master_count={self.other_online_master_count!r}, "
            f"other_online_masters_len={alt_len!r})"
        )
