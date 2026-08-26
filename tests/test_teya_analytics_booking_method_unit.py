"""Orchestrator wiring: booking method analytics subordinate to BOOKED success."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.amocrm_analytics_fields import (
    AmoCrmAnalyticsApplyDecision,
    AmoCrmAnalyticsBookingMethodEnum,
    AmoCrmAnalyticsFieldId,
)
from app.core.teya_request_types import (
    TeyaRequestOrchestratorOutcome,
    TeyaRequestPendingState,
)
from app.models.teya_request_pending import TeyaRequestPending
from app.services.teya_request_crm import TeyaCrmActionOutcome, TeyaCrmActionResult
from app.services.teya_request_orchestrator import TeyaRequestOrchestratorService
from app.services.teya_request_pending import TeyaRequestPendingService
from app.services.teya_request_reconciliation_worker import (
    TeyaRequestReconciliationWorker,
)


@dataclass
class _Remote:
    dto: object
    book_calls: list = field(default_factory=list)

    def get(self, *, request_id: str):
        return self.dto

    def book(self, **kwargs):
        self.book_calls.append(kwargs)
        return SimpleNamespace(ok=True)


@dataclass
class _Crm:
    result: TeyaCrmActionResult
    calls: list = field(default_factory=list)

    async def apply_lead_analytics_enum_if_empty(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _pending(**kwargs) -> TeyaRequestPending:
    now = datetime.now(timezone.utc)
    row = TeyaRequestPending(
        id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        state=TeyaRequestPendingState.VERIFYING.value,
        attempt_count=1,
        max_attempts=8,
        lease_token=uuid.uuid4(),
        lease_expires_at=now,
        next_retry_at=None,
        result_code="BOOK_VERIFIED_ANALYTICS_PENDING",
        result_outcome=None,
        manual_review_reason=None,
        contact_route_outcome=None,
        amocrm_contact_id="11",
        amocrm_deal_id="55",
        amocrm_task_id=None,
        structured_note=None,
        selected_starts_at=None,
        book_idempotency_key=None,
        created_at=now,
        updated_at=now,
    )
    for key, value in kwargs.items():
        setattr(row, key, value)
    return row


def _orch(remote, crm, *, clock=None):
    return TeyaRequestOrchestratorService(
        MagicMock(),
        pending_service=TeyaRequestPendingService(MagicMock()),
        remote=remote,
        crm=crm,  # type: ignore[arg-type]
        clock=clock,
    )


def _closed_dto(row):
    return SimpleNamespace(
        appointment_id=str(uuid.uuid4()),
        status="CLOSED",
        request_id=str(row.request_id),
        service_id=None,
        master_id=None,
        game_context=None,
        phone_e164=None,
        request_type="MANAGER_REQUEST",
    )


@pytest.mark.asyncio
async def test_verifying_applies_teya_booking_method(monkeypatch):
    row = _pending()
    remote = _Remote(_closed_dto(row))
    crm = _Crm(
        TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY,
            deal_id="55",
            analytics_decision=AmoCrmAnalyticsApplyDecision.APPLIED.value,
            error_code="ANALYTICS_APPLIED",
        )
    )
    orch = _orch(remote, crm)

    async def _advance(r, l, state, **kwargs):
        return SimpleNamespace(
            outcome=TeyaRequestOrchestratorOutcome.ADVANCED,
            pending_id=r.id,
            pending_state=state,
            result_code=kwargs.get("result_code"),
        )

    monkeypatch.setattr(orch, "_advance", _advance)
    result = await orch._step_verifying(row, row.lease_token)
    assert result.result_code == "BOOKED_ANALYTICS_APPLIED"
    assert len(crm.calls) == 1
    assert crm.calls[0]["field_id"] == int(
        AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD
    )
    assert crm.calls[0]["enum_id"] == int(AmoCrmAnalyticsBookingMethodEnum.TEYA)
    assert crm.calls[0]["deal_id"] == "55"


@pytest.mark.asyncio
async def test_analytics_conflict_preserves_booking_done(monkeypatch):
    row = _pending()
    remote = _Remote(_closed_dto(row))
    crm = _Crm(
        TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY,
            deal_id="55",
            analytics_decision=AmoCrmAnalyticsApplyDecision.CONFLICT_NONEMPTY.value,
            error_code="ANALYTICS_CONFLICT_NONEMPTY",
        )
    )
    orch = _orch(remote, crm)

    async def _advance(r, l, state, **kwargs):
        return SimpleNamespace(
            outcome=TeyaRequestOrchestratorOutcome.TERMINAL,
            pending_id=r.id,
            pending_state=state,
            result_code=kwargs.get("result_code"),
        )

    monkeypatch.setattr(orch, "_advance", _advance)
    result = await orch._step_verifying(row, row.lease_token)
    assert result.result_code == "BOOKED_ANALYTICS_CONFLICT"
    assert result.pending_state is TeyaRequestPendingState.DONE


@pytest.mark.asyncio
async def test_analytics_transient_retries_without_rebook(monkeypatch):
    row = _pending()
    remote = _Remote(_closed_dto(row))
    crm = _Crm(
        TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.RETRY,
            deal_id="55",
            analytics_decision=AmoCrmAnalyticsApplyDecision.TRANSIENT_RETRY.value,
            error_code="AMOCRM_ANALYTICS_PATCH_TRANSIENT",
        )
    )
    orch = _orch(remote, crm)
    retry = AsyncMock(
        return_value=SimpleNamespace(
            outcome=TeyaRequestOrchestratorOutcome.RETRY_SCHEDULED,
            pending_id=row.id,
            pending_state=TeyaRequestPendingState.VERIFYING,
            result_code="AMOCRM_ANALYTICS_PATCH_TRANSIENT",
        )
    )
    monkeypatch.setattr(orch, "_retry_post_book_analytics", retry)
    result = await orch._step_verifying(row, row.lease_token)
    assert result.outcome is TeyaRequestOrchestratorOutcome.RETRY_SCHEDULED
    assert remote.book_calls == []
    retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_analytics_exhaustion_done_manual_not_booking_manual(monkeypatch):
    row = _pending(attempt_count=8, max_attempts=8)
    remote = _Remote(_closed_dto(row))
    crm = _Crm(
        TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.RETRY,
            deal_id="55",
            analytics_decision=AmoCrmAnalyticsApplyDecision.TRANSIENT_RETRY.value,
            error_code="AMOCRM_ANALYTICS_PATCH_TRANSIENT",
        )
    )
    orch = _orch(remote, crm)

    async def _advance(r, l, state, **kwargs):
        return SimpleNamespace(
            outcome=TeyaRequestOrchestratorOutcome.TERMINAL,
            pending_id=r.id,
            pending_state=state,
            result_code=kwargs.get("result_code"),
        )

    monkeypatch.setattr(orch, "_advance", _advance)
    result = await orch._step_verifying(row, row.lease_token)
    assert result.pending_state is TeyaRequestPendingState.DONE
    assert result.result_code == "BOOKED_ANALYTICS_MANUAL"
    assert remote.book_calls == []


@pytest.mark.asyncio
async def test_no_deal_skips_analytics_truthfully(monkeypatch):
    row = _pending(amocrm_deal_id=None)
    remote = _Remote(_closed_dto(row))
    crm = _Crm(TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY))
    orch = _orch(remote, crm)

    async def _advance(r, l, state, **kwargs):
        return SimpleNamespace(
            outcome=TeyaRequestOrchestratorOutcome.TERMINAL,
            pending_id=r.id,
            pending_state=state,
            result_code=kwargs.get("result_code"),
        )

    monkeypatch.setattr(orch, "_advance", _advance)
    result = await orch._step_verifying(row, row.lease_token)
    assert result.result_code == "BOOKED_ANALYTICS_SKIPPED"
    assert crm.calls == []


@pytest.mark.asyncio
async def test_no_crm_skips_analytics_truthfully(monkeypatch):
    row = _pending()
    remote = _Remote(_closed_dto(row))
    orch = TeyaRequestOrchestratorService(
        MagicMock(),
        pending_service=TeyaRequestPendingService(MagicMock()),
        remote=remote,
        crm=None,
    )

    async def _advance(r, l, state, **kwargs):
        return SimpleNamespace(
            outcome=TeyaRequestOrchestratorOutcome.TERMINAL,
            pending_id=r.id,
            pending_state=state,
            result_code=kwargs.get("result_code"),
        )

    monkeypatch.setattr(orch, "_advance", _advance)
    result = await orch._step_verifying(row, row.lease_token)
    assert result.result_code == "BOOKED_ANALYTICS_SKIPPED"


@pytest.mark.asyncio
async def test_recon_closed_with_deal_resumes_verifying_analytics():
    row = _pending(state=TeyaRequestPendingState.BOOKING.value)
    dto = _closed_dto(row)
    remote = _Remote(dto)
    crm = _Crm(TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY))
    session = AsyncMock()
    session.flush = AsyncMock()
    worker = TeyaRequestReconciliationWorker(
        MagicMock(),
        remote=remote,
        crm=crm,  # type: ignore[arg-type]
    )
    pending = MagicMock()
    repaired = await worker._reconcile_one(session, pending, row)
    assert repaired is True
    assert row.state == TeyaRequestPendingState.VERIFYING.value
    assert row.result_code == "RECON_BOOKING_CLOSED_ANALYTICS_PENDING"
    assert remote.book_calls == []


@pytest.mark.asyncio
async def test_recon_closed_without_deal_skips_analytics_terminal():
    row = _pending(
        state=TeyaRequestPendingState.VERIFYING.value,
        amocrm_deal_id=None,
    )
    remote = _Remote(_closed_dto(row))
    worker = TeyaRequestReconciliationWorker(
        MagicMock(),
        remote=remote,
        crm=None,
    )
    session = AsyncMock()
    session.flush = AsyncMock()
    repaired = await worker._reconcile_one(session, MagicMock(), row)
    assert repaired is True
    assert row.state == TeyaRequestPendingState.DONE.value
    assert row.result_code == "BOOKED_ANALYTICS_SKIPPED"
    assert remote.book_calls == []


# --- F2: post-book remote GET must not use booking MANUAL_REVIEW ---


@dataclass
class _FailingGetRemote:
    """Remote GET raises; book is countable for no-rebook proof."""

    error_code: str
    book_calls: list = field(default_factory=list)

    def get(self, *, request_id: str):
        from app.core.booking_request_http import BookingRequestHttpError

        raise BookingRequestHttpError(self.error_code)

    def book(self, **kwargs):
        self.book_calls.append(kwargs)
        return SimpleNamespace(ok=True)


def test_post_book_phase_is_verifying_state_not_result_code():
    from app.core.teya_request_types import is_teya_post_book_analytics_phase

    assert is_teya_post_book_analytics_phase(TeyaRequestPendingState.VERIFYING)
    assert is_teya_post_book_analytics_phase("VERIFYING")
    assert not is_teya_post_book_analytics_phase(TeyaRequestPendingState.BOOKING)
    assert not is_teya_post_book_analytics_phase("BOOKING")
    # Overwritten transport code must not matter — phase is state-derived.
    row = _pending(result_code="TRANSPORT_ERROR")
    assert is_teya_post_book_analytics_phase(row.state)


@pytest.mark.asyncio
async def test_verifying_transient_get_uses_post_book_retry_not_manual(
    monkeypatch,
):
    row = _pending(result_code="BOOK_VERIFIED_ANALYTICS_PENDING")
    remote = _FailingGetRemote("TRANSPORT_ERROR")
    orch = _orch(remote, None)
    retry = AsyncMock(
        return_value=SimpleNamespace(
            outcome=TeyaRequestOrchestratorOutcome.RETRY_SCHEDULED,
            pending_id=row.id,
            pending_state=TeyaRequestPendingState.VERIFYING,
            result_code="TRANSPORT_ERROR",
        )
    )
    manual = AsyncMock()
    generic_retry = AsyncMock()
    monkeypatch.setattr(orch, "_retry_post_book_analytics", retry)
    monkeypatch.setattr(orch, "_manual_review", manual)
    monkeypatch.setattr(orch, "_retry", generic_retry)

    result = await orch.process_claimed(row)

    assert result.outcome is TeyaRequestOrchestratorOutcome.RETRY_SCHEDULED
    assert result.pending_state is TeyaRequestPendingState.VERIFYING
    assert result.result_code == "TRANSPORT_ERROR"
    retry.assert_awaited_once()
    manual.assert_not_awaited()
    generic_retry.assert_not_awaited()
    assert remote.book_calls == []


@pytest.mark.asyncio
async def test_verifying_get_retry_exhaustion_done_analytics_manual(
    monkeypatch,
):
    row = _pending(attempt_count=8, max_attempts=8, result_code="TIMEOUT")
    remote = _FailingGetRemote("TIMEOUT")
    orch = _orch(remote, None)

    async def _advance(r, l, state, **kwargs):
        return SimpleNamespace(
            outcome=TeyaRequestOrchestratorOutcome.TERMINAL,
            pending_id=r.id,
            pending_state=state,
            result_code=kwargs.get("result_code"),
        )

    monkeypatch.setattr(orch, "_advance", _advance)
    result = await orch.process_claimed(row)
    assert result.pending_state is TeyaRequestPendingState.DONE
    assert result.result_code == "BOOKED_ANALYTICS_MANUAL"
    assert remote.book_calls == []


@pytest.mark.asyncio
async def test_book_then_verifying_get_failures_never_rebook(monkeypatch):
    """Proven book then post-book GET retries: book() stays exactly once."""

    from app.core.booking_request_http import BookingRequestHttpError
    from app.core.booking_request_remote import BotBookingRequestDto

    now = datetime.now(timezone.utc)
    request_id = uuid.uuid4()
    lease = uuid.uuid4()
    key = str(uuid.uuid4())
    book_row = TeyaRequestPending(
        id=uuid.uuid4(),
        request_id=request_id,
        state=TeyaRequestPendingState.BOOKING.value,
        attempt_count=1,
        max_attempts=8,
        lease_token=lease,
        lease_expires_at=now,
        next_retry_at=None,
        result_code=None,
        result_outcome=None,
        manual_review_reason=None,
        contact_route_outcome=None,
        amocrm_contact_id="11",
        amocrm_deal_id="55",
        amocrm_task_id=None,
        structured_note=None,
        selected_starts_at="2026-08-25T14:00:00+05:00",
        book_idempotency_key=key,
        created_at=now,
        updated_at=now,
    )

    class _SeqRemote:
        def __init__(self) -> None:
            self.book_calls = 0
            self.get_phase = "booking"

        def get(self, *, request_id: str):
            if self.get_phase == "booking":
                return BotBookingRequestDto(
                    request_id=str(request_id),
                    status="NEW",
                    request_type="MANAGER_REQUEST",
                    service_id=str(uuid.uuid4()),
                    master_id=str(uuid.uuid4()),
                )
            raise BookingRequestHttpError("TRANSPORT_ERROR")

        def book(self, **_kwargs):
            self.book_calls += 1
            return SimpleNamespace(ok=True)

    remote = _SeqRemote()
    orch = _orch(remote, None, clock=lambda: now)
    monkeypatch.setattr(
        "app.services.teya_request_orchestrator.pending_repo.advance_state",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.teya_request_orchestrator.pending_repo.release_lease",
        AsyncMock(return_value=True),
    )

    booked = await orch.process_claimed(book_row)
    assert booked.pending_state is TeyaRequestPendingState.VERIFYING
    assert remote.book_calls == 1

    remote.get_phase = "verifying"
    for attempt in (2, 3, 4):
        verify_row = _pending(
            request_id=request_id,
            attempt_count=attempt,
            max_attempts=8,
            book_idempotency_key=key,
            selected_starts_at="2026-08-25T14:00:00+05:00",
            result_code="TRANSPORT_ERROR",
        )
        result = await orch.process_claimed(verify_row)
        assert result.outcome is TeyaRequestOrchestratorOutcome.RETRY_SCHEDULED
        assert result.pending_state is TeyaRequestPendingState.VERIFYING
        assert result.result_code == "TRANSPORT_ERROR"
        assert remote.book_calls == 1

    exhausted = _pending(
        request_id=request_id,
        attempt_count=8,
        max_attempts=8,
        book_idempotency_key=key,
        selected_starts_at="2026-08-25T14:00:00+05:00",
        result_code="TRANSPORT_ERROR",
    )
    terminal = await orch.process_claimed(exhausted)
    assert terminal.pending_state is TeyaRequestPendingState.DONE
    assert terminal.result_code == "BOOKED_ANALYTICS_MANUAL"
    assert remote.book_calls == 1


@pytest.mark.asyncio
async def test_prebook_transient_remote_still_uses_generic_retry(monkeypatch):
    row = _pending(
        state=TeyaRequestPendingState.BOOKING.value,
        selected_starts_at="2026-08-25T14:00:00+05:00",
        book_idempotency_key=str(uuid.uuid4()),
        result_code=None,
    )
    dto = SimpleNamespace(
        appointment_id=None,
        status="NEW",
        request_id=str(row.request_id),
        service_id=str(uuid.uuid4()),
        master_id=None,
        game_context=None,
        phone_e164=None,
        request_type="MANAGER_REQUEST",
    )

    class _BookFailRemote:
        def __init__(self) -> None:
            self.book_calls: list = []

        def get(self, *, request_id: str):
            return dto

        def book(self, **kwargs):
            self.book_calls.append(kwargs)
            from app.core.booking_request_http import BookingRequestHttpError

            raise BookingRequestHttpError("TRANSPORT_ERROR")

    book_remote = _BookFailRemote()
    orch = _orch(book_remote, None)
    generic_retry = AsyncMock(
        return_value=SimpleNamespace(
            outcome=TeyaRequestOrchestratorOutcome.RETRY_SCHEDULED,
            pending_id=row.id,
            pending_state=TeyaRequestPendingState.BOOKING,
            result_code="TRANSPORT_ERROR",
        )
    )
    post_book = AsyncMock()
    monkeypatch.setattr(orch, "_retry", generic_retry)
    monkeypatch.setattr(orch, "_retry_post_book_analytics", post_book)

    result = await orch.process_claimed(row)
    assert result.outcome is TeyaRequestOrchestratorOutcome.RETRY_SCHEDULED
    generic_retry.assert_awaited_once()
    post_book.assert_not_awaited()
    assert len(book_remote.book_calls) == 1


@pytest.mark.asyncio
async def test_expire_verifying_with_transport_result_code_is_analytics_manual():
    """Expiration must recognize post-book phase even if result_code was overwritten."""

    from app.repositories import teya_request_pendings as pending_repo

    captured: list = []

    class _Result:
        rowcount = 1

    class _Session:
        async def execute(self, stmt):
            captured.append(stmt)
            return _Result()

        async def flush(self):
            return None

    now = datetime.now(timezone.utc)
    n = await pending_repo.expire_exhausted_to_manual_review(_Session(), now=now)
    assert n == 2
    assert len(captured) == 2

    post_book_stmt = captured[0]
    compiled = post_book_stmt.compile()
    params = dict(compiled.params)
    sql = str(compiled)
    # Durable phase filter: VERIFYING only — no fragile result_code allowlist.
    assert TeyaRequestPendingState.VERIFYING.value in params.values()
    assert "BOOK_VERIFIED_ANALYTICS_PENDING" not in sql
    assert "AMOCRM_ANALYTICS_" not in sql
    assert "like" not in sql.lower()
    assert "BOOKED_ANALYTICS_MANUAL" in params.values()
    assert TeyaRequestPendingState.DONE.value in params.values()
    assert "MAX_ATTEMPTS_EXCEEDED" not in params.values()
    assert TeyaRequestPendingState.MANUAL_REVIEW.value not in params.values()
