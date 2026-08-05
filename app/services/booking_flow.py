"""Booking self-booking consumer (CURSOR-19/20/23).

Sole application boundary for self-booking decisions. Synthetic reply-plan
workers call resolve helpers via DI; live VK/MAX/Telegram channels remain
unwired. Callers must use this service (composition root / injection), which
always goes through an injected eligibility flow — never dialog policy
directly and never FastAPI request state from the worker process.

Availability S2S reads (available-days / slots) run only after eligibility
allows self-booking, at most once per resolve attempt, and only through this
service.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from app.core.booking_availability_http import (
    BookingAvailabilityHttpError,
    require_calendar_date,
    require_calendar_month,
)
from app.core.booking_availability_remote import (
    AvailableDaysResult,
    AvailableSlotsResult,
)
from app.core.booking_types import (
    AvailableDaysOfferDecision,
    AvailableSlot,
    BookingClientMessageKind,
    BookingDialogAction,
    BookingDomainError,
    BookingEligibilityOutcome,
    BookingEligibilityResult,
    BookingInternalReasonCode,
    BookingPolicyDecision,
    ManagerHandoffDecision,
    SelectedMaster,
    SelectedService,
    ServiceUnavailableDecision,
    SlotOfferDecision,
)
from app.core.manager_working_hours import is_manager_working_time

logger = logging.getLogger(__name__)

_ALLOWED_CONSUMER_LOG_CODES: frozenset[str] = frozenset(
    {
        BookingInternalReasonCode.BOOKING_FLOW_UNAVAILABLE.value,
        BookingInternalReasonCode.ELIGIBILITY_CLIENT_UNAVAILABLE.value,
        BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE.value,
        BookingInternalReasonCode.ELIGIBILITY_MASTER_MISMATCH.value,
        BookingInternalReasonCode.AVAILABILITY_CLIENT_UNAVAILABLE.value,
        BookingInternalReasonCode.AVAILABILITY_REQUEST_INVALID.value,
        BookingInternalReasonCode.AVAILABILITY_SERVICE_UNAVAILABLE.value,
        BookingInternalReasonCode.NO_AVAILABLE_DAYS.value,
        BookingInternalReasonCode.UNKNOWN_OUTCOME.value,
        BookingInternalReasonCode.MALFORMED_ELIGIBILITY.value,
    }
)

_AVAILABILITY_SERVICE_CODES: frozenset[str] = frozenset(
    {
        "UNAUTHORIZED",
        "RATE_LIMITED",
        "SERVICE_UNAVAILABLE",
        "VALIDATION_ERROR",
        "TRANSPORT_ERROR",
        "TIMEOUT",
        "RESPONSE_TOO_LARGE",
        "RESPONSE_INVALID",
        "REMOTE_REJECTED",
    }
)


class BookingEligibilityFlowPort(Protocol):
    """Port for the eligibility→policy orchestrator (CURSOR-18/23)."""

    def check_eligibility(
        self,
        service: SelectedService,
        master: SelectedMaster | None,
        *,
        include_alternatives: bool,
    ) -> BookingEligibilityResult: ...

    def decide_from_eligibility(
        self,
        eligibility: BookingEligibilityResult,
        raw_slots: object,
        *,
        now: datetime,
        alternate_master_consent: bool = False,
    ) -> BookingPolicyDecision: ...

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


class BookingAvailabilityPort(Protocol):
    """Read-only availability S2S port (CURSOR-22/23)."""

    def get_available_days(
        self,
        *,
        service_id: object,
        master_id: object,
        month: object,
    ) -> AvailableDaysResult: ...

    def get_available_slots(
        self,
        *,
        service_id: object,
        master_id: object,
        date: object,
    ) -> AvailableSlotsResult: ...


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


def _manager_handoff(
    *,
    now: datetime,
    reason: BookingInternalReasonCode,
) -> ManagerHandoffDecision:
    during = is_manager_working_time(now)
    if during:
        kind = BookingClientMessageKind.HANDOFF_DURING_MANAGER_HOURS
    else:
        kind = BookingClientMessageKind.HANDOFF_OUTSIDE_MANAGER_HOURS
    return ManagerHandoffDecision(
        action=BookingDialogAction.MANAGER_HANDOFF,
        client_message_kind=kind,
        during_manager_hours=during,
        internal_reason_code=reason,
    )


def _is_known_decision(decision: object) -> bool:
    return type(decision) in (
        SlotOfferDecision,
        AvailableDaysOfferDecision,
        ManagerHandoffDecision,
        ServiceUnavailableDecision,
    )


def _map_availability_error(exc: BookingAvailabilityHttpError) -> ServiceUnavailableDecision:
    code = exc.code
    if code == "CONFIG_INVALID":
        return _manager_bound_unavailable(
            reason=BookingInternalReasonCode.AVAILABILITY_CLIENT_UNAVAILABLE,
        )
    if code == "REQUEST_INVALID":
        return _manager_bound_unavailable(
            reason=BookingInternalReasonCode.AVAILABILITY_REQUEST_INVALID,
        )
    if code in _AVAILABILITY_SERVICE_CODES:
        return _manager_bound_unavailable(
            reason=BookingInternalReasonCode.AVAILABILITY_SERVICE_UNAVAILABLE,
        )
    return _manager_bound_unavailable(
        reason=BookingInternalReasonCode.AVAILABILITY_SERVICE_UNAVAILABLE,
    )


def _eligibility_confirmed_master(
    requested: SelectedMaster | None,
    eligibility: BookingEligibilityResult,
) -> SelectedMaster | BookingInternalReasonCode:
    """Return eligibility-selected master only; never prefer caller master.

    Availability may query only ``eligibility.selected_master``. A caller
    master that differs from that selection is fail-closed (no silent swap,
    no alternate-list auto-pick).
    """

    selected = eligibility.selected_master
    if type(selected) is not SelectedMaster:
        return BookingInternalReasonCode.MALFORMED_ELIGIBILITY
    if requested is not None:
        if type(requested) is not SelectedMaster:
            return BookingInternalReasonCode.MALFORMED_ELIGIBILITY
        if requested.master_id != selected.master_id:
            return BookingInternalReasonCode.ELIGIBILITY_MASTER_MISMATCH
    return selected


class BookingFlowService:
    """Consumer boundary: eligibility (+ optional availability) → policy decisions."""

    def __init__(
        self,
        eligibility_flow: BookingEligibilityFlowPort | None,
        availability_client: BookingAvailabilityPort | None = None,
    ) -> None:
        self._eligibility_flow = eligibility_flow
        self._availability_client = availability_client

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
        """Legacy/fixture path: eligibility once, then policy over provided slots."""

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
        return decision

    def resolve_available_days(
        self,
        service: SelectedService,
        master: SelectedMaster | None,
        month: object,
        *,
        now: datetime,
        include_alternatives: bool,
        alternate_master_consent: bool = False,
    ) -> BookingPolicyDecision:
        """Eligibility once, then one available-days read when self-booking allowed."""

        if type(service) is not SelectedService:
            raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
        if master is not None and type(master) is not SelectedMaster:
            raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
        if type(include_alternatives) is not bool:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
        if type(alternate_master_consent) is not bool:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None

        try:
            canonical_month = require_calendar_month(month)
        except Exception:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.AVAILABILITY_REQUEST_INVALID,
            )

        flow = self._eligibility_flow
        if flow is None:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.BOOKING_FLOW_UNAVAILABLE,
            )

        try:
            eligibility = flow.check_eligibility(
                service,
                master,
                include_alternatives=include_alternatives,
            )
        except Exception:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE,
            )
        if type(eligibility) is not BookingEligibilityResult:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.UNKNOWN_OUTCOME,
            )

        if eligibility.outcome is not BookingEligibilityOutcome.SELF_BOOKING_ALLOWED:
            try:
                decision = flow.decide_from_eligibility(
                    eligibility,
                    (),
                    now=now,
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
            return decision

        confirmed = _eligibility_confirmed_master(master, eligibility)
        if type(confirmed) is BookingInternalReasonCode:
            return _manager_bound_unavailable(reason=confirmed)
        if type(confirmed) is not SelectedMaster:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.MALFORMED_ELIGIBILITY,
            )

        availability = self._availability_client
        if availability is None:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.AVAILABILITY_CLIENT_UNAVAILABLE,
            )

        try:
            remote = availability.get_available_days(
                service_id=service.service_id,
                master_id=confirmed.master_id,
                month=canonical_month,
            )
        except BookingAvailabilityHttpError as exc:
            return _map_availability_error(exc)
        except Exception:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.AVAILABILITY_SERVICE_UNAVAILABLE,
            )

        if type(remote) is not AvailableDaysResult:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.AVAILABILITY_SERVICE_UNAVAILABLE,
            )
        if not remote.date_keys:
            return _manager_handoff(
                now=now,
                reason=BookingInternalReasonCode.NO_AVAILABLE_DAYS,
            )
        return AvailableDaysOfferDecision(
            action=BookingDialogAction.OFFER_DAYS,
            date_keys=remote.date_keys,
            studio_today=remote.studio_today,
        )

    def resolve_available_slots(
        self,
        service: SelectedService,
        master: SelectedMaster | None,
        date: object,
        *,
        now: datetime,
        include_alternatives: bool,
        alternate_master_consent: bool = False,
    ) -> BookingPolicyDecision:
        """Eligibility once, then one slots read when self-booking allowed."""

        if type(service) is not SelectedService:
            raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
        if master is not None and type(master) is not SelectedMaster:
            raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
        if type(include_alternatives) is not bool:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
        if type(alternate_master_consent) is not bool:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None

        try:
            canonical_date = require_calendar_date(date)
        except Exception:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.AVAILABILITY_REQUEST_INVALID,
            )

        flow = self._eligibility_flow
        if flow is None:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.BOOKING_FLOW_UNAVAILABLE,
            )

        try:
            eligibility = flow.check_eligibility(
                service,
                master,
                include_alternatives=include_alternatives,
            )
        except Exception:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.ELIGIBILITY_SERVICE_UNAVAILABLE,
            )
        if type(eligibility) is not BookingEligibilityResult:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.UNKNOWN_OUTCOME,
            )

        if eligibility.outcome is not BookingEligibilityOutcome.SELF_BOOKING_ALLOWED:
            try:
                decision = flow.decide_from_eligibility(
                    eligibility,
                    (),
                    now=now,
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
            return decision

        confirmed = _eligibility_confirmed_master(master, eligibility)
        if type(confirmed) is BookingInternalReasonCode:
            return _manager_bound_unavailable(reason=confirmed)
        if type(confirmed) is not SelectedMaster:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.MALFORMED_ELIGIBILITY,
            )

        availability = self._availability_client
        if availability is None:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.AVAILABILITY_CLIENT_UNAVAILABLE,
            )

        try:
            remote = availability.get_available_slots(
                service_id=service.service_id,
                master_id=confirmed.master_id,
                date=canonical_date,
            )
        except BookingAvailabilityHttpError as exc:
            return _map_availability_error(exc)
        except Exception:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.AVAILABILITY_SERVICE_UNAVAILABLE,
            )

        if type(remote) is not AvailableSlotsResult:
            return _manager_bound_unavailable(
                reason=BookingInternalReasonCode.AVAILABILITY_SERVICE_UNAVAILABLE,
            )

        slots: tuple[AvailableSlot, ...] = remote.slots
        try:
            decision = flow.decide_from_eligibility(
                eligibility,
                slots,
                now=now,
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
        return decision
