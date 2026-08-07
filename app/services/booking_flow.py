"""Booking self-booking consumer (CURSOR-19/20/23/25).

Sole application boundary for self-booking decisions. Synthetic reply-plan
workers call resolve helpers via DI; live VK/MAX/Telegram channels remain
unwired. Callers must use this service (composition root / injection), which
always goes through an injected eligibility flow — never dialog policy
directly and never FastAPI request state from the worker process.

Availability S2S reads (available-days / slots) run only after eligibility
allows self-booking, at most once per resolve attempt, and only through this
service.

Confirmed-slot create (CURSOR-25) is available via ``confirm_selected_slot``
for a future PII-safe durable command. Live dialog invocation stays unwired;
synthetic reply-plan never carries clientName/phone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol

from app.core.booking_availability_http import (
    BookingAvailabilityHttpError,
    require_calendar_date,
    require_calendar_month,
)
from app.core.booking_availability_remote import (
    AvailableDaysResult,
    AvailableSlotsResult,
    format_canonical_booking_starts_at,
    require_canonical_booking_starts_at,
)
from app.core.booking_create_http import BookingCreateHttpError
from app.core.booking_create_remote import (
    BookingCreateApplicationResult,
    BookingCreateConfirmedResult,
    BookingCreateMachineOutcome,
    BookingCreateRejectedResult,
    BookingCreateRemoteSuccess,
    assert_success_matches_available_slot,
    build_booking_create_remote_request,
    parse_bot_slot_id,
    require_canonical_idempotency_key,
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
from app.core.manager_working_hours import MANAGER_TIMEZONE_NAME, is_manager_working_time

logger = logging.getLogger(__name__)

_STUDIO_TZ = timezone(timedelta(hours=5), name=MANAGER_TIMEZONE_NAME)

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
        "BOOKING_CREATE_CLIENT_UNAVAILABLE",
        "BOOKING_CREATE_REQUEST_INVALID",
        "BOOKING_CREATE_FAIL_CLOSED",
        "BOOKING_CREATE_RETRY_LATER",
        "BOOKING_CREATE_SLOT_RESELECT",
        "BOOKING_CREATE_MANAGER_HANDOFF",
        "BOOKING_CREATE_SERVICE_UNAVAILABLE",
        "BOOKING_CREATE_CONFIRMED",
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

_CREATE_RETRY_LATER_CODES: frozenset[str] = frozenset(
    {
        "RATE_LIMITED",
        "IDEMPOTENCY_IN_PROGRESS",
        "INTERNAL_ERROR",
        "TIMEOUT",
        "TRANSPORT_ERROR",
        "RESPONSE_TOO_LARGE",
    }
)

_CREATE_SLOT_RESELECT_CODES: frozenset[str] = frozenset(
    {
        "SLOT_NO_LONGER_AVAILABLE",
        "BOOKING_CONFLICT",
        # BOOKING_REQUEST_CONFLICT is intentionally NOT here: backend has not
        # defined it as a selected-slot conflict; classify fail closed (ADR-016).
    }
)

_CREATE_SERVICE_UNAVAILABLE_CODES: frozenset[str] = frozenset(
    {
        "SERVICE_UNAVAILABLE",
        "MASTER_UNAVAILABLE",
    }
)

_CREATE_MANAGER_HANDOFF_CODES: frozenset[str] = frozenset(
    {
        "CLIENT_AMBIGUOUS",
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


class BookingCreatePort(Protocol):
    """Write booking-create S2S port (CURSOR-25). Exactly one HTTP call per invoke."""

    def create_booking(
        self,
        *,
        idempotency_key: object,
        slot_id: object,
        client_name: object,
        phone: object,
        personal_data_consent: object,
        offer_acknowledgement: object,
    ) -> BookingCreateRemoteSuccess: ...


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


def _parse_studio_starts_at(canonical: str) -> datetime:
    require_canonical_booking_starts_at(canonical)
    year = int(canonical[0:4])
    month = int(canonical[5:7])
    day = int(canonical[8:10])
    hour = int(canonical[11:13])
    minute = int(canonical[14:16])
    return datetime(year, month, day, hour, minute, 0, tzinfo=_STUDIO_TZ)


def _create_rejected(
    *,
    outcome: BookingCreateMachineOutcome,
    reason: str,
    idempotency_key: str,
    log_code: str,
) -> BookingCreateRejectedResult:
    _log_consumer_event("booking_create_consumer", log_code)
    return BookingCreateRejectedResult(
        outcome=outcome,
        internal_reason_code=reason,
        idempotency_key=idempotency_key,
    )


def _map_create_http_error(
    exc: BookingCreateHttpError,
    *,
    idempotency_key: str,
) -> BookingCreateRejectedResult:
    code = exc.code
    if code in _CREATE_RETRY_LATER_CODES:
        return _create_rejected(
            outcome=BookingCreateMachineOutcome.RETRY_LATER,
            reason=code,
            idempotency_key=idempotency_key,
            log_code="BOOKING_CREATE_RETRY_LATER",
        )
    if code in _CREATE_SLOT_RESELECT_CODES:
        return _create_rejected(
            outcome=BookingCreateMachineOutcome.SLOT_RESELECT_REQUIRED,
            reason=code,
            idempotency_key=idempotency_key,
            log_code="BOOKING_CREATE_SLOT_RESELECT",
        )
    if code in _CREATE_MANAGER_HANDOFF_CODES:
        return _create_rejected(
            outcome=BookingCreateMachineOutcome.MANAGER_HANDOFF,
            reason=code,
            idempotency_key=idempotency_key,
            log_code="BOOKING_CREATE_MANAGER_HANDOFF",
        )
    if code in _CREATE_SERVICE_UNAVAILABLE_CODES:
        return _create_rejected(
            outcome=BookingCreateMachineOutcome.SERVICE_UNAVAILABLE,
            reason=code,
            idempotency_key=idempotency_key,
            log_code="BOOKING_CREATE_SERVICE_UNAVAILABLE",
        )
    return _create_rejected(
        outcome=BookingCreateMachineOutcome.FAIL_CLOSED,
        reason=code,
        idempotency_key=idempotency_key,
        log_code="BOOKING_CREATE_FAIL_CLOSED",
    )


class BookingFlowService:
    """Consumer boundary: eligibility (+ optional availability/create) → outcomes."""

    def __init__(
        self,
        eligibility_flow: BookingEligibilityFlowPort | None,
        availability_client: BookingAvailabilityPort | None = None,
        booking_create_client: BookingCreatePort | None = None,
    ) -> None:
        self._eligibility_flow = eligibility_flow
        self._availability_client = availability_client
        self._booking_create_client = booking_create_client

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

    def confirm_selected_slot(
        self,
        slot: AvailableSlot,
        *,
        idempotency_key: object,
        client_name: object,
        phone: object,
        personal_data_consent: object,
        offer_acknowledgement: object,
    ) -> BookingCreateApplicationResult:
        """Confirm a backend-provided slot via at most one create S2S call.

        Never invents slotId/serviceId/masterId. Never generates a new
        idempotencyKey. Never returns CONFIRMED without a validated bookingId.
        Idempotent replay remains CONFIRMED. Does not wire live channels or
        store PII in synthetic reply-plan.
        """

        try:
            key = require_canonical_idempotency_key(idempotency_key)
        except ValueError:
            return _create_rejected(
                outcome=BookingCreateMachineOutcome.FAIL_CLOSED,
                reason="REQUEST_INVALID",
                idempotency_key="00000000-0000-4000-8000-000000000000",
                log_code="BOOKING_CREATE_REQUEST_INVALID",
            )

        if type(slot) is not AvailableSlot:
            return _create_rejected(
                outcome=BookingCreateMachineOutcome.FAIL_CLOSED,
                reason="REQUEST_INVALID",
                idempotency_key=key,
                log_code="BOOKING_CREATE_REQUEST_INVALID",
            )

        if personal_data_consent is not True or offer_acknowledgement is not True:
            return _create_rejected(
                outcome=BookingCreateMachineOutcome.FAIL_CLOSED,
                reason="REQUEST_INVALID",
                idempotency_key=key,
                log_code="BOOKING_CREATE_REQUEST_INVALID",
            )

        try:
            parsed_slot = parse_bot_slot_id(slot.slot_id)
            expected_starts = format_canonical_booking_starts_at(slot.starts_at)
            if parsed_slot.service_id != slot.service_id:
                raise ValueError("BOOKING_CREATE_SLOT_MISMATCH") from None
            if parsed_slot.master_id != slot.master_id:
                raise ValueError("BOOKING_CREATE_SLOT_MISMATCH") from None
            if expected_starts[0:10] != parsed_slot.date_key:
                raise ValueError("BOOKING_CREATE_SLOT_MISMATCH") from None
            if expected_starts[11:16] != parsed_slot.start_time:
                raise ValueError("BOOKING_CREATE_SLOT_MISMATCH") from None
            remote_request = build_booking_create_remote_request(
                idempotency_key=key,
                slot_id=slot.slot_id,
                client_name=client_name,
                phone=phone,
                personal_data_consent=True,
                offer_acknowledgement=True,
            )
        except ValueError:
            return _create_rejected(
                outcome=BookingCreateMachineOutcome.FAIL_CLOSED,
                reason="REQUEST_INVALID",
                idempotency_key=key,
                log_code="BOOKING_CREATE_REQUEST_INVALID",
            )

        create_client = self._booking_create_client
        if create_client is None:
            return _create_rejected(
                outcome=BookingCreateMachineOutcome.FAIL_CLOSED,
                reason="CONFIG_INVALID",
                idempotency_key=key,
                log_code="BOOKING_CREATE_CLIENT_UNAVAILABLE",
            )

        try:
            remote = create_client.create_booking(
                idempotency_key=remote_request.idempotency_key,
                slot_id=remote_request.slot_id,
                client_name=remote_request.client_name,
                phone=remote_request.phone,
                personal_data_consent=True,
                offer_acknowledgement=True,
            )
        except BookingCreateHttpError as exc:
            return _map_create_http_error(exc, idempotency_key=key)
        except Exception:
            # Untyped / unexpected failures are defects, not proven transient
            # transport errors. Never classify them as RETRY_LATER.
            return _create_rejected(
                outcome=BookingCreateMachineOutcome.FAIL_CLOSED,
                reason="UNEXPECTED_ERROR",
                idempotency_key=key,
                log_code="BOOKING_CREATE_FAIL_CLOSED",
            )

        if type(remote) is not BookingCreateRemoteSuccess:
            return _create_rejected(
                outcome=BookingCreateMachineOutcome.FAIL_CLOSED,
                reason="RESPONSE_INVALID",
                idempotency_key=key,
                log_code="BOOKING_CREATE_FAIL_CLOSED",
            )

        try:
            assert_success_matches_available_slot(success=remote, slot=slot)
            starts_at = _parse_studio_starts_at(remote.starts_at)
        except ValueError:
            return _create_rejected(
                outcome=BookingCreateMachineOutcome.FAIL_CLOSED,
                reason="RESPONSE_INVALID",
                idempotency_key=key,
                log_code="BOOKING_CREATE_FAIL_CLOSED",
            )

        if type(remote.booking_id) is not str or not remote.booking_id:
            return _create_rejected(
                outcome=BookingCreateMachineOutcome.FAIL_CLOSED,
                reason="RESPONSE_INVALID",
                idempotency_key=key,
                log_code="BOOKING_CREATE_FAIL_CLOSED",
            )

        _log_consumer_event("booking_create_consumer", "BOOKING_CREATE_CONFIRMED")
        return BookingCreateConfirmedResult(
            outcome=BookingCreateMachineOutcome.CONFIRMED,
            booking_id=remote.booking_id,
            slot_id=remote.slot_id,
            starts_at=starts_at,
            idempotent_replay=remote.idempotent_replay,
            idempotency_key=key,
        )
