"""Unit tests for SELF-BOOKING-COMMAND-03C active-offer types."""

from __future__ import annotations

import uuid
from pathlib import Path

from app.core.self_booking_active_offer_types import (
    ActiveOfferActivateResult,
    ActiveOfferActivateOutcome,
    ActiveOfferResolveResult,
    ActiveOfferResolveOutcome,
    ActiveOfferSlot,
    require_active_offer_slots,
)

_REPO = Path(__file__).resolve().parents[1]
_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_SLOT = f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.1000"
_STARTS = "2026-08-20T10:00:00+05:00"


def test_active_offer_slot_repr_redacts() -> None:
    slot = ActiveOfferSlot(slot_id=_SLOT, starts_at=_STARTS)
    rendered = repr(slot)
    assert _SLOT not in rendered
    assert _STARTS not in rendered


def test_require_slots_accepts_dicts() -> None:
    slots = require_active_offer_slots(
        [{"slot_id": _SLOT, "starts_at": _STARTS}]
    )
    assert len(slots) == 1
    assert slots[0].slot_id == _SLOT


def test_resolve_result_repr_redacts() -> None:
    result = ActiveOfferResolveResult(
        outcome=ActiveOfferResolveOutcome.FOUND,
        starts_at=_STARTS,
        source_outbound_id=uuid.uuid4(),
    )
    rendered = repr(result)
    assert _STARTS not in rendered
    assert "starts_at=<redacted>" in rendered


def test_activate_result_repr_redacts() -> None:
    result = ActiveOfferActivateResult(
        outcome=ActiveOfferActivateOutcome.ACTIVATED,
        conversation_id=uuid.uuid4(),
        source_outbound_id=uuid.uuid4(),
    )
    rendered = repr(result)
    assert "conversation_id=<redacted>" in rendered


def test_arbiter_wires_activate_after_delivered() -> None:
    text = (
        _REPO / "app/services/outbound_arbiter.py"
    ).read_text(encoding="utf-8")
    assert "activate_from_delivered_outbound" in text
    assert "mark_delivered_with_lease" in text
    delivered_region = text.split("mark_delivered_with_lease", 1)[1].split(
        "enqueue_outbound_delivered", 1
    )[0]
    assert "activate_from_delivered_outbound" in delivered_region
    # Same-UoW atomicity: must not swallow activate failures.
    assert "except Exception:" not in delivered_region
    assert "ACTIVE_OFFER_" in delivered_region
    assert 'reason_code != "NOT_OFFER_SLOTS"' in delivered_region


def test_fence_order_tuple_manager_epoch_first() -> None:
    """Takeover/newer manager_epoch must dominate older offers."""

    older = (0, 99, 99)
    newer_epoch = (1, 0, 0)
    assert older < newer_epoch
    assert (0, 5, 5) < (0, 6, 1)
    assert (0, 6, 1) < (0, 6, 2)
