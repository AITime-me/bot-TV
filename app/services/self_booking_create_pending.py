"""Self-booking confirmed-create pending foundation (SELF-BOOKING-COMMAND-01).

Admission / claim / fence-cancel only. Never reads plaintext PII.
Never calls Booking CREATE HTTP or dialog runtime.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.self_booking_create_types import (
    DEFAULT_MAX_ATTEMPTS,
    EXECUTION_LEASE_SECONDS,
    SelfBookingCreateAdmitOutcome,
    SelfBookingCreateAdmitResult,
    SelfBookingCreatePendingState,
    SelfBookingCreateSafeSelection,
    normalize_confirm_external_message_id,
    require_caller_idempotency_key,
    require_nonnegative_int,
    require_opaque_pii_ref_token,
    require_positive_max_attempts,
    require_self_booking_channel,
    require_true_consent,
)
from app.db.clock import db_statement_now
from app.models.conversation import conversation_allows_automatic_reply
from app.models.self_booking_create_pending import SelfBookingCreatePending
from app.repositories import conversations as conversation_repo
from app.repositories import self_booking_create_pendings as pending_repo

logger = logging.getLogger(__name__)

_ALLOWED_LOG_CODES: frozenset[str] = frozenset(
    {
        "SELF_BOOKING_ADMITTED",
        "SELF_BOOKING_DUPLICATE",
        "SELF_BOOKING_ACTIVE_EXISTS",
        "SELF_BOOKING_INVALID_INPUT",
        "SELF_BOOKING_FENCE_STALE",
        "SELF_BOOKING_HANDOFF_BLOCKED",
        "SELF_BOOKING_CLAIMED",
        "SELF_BOOKING_CLAIM_DENIED",
        "SELF_BOOKING_CANCELLED_STALE",
        "SELF_BOOKING_EXPIRED_ATTEMPTS",
    }
)


def _log(event: str) -> None:
    if type(event) is not str or event not in _ALLOWED_LOG_CODES:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return


class SelfBookingCreatePendingService:
    """Durable foundation boundary for confirmed self-booking create commands."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._clock = clock

    async def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        return await db_statement_now(self._session)

    async def admit_confirmed(
        self,
        *,
        conversation_id: object,
        channel: object,
        confirm_external_message_id: object,
        slot_id: object,
        starts_at: object,
        fence_context_version: object,
        fence_manager_epoch: object,
        fence_event_seq_hwm: object,
        personal_data_consent: object,
        offer_acknowledgement: object,
        phone_ref_token: object,
        name_ref_token: object,
        idempotency_key: object,
        max_attempts: object = DEFAULT_MAX_ATTEMPTS,
    ) -> SelfBookingCreateAdmitResult:
        """Admit a confirmed create command. Dedupe on confirmation message id."""

        try:
            cid = uuid.UUID(str(conversation_id))
            ch = require_self_booking_channel(channel)
            confirm_id = normalize_confirm_external_message_id(
                confirm_external_message_id
            )
            if type(slot_id) is not str or type(starts_at) is not str:
                raise ValueError("SELF_BOOKING_SLOT_INVALID") from None
            selection = SelfBookingCreateSafeSelection(
                slot_id=slot_id,
                starts_at=starts_at,
            )
            fence_cv = require_nonnegative_int(
                fence_context_version, code="SELF_BOOKING_FENCE_INVALID"
            )
            fence_me = require_nonnegative_int(
                fence_manager_epoch, code="SELF_BOOKING_FENCE_INVALID"
            )
            fence_hwm = require_nonnegative_int(
                fence_event_seq_hwm, code="SELF_BOOKING_FENCE_INVALID"
            )
            require_true_consent(personal_data_consent, field="consent")
            require_true_consent(offer_acknowledgement, field="offer")
            phone_ref = require_opaque_pii_ref_token(phone_ref_token)
            name_ref = require_opaque_pii_ref_token(name_ref_token)
            key = require_caller_idempotency_key(idempotency_key)
            attempts_cap = require_positive_max_attempts(max_attempts)
        except (ValueError, TypeError):
            _log("SELF_BOOKING_INVALID_INPUT")
            return SelfBookingCreateAdmitResult(
                outcome=SelfBookingCreateAdmitOutcome.INVALID_INPUT,
                reason_code="INVALID_INPUT",
            )

        existing = await pending_repo.get_by_confirm(
            self._session,
            channel=ch,
            confirm_external_message_id=confirm_id,
        )
        if existing is not None:
            _log("SELF_BOOKING_DUPLICATE")
            return SelfBookingCreateAdmitResult(
                outcome=SelfBookingCreateAdmitOutcome.DUPLICATE,
                pending_id=existing.id,
                idempotency_key=existing.idempotency_key,
                reason_code="CONFIRM_DUPLICATE",
            )

        conversation = await conversation_repo.get_by_id_for_update(
            self._session,
            conversation_id=cid,
        )
        if conversation is None:
            _log("SELF_BOOKING_INVALID_INPUT")
            return SelfBookingCreateAdmitResult(
                outcome=SelfBookingCreateAdmitOutcome.CONVERSATION_MISSING,
                reason_code="CONVERSATION_MISSING",
            )

        if not conversation_allows_automatic_reply(conversation):
            _log("SELF_BOOKING_HANDOFF_BLOCKED")
            return SelfBookingCreateAdmitResult(
                outcome=SelfBookingCreateAdmitOutcome.HANDOFF_BLOCKED,
                reason_code="HANDOFF_OR_TAKEOVER",
            )

        if (
            conversation.context_version != fence_cv
            or conversation.manager_epoch != fence_me
            or conversation.current_event_seq != fence_hwm
        ):
            _log("SELF_BOOKING_FENCE_STALE")
            return SelfBookingCreateAdmitResult(
                outcome=SelfBookingCreateAdmitOutcome.FENCE_STALE,
                reason_code="FENCE_STALE",
            )

        active = await pending_repo.lock_active_by_conversation(
            self._session,
            conversation_id=cid,
        )
        if active is not None:
            _log("SELF_BOOKING_ACTIVE_EXISTS")
            return SelfBookingCreateAdmitResult(
                outcome=SelfBookingCreateAdmitOutcome.ACTIVE_EXISTS,
                pending_id=active.id,
                idempotency_key=active.idempotency_key,
                reason_code="ACTIVE_PENDING_EXISTS",
            )

        now = await self._now()
        row_id = uuid.uuid4()
        try:
            row = await pending_repo.insert_pending(
                self._session,
                row_id=row_id,
                conversation_id=cid,
                channel=ch,
                confirm_external_message_id=confirm_id,
                state=SelfBookingCreatePendingState.READY,
                command_version=1,
                attempt_count=0,
                max_attempts=attempts_cap,
                idempotency_key=key,
                slot_id=selection.slot_id,
                starts_at=selection.starts_at,
                fence_context_version=fence_cv,
                fence_manager_epoch=fence_me,
                fence_event_seq_hwm=fence_hwm,
                phone_ref_token=phone_ref,
                name_ref_token=name_ref,
                now=now,
            )
        except IntegrityError:
            raced = await pending_repo.get_by_confirm(
                self._session,
                channel=ch,
                confirm_external_message_id=confirm_id,
            )
            if raced is not None:
                _log("SELF_BOOKING_DUPLICATE")
                return SelfBookingCreateAdmitResult(
                    outcome=SelfBookingCreateAdmitOutcome.DUPLICATE,
                    pending_id=raced.id,
                    idempotency_key=raced.idempotency_key,
                    reason_code="CONFIRM_DUPLICATE",
                )
            active_raced = await pending_repo.lock_active_by_conversation(
                self._session,
                conversation_id=cid,
            )
            if active_raced is not None:
                _log("SELF_BOOKING_ACTIVE_EXISTS")
                return SelfBookingCreateAdmitResult(
                    outcome=SelfBookingCreateAdmitOutcome.ACTIVE_EXISTS,
                    pending_id=active_raced.id,
                    idempotency_key=active_raced.idempotency_key,
                    reason_code="ACTIVE_PENDING_EXISTS",
                )
            _log("SELF_BOOKING_INVALID_INPUT")
            return SelfBookingCreateAdmitResult(
                outcome=SelfBookingCreateAdmitOutcome.INVALID_INPUT,
                reason_code="INSERT_CONFLICT",
            )

        _log("SELF_BOOKING_ADMITTED")
        return SelfBookingCreateAdmitResult(
            outcome=SelfBookingCreateAdmitOutcome.ADMITTED,
            pending_id=row.id,
            idempotency_key=row.idempotency_key,
        )

    async def claim_for_execution(
        self,
        *,
        pending_id: object,
        lease_token: object | None = None,
    ) -> SelfBookingCreatePending | None:
        """Claim READY pending for execution. Returns refreshed row or None."""

        try:
            pid = uuid.UUID(str(pending_id))
            token = (
                uuid.UUID(str(lease_token))
                if lease_token is not None
                else uuid.uuid4()
            )
        except (ValueError, TypeError, AttributeError):
            _log("SELF_BOOKING_CLAIM_DENIED")
            return None

        row = await pending_repo.get_by_id(self._session, pending_id=pid)
        if row is None:
            _log("SELF_BOOKING_CLAIM_DENIED")
            return None

        now = await self._now()
        if row.state == SelfBookingCreatePendingState.READY.value:
            if row.attempt_count >= row.max_attempts:
                await pending_repo.expire_exhausted_attempts(
                    self._session, row=row, now=now
                )
                _log("SELF_BOOKING_EXPIRED_ATTEMPTS")
                return None
            ok = await pending_repo.claim_for_execution(
                self._session,
                row=row,
                lease_token=token,
                lease_expires_at=now + timedelta(seconds=EXECUTION_LEASE_SECONDS),
                expected_version=row.command_version,
                now=now,
            )
        elif row.state == SelfBookingCreatePendingState.EXECUTING.value:
            lease_expired = (
                row.execution_lease_expires_at is not None
                and row.execution_lease_expires_at <= now
            )
            if lease_expired and row.attempt_count >= row.max_attempts:
                await pending_repo.mark_terminal(
                    self._session,
                    row=row,
                    state=SelfBookingCreatePendingState.EXPIRED,
                    result_code="MAX_ATTEMPTS_EXCEEDED",
                    result_outcome=SelfBookingCreatePendingState.EXPIRED.value,
                    now=now,
                )
                _log("SELF_BOOKING_EXPIRED_ATTEMPTS")
                return None
            ok = await pending_repo.reclaim_expired_execution(
                self._session,
                row=row,
                lease_token=token,
                lease_expires_at=now + timedelta(seconds=EXECUTION_LEASE_SECONDS),
                expected_version=row.command_version,
                now=now,
            )
        else:
            _log("SELF_BOOKING_CLAIM_DENIED")
            return None

        if not ok:
            _log("SELF_BOOKING_CLAIM_DENIED")
            return None

        refreshed = await pending_repo.get_by_id(self._session, pending_id=pid)
        if refreshed is None:
            _log("SELF_BOOKING_CLAIM_DENIED")
            return None
        _log("SELF_BOOKING_CLAIMED")
        return refreshed

    async def cancel_if_conversation_fences_stale(
        self,
        *,
        pending_id: object,
    ) -> bool:
        """Cancel active pending when conversation fences/ownership diverge."""

        try:
            pid = uuid.UUID(str(pending_id))
        except (ValueError, TypeError, AttributeError):
            return False

        row = await pending_repo.get_by_id(self._session, pending_id=pid)
        if row is None:
            return False
        if row.state not in {
            SelfBookingCreatePendingState.READY.value,
            SelfBookingCreatePendingState.EXECUTING.value,
        }:
            return False

        conversation = await conversation_repo.get_by_id_for_update(
            self._session,
            conversation_id=row.conversation_id,
        )
        if conversation is None:
            now = await self._now()
            cancelled = await pending_repo.mark_terminal(
                self._session,
                row=row,
                state=SelfBookingCreatePendingState.CANCELLED,
                result_code="CONVERSATION_MISSING",
                result_outcome=SelfBookingCreatePendingState.CANCELLED.value,
                now=now,
            )
            if cancelled:
                _log("SELF_BOOKING_CANCELLED_STALE")
            return cancelled

        stale = (
            not conversation_allows_automatic_reply(conversation)
            or conversation.context_version != row.fence_context_version
            or conversation.manager_epoch != row.fence_manager_epoch
            or conversation.current_event_seq != row.fence_event_seq_hwm
        )
        if not stale:
            return False

        now = await self._now()
        cancelled = await pending_repo.mark_terminal(
            self._session,
            row=row,
            state=SelfBookingCreatePendingState.CANCELLED,
            result_code="FENCE_STALE_OR_TAKEOVER",
            result_outcome=SelfBookingCreatePendingState.CANCELLED.value,
            now=now,
        )
        if cancelled:
            _log("SELF_BOOKING_CANCELLED_STALE")
        return cancelled
