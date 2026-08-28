"""A2.3b2 trusted acquisition-source analytics durable sync types.

Poll-only contour for consumed AcquisitionEvidence owners.
Never creates deals. No phone in durable pending.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Final

from app.core.amocrm_analytics_fields import AmoCrmAnalyticsSourcePrimaryEnum

__all__ = (
    "ACQUISITION_SOURCE_ANALYTICS_LOOP",
    "ACQUISITION_SOURCE_KEY_TO_ENUM_ID",
    "ACQUISITION_SOURCE_PURPOSE",
    "DEFAULT_MAX_ATTEMPTS",
    "EXECUTION_LEASE_SECONDS",
    "FEED_CURSOR_ID",
    "PURPOSE",
    "TERMINAL_ACQUISITION_SOURCE_STATES",
    "AcquisitionSourceAnalyticsOutcome",
    "AcquisitionSourceAnalyticsResult",
    "AcquisitionSourceOwnerKind",
    "AcquisitionSourcePendingState",
    "AcquisitionSourceWireKey",
    "enum_id_for_source_key",
)

ACQUISITION_SOURCE_ANALYTICS_LOOP: Final[str] = "acquisition_source_analytics"
PURPOSE: Final[str] = "SOURCE_PRIMARY"
ACQUISITION_SOURCE_PURPOSE: Final[str] = PURPOSE
FEED_CURSOR_ID: Final[str] = "acquisition_source"
EXECUTION_LEASE_SECONDS: Final[int] = 90
DEFAULT_MAX_ATTEMPTS: Final[int] = 8

AcquisitionSourceWireKey = str

ACQUISITION_SOURCE_KEY_TO_ENUM_ID: Final[dict[str, int]] = {
    "VK_ADS": int(AmoCrmAnalyticsSourcePrimaryEnum.VK_ADS),
    "VK_CONTENT": int(AmoCrmAnalyticsSourcePrimaryEnum.VK_CONTENT),
    "YANDEX": int(AmoCrmAnalyticsSourcePrimaryEnum.YANDEX),
    "TWO_GIS": int(AmoCrmAnalyticsSourcePrimaryEnum.TWO_GIS),
}


class AcquisitionSourcePendingState(str, enum.Enum):
    """Workflow states for acquisition-source analytics pending rows."""

    DISCOVERED = "DISCOVERED"
    RESOLVING = "RESOLVING"
    APPLYING = "APPLYING"
    DONE = "DONE"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    SKIPPED = "SKIPPED"


TERMINAL_ACQUISITION_SOURCE_STATES: Final[frozenset[AcquisitionSourcePendingState]] = (
    frozenset(
        {
            AcquisitionSourcePendingState.DONE,
            AcquisitionSourcePendingState.MANUAL_REVIEW,
            AcquisitionSourcePendingState.SKIPPED,
        }
    )
)


class AcquisitionSourceOwnerKind(str, enum.Enum):
    APPOINTMENT = "APPOINTMENT"
    BOOKING_REQUEST = "BOOKING_REQUEST"


def enum_id_for_source_key(source_key: AcquisitionSourceWireKey | str) -> int:
    """Map trusted wire sourceKey → amoCRM enum id. Reject unknown keys."""

    if type(source_key) is not str or not source_key:
        raise ValueError("ACQUISITION_SOURCE_KEY_INVALID")
    enum_id = ACQUISITION_SOURCE_KEY_TO_ENUM_ID.get(source_key)
    if enum_id is None:
        raise ValueError("ACQUISITION_SOURCE_KEY_INVALID")
    return enum_id


class AcquisitionSourceAnalyticsOutcome(str, enum.Enum):
    ADVANCED = "ADVANCED"
    TERMINAL = "TERMINAL"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    CLAIM_DENIED = "CLAIM_DENIED"
    IDLE = "IDLE"
    FEED_UNAVAILABLE = "FEED_UNAVAILABLE"


@dataclass(frozen=True, slots=True, repr=False)
class AcquisitionSourceAnalyticsResult:
    outcome: AcquisitionSourceAnalyticsOutcome
    pending_id: uuid.UUID | None = None
    pending_state: AcquisitionSourcePendingState | None = None
    result_code: str | None = None

    def __repr__(self) -> str:
        return (
            "AcquisitionSourceAnalyticsResult("
            f"outcome={self.outcome.value!r}, "
            "pending_id=<redacted>, "
            f"pending_state="
            f"{None if self.pending_state is None else self.pending_state.value!r}, "
            f"result_code={self.result_code!r})"
        )
