"""Self-booking confirmed-create execution (SELF-BOOKING-COMMAND-02).

claim READY → fence validate → purpose-bound PII read →
confirm_selected_slot_for_conversation → terminal / retry.

Never mints a new idempotency_key. Plaintext PII stays inside this boundary.
No dialog admission, ReplyPlan confirmation, CRM/n8n, or online-zapis-tv edits.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Callable, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.booking_availability_remote import require_canonical_booking_starts_at
from app.core.booking_create_remote import (
    BookingCreateConfirmedResult,
    BookingCreateMachineOutcome,
    BookingCreateRejectedResult,
    parse_bot_slot_id,
)
from app.core.booking_types import AvailableSlot
from app.core.ephemeral_pii_types import (
    EphemeralPiiKind,
    EphemeralPiiPurpose,
    EphemeralPiiReference,
)
from app.core.self_booking_create_types import (
    SelfBookingCreateExecutionOutcome,
    SelfBookingCreateExecutionResult,
    SelfBookingCreatePendingState,
)
from app.db.clock import db_statement_now
from app.models.self_booking_create_pending import SelfBookingCreatePending
from app.repositories import self_booking_create_pendings as pending_repo
from app.services.booking_flow import (
    BookingFlowService,
    ClientRefResolverPort,
    confirm_selected_slot_for_conversation,
)
from app.services.self_booking_create_pending import SelfBookingCreatePendingService

logger = logging.getLogger(__name__)

# Client self-booking purpose (not master S2S). Phone + name share it.
_SELF_BOOKING_PII_PURPOSE: EphemeralPiiPurpose = (
    EphemeralPiiPurpose.BOOKING_PHONE_WRITE
)

_ALLOWED_LOG_CODES: frozenset[str] = frozenset(
    {
        "SELF_BOOKING_EXEC_SUCCEEDED",
        "SELF_BOOKING_EXEC_FAILED",
        "SELF_BOOKING_EXEC_CANCELLED",
        "SELF_BOOKING_EXEC_EXPIRED",
        "SELF_BOOKING_EXEC_RETRY",
        "SELF_BOOKING_EXEC_CLAIM_DENIED",
        "SELF_BOOKING_EXEC_LEASE_MISMATCH",
        "SELF_BOOKING_EXEC_PII_UNAVAILABLE",
        "SELF_BOOKING_EXEC_ZERO_CREATE",
    }
)


class SelfBookingPiiStore(Protocol):
    async def read_plaintext(
        self,
        reference: EphemeralPiiReference,
        *,
        conversation_id: uuid.UUID,
        kind: EphemeralPiiKind,
        purpose: EphemeralPiiPurpose,
    ) -> str: ...

    async def delete(
        self,
        reference: EphemeralPiiReference,
        *,
        conversation_id: uuid.UUID,
        kind: EphemeralPiiKind,
        purpose: EphemeralPiiPurpose,
    ) -> None: ...


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


def _slot_from_pending(row: SelfBookingCreatePending) -> AvailableSlot:
    starts_raw = require_canonical_booking_starts_at(row.starts_at)
    parts = parse_bot_slot_id(row.slot_id)
    starts_at = datetime.fromisoformat(starts_raw)
    return AvailableSlot(
        slot_id=row.slot_id,
        starts_at=starts_at,
        master_id=parts.master_id,
        service_id=parts.service_id,
    )


class SelfBookingCreateExecutionService:
    """Execution boundary for confirmed self-booking CREATE commands."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        pending_service: SelfBookingCreatePendingService,
        booking_flow: BookingFlowService,
        client_ref_resolver: ClientRefResolverPort,
        pii_store: SelfBookingPiiStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._pending = pending_service
        self._booking_flow = booking_flow
        self._client_ref_resolver = client_ref_resolver
        self._pii = pii_store
        self._clock = clock

    async def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        return await db_statement_now(self._session)

    async def execute(
        self,
        *,
        pending_id: object,
        lease_token: object | None = None,
    ) -> SelfBookingCreateExecutionResult:
        """Claim → fences → PII → CREATE → terminal/retry. Same idempotency key."""

        claimed = await self._pending.claim_for_execution(
            pending_id=pending_id,
            lease_token=lease_token,
        )
        if claimed is None:
            pid: uuid.UUID | None
            try:
                pid = uuid.UUID(str(pending_id))
            except (ValueError, TypeError, AttributeError):
                pid = None
            if pid is not None:
                row = await pending_repo.get_by_id(self._session, pending_id=pid)
                if (
                    row is not None
                    and row.state == SelfBookingCreatePendingState.EXPIRED.value
                ):
                    _log("SELF_BOOKING_EXEC_EXPIRED")
                    return SelfBookingCreateExecutionResult(
                        outcome=SelfBookingCreateExecutionOutcome.EXPIRED,
                        pending_id=_as_uuid(row.id),
                        pending_state=SelfBookingCreatePendingState.EXPIRED,
                        result_code=row.result_code,
                        idempotency_key=row.idempotency_key,
                    )
            _log("SELF_BOOKING_EXEC_CLAIM_DENIED")
            return SelfBookingCreateExecutionResult(
                outcome=SelfBookingCreateExecutionOutcome.CLAIM_DENIED,
                pending_id=pid,
                result_code="CLAIM_DENIED",
            )

        lease = claimed.execution_lease_token
        if lease is None:
            _log("SELF_BOOKING_EXEC_CLAIM_DENIED")
            return SelfBookingCreateExecutionResult(
                outcome=SelfBookingCreateExecutionOutcome.CLAIM_DENIED,
                pending_id=_as_uuid(claimed.id),
                pending_state=SelfBookingCreatePendingState.EXECUTING,
                result_code="LEASE_MISSING",
                idempotency_key=claimed.idempotency_key,
            )

        cancelled = await self._pending.cancel_if_conversation_fences_stale(
            pending_id=claimed.id,
        )
        if cancelled:
            _log("SELF_BOOKING_EXEC_CANCELLED")
            _log("SELF_BOOKING_EXEC_ZERO_CREATE")
            refreshed = await pending_repo.get_by_id(
                self._session, pending_id=_as_uuid(claimed.id)
            )
            return SelfBookingCreateExecutionResult(
                outcome=SelfBookingCreateExecutionOutcome.CANCELLED,
                pending_id=_as_uuid(claimed.id),
                pending_state=SelfBookingCreatePendingState.CANCELLED,
                result_code=(
                    None if refreshed is None else refreshed.result_code
                ),
                idempotency_key=claimed.idempotency_key,
            )

        pii = await self._read_booking_pii(claimed)
        if pii is None:
            now = await self._now()
            ok = await pending_repo.mark_terminal(
                self._session,
                row=claimed,
                state=SelfBookingCreatePendingState.FAILED,
                result_code="PII_UNAVAILABLE",
                result_outcome=SelfBookingCreatePendingState.FAILED.value,
                now=now,
                lease_token=lease,
            )
            if not ok:
                return await self._lease_mismatch_result(claimed)
            _log("SELF_BOOKING_EXEC_PII_UNAVAILABLE")
            _log("SELF_BOOKING_EXEC_FAILED")
            _log("SELF_BOOKING_EXEC_ZERO_CREATE")
            return SelfBookingCreateExecutionResult(
                outcome=SelfBookingCreateExecutionOutcome.FAILED,
                pending_id=_as_uuid(claimed.id),
                pending_state=SelfBookingCreatePendingState.FAILED,
                result_code="PII_UNAVAILABLE",
                idempotency_key=claimed.idempotency_key,
            )
        phone, name = pii

        try:
            slot = _slot_from_pending(claimed)
        except (ValueError, TypeError):
            return await self._fail_terminal(
                claimed,
                lease=lease,
                result_code="SLOT_INVALID",
            )

        # Plaintext only for the CREATE call; never stored on pending.
        create_result = await confirm_selected_slot_for_conversation(
            self._booking_flow,
            self._client_ref_resolver,
            slot,
            conversation_id=claimed.conversation_id,
            idempotency_key=claimed.idempotency_key,
            client_name=name,
            phone=phone,
            personal_data_consent=claimed.personal_data_consent,
            offer_acknowledgement=claimed.offer_acknowledgement,
        )

        return await self._apply_create_result(
            claimed,
            lease=lease,
            create_result=create_result,
            phone_ref=claimed.phone_ref_token,
            name_ref=claimed.name_ref_token,
        )

    async def _apply_create_result(
        self,
        claimed: SelfBookingCreatePending,
        *,
        lease: uuid.UUID,
        create_result: BookingCreateConfirmedResult | BookingCreateRejectedResult,
        phone_ref: str,
        name_ref: str,
    ) -> SelfBookingCreateExecutionResult:
        now = await self._now()
        key = claimed.idempotency_key

        if isinstance(create_result, BookingCreateConfirmedResult):
            if create_result.outcome is not BookingCreateMachineOutcome.CONFIRMED:
                return await self._fail_terminal(
                    claimed, lease=lease, result_code="CREATE_OUTCOME_INVALID"
                )
            ok = await pending_repo.mark_terminal(
                self._session,
                row=claimed,
                state=SelfBookingCreatePendingState.SUCCEEDED,
                result_code="OK",
                result_outcome=SelfBookingCreatePendingState.SUCCEEDED.value,
                now=now,
                lease_token=lease,
            )
            if not ok:
                return await self._lease_mismatch_result(claimed)
            # Safe lifecycle: durable terminal first; best-effort purpose-bound
            # delete after SUCCEEDED CAS. Ciphertext also expires via TTL.
            await self._best_effort_delete_pii(
                conversation_id=_as_uuid(claimed.conversation_id),
                phone_ref=phone_ref,
                name_ref=name_ref,
            )
            _log("SELF_BOOKING_EXEC_SUCCEEDED")
            return SelfBookingCreateExecutionResult(
                outcome=SelfBookingCreateExecutionOutcome.SUCCEEDED,
                pending_id=_as_uuid(claimed.id),
                pending_state=SelfBookingCreatePendingState.SUCCEEDED,
                result_code="OK",
                idempotency_key=key,
                booking_id=create_result.booking_id,
            )

        if not isinstance(create_result, BookingCreateRejectedResult):
            return await self._fail_terminal(
                claimed, lease=lease, result_code="CREATE_RESULT_INVALID"
            )

        machine = create_result.outcome
        reason = create_result.internal_reason_code

        if machine is BookingCreateMachineOutcome.RETRY_LATER:
            ok = await pending_repo.release_to_ready(
                self._session,
                row=claimed,
                lease_token=lease,
                result_code=reason,
                now=now,
            )
            if not ok:
                return await self._lease_mismatch_result(claimed)
            _log("SELF_BOOKING_EXEC_RETRY")
            return SelfBookingCreateExecutionResult(
                outcome=SelfBookingCreateExecutionOutcome.RETRY_SCHEDULED,
                pending_id=_as_uuid(claimed.id),
                pending_state=SelfBookingCreatePendingState.READY,
                result_code=reason,
                idempotency_key=key,
            )

        # clientRef fail-closed / slot reselect / handoff / service / fail-closed
        # → terminal FAILED; action identity retained on the row (same key).
        ok = await pending_repo.mark_terminal(
            self._session,
            row=claimed,
            state=SelfBookingCreatePendingState.FAILED,
            result_code=reason,
            result_outcome=SelfBookingCreatePendingState.FAILED.value,
            now=now,
            lease_token=lease,
        )
        if not ok:
            return await self._lease_mismatch_result(claimed)
        if machine is BookingCreateMachineOutcome.FAIL_CLOSED and reason.startswith(
            "CLIENT_REF"
        ):
            _log("SELF_BOOKING_EXEC_ZERO_CREATE")
        _log("SELF_BOOKING_EXEC_FAILED")
        return SelfBookingCreateExecutionResult(
            outcome=SelfBookingCreateExecutionOutcome.FAILED,
            pending_id=_as_uuid(claimed.id),
            pending_state=SelfBookingCreatePendingState.FAILED,
            result_code=reason,
            idempotency_key=key,
        )

    async def _fail_terminal(
        self,
        claimed: SelfBookingCreatePending,
        *,
        lease: uuid.UUID,
        result_code: str,
    ) -> SelfBookingCreateExecutionResult:
        now = await self._now()
        ok = await pending_repo.mark_terminal(
            self._session,
            row=claimed,
            state=SelfBookingCreatePendingState.FAILED,
            result_code=result_code,
            result_outcome=SelfBookingCreatePendingState.FAILED.value,
            now=now,
            lease_token=lease,
        )
        if not ok:
            return await self._lease_mismatch_result(claimed)
        _log("SELF_BOOKING_EXEC_FAILED")
        return SelfBookingCreateExecutionResult(
            outcome=SelfBookingCreateExecutionOutcome.FAILED,
            pending_id=_as_uuid(claimed.id),
            pending_state=SelfBookingCreatePendingState.FAILED,
            result_code=result_code,
            idempotency_key=claimed.idempotency_key,
        )

    async def _lease_mismatch_result(
        self,
        claimed: SelfBookingCreatePending,
    ) -> SelfBookingCreateExecutionResult:
        _log("SELF_BOOKING_EXEC_LEASE_MISMATCH")
        refreshed = await pending_repo.get_by_id(
            self._session, pending_id=_as_uuid(claimed.id)
        )
        state: SelfBookingCreatePendingState | None = None
        if refreshed is not None:
            try:
                state = SelfBookingCreatePendingState(refreshed.state)
            except ValueError:
                state = None
        return SelfBookingCreateExecutionResult(
            outcome=SelfBookingCreateExecutionOutcome.LEASE_MISMATCH,
            pending_id=_as_uuid(claimed.id),
            pending_state=state,
            result_code="LEASE_MISMATCH",
            idempotency_key=claimed.idempotency_key,
        )

    async def _read_booking_pii(
        self,
        row: SelfBookingCreatePending,
    ) -> tuple[str, str] | None:
        """Non-destructive purpose-bound decrypt. Ciphertext retained for retry."""

        try:
            conversation_id = _as_uuid(row.conversation_id)
            phone = await self._pii.read_plaintext(
                EphemeralPiiReference.parse(row.phone_ref_token),
                conversation_id=conversation_id,
                kind=EphemeralPiiKind.PHONE,
                purpose=_SELF_BOOKING_PII_PURPOSE,
            )
            name = await self._pii.read_plaintext(
                EphemeralPiiReference.parse(row.name_ref_token),
                conversation_id=conversation_id,
                kind=EphemeralPiiKind.CLIENT_NAME,
                purpose=_SELF_BOOKING_PII_PURPOSE,
            )
        except Exception:
            return None
        if type(phone) is not str or not phone:
            return None
        if type(name) is not str or not name:
            return None
        return phone, name

    async def _best_effort_delete_pii(
        self,
        *,
        conversation_id: uuid.UUID,
        phone_ref: str,
        name_ref: str,
    ) -> None:
        """Purpose-bound delete after durable SUCCEEDED. Failures are ignored."""

        for token, kind in (
            (phone_ref, EphemeralPiiKind.PHONE),
            (name_ref, EphemeralPiiKind.CLIENT_NAME),
        ):
            try:
                await self._pii.delete(
                    EphemeralPiiReference.parse(token),
                    conversation_id=conversation_id,
                    kind=kind,
                    purpose=_SELF_BOOKING_PII_PURPOSE,
                )
            except Exception:
                continue
