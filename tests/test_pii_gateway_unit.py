from __future__ import annotations

import json
import traceback
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import BaseModel, Field

from app.core import pii_gateway
from app.core.pii_gateway import (
    PiiGatewayError,
    assert_safe_mapping,
    fingerprint_for_log,
    redact_for_log,
    safe_fingerprint,
    sanitize_for_ai,
)
from app.models.inbox import InboxMessage, MessageDirection, MessageType, ProcessingStatus
from app.models.ingress import IngressEvent, IngressEventType, IngressStatus
from app.models.manager_message import ManagerMessage, ManagerMessageStatus
from app.models.outbox import DeliveryStatus, DestinationType, OutboxMessage
from app.models.reply_plan import ReplyPlan, ReplyPlanStatus, ReplyPlanType
from app.schemas.inbound import SyntheticInboundEvent
from app.schemas.ingress import SyntheticIngressEvent
from app.schemas.manager_message import SyntheticManagerMessageEvent
from app.services.dialog_context import (
    DialogContext,
    DialogMessage,
    to_ai_safe_messages,
)

_SYNTHETIC_PHONE_RU = "+7 (999) 123-45-67"
_SYNTHETIC_PHONE_8 = "8 (999) 123-45-68"
_SYNTHETIC_PHONE_INTL = "+44 20 7946 0958"
_SYNTHETIC_EMAIL = "client.alpha@example.invalid"
_SYNTHETIC_FIRST = "Синтетик"
_SYNTHETIC_LAST = "Тестов"
_SYNTHETIC_TEXT = (
    f"Здравствуйте, {_SYNTHETIC_FIRST} {_SYNTHETIC_LAST}, "
    f"телефон {_SYNTHETIC_PHONE_RU}, email {_SYNTHETIC_EMAIL} 🙂"
)
_EXTERNAL_CONV = "synthetic-conv-001"
_EXTERNAL_MSG = "synthetic-msg-001"
_EXTERNAL_EVENT = "synthetic-event-001"


def _assert_no_raw_pii(blob: str) -> None:
    forbidden = (
        _SYNTHETIC_PHONE_RU,
        _SYNTHETIC_PHONE_8,
        _SYNTHETIC_PHONE_INTL,
        _SYNTHETIC_EMAIL,
        _SYNTHETIC_FIRST,
        _SYNTHETIC_LAST,
        _EXTERNAL_CONV,
        _EXTERNAL_MSG,
        _EXTERNAL_EVENT,
        "9991234567",
        "client.alpha",
        "example.invalid",
    )
    for item in forbidden:
        assert item not in blob


def test_fingerprint_deterministic_per_process() -> None:
    first = fingerprint_for_log("same-value", purpose="test")
    second = fingerprint_for_log("same-value", purpose="test")
    assert first == second
    assert first.startswith("pii_fp:test:")
    assert len(first.split(":")[-1]) == 16


def test_fingerprint_different_purpose() -> None:
    a = fingerprint_for_log("same-value", purpose="alpha")
    b = fingerprint_for_log("same-value", purpose="beta")
    assert a != b


def test_fingerprint_does_not_contain_source() -> None:
    token = fingerprint_for_log(_SYNTHETIC_EMAIL, purpose="email")
    _assert_no_raw_pii(token)


def test_fingerprint_fail_closed_on_empty() -> None:
    with pytest.raises(PiiGatewayError, match="FINGERPRINT_VALUE_INVALID") as exc_info:
        fingerprint_for_log("   ", purpose="test")
    assert exc_info.value.__cause__ is None
    _assert_no_raw_pii(str(exc_info.value))


@pytest.mark.parametrize(
    "text",
    [
        _SYNTHETIC_PHONE_RU,
        _SYNTHETIC_PHONE_8,
        _SYNTHETIC_PHONE_INTL,
        _SYNTHETIC_EMAIL,
        _SYNTHETIC_TEXT,
        "Hello 🙂 email client.beta@example.invalid",
        "Mixed RU/EN +7-999-111-22-33 call",
    ],
)
def test_sanitize_for_ai_masks_patterns(text: str) -> None:
    sanitized = sanitize_for_ai(text, known_pii=(_SYNTHETIC_FIRST, _SYNTHETIC_LAST))
    _assert_no_raw_pii(sanitized)


@pytest.mark.parametrize(
    ("text", "max_chars"),
    [
        (f"prefix {_SYNTHETIC_PHONE_RU} suffix", 18),
        (f"prefix {_SYNTHETIC_EMAIL} suffix", 18),
        (f"prefix {_SYNTHETIC_FIRST} suffix", 12),
    ],
)
def test_sanitize_masks_before_truncation(text: str, max_chars: int) -> None:
    sanitized = sanitize_for_ai(
        text,
        known_pii=(_SYNTHETIC_FIRST, _SYNTHETIC_LAST),
        max_chars=max_chars,
    )
    _assert_no_raw_pii(sanitized)
    assert sanitized.endswith("<truncated>")


def test_sanitize_known_pii_longest_first() -> None:
    text = "Синтетик Тестов Синтетик"
    sanitized = sanitize_for_ai(
        text,
        known_pii=("Синтетик", "Синтетик Тестов"),
    )
    _assert_no_raw_pii(sanitized)
    assert sanitized.count("<redacted>") >= 1


def test_sanitize_truncates_long_text() -> None:
    sanitized = sanitize_for_ai("x" * 50, max_chars=10)
    assert sanitized == ("x" * 10) + "<truncated>"


@pytest.mark.parametrize(
    ("text", "expected_fragment"),
    [
        ("+7\u200b9991234567", "<redacted>"),
        ("+7.999.123.45.67", "<redacted>"),
        ("+7\u2013999\u2013123\u201345\u201367", "<redacted>"),
        ("+7\uff08999\uff09123-45-67", "<redacted>"),
        ("+7\u00a0999\u00a0123-45-67", "<redacted>"),
        ("+44.20.7946.0958", "<redacted>"),
    ],
)
def test_sanitize_unicode_phone_variants(text: str, expected_fragment: str) -> None:
    sanitized = sanitize_for_ai(text)
    assert expected_fragment in sanitized
    _assert_no_raw_pii(sanitized)


def test_sanitize_does_not_mask_uuid_date_price_short_number() -> None:
    sample_uuid = str(uuid.uuid4())
    assert sanitize_for_ai(sample_uuid) == sample_uuid
    assert sanitize_for_ai("2026-07-30") == "2026-07-30"
    assert sanitize_for_ai("price 42.50 RUB") == "price 42.50 RUB"
    assert sanitize_for_ai("order 42") == "order 42"


def test_sanitize_fail_closed_without_cause() -> None:
    def _boom(_text: str) -> str:
        raise RuntimeError(f"boom {_SYNTHETIC_EMAIL}")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(pii_gateway, "_EMAIL_RE", type("X", (), {"sub": _boom})())
    try:
        with pytest.raises(PiiGatewayError, match="SANITIZE_FAILED") as exc_info:
            sanitize_for_ai(_SYNTHETIC_EMAIL)
        assert exc_info.value.__cause__ is None
        _assert_no_raw_pii("".join(traceback.format_exception(exc_info.value)))
    finally:
        monkeypatch.undo()


def test_redact_sensitive_keys_case_insensitive() -> None:
    redacted = redact_for_log(
        {"TEXT": "secret", "Body_Text": _SYNTHETIC_EMAIL},
        allowed_keys=frozenset({"text", "body_text"}),
    )
    assert set(redacted.values()) == {"<redacted>"}
    _assert_no_raw_pii(repr(redacted))


def test_redact_unknown_key_not_allowed() -> None:
    redacted = redact_for_log(
        {"safe_status": "OK", "mystery": "value"},
        allowed_keys=frozenset({"safe_status"}),
    )
    assert redacted["safe_status"] == "OK"
    assert list(redacted.values()).count("<redacted>") == 1
    _assert_no_raw_pii(repr(redacted))


def test_redact_allowed_string_with_email() -> None:
    redacted = redact_for_log(
        {"safe_status": _SYNTHETIC_EMAIL},
        allowed_keys=frozenset({"safe_status"}),
    )
    assert redacted["safe_status"] == "<redacted>"


def test_redact_nested_structures() -> None:
    redacted = redact_for_log(
        {
            "items": [{"text": "hide"}],
            "tuple": (1, 2),
            "set": {3},
        },
        allowed_keys=frozenset({"items", "tuple", "set"}),
    )
    nested = redacted["items"][0]
    assert list(nested.values()) == ["<redacted>"]
    assert sorted(redacted["tuple"]) == [1, 2]
    assert redacted["set"] == [3]


@dataclass
class _SampleDataclass:
    status: str
    body_text: str


class _SampleModel(BaseModel):
    status: str
    body_text: str = Field(repr=False)


def test_redact_dataclass_and_pydantic() -> None:
    dc = _SampleDataclass(status="OK", body_text=_SYNTHETIC_TEXT)
    model = _SampleModel(status="OK", body_text=_SYNTHETIC_TEXT)
    dc_redacted = redact_for_log(dc, allowed_keys=frozenset({"status"}))
    model_redacted = redact_for_log(model, allowed_keys=frozenset({"status"}))
    assert dc_redacted["status"] == "OK"
    assert model_redacted["status"] == "OK"
    assert list(dc_redacted.values()).count("<redacted>") == 1
    assert list(model_redacted.values()).count("<redacted>") == 1
    _assert_no_raw_pii(repr(dc_redacted))
    _assert_no_raw_pii(repr(model_redacted))


class _DangerousRepr:
    def __repr__(self) -> str:
        return _SYNTHETIC_TEXT


class _DangerousStr:
    def __str__(self) -> str:
        return _SYNTHETIC_TEXT


class _DangerousProperty:
    @property
    def value(self) -> str:
        raise RuntimeError("lazy")


def test_redact_unknown_and_dangerous_objects() -> None:
    for obj in (_DangerousRepr(), _DangerousStr(), _DangerousProperty()):
        rendered = repr(redact_for_log(obj))
        assert rendered.startswith("'<unsupported:")
        _assert_no_raw_pii(rendered)


def test_redact_cyclic_dict_and_list() -> None:
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    assert redact_for_log(cyclic, allowed_keys=frozenset({"self"}))["self"] == "<cycle>"
    cyclic_list: list[Any] = []
    cyclic_list.append(cyclic_list)
    assert redact_for_log(cyclic_list) == ["<cycle>"]


def test_redact_max_depth() -> None:
    deep = {"a": {"b": {"c": {"d": "x"}}}}
    assert redact_for_log(deep, allowed_keys=frozenset({"a", "b", "c", "d"}), max_depth=2) == {
        "a": {"b": "<max-depth>"}
    }


def test_redact_max_nodes() -> None:
    wide = {f"k{i}": i for i in range(4)}
    result = redact_for_log(wide, allowed_keys=frozenset(wide.keys()), max_nodes=3)
    assert result["k0"] == 0
    assert result["k1"] == 1
    assert result["k2"] == "<max-nodes>"
    assert result["k3"] == "<max-nodes>"


def test_redact_long_string_truncated() -> None:
    assert redact_for_log("x" * 1000, max_string_chars=20) == "<truncated>"


def test_uuid_and_short_number_not_phone() -> None:
    sample_uuid = str(uuid.uuid4())
    assert redact_for_log(sample_uuid) != "<redacted>"
    assert redact_for_log("42") == "42"


def test_redact_fail_closed() -> None:
    def _boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("boom")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(pii_gateway, "_redact_string", _boom)
    try:
        assert redact_for_log("plain") == "<redaction-error>"
    finally:
        monkeypatch.undo()


def test_redact_phone_as_string_key() -> None:
    redacted = redact_for_log({_SYNTHETIC_PHONE_RU: "value"})
    assert _SYNTHETIC_PHONE_RU not in redacted
    assert list(redacted.values()) == ["<redacted>"]
    _assert_no_raw_pii(repr(redacted))


def test_redact_email_as_string_key() -> None:
    redacted = redact_for_log({_SYNTHETIC_EMAIL: "value"})
    assert _SYNTHETIC_EMAIL not in redacted
    _assert_no_raw_pii(repr(redacted))


class _DangerousKeyRepr:
    called = False

    def __repr__(self) -> str:
        type(self).called = True
        return _SYNTHETIC_PHONE_RU


class _DangerousKeyStr:
    called = False

    def __str__(self) -> str:
        type(self).called = True
        return _SYNTHETIC_PHONE_RU


def test_redact_non_string_key_does_not_call_repr_or_str() -> None:
    _DangerousKeyRepr.called = False
    _DangerousKeyStr.called = False
    redacted = redact_for_log({_DangerousKeyRepr(): "x", _DangerousKeyStr(): "y"})
    assert _DangerousKeyRepr.called is False
    assert _DangerousKeyStr.called is False
    _assert_no_raw_pii(repr(redacted))


class _HostileDict(dict):
    items_called = False

    def items(self):
        type(self).items_called = True
        yield (_SYNTHETIC_PHONE_RU, "hostile")


class _HostileMapping(Mapping):
    items_called = False

    def __getitem__(self, key: object) -> str:
        return "value"

    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return 0

    def items(self):
        type(self).items_called = True
        yield (_SYNTHETIC_PHONE_RU, "hostile")


def test_redact_hostile_dict_subclass_ignores_items() -> None:
    _HostileDict.items_called = False
    redacted = redact_for_log(_HostileDict())
    assert _HostileDict.items_called is False
    _assert_no_raw_pii(repr(redacted))


def test_redact_hostile_mapping_is_fail_closed() -> None:
    _HostileMapping.items_called = False
    redacted = redact_for_log(_HostileMapping())
    assert _HostileMapping.items_called is False
    assert redacted == "<untrusted-mapping>"


@pytest.mark.parametrize(
    ("key", "allowed", "should_redact_value"),
    [
        ("user-id", frozenset({"user-id"}), True),
        ("user_id", frozenset({"user_id"}), True),
        ("external-message-id", frozenset({"external-message-id"}), True),
        ("context", frozenset({"context"}), False),
        ("filename", frozenset({"filename"}), False),
        ("namespace", frozenset({"namespace"}), False),
        ("status_text_code", frozenset({"status_text_code"}), False),
    ],
)
def test_sensitive_key_normalization(
    key: str,
    allowed: frozenset[str],
    should_redact_value: bool,
) -> None:
    redacted = redact_for_log({key: "plain"}, allowed_keys=allowed)
    if should_redact_value:
        assert list(redacted.values()) == ["<redacted>"]
    else:
        assert redacted[key] == "plain"


def test_assert_safe_mapping_accepts_allowlist() -> None:
    assert_safe_mapping(
        {"status": "OK", "count": 1},
        allowlist=frozenset({"status", "count"}),
    )


def test_assert_safe_mapping_rejects_unknown_and_sensitive() -> None:
    with pytest.raises(PiiGatewayError, match="SAFE_MAPPING_UNKNOWN_KEY") as exc_info:
        assert_safe_mapping({"mystery": "x"}, allowlist=frozenset({"status"}))
    assert exc_info.value.__cause__ is None

    with pytest.raises(PiiGatewayError, match="SAFE_MAPPING_SENSITIVE_KEY") as exc_info:
        assert_safe_mapping({"text": "x"}, allowlist=frozenset({"text"}))
    assert exc_info.value.__cause__ is None


def test_assert_safe_mapping_rejects_raw_pii_strings() -> None:
    with pytest.raises(PiiGatewayError, match="SAFE_MAPPING_PII_STRING") as exc_info:
        assert_safe_mapping(
            {"status": _SYNTHETIC_EMAIL},
            allowlist=frozenset({"status"}),
        )
    assert exc_info.value.__cause__ is None


def test_assert_safe_mapping_rejects_sensitive_normalized_key() -> None:
    with pytest.raises(PiiGatewayError, match="SAFE_MAPPING_SENSITIVE_KEY"):
        assert_safe_mapping({"user-id": "x"}, allowlist=frozenset({"user-id"}))


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SyntheticInboundEvent(
            external_conversation_id=_EXTERNAL_CONV,
            external_message_id=_EXTERNAL_MSG,
            text=_SYNTHETIC_TEXT,
        ),
        lambda: SyntheticIngressEvent(
            external_event_id=_EXTERNAL_EVENT,
            external_conversation_id=_EXTERNAL_CONV,
            text=_SYNTHETIC_TEXT,
        ),
        lambda: SyntheticManagerMessageEvent(
            external_conversation_id=_EXTERNAL_CONV,
            external_message_id=_EXTERNAL_MSG,
            provider_sequence=1,
            text=_SYNTHETIC_TEXT,
        ),
    ],
)
def test_dto_repr_and_str_are_safe(factory) -> None:
    event = factory()
    for rendered in (repr(event), str(event), format(event), repr([event])):
        _assert_no_raw_pii(rendered)
    _assert_no_raw_pii(repr(event.redacted_view()))
    assert event.model_dump()["text"] == _SYNTHETIC_TEXT


def test_inbound_safe_payload_remains_storage_plaintext() -> None:
    event = SyntheticInboundEvent(
        external_conversation_id=_EXTERNAL_CONV,
        external_message_id=_EXTERNAL_MSG,
        text=_SYNTHETIC_TEXT,
    )
    assert event.safe_payload()["text"] == _SYNTHETIC_TEXT
    _assert_no_raw_pii(repr(event))


def test_ingress_safe_envelope_remains_storage_plaintext() -> None:
    event = SyntheticIngressEvent(
        external_event_id=_EXTERNAL_EVENT,
        external_conversation_id=_EXTERNAL_CONV,
        text=_SYNTHETIC_TEXT,
    )
    assert event.safe_envelope()["text"] == _SYNTHETIC_TEXT
    _assert_no_raw_pii(repr(event))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_orm_repr_safe() -> None:
    conversation_id = uuid.uuid4()
    inbox = InboxMessage(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        channel="synthetic",
        external_message_id=_EXTERNAL_MSG,
        direction=MessageDirection.INBOUND.value,
        message_type=MessageType.TEXT.value,
        payload_json={"schema": "synthetic.inbound.v1", "text": _SYNTHETIC_TEXT},
        received_at=_now(),
        processing_status=ProcessingStatus.RECEIVED.value,
        conversation_event_seq=1,
    )
    ingress = IngressEvent(
        id=uuid.uuid4(),
        channel="synthetic",
        external_event_id=_EXTERNAL_EVENT,
        external_conversation_id=_EXTERNAL_CONV,
        event_type=IngressEventType.SYNTHETIC_MESSAGE.value,
        status=IngressStatus.RECEIVED.value,
        correlation_id=uuid.uuid4(),
        envelope_json={"schema": "synthetic.ingress.v1", "text": _SYNTHETIC_TEXT},
    )
    manager = ManagerMessage(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        channel="synthetic",
        external_message_id=_EXTERNAL_MSG,
        body_text=_SYNTHETIC_TEXT,
        status=ManagerMessageStatus.APPLIED.value,
        conversation_event_seq=2,
    )
    outbox = OutboxMessage(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        destination_type=DestinationType.INTERNAL_DRAFT.value,
        delivery_status=DeliveryStatus.PENDING.value,
        idempotency_key=f"draft:{uuid.uuid4()}",
        payload_json={"schema": "internal.draft.v1", "draft_text": _SYNTHETIC_TEXT},
    )
    plan = ReplyPlan(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        context_version=1,
        plan_type=ReplyPlanType.CLIENT_REPLY.value,
        status=ReplyPlanStatus.PENDING.value,
        not_before=_now(),
        payload_json={"schema": "synthetic.reply_plan.v1"},
    )
    for obj in (inbox, ingress, manager, outbox, plan):
        rendered = repr(obj)
        _assert_no_raw_pii(rendered)
        assert str(conversation_id) not in rendered


def test_redact_sqlalchemy_object_without_relationships() -> None:
    conversation_id = uuid.uuid4()
    inbox = InboxMessage(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        channel="synthetic",
        external_message_id=_EXTERNAL_MSG,
        direction=MessageDirection.INBOUND.value,
        message_type=MessageType.TEXT.value,
        payload_json={"schema": "synthetic.inbound.v1", "text": _SYNTHETIC_TEXT},
        received_at=_now(),
        processing_status=ProcessingStatus.RECEIVED.value,
        conversation_event_seq=1,
    )
    redacted = redact_for_log(inbox, allowed_keys=frozenset({"channel", "processing_status"}))
    assert redacted == "<unsupported:InboxMessage>"
    _assert_no_raw_pii(repr(redacted))


def test_safe_fingerprint_hostile_str_returns_marker() -> None:
    class _HostileStr:
        def __str__(self) -> str:
            raise RuntimeError(f"boom {_SYNTHETIC_PHONE_RU}")

    token = safe_fingerprint(_HostileStr(), purpose="test")
    assert token == "<redacted>"
    _assert_no_raw_pii(token)


def test_dialog_context_repr_is_safe() -> None:
    conversation_id = uuid.uuid4()
    context = DialogContext(
        conversation_id=conversation_id,
        event_seq_hwm=3,
        messages=(DialogMessage(1, "client", _SYNTHETIC_TEXT),),
        total_chars=10,
    )
    rendered = repr(context)
    _assert_no_raw_pii(rendered)
    assert str(conversation_id) not in rendered
    assert "event_seq_hwm" not in rendered
    assert _SYNTHETIC_TEXT not in rendered


def test_to_ai_safe_messages_preserves_boundaries_without_ids() -> None:
    context = DialogContext(
        conversation_id=uuid.uuid4(),
        event_seq_hwm=2,
        messages=(
            DialogMessage(1, "client", _SYNTHETIC_TEXT),
            DialogMessage(2, "manager", f"Ответ для {_SYNTHETIC_EMAIL}"),
        ),
        total_chars=100,
    )
    safe = to_ai_safe_messages(
        context,
        known_pii=(_SYNTHETIC_FIRST, _SYNTHETIC_LAST),
    )
    assert len(safe) == 2
    assert safe[0]["author"] == "client"
    assert safe[1]["author"] == "manager"
    blob = repr(safe)
    _assert_no_raw_pii(blob)
    assert "conversation_id" not in blob
    assert "event_seq" not in blob


def _assert_exception_safe(exc: BaseException) -> None:
    _assert_no_raw_pii(str(exc))
    _assert_no_raw_pii(repr(exc))
    _assert_no_raw_pii("".join(traceback.format_exception(exc)))
    assert exc.__cause__ is None


@pytest.mark.parametrize("author", ["client", "manager"])
def test_dialog_message_repr_safe_author(author: str) -> None:
    message = DialogMessage(1, author, _SYNTHETIC_TEXT)
    rendered = repr(message)
    assert f"author={author!r}" in rendered
    assert "text=<redacted>" in rendered
    _assert_no_raw_pii(rendered)


@pytest.mark.parametrize("author", [_SYNTHETIC_PHONE_RU, _SYNTHETIC_EMAIL, "bot"])
def test_dialog_message_repr_redacts_unsafe_author(author: str) -> None:
    message = DialogMessage(1, author, _SYNTHETIC_TEXT)
    rendered = repr(message)
    assert "author='<redacted>'" in rendered
    assert author not in rendered
    assert _SYNTHETIC_TEXT not in rendered
    _assert_no_raw_pii(rendered)


def test_dialog_message_repr_hostile_author_does_not_call_str_or_repr() -> None:
    class _HostileAuthor:
        called = False

        def __str__(self) -> str:
            type(self).called = True
            return _SYNTHETIC_PHONE_RU

        def __repr__(self) -> str:
            type(self).called = True
            return _SYNTHETIC_EMAIL

    _HostileAuthor.called = False
    message = DialogMessage(1, _HostileAuthor(), _SYNTHETIC_TEXT)  # type: ignore[arg-type]
    rendered = repr(message)
    assert _HostileAuthor.called is False
    assert "author='<redacted>'" in rendered
    _assert_no_raw_pii(rendered)


@pytest.mark.parametrize("max_chars", [-1, -100, True, False, "10"])
def test_sanitize_rejects_invalid_max_chars(max_chars: object) -> None:
    with pytest.raises(PiiGatewayError, match="SANITIZE_LIMIT_INVALID") as exc_info:
        sanitize_for_ai(_SYNTHETIC_TEXT, max_chars=max_chars)  # type: ignore[arg-type]
    _assert_exception_safe(exc_info.value)


def test_sanitize_max_chars_zero_is_truncated_only() -> None:
    sanitized = sanitize_for_ai(f"prefix {_SYNTHETIC_PHONE_RU}", max_chars=0)
    assert sanitized == "<truncated>"
    _assert_no_raw_pii(sanitized)


def test_redact_non_finite_floats_are_json_safe() -> None:
    redacted = redact_for_log(
        {
            "nan": float("nan"),
            "inf": float("inf"),
            "neg_inf": float("-inf"),
            "nested": {"count": float("nan")},
            "finite": 1.5,
            "label": "nan",
        },
        allowed_keys=frozenset(
            {"nan", "inf", "neg_inf", "nested", "count", "finite", "label"}
        ),
    )
    assert redacted["nan"] == "<non-finite-number>"
    assert redacted["inf"] == "<non-finite-number>"
    assert redacted["neg_inf"] == "<non-finite-number>"
    assert redacted["nested"] == {"count": "<non-finite-number>"}
    assert redacted["finite"] == 1.5
    assert redacted["label"] == "nan"
    json.dumps(redacted, allow_nan=False)


class _HostileAllowlistItem:
    called = False

    def __str__(self) -> str:
        type(self).called = True
        return _SYNTHETIC_PHONE_RU

    def __repr__(self) -> str:
        type(self).called = True
        return _SYNTHETIC_EMAIL


def test_assert_safe_mapping_rejects_non_string_allowlist_item() -> None:
    _HostileAllowlistItem.called = False
    with pytest.raises(PiiGatewayError, match="SAFE_MAPPING_ALLOWLIST_INVALID") as exc_info:
        assert_safe_mapping(
            {"status": "OK"},
            allowlist=[_HostileAllowlistItem()],  # type: ignore[list-item]
        )
    assert _HostileAllowlistItem.called is False
    _assert_exception_safe(exc_info.value)


def test_assert_safe_mapping_rejects_generator_allowlist() -> None:
    def _allowlist() -> object:
        yield "status"

    with pytest.raises(PiiGatewayError, match="SAFE_MAPPING_ALLOWLIST_INVALID") as exc_info:
        assert_safe_mapping({"status": "OK"}, allowlist=_allowlist())  # type: ignore[arg-type]
    _assert_exception_safe(exc_info.value)


def test_assert_safe_mapping_allowlist_collection_types() -> None:
    for allowlist in (
        frozenset({"status", "count"}),
        {"status", "count"},
        ["status", "count"],
        ("status", "count"),
    ):
        assert_safe_mapping({"status": "OK", "count": 1}, allowlist=allowlist)


def test_assert_safe_mapping_normalize_failure_is_fail_closed() -> None:
    def _boom(_key: str) -> str:
        raise RuntimeError(f"boom {_SYNTHETIC_PHONE_RU} {_SYNTHETIC_EMAIL}")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(pii_gateway, "_normalize_key_name", _boom)
    try:
        with pytest.raises(PiiGatewayError, match="SAFE_MAPPING_FAILED") as exc_info:
            assert_safe_mapping({"status": "OK"}, allowlist=frozenset({"status"}))
        _assert_exception_safe(exc_info.value)
        assert "RuntimeError" not in "".join(traceback.format_exception(exc_info.value))
    finally:
        monkeypatch.undo()


@pytest.mark.parametrize(
    ("model_cls", "factory"),
    [
        (
            InboxMessage,
            lambda cid: InboxMessage(
                id=uuid.uuid4(),
                conversation_id=cid,
                channel="synthetic",
                external_message_id=_EXTERNAL_MSG,
                direction=MessageDirection.INBOUND.value,
                message_type=MessageType.TEXT.value,
                payload_json={"schema": "synthetic.inbound.v1", "text": _SYNTHETIC_TEXT},
                received_at=_now(),
                processing_status=ProcessingStatus.RECEIVED.value,
                conversation_event_seq=1,
            ),
        ),
        (
            IngressEvent,
            lambda cid: IngressEvent(
                id=uuid.uuid4(),
                channel="synthetic",
                external_event_id=_EXTERNAL_EVENT,
                external_conversation_id=_EXTERNAL_CONV,
                event_type=IngressEventType.SYNTHETIC_MESSAGE.value,
                status=IngressStatus.RECEIVED.value,
                correlation_id=uuid.uuid4(),
                envelope_json={"schema": "synthetic.ingress.v1", "text": _SYNTHETIC_TEXT},
            ),
        ),
        (
            ManagerMessage,
            lambda cid: ManagerMessage(
                id=uuid.uuid4(),
                conversation_id=cid,
                channel="synthetic",
                external_message_id=_EXTERNAL_MSG,
                body_text=_SYNTHETIC_TEXT,
                status=ManagerMessageStatus.APPLIED.value,
                conversation_event_seq=2,
            ),
        ),
        (
            OutboxMessage,
            lambda cid: OutboxMessage(
                id=uuid.uuid4(),
                conversation_id=cid,
                destination_type=DestinationType.INTERNAL_DRAFT.value,
                delivery_status=DeliveryStatus.PENDING.value,
                idempotency_key=f"draft:{uuid.uuid4()}",
                payload_json={"schema": "internal.draft.v1", "draft_text": _SYNTHETIC_TEXT},
            ),
        ),
        (
            ReplyPlan,
            lambda cid: ReplyPlan(
                id=uuid.uuid4(),
                conversation_id=cid,
                context_version=1,
                plan_type=ReplyPlanType.CLIENT_REPLY.value,
                status=ReplyPlanStatus.PENDING.value,
                not_before=_now(),
                payload_json={"schema": "synthetic.reply_plan.v1"},
            ),
        ),
    ],
)
def test_orm_repr_transient_and_incomplete(model_cls, factory) -> None:
    conversation_id = uuid.uuid4()
    populated = factory(conversation_id)
    populated_rendered = repr(populated)
    _assert_no_raw_pii(populated_rendered)
    assert str(conversation_id) not in populated_rendered
    assert _SYNTHETIC_TEXT not in populated_rendered

    transient = model_cls()
    transient_rendered = repr(transient)
    _assert_no_raw_pii(transient_rendered)
    assert "<unset>" in transient_rendered

    incomplete = object.__new__(model_cls)
    incomplete_rendered = repr(incomplete)
    _assert_no_raw_pii(incomplete_rendered)
    assert "<unset>" in incomplete_rendered


@pytest.mark.parametrize("author", ["+7 (999) 123-45-67", _SYNTHETIC_EMAIL, "bot"])
def test_to_ai_safe_messages_rejects_unsafe_author(author: str) -> None:
    context = DialogContext(
        conversation_id=uuid.uuid4(),
        event_seq_hwm=1,
        messages=(DialogMessage(1, author, "plain text"),),
        total_chars=10,
    )
    with pytest.raises(PiiGatewayError, match="AI_AUTHOR_INVALID") as exc_info:
        to_ai_safe_messages(context)
    assert exc_info.value.__cause__ is None
    _assert_no_raw_pii(str(exc_info.value))
    _assert_no_raw_pii("".join(traceback.format_exception(exc_info.value)))


class _FakeAuthor:
    hash_called = False
    eq_called = False
    str_called = False
    repr_called = False

    def __hash__(self) -> int:
        type(self).hash_called = True
        raise AssertionError("__hash__ must not be called")

    def __eq__(self, other: object) -> bool:
        type(self).eq_called = True
        raise AssertionError("__eq__ must not be called")

    def __str__(self) -> str:
        type(self).str_called = True
        return _SYNTHETIC_PHONE_RU

    def __repr__(self) -> str:
        type(self).repr_called = True
        return _SYNTHETIC_EMAIL


def test_to_ai_safe_messages_rejects_hostile_non_string_author() -> None:
    _FakeAuthor.hash_called = False
    _FakeAuthor.eq_called = False
    _FakeAuthor.str_called = False
    _FakeAuthor.repr_called = False
    context = DialogContext(
        conversation_id=uuid.uuid4(),
        event_seq_hwm=1,
        messages=(DialogMessage(1, _FakeAuthor(), "plain text"),),  # type: ignore[arg-type]
        total_chars=10,
    )
    with pytest.raises(PiiGatewayError) as exc_info:
        to_ai_safe_messages(context)
    exc = exc_info.value
    assert str(exc) == "AI_AUTHOR_INVALID"
    assert exc.__cause__ is None
    assert _FakeAuthor.hash_called is False
    assert _FakeAuthor.eq_called is False
    assert _FakeAuthor.str_called is False
    assert _FakeAuthor.repr_called is False
    _assert_no_raw_pii(str(exc))
    _assert_no_raw_pii(repr(exc))
    _assert_no_raw_pii("".join(traceback.format_exception(exc)))


class _ClientImpersonator:
    hash_called = False
    eq_called = False
    str_called = False
    repr_called = False

    def __hash__(self) -> int:
        type(self).hash_called = True
        return hash("client")

    def __eq__(self, other: object) -> bool:
        type(self).eq_called = True
        return other == "client"

    def __str__(self) -> str:
        type(self).str_called = True
        return _SYNTHETIC_PHONE_RU

    def __repr__(self) -> str:
        type(self).repr_called = True
        return _SYNTHETIC_EMAIL


def test_to_ai_safe_messages_rejects_client_impersonator_before_membership() -> None:
    _ClientImpersonator.hash_called = False
    _ClientImpersonator.eq_called = False
    _ClientImpersonator.str_called = False
    _ClientImpersonator.repr_called = False
    context = DialogContext(
        conversation_id=uuid.uuid4(),
        event_seq_hwm=1,
        messages=(DialogMessage(1, _ClientImpersonator(), "plain text"),),  # type: ignore[arg-type]
        total_chars=10,
    )
    with pytest.raises(PiiGatewayError, match="AI_AUTHOR_INVALID") as exc_info:
        to_ai_safe_messages(context)
    assert _ClientImpersonator.hash_called is False
    assert _ClientImpersonator.eq_called is False
    assert _ClientImpersonator.str_called is False
    assert _ClientImpersonator.repr_called is False
    _assert_exception_safe(exc_info.value)


class _FakeString(str):
    pass


def test_to_ai_safe_messages_rejects_str_subclass_author() -> None:
    context = DialogContext(
        conversation_id=uuid.uuid4(),
        event_seq_hwm=1,
        messages=(DialogMessage(1, _FakeString("client"), "plain text"),),  # type: ignore[arg-type]
        total_chars=10,
    )
    with pytest.raises(PiiGatewayError, match="AI_AUTHOR_INVALID") as exc_info:
        to_ai_safe_messages(context)
    _assert_exception_safe(exc_info.value)


def test_to_ai_safe_messages_accepts_exact_str_authors() -> None:
    context = DialogContext(
        conversation_id=uuid.uuid4(),
        event_seq_hwm=2,
        messages=(
            DialogMessage(1, "client", _SYNTHETIC_TEXT),
            DialogMessage(2, "manager", f"Ответ для {_SYNTHETIC_EMAIL}"),
        ),
        total_chars=100,
    )
    safe = to_ai_safe_messages(
        context,
        known_pii=(_SYNTHETIC_FIRST, _SYNTHETIC_LAST),
    )
    assert len(safe) == 2
    assert safe[0]["author"] == "client"
    assert safe[1]["author"] == "manager"
    assert type(safe[0]["author"]) is str
    assert type(safe[1]["author"]) is str
    _assert_no_raw_pii(safe[0]["text"])
    _assert_no_raw_pii(safe[1]["text"])
    blob = repr(safe)
    _assert_no_raw_pii(blob)
    assert "conversation_id" not in blob
    assert "event_seq" not in blob
