"""Safe, read-only clientRef resolver (CURSOR-??).

Downstream systems (online-zapis-tv) must address exactly one canonical
client. For this stage we resolve ``clientRef`` only from the conversation's
attached ACTIVE ``canonical_identity_id``.

clientRef encoding contract:
- If there is no dedicated persisted clientRef alias yet, we use the canonical
  UUID string itself as the opaque stable clientRef.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

__all__ = (
    "ClientRefResolutionOutcome",
    "ClientRefResolutionResult",
)


class ClientRefResolutionOutcome(str, enum.Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    REFUSED = "REFUSED"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True, slots=True, repr=False)
class ClientRefResolutionResult:
    """Typed outcome for the safe clientRef resolver boundary."""

    outcome: ClientRefResolutionOutcome
    client_ref: str | None = None
    reason_code: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.outcome is ClientRefResolutionOutcome.FOUND:
            if type(self.client_ref) is not str or not self.client_ref:
                raise TypeError("FOUND requires client_ref") from None
            if self.reason_code is not None or self.error_code is not None:
                raise TypeError("FOUND must not carry reason_code/error_code") from None
            return

        if self.outcome is ClientRefResolutionOutcome.NOT_FOUND:
            if self.client_ref is not None or self.error_code is not None:
                raise TypeError("NOT_FOUND must not carry client_ref/error_code") from None
            return

        if self.outcome is ClientRefResolutionOutcome.REFUSED:
            if self.client_ref is not None:
                raise TypeError("REFUSED must not carry client_ref") from None
            if type(self.reason_code) is not str or not self.reason_code:
                raise TypeError("REFUSED requires reason_code") from None
            if self.error_code is not None:
                raise TypeError("REFUSED must not carry error_code") from None
            return

        if self.outcome is ClientRefResolutionOutcome.INVALID_INPUT:
            if self.client_ref is not None:
                raise TypeError("INVALID_INPUT must not carry client_ref") from None
            if type(self.error_code) is not str or not self.error_code:
                raise TypeError("INVALID_INPUT requires error_code") from None
            if self.reason_code is not None:
                raise TypeError("INVALID_INPUT must not carry reason_code") from None
            return

        raise TypeError(f"Unknown outcome: {self.outcome!r}")  # pragma: no cover

    def __repr__(self) -> str:
        return (
            "ClientRefResolutionResult("
            f"outcome={self.outcome.value!r}, "
            "client_ref=<redacted>, "
            f"reason_code={self.reason_code!r}, "
            f"error_code={self.error_code!r})"
        )

