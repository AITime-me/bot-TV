"""amoCRM identity lookup result types (IR-2).

Read-only contact discovery outcomes. No attach/create. No PII in repr.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

__all__ = (
    "AMOCRM_IDENTITY_PROVIDER",
    "AmoCrmIdentityLookupOutcome",
    "AmoCrmIdentityLookupResult",
)

AMOCRM_IDENTITY_PROVIDER = "amocrm"


class AmoCrmIdentityLookupOutcome(str, enum.Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    INVALID_INPUT = "INVALID_INPUT"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    PERMANENT_ERROR = "PERMANENT_ERROR"
    DISABLED = "DISABLED"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmIdentityLookupResult:
    """Lookup receipt. ``contact_id`` / ``contact_ids`` are technical amo ids only."""

    outcome: AmoCrmIdentityLookupOutcome
    contact_id: str | None = None
    contact_ids: tuple[str, ...] = ()
    error_code: str | None = None
    http_calls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome is AmoCrmIdentityLookupOutcome.FOUND:
            if type(self.contact_id) is not str or not self.contact_id:
                raise TypeError("FOUND requires contact_id") from None
        elif self.outcome is AmoCrmIdentityLookupOutcome.AMBIGUOUS:
            if len(self.contact_ids) < 2:
                raise TypeError("AMBIGUOUS requires >=2 contact_ids") from None
        elif self.contact_id is not None:
            raise TypeError(f"{self.outcome.value} must not carry contact_id") from None

    def __repr__(self) -> str:
        return (
            "AmoCrmIdentityLookupResult("
            f"outcome={self.outcome.value!r}, "
            f"contact_id={self.contact_id!r}, "
            f"contact_ids_count={len(self.contact_ids)}, "
            f"error_code={self.error_code!r}, "
            f"http_calls={self.http_calls!r})"
        )
