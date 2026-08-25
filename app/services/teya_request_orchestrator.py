"""Teya BookingRequest orchestrator state machine (Phase 1).

Capability boundary: NEVER sends client messages. NEVER calls OutboundArbiter
send paths. Deterministic worker role only — not mixed into ReplyPlan/inbound.

online-zapis remains SoT for BookingRequest. This module advances workflow
state on bot-TV pendings using opaque request_id + remote S2S adapters.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Callable, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.booking_request_http import BookingRequestHttpError
from app.core.booking_request_remote import (
    AppointmentsLookupOutcome,
    BotBookingRequestDto,
)
from app.core.teya_request_retry import (
    classify_remote_code,
    load_teya_retry_policy,
    TeyaRetryPolicy,
)
from app.core.amocrm_circuit_breaker import (
    ProbeClaimOutcome,
    load_amocrm_breaker_policy,
    is_breaker_failure_code,
)
from app.core.teya_request_types import (
    ContactRouteOutcome,
    TeyaRequestOrchestratorOutcome,
    TeyaRequestOrchestratorResult,
    TeyaRequestPendingState,
)
from app.db.clock import db_statement_now
from app.models.teya_request_pending import TeyaRequestPending
from app.repositories import integration_circuit_breakers as breaker_repo
from app.repositories import teya_request_pendings as pending_repo
from app.services.teya_request_contact_route import ConversationLocator
from app.core.amocrm_crm_writes_http import TASK_TEXT_DEFAULT
from app.services.teya_request_crm import (
    TeyaCrmActionOutcome,
    TeyaRequestCrmService,
    build_game_task_text,
    build_teya_crm_task_text,
    build_teya_structured_note,
)
from app.services.teya_request_pending import TeyaRequestPendingService

logger = logging.getLogger(__name__)

_ALLOWED_LOG_CODES: frozenset[str] = frozenset(
    {
        "TEYA_ORCH_ADVANCED",
        "TEYA_ORCH_TERMINAL",
        "TEYA_ORCH_RETRY",
        "TEYA_ORCH_CLAIM_DENIED",
        "TEYA_ORCH_FAIL_CLOSED",
        "TEYA_ORCH_MANUAL_REVIEW",
        "TEYA_ORCH_BREAKER_OPEN",
    }
)


class BookingRequestRemotePort(Protocol):
    def get(self, *, request_id: object) -> BotBookingRequestDto: ...

    def appointments_lookup(
        self, *, phone: object = None, client_id: object = None
    ): ...

    def book(
        self,
        *,
        request_id: object,
        starts_at: object,
        idempotency_key: object,
        service_id: object = None,
    ): ...


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
    return uuid.UUID(str(value))


class TeyaRequestOrchestratorService:
    """Advance one claimed pending through the BookingRequest workflow."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        pending_service: TeyaRequestPendingService,
        remote: BookingRequestRemotePort,
        crm: TeyaRequestCrmService | None = None,
        contact_locator: ConversationLocator | None = None,
        clock: Callable[[], datetime] | None = None,
        retry_policy: TeyaRetryPolicy | None = None,
    ) -> None:
        self._session = session
        self._pending = pending_service
        self._remote = remote
        self._crm = crm
        self._locator = contact_locator or ConversationLocator()
        self._clock = clock
        self._retry_policy = retry_policy or load_teya_retry_policy()
        self._breaker_policy = load_amocrm_breaker_policy()

    async def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        return await db_statement_now(self._session)

    async def process_claimed(
        self, row: TeyaRequestPending
    ) -> TeyaRequestOrchestratorResult:
        lease = row.lease_token
        if lease is None:
            _log("TEYA_ORCH_CLAIM_DENIED")
            return TeyaRequestOrchestratorResult(
                outcome=TeyaRequestOrchestratorOutcome.CLAIM_DENIED,
                pending_id=_as_uuid(row.id),
                result_code="LEASE_MISSING",
            )
        state = TeyaRequestPendingState(row.state)
        try:
            if state is TeyaRequestPendingState.DISCOVERED:
                return await self._step_discovered(row, lease)
            if state is TeyaRequestPendingState.IDENTITY:
                return await self._step_identity(row, lease)
            if state is TeyaRequestPendingState.CRM_READY:
                return await self._step_crm_ready(row, lease)
            if state is TeyaRequestPendingState.RECONCILED:
                return await self._step_reconciled(row, lease)
            if state is TeyaRequestPendingState.CONTACT_ROUTE:
                return await self._step_contact_route(row, lease)
            if state is TeyaRequestPendingState.READY_TO_BOOK:
                return await self._step_ready_to_book(row, lease)
            if state is TeyaRequestPendingState.WAITING_CONTACT:
                return await self._terminal_wait(row, lease)
            if state is TeyaRequestPendingState.BOOKING:
                return await self._step_booking(row, lease)
            if state is TeyaRequestPendingState.VERIFYING:
                return await self._step_verifying(row, lease)
        except BookingRequestHttpError as exc:
            return await self._handle_remote_error(row, lease, exc.code)
        return await self._fail_closed(row, lease, "STATE_UNEXPECTED")

    async def _advance(
        self,
        row: TeyaRequestPending,
        lease: uuid.UUID,
        state: TeyaRequestPendingState,
        *,
        result_code: str | None = None,
        result_outcome: str | None = None,
        contact_route_outcome: str | None = None,
        amocrm_contact_id: str | None = None,
        amocrm_deal_id: str | None = None,
        amocrm_task_id: str | None = None,
        structured_note: str | None = None,
        selected_starts_at: str | None = None,
        book_idempotency_key: str | None = None,
        clear_lease: bool = True,
    ) -> TeyaRequestOrchestratorResult:
        now = await self._now()
        ok = await pending_repo.advance_state(
            self._session,
            row=row,
            lease_token=lease,
            state=state,
            now=now,
            result_code=result_code,
            result_outcome=result_outcome,
            contact_route_outcome=contact_route_outcome,
            amocrm_contact_id=amocrm_contact_id,
            amocrm_deal_id=amocrm_deal_id,
            amocrm_task_id=amocrm_task_id,
            structured_note=structured_note,
            selected_starts_at=selected_starts_at,
            book_idempotency_key=book_idempotency_key,
            clear_lease=clear_lease,
        )
        if not ok:
            _log("TEYA_ORCH_CLAIM_DENIED")
            return TeyaRequestOrchestratorResult(
                outcome=TeyaRequestOrchestratorOutcome.CLAIM_DENIED,
                pending_id=_as_uuid(row.id),
                result_code="LEASE_MISMATCH",
            )
        terminal = state in {
            TeyaRequestPendingState.DONE,
            TeyaRequestPendingState.FAIL_CLOSED,
            TeyaRequestPendingState.RECONCILIATION_REQUIRED,
            TeyaRequestPendingState.MANUAL_REVIEW,
        }
        if terminal:
            _log("TEYA_ORCH_TERMINAL")
            return TeyaRequestOrchestratorResult(
                outcome=TeyaRequestOrchestratorOutcome.TERMINAL,
                pending_id=_as_uuid(row.id),
                pending_state=state,
                result_code=result_code,
            )
        _log("TEYA_ORCH_ADVANCED")
        return TeyaRequestOrchestratorResult(
            outcome=TeyaRequestOrchestratorOutcome.ADVANCED,
            pending_id=_as_uuid(row.id),
            pending_state=state,
            result_code=result_code,
        )

    async def _retry(
        self, row: TeyaRequestPending, lease: uuid.UUID, code: str
    ) -> TeyaRequestOrchestratorResult:
        now = await self._now()
        if row.attempt_count >= row.max_attempts:
            return await self._manual_review(row, lease, "MAX_ATTEMPTS_EXCEEDED")
        delay = self._retry_policy.delay_seconds(row.attempt_count)
        await pending_repo.release_lease(
            self._session,
            row=row,
            lease_token=lease,
            now=now,
            next_retry_at=now + timedelta(seconds=delay),
            result_code=code,
        )
        if is_breaker_failure_code(code):
            await breaker_repo.record_failure(
                self._session,
                now=now,
                policy=self._breaker_policy,
            )
        _log("TEYA_ORCH_RETRY")
        return TeyaRequestOrchestratorResult(
            outcome=TeyaRequestOrchestratorOutcome.RETRY_SCHEDULED,
            pending_id=_as_uuid(row.id),
            pending_state=TeyaRequestPendingState(row.state),
            result_code=code,
        )

    async def _manual_review(
        self, row: TeyaRequestPending, lease: uuid.UUID, code: str
    ) -> TeyaRequestOrchestratorResult:
        _log("TEYA_ORCH_MANUAL_REVIEW")
        now = await self._now()
        await pending_repo.mark_manual_review(
            self._session,
            row=row,
            now=now,
            reason=code,
            lease_token=lease,
        )
        return TeyaRequestOrchestratorResult(
            outcome=TeyaRequestOrchestratorOutcome.TERMINAL,
            pending_id=_as_uuid(row.id),
            pending_state=TeyaRequestPendingState.MANUAL_REVIEW,
            result_code=code,
        )

    async def _fail_closed(
        self, row: TeyaRequestPending, lease: uuid.UUID, code: str
    ) -> TeyaRequestOrchestratorResult:
        _log("TEYA_ORCH_FAIL_CLOSED")
        return await self._advance(
            row,
            lease,
            TeyaRequestPendingState.FAIL_CLOSED,
            result_code=code,
            result_outcome=TeyaRequestPendingState.FAIL_CLOSED.value,
        )

    async def _reconciliation(
        self, row: TeyaRequestPending, lease: uuid.UUID, code: str
    ) -> TeyaRequestOrchestratorResult:
        return await self._advance(
            row,
            lease,
            TeyaRequestPendingState.RECONCILIATION_REQUIRED,
            result_code=code,
            result_outcome=TeyaRequestPendingState.RECONCILIATION_REQUIRED.value,
        )

    async def _handle_remote_error(
        self, row: TeyaRequestPending, lease: uuid.UUID, code: str
    ) -> TeyaRequestOrchestratorResult:
        if code == "CONSULTATION_SERVICE_REQUIRED":
            return await self._advance(
                row,
                lease,
                TeyaRequestPendingState.WAITING_CONTACT,
                result_code=code,
                result_outcome=TeyaRequestPendingState.WAITING_CONTACT.value,
            )
        if code == "RECONCILIATION_REQUIRED":
            return await self._reconciliation(row, lease, code)
        kind = classify_remote_code(code)
        if kind == "RETRY":
            return await self._retry(row, lease, code)
        if kind == "MANUAL":
            return await self._manual_review(row, lease, code)
        return await self._fail_closed(row, lease, code)

    async def _guard_crm_breaker(
        self, row: TeyaRequestPending, lease: uuid.UUID
    ) -> TeyaRequestOrchestratorResult | None:
        now = await self._now()
        claim = await breaker_repo.try_claim_probe(
            self._session, now=now, policy=self._breaker_policy
        )
        if claim.outcome is ProbeClaimOutcome.ALLOWED:
            return None
        _log("TEYA_ORCH_BREAKER_OPEN")
        delay = max(
            self._breaker_policy.cooldown_seconds,
            self._breaker_policy.probe_lease_seconds,
            self._retry_policy.base_seconds,
        )
        if row.attempt_count >= row.max_attempts:
            return await self._manual_review(
                row, lease, "BREAKER_OPEN_EXHAUSTED"
            )
        code = (
            "AMOCRM_BREAKER_PROBE_BUSY"
            if claim.outcome is ProbeClaimOutcome.DENIED_PROBE_BUSY
            else "AMOCRM_BREAKER_OPEN"
        )
        await pending_repo.release_lease(
            self._session,
            row=row,
            lease_token=lease,
            now=now,
            next_retry_at=now + timedelta(seconds=delay),
            result_code=code,
        )
        return TeyaRequestOrchestratorResult(
            outcome=TeyaRequestOrchestratorOutcome.RETRY_SCHEDULED,
            pending_id=_as_uuid(row.id),
            pending_state=TeyaRequestPendingState(row.state),
            result_code=code,
        )

    async def _step_discovered(
        self, row: TeyaRequestPending, lease: uuid.UUID
    ) -> TeyaRequestOrchestratorResult:
        # Validate remote still exists; advance to IDENTITY.
        self._remote.get(request_id=str(row.request_id))
        return await self._advance(
            row, lease, TeyaRequestPendingState.IDENTITY
        )

    async def _step_identity(
        self, row: TeyaRequestPending, lease: uuid.UUID
    ) -> TeyaRequestOrchestratorResult:
        dto = self._remote.get(request_id=str(row.request_id))
        if self._crm is None:
            return await self._manual_review(row, lease, "CRM_UNBOUND")
        blocked = await self._guard_crm_breaker(row, lease)
        if blocked is not None:
            return blocked
        if not dto.phone_e164:
            return await self._fail_closed(row, lease, "PHONE_MISSING")
        try:
            from app.core.identity_resolution import (
                IdentityResolutionError,
                normalize_phone_e164,
            )

            phone_e164 = normalize_phone_e164(dto.phone_e164)
        except IdentityResolutionError:
            return await self._fail_closed(row, lease, "PHONE_INVALID")
        crm = await self._crm.ensure_contact_and_deal(
            phone_e164=phone_e164,
            client_name=dto.client_name,
        )
        if crm.outcome is TeyaCrmActionOutcome.RETRY:
            return await self._retry(row, lease, crm.error_code or "CRM_RETRY")
        if crm.outcome is TeyaCrmActionOutcome.MANUAL_REVIEW:
            return await self._manual_review(
                row, lease, crm.error_code or "CRM_MANUAL_REVIEW"
            )
        if crm.outcome is TeyaCrmActionOutcome.FAIL_CLOSED:
            return await self._fail_closed(
                row, lease, crm.error_code or "CRM_FAIL_CLOSED"
            )
        if crm.outcome is TeyaCrmActionOutcome.RECONCILIATION_REQUIRED:
            return await self._reconciliation(
                row, lease, crm.error_code or "CRM_RECONCILIATION"
            )
        now = await self._now()
        await breaker_repo.record_success(
            self._session, now=now, policy=self._breaker_policy
        )
        return await self._advance(
            row,
            lease,
            TeyaRequestPendingState.CRM_READY,
            amocrm_contact_id=crm.contact_id,
            amocrm_deal_id=crm.deal_id,
        )

    async def _step_crm_ready(
        self, row: TeyaRequestPending, lease: uuid.UUID
    ) -> TeyaRequestOrchestratorResult:
        dto = self._remote.get(request_id=str(row.request_id))
        deal_id = row.amocrm_deal_id
        if self._crm is None:
            return await self._manual_review(row, lease, "CRM_UNBOUND")
        if not deal_id:
            return await self._fail_closed(row, lease, "CRM_DEAL_MISSING")
        blocked = await self._guard_crm_breaker(row, lease)
        if blocked is not None:
            return blocked
        note = build_teya_structured_note(dto)
        task_text = build_teya_crm_task_text(dto, appointment_id=None)
        attached = await self._crm.attach_note_and_task(
            deal_id=deal_id, note_text=note, task_text=task_text
        )
        if attached.outcome is TeyaCrmActionOutcome.RETRY:
            return await self._retry(
                row, lease, attached.error_code or "CRM_NOTE_RETRY"
            )
        if attached.outcome is TeyaCrmActionOutcome.MANUAL_REVIEW:
            return await self._manual_review(
                row, lease, attached.error_code or "CRM_NOTE_MANUAL"
            )
        if attached.outcome is TeyaCrmActionOutcome.FAIL_CLOSED:
            return await self._fail_closed(
                row, lease, attached.error_code or "CRM_NOTE_FAIL"
            )
        if attached.outcome is TeyaCrmActionOutcome.RECONCILIATION_REQUIRED:
            return await self._reconciliation(
                row, lease, attached.error_code or "CRM_NOTE_RECON"
            )
        now = await self._now()
        await breaker_repo.record_success(
            self._session, now=now, policy=self._breaker_policy
        )
        return await self._advance(
            row,
            lease,
            TeyaRequestPendingState.RECONCILED,
            amocrm_task_id=attached.task_id,
            structured_note=note,
        )

    async def _step_reconciled(
        self, row: TeyaRequestPending, lease: uuid.UUID
    ) -> TeyaRequestOrchestratorResult:
        dto = self._remote.get(request_id=str(row.request_id))
        if dto.game_context is not None:
            if not dto.phone_e164:
                return await self._fail_closed(row, lease, "PHONE_MISSING")
            lookup = self._remote.appointments_lookup(phone=dto.phone_e164)
            if lookup.outcome is AppointmentsLookupOutcome.AMBIGUOUS:
                if self._crm is not None and row.amocrm_deal_id:
                    await self._crm.attach_note_and_task(
                        deal_id=row.amocrm_deal_id,
                        note_text="GAME_APPOINTMENTS_AMBIGUOUS",
                        task_text=TASK_TEXT_DEFAULT,
                    )
                return await self._reconciliation(
                    row, lease, "GAME_APPOINTMENTS_AMBIGUOUS"
                )
            if lookup.outcome is AppointmentsLookupOutcome.UNIQUE:
                if self._crm is not None and row.amocrm_deal_id:
                    task_text = build_game_task_text(
                        gift=dto.game_context.gift,
                        procedure=dto.game_context.procedure,
                        appointment_id=lookup.appointment_id,
                    )
                    attached = await self._crm.attach_note_and_task(
                        deal_id=row.amocrm_deal_id,
                        note_text=build_teya_structured_note(dto),
                        task_text=task_text,
                    )
                    if attached.outcome is TeyaCrmActionOutcome.RECONCILIATION_REQUIRED:
                        return await self._reconciliation(
                            row, lease, attached.error_code or "GAME_TASK_RECON"
                        )
                return await self._advance(
                    row,
                    lease,
                    TeyaRequestPendingState.CONTACT_ROUTE,
                    result_code="GAME_SELF_BOOKED",
                    amocrm_task_id=None,
                )
            # NONE — game without booking; refresh task text
            if self._crm is not None and row.amocrm_deal_id:
                task_text = build_game_task_text(
                    gift=dto.game_context.gift,
                    procedure=dto.game_context.procedure,
                    appointment_id=None,
                )
                await self._crm.attach_note_and_task(
                    deal_id=row.amocrm_deal_id,
                    note_text=build_teya_structured_note(dto),
                    task_text=task_text,
                )
        return await self._advance(
            row, lease, TeyaRequestPendingState.CONTACT_ROUTE
        )

    async def _step_contact_route(
        self, row: TeyaRequestPending, lease: uuid.UUID
    ) -> TeyaRequestOrchestratorResult:
        dto = self._remote.get(request_id=str(row.request_id))
        route = await self._locator.resolve(canonical_identity_id=None)
        # PHONE_ONLY is success; never outbound.
        if route.outcome is ContactRouteOutcome.AMBIGUOUS_CHANNEL:
            return await self._fail_closed(row, lease, "CONTACT_ROUTE_AMBIGUOUS")
        if route.outcome is ContactRouteOutcome.NO_CONTACT_ROUTE:
            return await self._fail_closed(row, lease, "NO_CONTACT_ROUTE")

        # Game UNIQUE self-booking already booked → DONE after contact route.
        if row.result_code == "GAME_SELF_BOOKED":
            return await self._advance(
                row,
                lease,
                TeyaRequestPendingState.DONE,
                contact_route_outcome=route.outcome.value,
                result_code="GAME_SELF_BOOKED",
                result_outcome=TeyaRequestPendingState.DONE.value,
            )

        if dto.service_id and dto.master_id:
            next_state = TeyaRequestPendingState.READY_TO_BOOK
        else:
            next_state = TeyaRequestPendingState.WAITING_CONTACT
            if dto.request_type == "CONSULTATION_REQUEST" and not dto.service_id:
                return await self._advance(
                    row,
                    lease,
                    TeyaRequestPendingState.WAITING_CONTACT,
                    contact_route_outcome=route.outcome.value,
                    result_code="CONSULTATION_SERVICE_REQUIRED",
                    result_outcome=TeyaRequestPendingState.WAITING_CONTACT.value,
                )
        return await self._advance(
            row,
            lease,
            next_state,
            contact_route_outcome=route.outcome.value,
            result_code=(
                "NEEDS_SLOT_SELECTION"
                if next_state is TeyaRequestPendingState.WAITING_CONTACT
                else None
            ),
        )

    async def _terminal_wait(
        self, row: TeyaRequestPending, lease: uuid.UUID
    ) -> TeyaRequestOrchestratorResult:
        # Waiting for operator/slot; release lease without advancing.
        if row.attempt_count >= row.max_attempts:
            return await self._manual_review(row, lease, "MAX_ATTEMPTS_EXCEEDED")
        now = await self._now()
        await pending_repo.release_lease(
            self._session,
            row=row,
            lease_token=lease,
            now=now,
            next_retry_at=now + timedelta(seconds=300),
            result_code=row.result_code or "WAITING_CONTACT",
        )
        return TeyaRequestOrchestratorResult(
            outcome=TeyaRequestOrchestratorOutcome.RETRY_SCHEDULED,
            pending_id=_as_uuid(row.id),
            pending_state=TeyaRequestPendingState.WAITING_CONTACT,
            result_code=row.result_code,
        )

    async def _step_ready_to_book(
        self, row: TeyaRequestPending, lease: uuid.UUID
    ) -> TeyaRequestOrchestratorResult:
        if not row.selected_starts_at:
            return await self._advance(
                row,
                lease,
                TeyaRequestPendingState.WAITING_CONTACT,
                result_code="NEEDS_SLOT_SELECTION",
                result_outcome=TeyaRequestPendingState.WAITING_CONTACT.value,
            )
        key = row.book_idempotency_key or str(uuid.uuid4())
        return await self._advance(
            row,
            lease,
            TeyaRequestPendingState.BOOKING,
            book_idempotency_key=key,
        )

    async def _step_booking(
        self, row: TeyaRequestPending, lease: uuid.UUID
    ) -> TeyaRequestOrchestratorResult:
        dto = self._remote.get(request_id=str(row.request_id))
        if not row.selected_starts_at or not row.book_idempotency_key:
            return await self._fail_closed(row, lease, "BOOKING_INPUT_MISSING")
        try:
            self._remote.book(
                request_id=str(row.request_id),
                starts_at=row.selected_starts_at,
                idempotency_key=row.book_idempotency_key,
                service_id=dto.service_id,
            )
        except BookingRequestHttpError as exc:
            return await self._handle_remote_error(row, lease, exc.code)
        return await self._advance(
            row, lease, TeyaRequestPendingState.VERIFYING
        )

    async def _step_verifying(
        self, row: TeyaRequestPending, lease: uuid.UUID
    ) -> TeyaRequestOrchestratorResult:
        dto = self._remote.get(request_id=str(row.request_id))
        if not dto.appointment_id:
            return await self._reconciliation(row, lease, "BOOK_POSTCHECK_MISSING")
        # online-zapis closes only after successful verified book.
        if dto.status != "CLOSED":
            return await self._reconciliation(row, lease, "BOOK_POSTCHECK_STATUS")
        return await self._advance(
            row,
            lease,
            TeyaRequestPendingState.DONE,
            result_code="BOOKED",
            result_outcome=TeyaRequestPendingState.DONE.value,
        )
