"""Unit tests for SELF-BOOKING-COMMAND-03D/03J structured confirm action."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.self_booking_pii_admission_types import (
    REQUEST_ID_MAX_LENGTH,
    require_pii_admission_request_id,
)
from app.schemas.inbound import SyntheticInboundEvent
from app.schemas.ingress import SyntheticIngressEvent
from app.schemas.self_booking_confirm_action import (
    CONFIRM_SELECTED_SLOT_KIND,
    SyntheticConfirmSelectedSlotAction,
)
from app.services.booking_synthetic import client_reply_plan_payload

_REPO = Path(__file__).resolve().parents[1]
_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_SLOT = f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.1000"
_PII_REQ = "pii-req-confirm-1"


def _valid_action(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": CONFIRM_SELECTED_SLOT_KIND,
        "slot_id": _SLOT,
        "pii_admission_request_id": _PII_REQ,
        "personal_data_consent": True,
        "offer_acknowledgement": True,
    }
    payload.update(overrides)
    return payload


def test_confirm_action_accepts_exact_true_consents_and_canonical_slot() -> None:
    action = SyntheticConfirmSelectedSlotAction.model_validate(_valid_action())
    assert action.kind == "CONFIRM_SELECTED_SLOT"
    assert action.slot_id == _SLOT
    assert action.pii_admission_request_id == _PII_REQ
    assert action.personal_data_consent is True
    assert action.offer_acknowledgement is True


def test_confirm_requires_canonical_pii_admission_request_id() -> None:
    action = SyntheticConfirmSelectedSlotAction.model_validate(_valid_action())
    assert action.pii_admission_request_id == require_pii_admission_request_id(
        _PII_REQ
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"personal_data_consent": False},
        {"offer_acknowledgement": False},
        {"personal_data_consent": "true"},
        {"offer_acknowledgement": "true"},
        {"personal_data_consent": 1},
        {"offer_acknowledgement": 1},
        {"slot_id": "not-a-slot"},
        {"slot_id": f"bs2.{_SERVICE}.{_MASTER}.2026-08-20.1000"},
        {"slot_id": f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.10:00"},
    ],
)
def test_confirm_action_rejects_invalid_consents_and_slot_ids(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SyntheticConfirmSelectedSlotAction.model_validate(_valid_action(**overrides))


@pytest.mark.parametrize(
    "bad_request_id",
    [
        "",
        " ",
        "req id",
        "req\nid",
        "req\x00",
        "требование",
        "x" * (REQUEST_ID_MAX_LENGTH + 1),
        123,
        True,
        None,
    ],
)
def test_confirm_rejects_missing_or_invalid_pii_admission_request_id(
    bad_request_id: object,
) -> None:
    payload = _valid_action()
    if bad_request_id is None:
        del payload["pii_admission_request_id"]
    else:
        payload["pii_admission_request_id"] = bad_request_id  # type: ignore[assignment]
    with pytest.raises(ValidationError):
        SyntheticConfirmSelectedSlotAction.model_validate(payload)


@pytest.mark.parametrize(
    "forbidden",
    [
        {"starts_at": "2026-08-20T10:00:00+05:00"},
        {"idempotency_key": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"},
        {"phone": "+79001234567"},
        {"client_name": "Ivan"},
        {"phone_ref_token": "tok-phone"},
        {"name_ref_token": "tok-name"},
        {"phone_ref": "tok-phone"},
        {"name_ref": "tok-name"},
    ],
)
def test_confirm_action_forbids_internal_and_pii_fields(
    forbidden: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SyntheticConfirmSelectedSlotAction.model_validate(
            _valid_action(**forbidden)
        )


def test_inbound_action_uses_envelope_ids_not_action_body() -> None:
    event = SyntheticInboundEvent(
        channel="synthetic",
        external_conversation_id="conv-confirm-1",
        external_message_id="msg-confirm-1",
        text="structured-confirm",
        action=SyntheticConfirmSelectedSlotAction.model_validate(_valid_action()),
    )
    assert event.channel == "synthetic"
    assert event.external_conversation_id == "conv-confirm-1"
    assert event.external_message_id == "msg-confirm-1"
    assert event.preserves_active_offer() is True
    payload = event.safe_payload()
    assert payload == {
        "schema": "synthetic.inbound.v1",
        "text": "structured-confirm",
    }
    assert "action" not in payload
    assert "slot_id" not in payload
    assert "pii_admission_request_id" not in payload
    assert _SLOT not in repr(event)
    assert _PII_REQ not in repr(event)
    assert event.redacted_view()["action_kind"] == "CONFIRM_SELECTED_SLOT"


def test_inbound_rejects_booking_and_action_together() -> None:
    from datetime import datetime, timezone

    from app.schemas.booking_input import SyntheticBookingInput

    booking = SyntheticBookingInput(
        service_id=_SERVICE,
        include_alternatives=False,
        decision_at=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
    )
    with pytest.raises(ValidationError):
        SyntheticInboundEvent(
            external_conversation_id="conv-1",
            external_message_id="msg-1",
            text="x",
            booking=booking,
            action=SyntheticConfirmSelectedSlotAction.model_validate(
                _valid_action()
            ),
        )


def test_ingress_envelope_roundtrips_action_without_admit_fields() -> None:
    ingress = SyntheticIngressEvent(
        external_event_id="e-confirm-1",
        external_conversation_id="c-confirm-1",
        text="structured-confirm",
        action=SyntheticConfirmSelectedSlotAction.model_validate(_valid_action()),
    )
    envelope = ingress.safe_envelope()
    assert envelope["action"]["kind"] == "CONFIRM_SELECTED_SLOT"
    assert envelope["action"]["slot_id"] == _SLOT
    assert envelope["action"]["pii_admission_request_id"] == _PII_REQ
    assert "starts_at" not in envelope["action"]
    assert "idempotency_key" not in envelope["action"]
    assert "phone" not in envelope["action"]
    assert "client_name" not in envelope["action"]
    assert "phone_ref_token" not in envelope["action"]
    assert "name_ref_token" not in envelope["action"]
    inbound = SyntheticInboundEvent(
        channel="synthetic",
        external_conversation_id=ingress.external_conversation_id,
        external_message_id=ingress.external_event_id,
        text=envelope["text"],
        action=SyntheticConfirmSelectedSlotAction.model_validate(
            envelope["action"]
        ),
    )
    assert inbound.preserves_active_offer() is True
    assert inbound.action is not None
    assert inbound.action.pii_admission_request_id == _PII_REQ
    assert "pii_admission_request_id" not in inbound.safe_payload()
    plan = client_reply_plan_payload(inbox_id="inbox-x", booking=inbound.booking)
    assert "booking" not in plan or plan.get("booking") is None


def test_confirm_schema_source_has_no_admit_or_create_hooks() -> None:
    confirm = (_REPO / "app/schemas/self_booking_confirm_action.py").read_text(
        encoding="utf-8"
    )
    inbound = (_REPO / "app/services/inbound.py").read_text(encoding="utf-8")
    assert "require_pii_admission_request_id" in confirm
    assert "pii_admission_request_id" in confirm
    assert "admit_confirmed" not in confirm
    assert ".confirm_selected_slot" not in confirm
    assert "SelfBookingPiiAdmissionService" not in confirm
    assert "admit(" not in confirm
    assert "admit_confirmed" not in inbound
    assert ".confirm_selected_slot" not in inbound
    assert "SelfBookingPiiAdmissionService" not in inbound
    assert "preserves_active_offer" in inbound
    assert "SelfBookingActiveOfferService" in inbound
