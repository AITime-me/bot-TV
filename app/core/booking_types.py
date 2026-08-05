"""Closed booking-domain types for bot-TV (CURSOR-15).

Pure dialog contracts only. No HTTP adapter, persona wiring, channels,
or production pipeline integration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

_ALLOWED_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "BOOKING_DOMAIN_CONFIG_INVALID",
        "BOOKING_DOMAIN_VALUE_INVALID",
        "BOOKING_DOMAIN_POLICY_INVALID",
    }
)

MAX_OFFERED_SLOTS: Final[int] = 3
_MAX_ID_LENGTH: Final[int] = 128


class BookingDomainError(RuntimeError):
    """Fail-closed booking domain error. Message is a fixed code only."""

    def __init__(self, code: object) -> None:
        if type(code) is not str or code not in _ALLOWED_ERROR_CODES:
            super().__init__("BOOKING_DOMAIN_CONFIG_INVALID")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "BOOKING_DOMAIN_CONFIG_INVALID"


class BookingEligibilityOutcome(StrEnum):
    """Public eligibility outcomes for bot dialog policy."""

    SELF_BOOKING_ALLOWED = "SELF_BOOKING_ALLOWED"
    MANAGER_HANDOFF = "MANAGER_HANDOFF"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


class BookingInternalReasonCode(StrEnum):
    """Technical reason codes. Never render into client-facing text."""

    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    MALFORMED_ELIGIBILITY = "MALFORMED_ELIGIBILITY"
    NO_VALID_SLOTS = "NO_VALID_SLOTS"
    ALTERNATE_MASTER_WITHOUT_CONSENT = "ALTERNATE_MASTER_WITHOUT_CONSENT"
    ELIGIBILITY_MANAGER_HANDOFF = "ELIGIBILITY_MANAGER_HANDOFF"
    ELIGIBILITY_SERVICE_UNAVAILABLE = "ELIGIBILITY_SERVICE_UNAVAILABLE"


class BookingDialogAction(StrEnum):
    OFFER_SLOTS = "OFFER_SLOTS"
    MANAGER_HANDOFF = "MANAGER_HANDOFF"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


class BookingClientMessageKind(StrEnum):
    """Stable client message kinds. Copy never embeds internal reason codes."""

    OFFER_SLOTS = "OFFER_SLOTS"
    HANDOFF_DURING_MANAGER_HOURS = "HANDOFF_DURING_MANAGER_HOURS"
    HANDOFF_OUTSIDE_MANAGER_HOURS = "HANDOFF_OUTSIDE_MANAGER_HOURS"
    SERVICE_TEMPORARILY_UNAVAILABLE = "SERVICE_TEMPORARILY_UNAVAILABLE"


_CLIENT_MESSAGE_TEXT: Final[dict[BookingClientMessageKind, str]] = {
    BookingClientMessageKind.OFFER_SLOTS: (
        "Могу предложить ближайшие свободные окна. Выберите удобное время."
    ),
    BookingClientMessageKind.HANDOFF_DURING_MANAGER_HOURS: (
        "Передаю ваш запрос менеджеру. Он скоро свяжется с вами."
    ),
    BookingClientMessageKind.HANDOFF_OUTSIDE_MANAGER_HOURS: (
        "Передаю ваш запрос менеджеру. Он свяжется с вами в ближайшее рабочее время."
    ),
    BookingClientMessageKind.SERVICE_TEMPORARILY_UNAVAILABLE: (
        "Сейчас не могу завершить запись самостоятельно. "
        "Передаю ваш запрос менеджеру."
    ),
}


def _require_non_empty_id(name: str, value: object) -> str:
    if type(value) is not str:
        raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
    if not value or len(value) > _MAX_ID_LENGTH:
        raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
    if any(ch.isspace() for ch in value):
        raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
    return value


def _require_aware_datetime(name: str, value: object) -> datetime:
    if not isinstance(value, datetime):
        raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
    if value.tzinfo is None or value.utcoffset() is None:
        raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
    return value


def _require_exact_outcome(value: object) -> BookingEligibilityOutcome:
    if type(value) is not BookingEligibilityOutcome:
        raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
    return value


def _require_optional_internal_reason(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is BookingInternalReasonCode:
        return value.value
    if type(value) is not str or not value or len(value) > _MAX_ID_LENGTH:
        raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
    if any(ch.isspace() for ch in value):
        raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
    return value


@dataclass(frozen=True, slots=True)
class SelectedService:
    """Chosen service identity. No client name or phone."""

    service_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "service_id", _require_non_empty_id("service_id", self.service_id)
        )


@dataclass(frozen=True, slots=True)
class SelectedMaster:
    """Chosen master identity. No client name or phone."""

    master_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "master_id", _require_non_empty_id("master_id", self.master_id)
        )


@dataclass(frozen=True, slots=True)
class BookingEligibilityResult:
    """Normalized eligibility for pure dialog policy. No PII fields."""

    outcome: BookingEligibilityOutcome
    selected_service: SelectedService | None = None
    selected_master: SelectedMaster | None = None
    other_online_master_ids: tuple[str, ...] = ()
    internal_reason_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcome", _require_exact_outcome(self.outcome))
        if self.selected_service is not None and type(self.selected_service) is not SelectedService:
            raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
        if self.selected_master is not None and type(self.selected_master) is not SelectedMaster:
            raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
        if type(self.other_online_master_ids) is not tuple:
            raise BookingDomainError("BOOKING_DOMAIN_VALUE_INVALID") from None
        normalized_ids: list[str] = []
        for item in self.other_online_master_ids:
            normalized_ids.append(_require_non_empty_id("other_online_master_id", item))
        object.__setattr__(self, "other_online_master_ids", tuple(normalized_ids))
        object.__setattr__(
            self,
            "internal_reason_code",
            _require_optional_internal_reason(self.internal_reason_code),
        )


@dataclass(frozen=True, slots=True)
class AvailableSlot:
    """Backend-provided bookable slot. Times are never invented by the bot."""

    slot_id: str
    starts_at: datetime
    master_id: str
    service_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "slot_id", _require_non_empty_id("slot_id", self.slot_id)
        )
        object.__setattr__(
            self, "starts_at", _require_aware_datetime("starts_at", self.starts_at)
        )
        object.__setattr__(
            self, "master_id", _require_non_empty_id("master_id", self.master_id)
        )
        object.__setattr__(
            self, "service_id", _require_non_empty_id("service_id", self.service_id)
        )


@dataclass(frozen=True, slots=True)
class SlotOfferDecision:
    """Decision to offer backend-provided slots (never invented)."""

    action: BookingDialogAction
    offered_slots: tuple[AvailableSlot, ...]
    client_message_kind: BookingClientMessageKind

    def __post_init__(self) -> None:
        if self.action is not BookingDialogAction.OFFER_SLOTS:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
        if type(self.offered_slots) is not tuple:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
        if not self.offered_slots or len(self.offered_slots) > MAX_OFFERED_SLOTS:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
        for slot in self.offered_slots:
            if type(slot) is not AvailableSlot:
                raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
        if self.client_message_kind is not BookingClientMessageKind.OFFER_SLOTS:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None


@dataclass(frozen=True, slots=True)
class ManagerHandoffDecision:
    """Decision to hand the dialog to a manager. No invented slots."""

    action: BookingDialogAction
    client_message_kind: BookingClientMessageKind
    during_manager_hours: bool
    internal_reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.action is not BookingDialogAction.MANAGER_HANDOFF:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
        if type(self.during_manager_hours) is not bool:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
        allowed_kinds = {
            BookingClientMessageKind.HANDOFF_DURING_MANAGER_HOURS,
            BookingClientMessageKind.HANDOFF_OUTSIDE_MANAGER_HOURS,
        }
        if self.client_message_kind not in allowed_kinds:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
        if self.during_manager_hours:
            expected = BookingClientMessageKind.HANDOFF_DURING_MANAGER_HOURS
        else:
            expected = BookingClientMessageKind.HANDOFF_OUTSIDE_MANAGER_HOURS
        if self.client_message_kind is not expected:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
        object.__setattr__(
            self,
            "internal_reason_code",
            _require_optional_internal_reason(self.internal_reason_code),
        )


@dataclass(frozen=True, slots=True)
class ServiceUnavailableDecision:
    """Fail-closed / unavailable path. Must not claim master or service closed."""

    action: BookingDialogAction
    client_message_kind: BookingClientMessageKind
    internal_reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.action is not BookingDialogAction.SERVICE_UNAVAILABLE:
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
        if (
            self.client_message_kind
            is not BookingClientMessageKind.SERVICE_TEMPORARILY_UNAVAILABLE
        ):
            raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
        object.__setattr__(
            self,
            "internal_reason_code",
            _require_optional_internal_reason(self.internal_reason_code),
        )


BookingPolicyDecision = (
    SlotOfferDecision | ManagerHandoffDecision | ServiceUnavailableDecision
)


def render_client_message(kind: BookingClientMessageKind) -> str:
    """Return fixed client-safe copy for a message kind.

    Never includes internal reason codes, flags, or closure causes.
    """

    if type(kind) is not BookingClientMessageKind:
        raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
    text = _CLIENT_MESSAGE_TEXT.get(kind)
    if type(text) is not str or not text:
        raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
    return text


def client_message_for_decision(decision: BookingPolicyDecision) -> str:
    """Render the client-facing text for a policy decision."""

    if type(decision) not in (
        SlotOfferDecision,
        ManagerHandoffDecision,
        ServiceUnavailableDecision,
    ):
        raise BookingDomainError("BOOKING_DOMAIN_POLICY_INVALID") from None
    return render_client_message(decision.client_message_kind)
