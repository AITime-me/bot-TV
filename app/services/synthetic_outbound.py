from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SyntheticOutboundOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    PERMANENT_ERROR = "PERMANENT_ERROR"


@dataclass(frozen=True, repr=False)
class SyntheticOutboundRequest:
    outbound_id: str
    conversation_id: str
    reply_plan_id: str | None
    context_version: int | None
    correlation_id: str | None
    # Payload / reply body are never included in repr/logs.
    _payload_schema: str
    # Authoritative persisted user-facing body (outbound payload ``text``).
    # None only for machine-only OFFER_DAYS envelopes.
    _text: str | None = None

    def __repr__(self) -> str:
        return (
            f"SyntheticOutboundRequest(outbound_id={self.outbound_id!r}, "
            f"conversation_id={self.conversation_id!r}, "
            f"reply_plan_id={self.reply_plan_id!r}, "
            f"context_version={self.context_version!r}, "
            f"correlation_id={self.correlation_id!r}, payload=<redacted>)"
        )


@dataclass(frozen=True)
class SyntheticOutboundResult:
    outcome: SyntheticOutboundOutcome
    error_code: str | None = None
    provider_message_id: int | None = None


class SyntheticOutboundAdapter:
    """In-process synthetic sink. No HTTP, channels, AI, or client sends."""

    def __init__(
        self,
        *,
        forced_outcome: SyntheticOutboundOutcome = SyntheticOutboundOutcome.SUCCESS,
    ) -> None:
        self._forced_outcome = forced_outcome
        self._delivered_ids: set[str] = set()
        self.calls: list[SyntheticOutboundRequest] = []

    def deliver(self, request: SyntheticOutboundRequest) -> SyntheticOutboundResult:
        # ``outbound_id`` is the transport idempotency key. A future live
        # adapter must provide the same guarantee before it can be registered.
        if request.outbound_id in self._delivered_ids:
            return SyntheticOutboundResult(outcome=SyntheticOutboundOutcome.SUCCESS)
        self.calls.append(request)
        if self._forced_outcome is SyntheticOutboundOutcome.SUCCESS:
            self._delivered_ids.add(request.outbound_id)
            return SyntheticOutboundResult(outcome=SyntheticOutboundOutcome.SUCCESS)
        if self._forced_outcome is SyntheticOutboundOutcome.TRANSIENT_ERROR:
            return SyntheticOutboundResult(
                outcome=SyntheticOutboundOutcome.TRANSIENT_ERROR,
                error_code="SYNTHETIC_TRANSIENT",
            )
        return SyntheticOutboundResult(
            outcome=SyntheticOutboundOutcome.PERMANENT_ERROR,
            error_code="SYNTHETIC_PERMANENT",
        )
