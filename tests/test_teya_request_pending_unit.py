"""Unit tests for teya_request pending claim / dedupe."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.teya_request_types import TeyaRequestPendingState
from app.models.teya_request_pending import TeyaRequestPending
from app.services.teya_request_pending import TeyaRequestPendingService

_REPO = Path(__file__).resolve().parents[1]
_REQUEST = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _row(**overrides: object) -> TeyaRequestPending:
    row = TeyaRequestPending()
    row.id = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    row.request_id = _REQUEST
    row.state = TeyaRequestPendingState.DISCOVERED.value
    row.attempt_count = 0
    row.max_attempts = 8
    row.lease_token = None
    row.lease_expires_at = None
    row.next_retry_at = None
    row.created_at = _NOW
    row.updated_at = _NOW
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def test_pending_model_repr_redacts_ids() -> None:
    rendered = repr(_row(amocrm_contact_id="123", selected_starts_at="2026-08-25T10:00:00+05:00"))
    assert "aaaaaaaa-aaaa" not in rendered
    assert "123" not in rendered
    assert "2026-08-25T10:00:00" not in rendered
    assert "request_id=<redacted>" in rendered


def test_migration_defines_table_and_heartbeat_loop() -> None:
    text = (
        _REPO / "alembic/versions/20260825_32_teya_req_orch.py"
    ).read_text(encoding="utf-8")
    assert "teya_request_pendings" in text
    assert "teya_request_orchestrator" in text
    assert "20260821_31_sbc_exec_loop" in text
    assert "selected_starts_at" in text
    assert "book_idempotency_key" in text


@pytest.mark.asyncio
async def test_upsert_dedupes_by_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = _row()
    session = MagicMock()
    monkeypatch.setattr(
        "app.services.teya_request_pending.pending_repo.upsert_discovered",
        AsyncMock(return_value=existing),
    )
    service = TeyaRequestPendingService(session, clock=lambda: _NOW)
    first = await service.upsert_discovered(request_id=_REQUEST)
    second = await service.upsert_discovered(request_id=_REQUEST)
    assert first.id == second.id == existing.id


@pytest.mark.asyncio
async def test_duplicate_worker_claim_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row(
        lease_token=uuid.uuid4(),
        lease_expires_at=_NOW + timedelta(seconds=60),
        attempt_count=1,
    )
    session = MagicMock()
    monkeypatch.setattr(
        "app.services.teya_request_pending.pending_repo.get_by_id",
        AsyncMock(return_value=row),
    )
    monkeypatch.setattr(
        "app.services.teya_request_pending.pending_repo.claim_lease",
        AsyncMock(return_value=False),
    )
    service = TeyaRequestPendingService(session, clock=lambda: _NOW)
    claimed = await service.claim_by_id(pending_id=row.id)
    assert claimed is None


@pytest.mark.asyncio
async def test_claim_succeeds_when_lease_free(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row()
    refreshed = _row(attempt_count=1, lease_token=uuid.uuid4())
    session = MagicMock()
    monkeypatch.setattr(
        "app.services.teya_request_pending.pending_repo.lock_next_claimable_id",
        AsyncMock(return_value=row.id),
    )
    monkeypatch.setattr(
        "app.services.teya_request_pending.pending_repo.get_by_id",
        AsyncMock(side_effect=[row, refreshed]),
    )
    monkeypatch.setattr(
        "app.services.teya_request_pending.pending_repo.claim_lease",
        AsyncMock(return_value=True),
    )
    service = TeyaRequestPendingService(session, clock=lambda: _NOW)
    claimed = await service.claim_one()
    assert claimed is not None
    assert claimed.attempt_count == 1
