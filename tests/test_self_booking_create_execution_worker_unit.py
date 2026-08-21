"""Unit tests for SELF-BOOKING-COMMAND-03L execution worker wiring."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.self_booking_create_types import (
    SelfBookingCreateExecutionOutcome,
    SelfBookingCreateExecutionResult,
    SelfBookingCreatePendingState,
)
from app.models.worker_heartbeat import REQUIRED_WORKER_LOOPS, SELF_BOOKING_CREATE_LOOP
from app.services.self_booking_create_execution_worker import (
    SelfBookingCreateExecutionWorker,
)
from app.services.worker_runtime import build_default_loop_specs

_REPO = Path(__file__).resolve().parents[1]
_KEY = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_PENDING = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")


def test_worker_source_no_key_mint_no_inbound_create() -> None:
    worker = (
        _REPO / "app" / "services" / "self_booking_create_execution_worker.py"
    ).read_text(encoding="utf-8")
    inbound = (_REPO / "app" / "services" / "inbound.py").read_text(encoding="utf-8")
    runtime = (_REPO / "app" / "services" / "worker_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "SelfBookingCreateExecutionService" in worker
    assert "lock_next_claimable_id" in worker or "claim_one" in worker
    assert "uuid.uuid4(" not in worker
    assert ".read_plaintext(" not in worker
    assert "admit_from_confirm" not in worker
    assert "client_reply_plan" not in worker
    assert "SelfBookingCreateExecutionWorker" in runtime
    assert SELF_BOOKING_CREATE_LOOP in REQUIRED_WORKER_LOOPS
    assert "self_booking_create" in runtime
    assert "BookingCreateHttpClient" not in runtime
    assert ".confirm_selected_slot" not in runtime
    assert "SelfBookingCreateExecutionService" not in inbound
    assert "claim_for_execution" not in inbound


def test_runtime_registers_self_booking_create_loop() -> None:
    from app.config import Settings

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
    assert tuple(spec.name for spec in specs) == REQUIRED_WORKER_LOOPS
    assert SELF_BOOKING_CREATE_LOOP in {spec.name for spec in specs}


@pytest.mark.asyncio
async def test_claim_one_skipped_without_pii_store() -> None:
    worker = SelfBookingCreateExecutionWorker(
        AsyncMock(),
        booking_flow=MagicMock(),
        pii_store=None,
    )
    assert await worker.claim_one() is None


@pytest.mark.asyncio
async def test_process_one_delegates_to_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = SelfBookingCreateExecutionResult(
        outcome=SelfBookingCreateExecutionOutcome.SUCCEEDED,
        pending_id=_PENDING,
        pending_state=SelfBookingCreatePendingState.SUCCEEDED,
        idempotency_key=_KEY,
        booking_id="33333333-3333-4333-8333-333333333333",
    )
    execute = AsyncMock(return_value=expected)
    fake_exec = MagicMock()
    fake_exec.execute = execute
    monkeypatch.setattr(
        "app.services.self_booking_create_execution_worker.SelfBookingCreateExecutionService",
        MagicMock(return_value=fake_exec),
    )
    monkeypatch.setattr(
        "app.services.self_booking_create_execution_worker.SelfBookingCreatePendingService",
        MagicMock(),
    )
    monkeypatch.setattr(
        "app.services.self_booking_create_execution_worker.ClientRefResolverService",
        MagicMock(),
    )

    class _Scope:
        async def __aenter__(self) -> object:
            return MagicMock()

        async def __aexit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr(
        "app.services.self_booking_create_execution_worker.session_scope",
        lambda _sf: _Scope(),
    )

    worker = SelfBookingCreateExecutionWorker(
        AsyncMock(),
        booking_flow=MagicMock(),
        pii_store=MagicMock(),
    )
    result = await worker.process_one(_PENDING)
    assert result.outcome is SelfBookingCreateExecutionOutcome.SUCCEEDED
    assert result.idempotency_key == _KEY
    execute.assert_awaited_once()
    assert execute.await_args.kwargs["pending_id"] == _PENDING


@pytest.mark.asyncio
async def test_result_repr_redacts_sensitive_fields() -> None:
    result = SelfBookingCreateExecutionResult(
        outcome=SelfBookingCreateExecutionOutcome.SUCCEEDED,
        pending_id=_PENDING,
        pending_state=SelfBookingCreatePendingState.SUCCEEDED,
        idempotency_key=_KEY,
        booking_id="33333333-3333-4333-8333-333333333333",
    )
    rendered = repr(result)
    assert _KEY not in rendered
    assert "idempotency_key=<redacted>" in rendered
    assert "+7900" not in rendered
