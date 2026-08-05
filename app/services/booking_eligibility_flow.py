"""Booking eligibility → dialog policy orchestrator (CURSOR-18).

Wires an injected eligibility client into pure ``decide_booking_dialog`` so
self-booking cannot proceed without a remote check (or an explicit fail-closed
path when the client is unset). Exactly one eligibility call per decision.

No channel adapters, outbound, worker loops, or env loading.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from app.core.booking_dialog_policy import (
    decide_booking_dialog,
    normalize_eligibility_outcome,
)
from app.core.booking_types import (
    BookingDomainError,
    BookingEligibilityOutcome,
    BookingEligibilityResult,
    BookingInternalReasonCode,
    BookingPolicyDecision,
    SelectedMaster,
    SelectedService,
)

logger = logging.getLogger(__name__)

_ALLOWED_FLOW_LOG_CODES: frozenset[str] = frozenset(
    {
        BookingInternalReasonCode.ELIGIBILITY_CLIENT_UNAVAILABLE.value,
        BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE.value,
        BookingInternalReasonCode.UNKNOWN_OUTCOME.value,
        "REMOTE_REJECTED",
        "TIMEOUT",
        "TRANSPORT_ERROR",
        "RESPONSE_TOO_LARGE",
        "RESPONSE_INVALID",
        "CONFIG_INVALID",
    }
)


class BookingEligibilityPort(Protocol):
    """Minimal port for eligibility checks. Fake clients implement this."""

    def check_eligibility(
        self,
        service: SelectedService,
        master: SelectedMaster | None = None,
        *,
        include_alternatives: bool = False,
    ) -> BookingEligibilityResult: ...


def _log_flow_event(event: str, code: str) -> None:
    if type(event) is not str or not event:
        return
    if type(code) is not str or code not in _ALLOWED_FLOW_LOG_CODES:
        return
    try:
        logger.info("%s code=%s", event, code)
    except Exception:
        return


def _unavailable_eligibility(
    *,
    service: SelectedService,
    master: SelectedMaster | None,
    reason: BookingInternalReasonCode,
) -> BookingEligibilityResult:
    _log_flow_event("booking_eligibility_flow_fail_closed", reason.value)
    return normalize_eligibility_outcome(
        BookingEligibilityOutcome.SERVICE_UNAVAILABLE,
        selected_service=service,
        selected_master=master,
        other_online_master_ids=(),
        internal_reason_code=reason,
    )


class BookingEligibilityFlowService:
    """Resolve one booking dialog decision with a single eligibility check."""

    def __init__(self, client: BookingEligibilityPort | None) -> None:
        self._client = client

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
        """Check eligibility once, then apply booking dialog policy.

        ``include_alternatives`` must be chosen explicitly by the caller
        (no implicit default at this boundary).
        """

        if type(service) is not SelectedService:
            raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
        if master is not None and type(master) is not SelectedMaster:
            raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
        if type(include_alternatives) is not bool:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
        if type(alternate_master_consent) is not bool:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None

        if self._client is None:
            eligibility = _unavailable_eligibility(
                service=service,
                master=master,
                reason=BookingInternalReasonCode.ELIGIBILITY_CLIENT_UNAVAILABLE,
            )
        else:
            try:
                eligibility = self._client.check_eligibility(
                    service,
                    master,
                    include_alternatives=include_alternatives,
                )
            except Exception:
                eligibility = _unavailable_eligibility(
                    service=service,
                    master=master,
                    reason=BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE,
                )
            else:
                if type(eligibility) is not BookingEligibilityResult:
                    eligibility = _unavailable_eligibility(
                        service=service,
                        master=master,
                        reason=BookingInternalReasonCode.UNKNOWN_OUTCOME,
                    )

        return decide_booking_dialog(
            eligibility,
            raw_slots,
            now=now,
            alternate_master_consent=alternate_master_consent,
        )
