"""Unit tests for Teya game reconciliation paths."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.booking_request_remote import (
    AppointmentsLookupOutcome,
    BookingRequestAppointmentsLookupResult,
    BotBookingRequestDto,
    BotBookingRequestGameContext,
)
from app.core.teya_request_types import (
    TeyaRequestOrchestratorOutcome,
    TeyaRequestPendingState,
)
from app.models.teya_request_pending import TeyaRequestPending
from app.services.teya_request_crm import (
    TeyaCrmActionOutcome,
    TeyaCrmActionResult,
    build_game_task_text,
)
from app.services.teya_request_orchestrator import TeyaRequestOrchestratorService
from app.services.teya_request_pending import TeyaRequestPendingService

_NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
_REQUEST = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_LEASE = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


def _pending(**overrides: object) -> TeyaRequestPending:
    row = TeyaRequestPending()
    row.id = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    row.request_id = uuid.UUID(_REQUEST)
    row.state = TeyaRequestPendingState.RECONCILED.value
    row.attempt_count = 1
    row.max_attempts = 8
    row.lease_token = _LEASE
    row.amocrm_deal_id = "200"
    row.created_at = _NOW
    row.updated_at = _NOW
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _dto(*, appointment_id: str | None = None) -> BotBookingRequestDto:
    return BotBookingRequestDto(
        request_id=_REQUEST,
        status="NEW",
        request_type="MANAGER_REQUEST",
        service_id="11111111-1111-4111-8111-111111111111",
        master_id="22222222-2222-4222-8222-222222222222",
        client_name="Client",
        phone_e164="+79001234567",
        game_context=BotBookingRequestGameContext(
            gift="маска", procedure="чистка"
        ),
        appointment_id=appointment_id,
    )


class _Remote:
    def __init__(self, lookup: BookingRequestAppointmentsLookupResult) -> None:
        self.lookup = lookup
        self.dto = _dto(
            appointment_id=lookup.appointment_id
            if lookup.outcome is AppointmentsLookupOutcome.UNIQUE
            else None
        )

    def get(self, *, request_id: object) -> BotBookingRequestDto:
        return self.dto

    def appointments_lookup(self, *, phone: object = None, client_id: object = None):
        return self.lookup

    def book(self, **_kwargs: object):
        raise AssertionError("book must not run in game reconcile tests")


class _Crm:
    def __init__(self) -> None:
        self.tasks: list[str] = []

    async def attach_note_and_task(self, *, deal_id: str, note_text: str, task_text: str):
        self.tasks.append(task_text)
        return TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY,
            deal_id=deal_id,
            task_id="77",
            note_id="9",
        )


@pytest.mark.asyncio
async def test_game_no_booking_updates_task(monkeypatch: pytest.MonkeyPatch) -> None:
    crm = _Crm()
    remote = _Remote(
        BookingRequestAppointmentsLookupResult(outcome=AppointmentsLookupOutcome.NONE)
    )
    monkeypatch.setattr(
        "app.services.teya_request_orchestrator.pending_repo.advance_state",
        AsyncMock(return_value=True),
    )
    orch = TeyaRequestOrchestratorService(
        MagicMock(),
        pending_service=TeyaRequestPendingService(MagicMock(), clock=lambda: _NOW),
        remote=remote,
        crm=crm,  # type: ignore[arg-type]
        clock=lambda: _NOW,
    )
    result = await orch.process_claimed(_pending())
    assert result.outcome is TeyaRequestOrchestratorOutcome.ADVANCED
    assert result.pending_state is TeyaRequestPendingState.CONTACT_ROUTE
    assert any("подарок" in t for t in crm.tasks)
    assert "уже записался" not in " ".join(crm.tasks)


@pytest.mark.asyncio
async def test_game_with_self_booking_task(monkeypatch: pytest.MonkeyPatch) -> None:
    crm = _Crm()
    appt = "33333333-3333-4333-8333-333333333333"
    remote = _Remote(
        BookingRequestAppointmentsLookupResult(
            outcome=AppointmentsLookupOutcome.UNIQUE,
            appointment_id=appt,
        )
    )
    monkeypatch.setattr(
        "app.services.teya_request_orchestrator.pending_repo.advance_state",
        AsyncMock(return_value=True),
    )
    orch = TeyaRequestOrchestratorService(
        MagicMock(),
        pending_service=TeyaRequestPendingService(MagicMock(), clock=lambda: _NOW),
        remote=remote,
        crm=crm,  # type: ignore[arg-type]
        clock=lambda: _NOW,
    )
    result = await orch.process_claimed(_pending())
    assert result.outcome is TeyaRequestOrchestratorOutcome.ADVANCED
    assert any("уже записался" in t for t in crm.tasks)
    assert appt in crm.tasks[-1]


@pytest.mark.asyncio
async def test_game_ambiguous_requires_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crm = _Crm()
    remote = _Remote(
        BookingRequestAppointmentsLookupResult(
            outcome=AppointmentsLookupOutcome.AMBIGUOUS,
            appointment_ids=(
                "33333333-3333-4333-8333-333333333333",
                "44444444-4444-4444-8444-444444444444",
            ),
        )
    )
    monkeypatch.setattr(
        "app.services.teya_request_orchestrator.pending_repo.advance_state",
        AsyncMock(return_value=True),
    )
    orch = TeyaRequestOrchestratorService(
        MagicMock(),
        pending_service=TeyaRequestPendingService(MagicMock(), clock=lambda: _NOW),
        remote=remote,
        crm=crm,  # type: ignore[arg-type]
        clock=lambda: _NOW,
    )
    result = await orch.process_claimed(_pending())
    assert result.outcome is TeyaRequestOrchestratorOutcome.TERMINAL
    assert result.pending_state is TeyaRequestPendingState.RECONCILIATION_REQUIRED


def test_game_task_text_templates() -> None:
    no_book = build_game_task_text(gift="маска", procedure="чистка", appointment_id=None)
    with_book = build_game_task_text(
        gift="маска",
        procedure="чистка",
        appointment_id="33333333-3333-4333-8333-333333333333",
    )
    assert "подарок: маска" in no_book
    assert "уже записался" in with_book
    assert "33333333-3333-4333-8333-333333333333" in with_book


def test_dup_task_prevention_fingerprint_stable() -> None:
    a = build_game_task_text(gift="X", procedure="Y", appointment_id=None)
    b = build_game_task_text(gift="X", procedure="Y", appointment_id=None)
    assert a == b
