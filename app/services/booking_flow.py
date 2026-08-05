"""Booking self-booking consumer (CURSOR-19).

Prepared application boundary for the next channel-wiring gate. Not yet invoked
from live channels, inbound, worker, or outbound. Callers that form a
self-booking decision must use this service (via DI), which always goes through
an injected eligibility flow — never dialog policy directly and never FastAPI
application state.

No channel adapters, outbound, worker loops, DB writes, or live HTTP.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from app.core.booking_types import (
    BookingClientMessageKind,
    BookingDialogAction,
    BookingDomainError,
    BookingInternalReasonCode,
    BookingPolicyDecision,
    ManagerHandoffDecision,
    SelectedMaster,
    SelectedService,
    ServiceUnavailableDecision,
    SlotOfferDecision,
)

logger = logging.getLogger(__name__)

_ALLOWED_CONSUMER_LOG_CODES: frozenset[str] = frozenset(
    {
        BookingInternalReasonCode.BOOKING_FLOW_UNAVAILABLE.value,
        BookingInternalReasonCode.ELIGIBILITY_CLIENT_UNAVAILABLE.value,
        BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE.value,
        BookingInternalReasonCode.UNKNOWN_OUTCOME.value,
    }
)


class BookingEligibilityFlowPort(Protocol):
    """Port for the CURSOR-18 eligibility→policy orchestrator."""

    def resolve(
        self,
        service: SelectedService,
        master: SelectedMaster | None,
        raw_slots: object,
        *,
        now: datetime,
        include_alternatives: bool,
        alternate_master_consent: bool = False,
    ) -> BookingPolicyDecision: ...


def _log_consumer_event(event: str, code: str) -> None:
    if type(event) is not str or not event:
        return
    if type(code) is not str or code not in _ALLOWED_CONSUMER_LOG_CODES:
        return
    try:
        logger.info("%s code=%s", event, code)
    except Exception:
        return


def _manager_bound_unavailable(
    *,
    reason: BookingInternalReasonCode,
) -> ServiceUnavailableDecision:
    """Fail-closed manager-bound result without calling dialog policy."""

    _log_consumer_event("booking_flow_consumer_fail_closed", reason.value)
    return ServiceUnavailableDecision(
        action=BookingDialogAction.SERVICE_UNAVAILABLE,
        client_message_kind=BookingClientMessageKind.SERVICE_TEMPORARILY_UNAVAILABLE,
        internal_reason_code=reason,
    )


def _is_known_decision(decision: object) -> bool:
    return type(decision) in (
        SlotOfferDecision,
        ManagerHandoffDecision,
        ServiceUnavailableDecision,
    )


class BookingFlowService:
    """Consumer: self-booking decisions only via eligibility flow.resolve."""

    def __init__(self, eligibility_flow: BookingEligibilityFlowPort | None) -> None:
        self._eligibility_flow = eligibility_flow

    def resolve(
        self,
        service: SelectedService,
        master: SelectedMaster | None,
        raw_slots: object,
        *,
        now: datetime,
        include_alternatives: bool,
        alternate_master_consent: bool = False,
    ) -> BookingPolicyDecision:
        """Gate self-booking through eligibility flow exactly once.

        ``include_alternatives`` must be chosen explicitly (no default here).
        ``OFFER_SLOTS`` continues the booking flow; ``MANAGER_HANDOFF`` and
        ``SERVICE_UNAVAILABLE`` are returned as manager-bound results with
        action and internal reason preserved.
        """

        if type(service) is not SelectedService:
            raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
        if master is not None and type(master) is not SelectedMaster:
            raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
        if type(include_alternatives) is not bool:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
        if type(alternate_master_consent) is not bool:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None

        flow = self._eligibility_flow
        if flow is None:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.BOOKING_FLOW_UNAVAILABLE,
            )

        try:
            decision = flow.resolve(
                service,
                master,
                raw_slots,
                now=now,
                include_alternatives=include_alternatives,
                alternate_master_consent=alternate_master_consent,
            )
        except Exception:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE,
            )

        if not _is_known_decision(decision):
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.UNKNOWN_OUTCOME,
            )

        # Passthrough: preserve action + internal_reason_code for manager-bound
        # outcomes; OFFER_SLOTS continues self-booking with offered slots.
        return decision
