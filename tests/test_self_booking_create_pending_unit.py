"""Unit tests for SELF-BOOKING-COMMAND-01 durable foundation."""

from __future__ import annotations

from pathlib import Path

from app.core.self_booking_create_types import (
    SelfBookingCreateAdmitResult,
    SelfBookingCreateAdmitOutcome,
    SelfBookingCreateSafeSelection,
)
from app.models.self_booking_create_pending import SelfBookingCreatePending

_REPO = Path(__file__).resolve().parents[1]
_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_SLOT = f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.1000"
_STARTS = "2026-08-20T10:00:00+05:00"
_KEY = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def test_safe_selection_repr_redacts_slot() -> None:
    selection = SelfBookingCreateSafeSelection(slot_id=_SLOT, starts_at=_STARTS)
    rendered = repr(selection)
    assert _SLOT not in rendered
    assert _STARTS not in rendered
    assert "slot_id=<redacted>" in rendered


def test_admit_result_repr_redacts_ids() -> None:
    import uuid

    result = SelfBookingCreateAdmitResult(
        outcome=SelfBookingCreateAdmitOutcome.ADMITTED,
        pending_id=uuid.uuid4(),
        idempotency_key=_KEY,
    )
    rendered = repr(result)
    assert _KEY not in rendered
    assert "idempotency_key=<redacted>" in rendered


def test_pending_model_repr_redacts_pii_and_slot() -> None:
    row = SelfBookingCreatePending()
    row.id = __import__("uuid").uuid4()
    row.conversation_id = __import__("uuid").uuid4()
    row.channel = "synthetic"
    row.confirm_external_message_id = "confirm-msg-1"
    row.state = "READY"
    row.command_version = 1
    row.attempt_count = 0
    row.max_attempts = 3
    row.idempotency_key = _KEY
    row.slot_id = _SLOT
    row.starts_at = _STARTS
    row.fence_context_version = 0
    row.fence_manager_epoch = 0
    row.fence_event_seq_hwm = 0
    row.personal_data_consent = True
    row.offer_acknowledgement = True
    row.phone_ref_token = "phone-ref-token-value-xxxxxxxxxxxxxxxx"
    row.name_ref_token = "name-ref-token-value-xxxxxxxxxxxxxxxxx"
    rendered = repr(row)
    assert _SLOT not in rendered
    assert _STARTS not in rendered
    assert _KEY not in rendered
    assert "confirm-msg-1" not in rendered
    assert "phone-ref-token" not in rendered
    assert "name-ref-token" not in rendered
    assert "phone_ref_token=<redacted>" in rendered
    assert "name_ref_token=<redacted>" in rendered


def test_migration_defines_confirm_dedupe_and_active_conversation() -> None:
    migration = (
        _REPO / "alembic/versions/20260820_28_self_booking_create.py"
    ).read_text(encoding="utf-8")
    assert "self_booking_create_pendings" in migration
    assert "uq_self_booking_create_pendings_confirm" in migration
    assert "uq_self_booking_create_pendings_active_conversation" in migration
    assert "state IN ('READY', 'EXECUTING')" in migration
    assert "20260818_27_amocrm_deal_kind" in migration
    assert "phone_ref_token" in migration
    assert "personal_data_consent IS TRUE" in migration
    assert "plaintext" not in migration.lower() or "No plaintext" in migration


def test_service_has_no_booking_http_or_pii_read_surface() -> None:
    path = _REPO / "app" / "services" / "self_booking_create_pending.py"
    text = path.read_text(encoding="utf-8")
    assert "confirm_selected_slot" not in text
    assert "BookingCreateHttp" not in text
    assert "read_plaintext" not in text
    assert "create_booking" not in text
    assert "ClientRefResolver" not in text
