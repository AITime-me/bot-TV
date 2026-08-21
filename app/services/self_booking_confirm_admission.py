"""Confirm → self-booking pending orchestration (SELF-BOOKING-COMMAND-03K1).

CONFIRM_SELECTED_SLOT → active offer → PII admission map → refs alive →
admit_confirmed pending. Mints one caller-owned idempotency_key on create.

No plaintext PII, no PII decrypt, no CREATE HTTP, no remote booking write,
no reply-plan changes, no ingress wiring.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Callable, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.self_booking_active_offer_types import ActiveOfferResolveOutcome
from app.core.self_booking_confirm_admission_types import (
    SelfBookingConfirmAdmissionOutcome,
    SelfBookingConfirmAdmissionResult,
)
from app.core.self_booking_create_types import (
    SelfBookingCreateAdmitOutcome,
    normalize_confirm_external_message_id,
    require_self_booking_channel,
)
from app.core.self_booking_pii_admission_types import require_pii_admission_request_id
from app.repositories import self_booking_create_pendings as pending_repo
from app.repositories import self_booking_pii_admissions as admission_repo
from app.schemas.self_booking_confirm_action import SyntheticConfirmSelectedSlotAction
from app.services.self_booking_active_offer import SelfBookingActiveOfferService
from app.services.self_booking_create_pending import SelfBookingCreatePendingService

logger = logging.getLogger(__name__)

_ALLOWED_LOG_CODES: frozenset[str] = frozenset(
    {
        "CONFIRM_ADMISSION_ADMITTED",
        "CONFIRM_ADMISSION_DUPLICATE",
        "CONFIRM_ADMISSION_OFFER_NOT_ACTIVE",
        "CONFIRM_ADMISSION_PII_NOT_FOUND",
        "CONFIRM_ADMISSION_PII_EXPIRED",
        "CONFIRM_ADMISSION_HANDOFF_BLOCKED",
        "CONFIRM_ADMISSION_FAIL_CLOSED",
    }
)


class ConfirmAdmissionPiiStore(Protocol):
    """Alive-check only. Orchestration path must not decrypt PII."""

    async def booking_phone_write_pair_alive(
        self,
        session: AsyncSession,
        *,
        phone_ref_token: str,
        name_ref_token: str,
        conversation_id: UUID,
    ) -> bool: ...


def _log(event: str) -> None:
    if type(event) is not str or event not in _ALLOWED_LOG_CODES:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return


def _as_uuid(value: object) -> uuid.UUID:
    if type(value) is uuid.UUID:
        return value
    if isinstance(value, uuid.UUID):
        return uuid.UUID(str(value))
    return uuid.UUID(str(value))


def _fail_closed(*, reason_code: str) -> SelfBookingConfirmAdmissionResult:
    _log("CONFIRM_ADMISSION_FAIL_CLOSED")
    return SelfBookingConfirmAdmissionResult(
        outcome=SelfBookingConfirmAdmissionOutcome.FAIL_CLOSED,
        reason_code=reason_code,
    )


class SelfBookingConfirmAdmissionService:
    """Internal orchestration: confirm action → durable create pending."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        pii_store: ConfirmAdmissionPiiStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._pii = pii_store
        self._offers = SelfBookingActiveOfferService(session, clock=clock)
        self._pendings = SelfBookingCreatePendingService(session, clock=clock)

    async def admit_from_confirm(
        self,
        *,
        conversation_id: object,
        channel: object,
        confirm_external_message_id: object,
        action: SyntheticConfirmSelectedSlotAction,
        fence_context_version: object,
        fence_manager_epoch: object,
        fence_event_seq_hwm: object,
    ) -> SelfBookingConfirmAdmissionResult:
        """Bind CONFIRM_SELECTED_SLOT to a single self-booking pending."""

        try:
            cid = _as_uuid(conversation_id)
            ch = require_self_booking_channel(channel)
            confirm_id = normalize_confirm_external_message_id(
                confirm_external_message_id
            )
            if type(action) is not SyntheticConfirmSelectedSlotAction:
                raise TypeError("CONFIRM_ACTION_INVALID") from None
            request_id = require_pii_admission_request_id(
                action.pii_admission_request_id
            )
            slot_id = action.slot_id
            if type(slot_id) is not str or not slot_id:
                raise ValueError("CONFIRM_SLOT_INVALID") from None
        except (ValueError, TypeError, AttributeError):
            return _fail_closed(reason_code="INVALID_INPUT")

        # Exact active-offer membership — no implicit latest offer.
        offer = await self._offers.resolve_slot(
            conversation_id=cid, slot_id=slot_id
        )
        if offer.outcome is not ActiveOfferResolveOutcome.FOUND:
            _log("CONFIRM_ADMISSION_OFFER_NOT_ACTIVE")
            return SelfBookingConfirmAdmissionResult(
                outcome=SelfBookingConfirmAdmissionOutcome.OFFER_NOT_ACTIVE,
                reason_code="OFFER_NOT_ACTIVE",
            )
        starts_at = offer.starts_at
        if type(starts_at) is not str or not starts_at:
            return _fail_closed(reason_code="OFFER_STARTS_AT_MISSING")

        # Exact (conversation_id, pii_admission_request_id) — no latest fallback.
        admission = await admission_repo.get_by_request(
            self._session,
            conversation_id=cid,
            request_id=request_id,
        )
        if admission is None:
            _log("CONFIRM_ADMISSION_PII_NOT_FOUND")
            return SelfBookingConfirmAdmissionResult(
                outcome=SelfBookingConfirmAdmissionOutcome.PII_NOT_FOUND,
                reason_code="PII_NOT_FOUND",
            )

        try:
            alive = await self._pii.booking_phone_write_pair_alive(
                self._session,
                phone_ref_token=admission.phone_ref_token,
                name_ref_token=admission.name_ref_token,
                conversation_id=cid,
            )
        except Exception:
            return _fail_closed(reason_code="PII_ALIVE_CHECK_FAILED")
        if not alive:
            _log("CONFIRM_ADMISSION_PII_EXPIRED")
            return SelfBookingConfirmAdmissionResult(
                outcome=SelfBookingConfirmAdmissionOutcome.PII_EXPIRED,
                reason_code="PII_EXPIRED",
            )

        # Safe duplicate return before minting a new key.
        existing = await pending_repo.get_by_confirm(
            self._session,
            channel=ch,
            confirm_external_message_id=confirm_id,
        )
        if existing is not None:
            _log("CONFIRM_ADMISSION_DUPLICATE")
            return SelfBookingConfirmAdmissionResult(
                outcome=SelfBookingConfirmAdmissionOutcome.DUPLICATE,
                pending_id=_as_uuid(existing.id),
                idempotency_key=existing.idempotency_key,
                reason_code="CONFIRM_DUPLICATE",
            )

        # Caller-owned key minted once for this pending create path.
        idempotency_key = str(uuid.uuid4())

        admitted = await self._pendings.admit_confirmed(
            conversation_id=cid,
            channel=ch,
            confirm_external_message_id=confirm_id,
            slot_id=slot_id,
            starts_at=starts_at,
            fence_context_version=fence_context_version,
            fence_manager_epoch=fence_manager_epoch,
            fence_event_seq_hwm=fence_event_seq_hwm,
            personal_data_consent=action.personal_data_consent,
            offer_acknowledgement=action.offer_acknowledgement,
            phone_ref_token=admission.phone_ref_token,
            name_ref_token=admission.name_ref_token,
            idempotency_key=idempotency_key,
        )

        if admitted.outcome is SelfBookingCreateAdmitOutcome.ADMITTED:
            _log("CONFIRM_ADMISSION_ADMITTED")
            return SelfBookingConfirmAdmissionResult(
                outcome=SelfBookingConfirmAdmissionOutcome.ADMITTED,
                pending_id=admitted.pending_id,
                idempotency_key=admitted.idempotency_key,
            )
        if admitted.outcome is SelfBookingCreateAdmitOutcome.DUPLICATE:
            _log("CONFIRM_ADMISSION_DUPLICATE")
            return SelfBookingConfirmAdmissionResult(
                outcome=SelfBookingConfirmAdmissionOutcome.DUPLICATE,
                pending_id=admitted.pending_id,
                idempotency_key=admitted.idempotency_key,
                reason_code=admitted.reason_code or "CONFIRM_DUPLICATE",
            )
        if admitted.outcome is SelfBookingCreateAdmitOutcome.HANDOFF_BLOCKED:
            _log("CONFIRM_ADMISSION_HANDOFF_BLOCKED")
            return SelfBookingConfirmAdmissionResult(
                outcome=SelfBookingConfirmAdmissionOutcome.HANDOFF_BLOCKED,
                reason_code=admitted.reason_code or "HANDOFF_OR_TAKEOVER",
            )
        return _fail_closed(
            reason_code=admitted.reason_code or admitted.outcome.value
        )
