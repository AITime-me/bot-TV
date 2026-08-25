"""Unit tests for Teya booking step fail-closed outcomes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.booking_request_http import BookingRequestHttpError
from app.core.booking_request_remote import BotBookingRequestDto
from app.core.teya_request_types import (
    TeyaRequestOrchestratorOutcome,
    TeyaRequestPendingState,
)
from app.models.teya_request_pending import TeyaRequestPending
from app.services.teya_request_orchestrator import TeyaRequestOrchestratorService
from app.services.teya_request_pending import TeyaRequestPendingService

_NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
_REQUEST = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_LEASE = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_KEY = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


def _pending(*, state: str, **overrides: object) -> TeyaRequestPending:
    row = TeyaRequestPending()
    row.id = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    row.request_id = uuid.UUID(_REQUEST)
    row.state = state
    row.attempt_count = 1
    row.max_attempts = 8
    row.lease_token = _LEASE
    row.selected_starts_at = "2026-08-25T14:00:00+05:00"
    row.book_idempotency_key = _KEY
    row.created_at = _NOW
    row.updated_at = _NOW
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _dto(**overrides: object) -> BotBookingRequestDto:
    values = dict(
        request_id=_REQUEST,
        status="NEW",
        request_type="MANAGER_REQUEST",
        service_id=_SERVICE,
        master_id=_MASTER,
    )
    values.update(overrides)
    return BotBookingRequestDto(**values)  # type: ignore[arg-type]


class _Remote:
    def __init__(
        self,
        *,
        book_error: str | None = None,
        dto: BotBookingRequestDto | None = None,
    ) -> None:
        self.book_error = book_error
        self.dto = dto or _dto()
        self.book_calls = 0

    def get(self, *, request_id: object) -> BotBookingRequestDto:
        return self.dto

    def appointments_lookup(self, *, request_id: object):
        raise AssertionError("not used")

    def book(self, **_kwargs: object):
        self.book_calls += 1
        if self.book_error:
            raise BookingRequestHttpError(self.book_error)
        return MagicMock()


@pytest.mark.asyncio
async def test_book_postcheck_fail_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = _Remote(dto=_dto(status="NEW", appointment_id=None))
    monkeypatch.setattr(
        "app.services.teya_request_orchestrator.pending_repo.advance_state",
        AsyncMock(return_value=True),
    )
    orch = TeyaRequestOrchestratorService(
        MagicMock(),
        pending_service=TeyaRequestPendingService(MagicMock(), clock=lambda: _NOW),
        remote=remote,
        clock=lambda: _NOW,
    )
    # First BOOKING → VERIFYING
    booking_result = await orch.process_claimed(
        _pending(state=TeyaRequestPendingState.BOOKING.value)
    )
    assert booking_result.pending_state is TeyaRequestPendingState.VERIFYING
    assert remote.book_calls == 1

    # VERIFYING without appointment_id → RECONCILIATION_REQUIRED
    verify_row = _pending(state=TeyaRequestPendingState.VERIFYING.value)
    verify_result = await orch.process_claimed(verify_row)
    assert verify_result.outcome is TeyaRequestOrchestratorOutcome.TERMINAL
    assert (
        verify_result.pending_state
        is TeyaRequestPendingState.RECONCILIATION_REQUIRED
    )
    assert verify_result.result_code == "BOOK_POSTCHECK_MISSING"


@pytest.mark.asyncio
async def test_consultation_service_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = _Remote(book_error="CONSULTATION_SERVICE_REQUIRED")
    monkeypatch.setattr(
        "app.services.teya_request_orchestrator.pending_repo.advance_state",
        AsyncMock(return_value=True),
    )
    orch = TeyaRequestOrchestratorService(
        MagicMock(),
        pending_service=TeyaRequestPendingService(MagicMock(), clock=lambda: _NOW),
        remote=remote,
        clock=lambda: _NOW,
    )
    result = await orch.process_claimed(
        _pending(state=TeyaRequestPendingState.BOOKING.value)
    )
    assert result.outcome is TeyaRequestOrchestratorOutcome.ADVANCED
    assert result.pending_state is TeyaRequestPendingState.WAITING_CONTACT
    assert result.result_code == "CONSULTATION_SERVICE_REQUIRED"


def test_runtime_registers_teya_loop() -> None:
    from unittest.mock import AsyncMock

    from app.config import Settings
    from app.models.worker_heartbeat import (
        REQUIRED_WORKER_LOOPS,
        TEYA_REQUEST_ORCHESTRATOR_LOOP,
    )
    from app.services.worker_runtime import build_default_loop_specs

    settings = Settings(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:5432/bot_tv_foundation_test",
        worker_heartbeat_interval_seconds=1,
        worker_heartbeat_stale_seconds=45,
    )
    specs = build_default_loop_specs(
        settings=settings,
        session_factory=AsyncMock(),
        worker_id="unit-worker",
    )
    assert TEYA_REQUEST_ORCHESTRATOR_LOOP in REQUIRED_WORKER_LOOPS
    assert tuple(spec.name for spec in specs) == REQUIRED_WORKER_LOOPS
