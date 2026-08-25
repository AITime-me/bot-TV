"""Independent Teya request reconciliation (verify-only; no blind creates)."""

from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.booking_request_remote import AppointmentsLookupOutcome
from app.core.teya_request_types import TeyaRequestPendingState
from app.db.clock import db_statement_now
from app.db.session import session_scope
from app.models.teya_request_pending import TeyaRequestPending
from app.repositories import teya_request_pendings as pending_repo
from app.services.teya_request_crm import (
    TeyaCrmActionOutcome,
    TeyaRequestCrmService,
    build_teya_crm_task_text,
    build_teya_structured_note,
)
from app.services.teya_request_pending import TeyaRequestPendingService

logger = logging.getLogger(__name__)

_ALLOWED: frozenset[str] = frozenset(
    {
        "TEYA_RECON_SCAN",
        "TEYA_RECON_REPAIRED",
        "TEYA_RECON_MANUAL",
        "TEYA_RECON_EMPTY",
        "TEYA_RECON_CRM",
        "TEYA_RECON_PARTIAL",
    }
)

_BOOKING_RECON_STATES = (
    TeyaRequestPendingState.VERIFYING.value,
    TeyaRequestPendingState.BOOKING.value,
    TeyaRequestPendingState.RECONCILIATION_REQUIRED.value,
    TeyaRequestPendingState.IDENTITY.value,
    TeyaRequestPendingState.CRM_READY.value,
)

_CRM_RECON_STATES = (
    TeyaRequestPendingState.IDENTITY.value,
    TeyaRequestPendingState.CRM_READY.value,
    TeyaRequestPendingState.RECONCILIATION_REQUIRED.value,
    TeyaRequestPendingState.MANUAL_REVIEW.value,
)


def _log(event: str) -> None:
    if event not in _ALLOWED:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return


def _is_operator_owned(row: TeyaRequestPending) -> bool:
    """MANUAL_REVIEW or exhausted retry budget — operator-owned, no reopen to claim."""

    return (
        row.state == TeyaRequestPendingState.MANUAL_REVIEW.value
        or int(row.attempt_count) >= int(row.max_attempts)
    )


class _Remote(Protocol):
    def get(self, *, request_id: object): ...

    def appointments_lookup(self, *, phone: object = None, client_id: object = None): ...


class TeyaRequestReconciliationWorker:
    """Bounded verify-only repairs for the Teya request contour."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        remote: _Remote | None,
        crm: TeyaRequestCrmService | None = None,
        batch_size: int = 20,
    ) -> None:
        self._session_factory = session_factory
        self._remote = remote
        self._crm = crm
        self._batch_size = batch_size

    async def tick(self) -> int:
        if self._remote is None:
            return 0
        repaired = 0
        async with session_scope(self._session_factory) as session:
            now = await db_statement_now(session)
            await pending_repo.expire_exhausted_to_manual_review(
                session, now=now
            )
            rows = (
                await session.scalars(
                    select(TeyaRequestPending)
                    .where(
                        or_(
                            TeyaRequestPending.state.in_(_BOOKING_RECON_STATES),
                            TeyaRequestPending.state.in_(_CRM_RECON_STATES),
                        )
                    )
                    .order_by(TeyaRequestPending.updated_at.asc())
                    .limit(self._batch_size)
                )
            ).all()
            if not rows:
                _log("TEYA_RECON_EMPTY")
                return 0
            _log("TEYA_RECON_SCAN")
            pending = TeyaRequestPendingService(session)
            for row in rows:
                if await self._reconcile_one(session, pending, row):
                    repaired += 1
        return repaired

    async def _reconcile_one(
        self,
        session: AsyncSession,
        pending: TeyaRequestPendingService,
        row: TeyaRequestPending,
    ) -> bool:
        assert self._remote is not None
        try:
            dto = self._remote.get(request_id=str(row.request_id))
        except Exception:
            return False

        now = await db_statement_now(session)

        if (
            dto.status == "CLOSED"
            and dto.appointment_id
            and row.state
            in {
                TeyaRequestPendingState.BOOKING.value,
                TeyaRequestPendingState.VERIFYING.value,
                TeyaRequestPendingState.RECONCILIATION_REQUIRED.value,
                TeyaRequestPendingState.READY_TO_BOOK.value,
                TeyaRequestPendingState.WAITING_CONTACT.value,
                TeyaRequestPendingState.CONTACT_ROUTE.value,
                TeyaRequestPendingState.RECONCILED.value,
                TeyaRequestPendingState.CRM_READY.value,
                TeyaRequestPendingState.IDENTITY.value,
                TeyaRequestPendingState.DISCOVERED.value,
                TeyaRequestPendingState.MANUAL_REVIEW.value,
            }
        ):
            row.state = TeyaRequestPendingState.DONE.value
            row.result_code = "RECON_BOOKING_CLOSED"
            row.result_outcome = TeyaRequestPendingState.DONE.value
            row.manual_review_reason = None
            row.lease_token = None
            row.lease_expires_at = None
            row.next_retry_at = None
            row.updated_at = now
            await session.flush()
            _log("TEYA_RECON_REPAIRED")
            return True

        if row.state == TeyaRequestPendingState.VERIFYING.value:
            if row.attempt_count >= row.max_attempts:
                await pending_repo.mark_manual_review(
                    session,
                    row=row,
                    now=now,
                    reason="VERIFY_POSTCHECK_EXHAUSTED",
                )
                _log("TEYA_RECON_MANUAL")
                return True
            return False

        if (
            row.state
            in {
                TeyaRequestPendingState.RECONCILED.value,
                TeyaRequestPendingState.RECONCILIATION_REQUIRED.value,
            }
            and dto.phone_e164
        ):
            try:
                lookup = self._remote.appointments_lookup(phone=dto.phone_e164)
            except Exception:
                return False
            if lookup.outcome is AppointmentsLookupOutcome.AMBIGUOUS:
                await pending_repo.mark_manual_review(
                    session,
                    row=row,
                    now=now,
                    reason="APPOINTMENTS_AMBIGUOUS",
                )
                _log("TEYA_RECON_MANUAL")
                return True

        if self._crm is not None and await self._reconcile_crm(
            session, row, dto, now
        ):
            return True
        return False

    async def _reconcile_crm(
        self,
        session: AsyncSession,
        row: TeyaRequestPending,
        dto: object,
        now: object,
    ) -> bool:
        assert self._crm is not None
        if row.state not in _CRM_RECON_STATES:
            return False
        phone = getattr(dto, "phone_e164", None)
        if type(phone) is not str or not phone:
            return False
        needs_crm = (
            not row.amocrm_contact_id
            or not row.amocrm_deal_id
            or (
                row.state
                in {
                    TeyaRequestPendingState.CRM_READY.value,
                    TeyaRequestPendingState.RECONCILIATION_REQUIRED.value,
                    TeyaRequestPendingState.MANUAL_REVIEW.value,
                }
                and not row.amocrm_task_id
            )
        )
        if not needs_crm:
            return False

        note_text = build_teya_structured_note(dto)
        task_text = build_teya_crm_task_text(dto, appointment_id=None)
        result = await self._crm.reconcile_readonly(
            phone_e164=phone,
            note_text=note_text,
            task_text=task_text,
        )
        _log("TEYA_RECON_CRM")

        if result.outcome is TeyaCrmActionOutcome.MANUAL_REVIEW:
            await pending_repo.mark_manual_review(
                session,
                row=row,
                now=now,  # type: ignore[arg-type]
                reason=result.error_code or "CRM_RECON_AMBIGUOUS",
            )
            _log("TEYA_RECON_MANUAL")
            return True

        if result.outcome is TeyaCrmActionOutcome.NONE:
            # Leave durable; no creates from reconciler.
            return False

        if result.outcome is TeyaCrmActionOutcome.RETRY:
            return False

        if result.outcome is not TeyaCrmActionOutcome.READY:
            return False

        # Exact verified recovery — attach ids, never create.
        if result.contact_id:
            row.amocrm_contact_id = result.contact_id
        if result.deal_id:
            row.amocrm_deal_id = result.deal_id
        if result.task_id:
            row.amocrm_task_id = result.task_id
        if result.note_id and not row.structured_note:
            row.structured_note = note_text

        full = bool(
            row.amocrm_contact_id and row.amocrm_deal_id and row.amocrm_task_id
        )
        operator_owned = _is_operator_owned(row)

        if full and row.state in {
            TeyaRequestPendingState.IDENTITY.value,
            TeyaRequestPendingState.MANUAL_REVIEW.value,
            TeyaRequestPendingState.RECONCILIATION_REQUIRED.value,
            TeyaRequestPendingState.CRM_READY.value,
        }:
            row.state = TeyaRequestPendingState.RECONCILED.value
            row.result_code = "RECON_CRM_VERIFIED"
            row.result_outcome = None
            row.manual_review_reason = None
            row.lease_token = None
            row.lease_expires_at = None
            row.next_retry_at = None
            row.updated_at = now  # type: ignore[assignment]
            await session.flush()
            _log("TEYA_RECON_REPAIRED")
            return True

        # Partial: contact+deal verified, task still missing.
        if row.amocrm_contact_id and row.amocrm_deal_id and not row.amocrm_task_id:
            if operator_owned:
                # Stay MANUAL_REVIEW; never reopen exhausted into claimable CRM_READY.
                row.state = TeyaRequestPendingState.MANUAL_REVIEW.value
                row.result_code = "RECON_CRM_PARTIAL"
                row.result_outcome = TeyaRequestPendingState.MANUAL_REVIEW.value
                row.manual_review_reason = "RECON_CRM_PARTIAL"
                row.lease_token = None
                row.lease_expires_at = None
                row.next_retry_at = None
                row.updated_at = now  # type: ignore[assignment]
                await session.flush()
                _log("TEYA_RECON_PARTIAL")
                return True
            if row.state in {
                TeyaRequestPendingState.IDENTITY.value,
                TeyaRequestPendingState.RECONCILIATION_REQUIRED.value,
            }:
                row.state = TeyaRequestPendingState.CRM_READY.value
                row.result_code = "RECON_CRM_CONTACT_DEAL"
                row.result_outcome = None
                row.manual_review_reason = None
                row.lease_token = None
                row.lease_expires_at = None
                row.next_retry_at = None
                row.updated_at = now  # type: ignore[assignment]
                await session.flush()
                _log("TEYA_RECON_REPAIRED")
                return True

        # Partial attach without state advance still counts as durable recovery.
        row.updated_at = now  # type: ignore[assignment]
        await session.flush()
        return bool(result.contact_id or result.deal_id)
