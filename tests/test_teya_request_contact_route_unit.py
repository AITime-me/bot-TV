"""Unit tests for Teya contact-route (PHONE_ONLY, no outbound)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.core.teya_request_types import ContactRouteOutcome, TransportCapability
from app.services.teya_request_contact_route import ConversationLocator

_REPO = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_phone_only_when_no_canonical() -> None:
    locator = ConversationLocator()
    result = await locator.resolve(canonical_identity_id=None)
    assert result.outcome is ContactRouteOutcome.PHONE_ONLY
    assert result.capabilities is TransportCapability.NONE


@pytest.mark.asyncio
async def test_phone_only_for_synthetic_only_dialog() -> None:
    class _Conv:
        id = uuid.uuid4()
        channel = "synthetic"

    class _Query:
        async def list_by_canonical_identity(self, *, canonical_identity_id):
            return [_Conv()]

    locator = ConversationLocator(conversations=_Query())
    result = await locator.resolve(canonical_identity_id=uuid.uuid4())
    assert result.outcome is ContactRouteOutcome.PHONE_ONLY
    assert result.reason_code == "SYNTHETIC_ONLY_OR_NO_TEXT_DIALOG"


def test_orchestrator_never_calls_outbound_arbiter() -> None:
    orch = (
        _REPO / "app/services/teya_request_orchestrator.py"
    ).read_text(encoding="utf-8")
    worker = (
        _REPO / "app/services/teya_request_orchestrator_worker.py"
    ).read_text(encoding="utf-8")
    contact = (
        _REPO / "app/services/teya_request_contact_route.py"
    ).read_text(encoding="utf-8")
    for text in (orch, worker, contact):
        assert "from app.services.outbound_arbiter" not in text
        assert "OutboundArbiter(" not in text
        assert "send_outbound" not in text
    assert "NEVER sends client messages" in orch or "Never sends" in orch
    assert "Never sends outbound" in contact or "Never sends" in contact
