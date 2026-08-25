"""Teya BookingRequest orchestrator types (TEYA_REQUEST_ORCHESTRATOR Phase 1).

Deterministic worker contour only. Never mixed into ReplyPlan/inbound.
Never sends client outbound messages (no OutboundArbiter send).
online-zapis remains SoT for BookingRequest; bot-TV stores workflow state + opaque request_id.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Final

__all__ = (
    "ACTIVE_TEYA_REQUEST_STATES",
    "DEFAULT_MAX_ATTEMPTS",
    "EXECUTION_LEASE_SECONDS",
    "TERMINAL_TEYA_REQUEST_STATES",
    "TEYA_REQUEST_ORCHESTRATOR_LOOP",
    "ContactRouteOutcome",
    "ContactRouteResolution",
    "TeyaRequestOrchestratorOutcome",
    "TeyaRequestOrchestratorResult",
    "TeyaRequestPendingState",
    "TransportCapability",
)

TEYA_REQUEST_ORCHESTRATOR_LOOP: Final[str] = "teya_request_orchestrator"
EXECUTION_LEASE_SECONDS: Final[int] = 90
DEFAULT_MAX_ATTEMPTS: Final[int] = 8


class TeyaRequestPendingState(str, enum.Enum):
    """Workflow states for a BookingRequest pending row."""

    DISCOVERED = "DISCOVERED"
    IDENTITY = "IDENTITY"
    CRM_READY = "CRM_READY"
    RECONCILED = "RECONCILED"
    CONTACT_ROUTE = "CONTACT_ROUTE"
    READY_TO_BOOK = "READY_TO_BOOK"
    WAITING_CONTACT = "WAITING_CONTACT"
    BOOKING = "BOOKING"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    FAIL_CLOSED = "FAIL_CLOSED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


TERMINAL_TEYA_REQUEST_STATES: Final[frozenset[TeyaRequestPendingState]] = frozenset(
    {
        TeyaRequestPendingState.DONE,
        TeyaRequestPendingState.FAIL_CLOSED,
        TeyaRequestPendingState.RECONCILIATION_REQUIRED,
    }
)

ACTIVE_TEYA_REQUEST_STATES: Final[frozenset[TeyaRequestPendingState]] = frozenset(
    s for s in TeyaRequestPendingState if s not in TERMINAL_TEYA_REQUEST_STATES
)


class ContactRouteOutcome(str, enum.Enum):
    """Business contact-route outcomes. PHONE_ONLY is success, not an error."""

    TEXT_CHANNEL_AVAILABLE = "TEXT_CHANNEL_AVAILABLE"
    PHONE_ONLY = "PHONE_ONLY"
    AMBIGUOUS_CHANNEL = "AMBIGUOUS_CHANNEL"
    NO_CONTACT_ROUTE = "NO_CONTACT_ROUTE"


class TransportCapability(enum.Flag):
    """Channel-neutral transport flags (VOICE reserved / unused in v1)."""

    NONE = 0
    TEXT_INBOUND = enum.auto()
    TEXT_OUTBOUND = enum.auto()
    VOICE = enum.auto()


@dataclass(frozen=True, slots=True, repr=False)
class ContactRouteResolution:
    outcome: ContactRouteOutcome
    conversation_id: uuid.UUID | None = None
    capabilities: TransportCapability = TransportCapability.NONE
    reason_code: str | None = None

    def __repr__(self) -> str:
        return (
            "ContactRouteResolution("
            f"outcome={self.outcome.value!r}, "
            "conversation_id=<redacted>, "
            f"capabilities={self.capabilities!r}, "
            f"reason_code={self.reason_code!r})"
        )


class TeyaRequestOrchestratorOutcome(str, enum.Enum):
    ADVANCED = "ADVANCED"
    TERMINAL = "TERMINAL"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    CLAIM_DENIED = "CLAIM_DENIED"
    IDLE = "IDLE"


@dataclass(frozen=True, slots=True, repr=False)
class TeyaRequestOrchestratorResult:
    outcome: TeyaRequestOrchestratorOutcome
    pending_id: uuid.UUID | None = None
    pending_state: TeyaRequestPendingState | None = None
    result_code: str | None = None

    def __repr__(self) -> str:
        return (
            "TeyaRequestOrchestratorResult("
            f"outcome={self.outcome.value!r}, "
            "pending_id=<redacted>, "
            f"pending_state="
            f"{None if self.pending_state is None else self.pending_state.value!r}, "
            f"result_code={self.result_code!r})"
        )
