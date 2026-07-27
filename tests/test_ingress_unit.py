from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.outbound_policy import OutboundAction, is_automatic_outbound_allowed
from app.config import Settings
from app.models.ingress import (
    INGRESS_TRANSITIONS,
    IngressStatus,
    ingress_transition_allowed,
)
from app.schemas.ingress import SyntheticIngressEvent
from app.services import ingress as ingress_service
from app.services.ingress import assert_no_client_outbound_path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_ingress_transitions_are_explicit_and_closed() -> None:
    assert set(INGRESS_TRANSITIONS) == set(IngressStatus)
    allowed = {
        (IngressStatus.RECEIVED, IngressStatus.PROCESSING),
        (IngressStatus.PROCESSING, IngressStatus.PROCESSED),
        (IngressStatus.PROCESSING, IngressStatus.FAILED),
        (IngressStatus.PROCESSING, IngressStatus.DEAD),
        (IngressStatus.FAILED, IngressStatus.PROCESSING),
        (IngressStatus.FAILED, IngressStatus.DEAD),
    }
    for current, targets in INGRESS_TRANSITIONS.items():
        for target in IngressStatus:
            expect = (current, target) in allowed
            assert ingress_transition_allowed(current, target) is expect


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (IngressStatus.RECEIVED, IngressStatus.PROCESSED),
        (IngressStatus.RECEIVED, IngressStatus.FAILED),
        (IngressStatus.RECEIVED, IngressStatus.DEAD),
        (IngressStatus.PROCESSED, IngressStatus.PROCESSING),
        (IngressStatus.DEAD, IngressStatus.PROCESSING),
        (IngressStatus.FAILED, IngressStatus.RECEIVED),
        (IngressStatus.PROCESSING, IngressStatus.RECEIVED),
    ],
)
def test_forbidden_ingress_transitions(
    current: IngressStatus,
    target: IngressStatus,
) -> None:
    assert ingress_transition_allowed(current, target) is False


def test_invalid_ingress_status_rejected() -> None:
    with pytest.raises(ValueError):
        IngressStatus("SENT")
    with pytest.raises(ValueError):
        IngressStatus("UNKNOWN")
    with pytest.raises(ValueError):
        ingress_transition_allowed("RECEIVED", "SENT")


def test_synthetic_ingress_event_rejects_secrets_and_pii() -> None:
    with pytest.raises(ValidationError):
        SyntheticIngressEvent(
            external_event_id="evt-1",
            external_conversation_id="conv-1",
            text="hello",
            token="leak",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        SyntheticIngressEvent.model_validate(
            {
                "external_event_id": "evt-1",
                "external_conversation_id": "conv-1",
                "text": "hello",
                "phone": "+10000000000",
            }
        )
    with pytest.raises(ValidationError):
        SyntheticIngressEvent.model_validate(
            {
                "external_event_id": "evt-1",
                "external_conversation_id": "conv-1",
                "text": "hello",
                "signature": "sig",
            }
        )


def test_synthetic_ingress_repr_redacts_text() -> None:
    event = SyntheticIngressEvent(
        external_event_id="evt-1",
        external_conversation_id="conv-1",
        text="client-secret-message",
    )
    rendered = repr(event)
    assert "client-secret-message" not in rendered
    assert "<redacted>" in rendered
    assert event.redacted_view()["text"] == "<redacted>"
    envelope = event.safe_envelope()
    assert envelope["schema"] == "synthetic.ingress.v1"
    assert envelope["text"] == "client-secret-message"


def test_ingress_service_has_no_outbound_path() -> None:
    assert_no_client_outbound_path()
    source = (_REPO_ROOT / "app" / "services" / "ingress.py").read_text(
        encoding="utf-8"
    )
    banned = (
        "def send_to_client",
        "def publish_outbound",
        "transport_send(",
        "DeliveryStatus.SENT",
        'delivery_status="SENT"',
    )
    for token in banned:
        assert token not in source
    assert is_automatic_outbound_allowed(Settings(), OutboundAction.SEND_MESSAGE) is False


def test_ingress_modules_do_not_weaken_fail_closed() -> None:
    roots = [
        _REPO_ROOT / "app" / "services" / "ingress.py",
        _REPO_ROOT / "app" / "repositories" / "ingress.py",
        _REPO_ROOT / "app" / "models" / "ingress.py",
        _REPO_ROOT / "app" / "schemas" / "ingress.py",
    ]
    for path in roots:
        text = path.read_text(encoding="utf-8")
        assert "AUTO_WRITE" not in text or "fail-closed" in text.lower()
        assert "webhook" not in text.lower() or "not" in text.lower()
        assert "vk.com" not in text.lower()
        assert "telegram" not in text.lower()


def test_ingress_persist_error_message_has_no_payload() -> None:
    err = ingress_service.IngressPersistError("INGRESS_PERSIST_FAILED (OperationalError)")
    assert "password" not in str(err).lower()
    assert "://" not in str(err)
