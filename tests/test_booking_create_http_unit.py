"""Mutation-sensitive unit tests for CURSOR-25 booking create S2S client.

Uses a fake S2sHttpTransport only. No live network, env, Docker, channels,
or automatic UUID generation inside the adapter.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any
from urllib.parse import urlsplit

import pytest

from app.config import Settings
from app.core.booking_create_http import (
    BOOKINGS_ROUTE_PATH,
    MAX_BOOKING_CREATE_REQUEST_BYTES,
    BookingCreateHttpClient,
    BookingCreateHttpError,
    encode_booking_create_request_body,
)
from app.core.booking_create_remote import (
    BookingCreateRemoteSuccess,
    build_booking_create_remote_request,
    expected_canonical_starts_at_from_slot_parts,
    parse_bot_slot_id,
    parse_booking_create_success_payload,
)
from app.core.booking_eligibility_factory import (
    build_booking_create_client,
    build_booking_s2s_clients,
    build_booking_s2s_config,
)
from app.core.booking_eligibility_http import BookingEligibilityHttpConfig
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransportError,
)

_VALID_TOKEN = "t" * 32
_SERVICE_UUID = "11111111-1111-4111-8111-111111111111"
_MASTER_UUID = "22222222-2222-4222-8222-222222222222"
_BOOKING_UUID = "33333333-3333-4333-8333-333333333333"
_IDEMPOTENCY_KEY = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_DATE = "2026-08-06"
_SLOT_ID = f"bs1.{_SERVICE_UUID}.{_MASTER_UUID}.{_DATE}.1000"
_CLIENT_NAME = "Иван Тестов"
_PHONE = "+79001234567"
_STARTS_AT = "2026-08-06T10:00:00+05:00"


class FakeTransport:
    def __init__(
        self,
        *,
        response: S2sHttpResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._response = response
        self._error = error
        self.calls: list[S2sHttpRequest] = []

    def request(self, request: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        if self._response is None:
            raise S2sHttpTransportError("TRANSPORT_ERROR")
        return self._response


def _config(**overrides: Any) -> BookingEligibilityHttpConfig:
    values: dict[str, Any] = {
        "base_url": "https://eligibility.example",
        "bearer_token": _VALID_TOKEN,
        "timeout_seconds": 3.5,
        "max_response_bytes": 4096,
    }
    values.update(overrides)
    return BookingEligibilityHttpConfig(**values)


def _json_response(
    payload: object,
    *,
    status: int = 200,
    content_type: str = "application/json",
) -> S2sHttpResponse:
    body = b""
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
    return S2sHttpResponse(status_code=status, headers=headers, body=body)


def _client(transport: FakeTransport, **config_overrides: Any) -> BookingCreateHttpClient:
    return BookingCreateHttpClient(_config(**config_overrides), transport)


def _success(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "bookingId": _BOOKING_UUID,
        "slotId": _SLOT_ID,
        "status": "SCHEDULED",
        "startsAt": _STARTS_AT,
        "idempotentReplay": False,
    }
    payload.update(overrides)
    return payload


def _create_kwargs(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "idempotency_key": _IDEMPOTENCY_KEY,
        "slot_id": _SLOT_ID,
        "client_name": _CLIENT_NAME,
        "phone": _PHONE,
        "personal_data_consent": True,
        "offer_acknowledgement": True,
    }
    values.update(overrides)
    return values


def _assert_no_secrets(text: str, *, body_fragment: str | None = None) -> None:
    assert _VALID_TOKEN not in text
    assert "Authorization" not in text
    assert "https://eligibility.example" not in text
    assert _CLIENT_NAME not in text
    assert _PHONE not in text
    assert _SLOT_ID not in text
    assert _BOOKING_UUID not in text
    if body_fragment is not None:
        assert body_fragment not in text


# ---------------------------------------------------------------------------
# Exact request
# ---------------------------------------------------------------------------


def test_exact_request_method_path_headers_body() -> None:
    transport = FakeTransport(response=_json_response(_success()))
    result = _client(transport).create_booking(**_create_kwargs())
    assert type(result) is BookingCreateRemoteSuccess
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call.method == "POST"
    assert urlsplit(call.url).path == BOOKINGS_ROUTE_PATH
    assert call.allow_redirects is False
    assert call.headers["Content-Type"] == "application/json"
    assert call.headers["Accept"] == "application/json"
    assert call.headers["Authorization"] == f"Bearer {_VALID_TOKEN}"
    body = json.loads(call.body.decode("utf-8"))
    assert body == {
        "idempotencyKey": _IDEMPOTENCY_KEY,
        "slotId": _SLOT_ID,
        "clientName": _CLIENT_NAME,
        "phone": _PHONE,
        "personalDataConsent": True,
        "offerAcknowledgement": True,
    }
    assert set(body) == {
        "idempotencyKey",
        "slotId",
        "clientName",
        "phone",
        "personalDataConsent",
        "offerAcknowledgement",
    }


def test_caller_supplied_idempotency_key_unchanged() -> None:
    transport = FakeTransport(response=_json_response(_success()))
    _client(transport).create_booking(**_create_kwargs())
    body = json.loads(transport.calls[0].body.decode("utf-8"))
    assert body["idempotencyKey"] == _IDEMPOTENCY_KEY


def test_repeat_call_reuses_same_key() -> None:
    transport = FakeTransport(response=_json_response(_success(idempotentReplay=True)))
    client = _client(transport)
    first = client.create_booking(**_create_kwargs())
    second = client.create_booking(**_create_kwargs())
    assert first.idempotent_replay is True
    assert second.idempotent_replay is True
    assert len(transport.calls) == 2
    keys = [
        json.loads(call.body.decode("utf-8"))["idempotencyKey"]
        for call in transport.calls
    ]
    assert keys == [_IDEMPOTENCY_KEY, _IDEMPOTENCY_KEY]


def test_client_does_not_generate_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("uuid must not be generated inside create client")

    monkeypatch.setattr(uuid, "uuid4", _boom)
    monkeypatch.setattr(uuid, "uuid1", _boom)
    transport = FakeTransport(response=_json_response(_success()))
    _client(transport).create_booking(**_create_kwargs())
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# Pre-network validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"personal_data_consent": False},
        {"offer_acknowledgement": False},
        {"personal_data_consent": "true"},
        {"offer_acknowledgement": 1},
        {"client_name": "A"},
        {"client_name": ""},
        {"phone": "79001234567"},
        {"phone": "+7900"},
        {"phone": "+79001234567890123"},
        {"slot_id": "not-a-slot"},
        {"slot_id": f"bs2.{_SERVICE_UUID}.{_MASTER_UUID}.{_DATE}.1000"},
        {"idempotency_key": "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"},
        {"idempotency_key": "not-a-uuid"},
        {"idempotency_key": ""},
    ],
)
def test_invalid_inputs_blocked_before_network(kwargs: dict[str, Any]) -> None:
    transport = FakeTransport(response=_json_response(_success()))
    with pytest.raises(BookingCreateHttpError) as exc_info:
        _client(transport).create_booking(**_create_kwargs(**kwargs))
    assert exc_info.value.code == "REQUEST_INVALID"
    assert transport.calls == []
    rendered = f"{exc_info.value!s}{exc_info.value!r}"
    _assert_no_secrets(rendered)


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


def test_successful_new_booking() -> None:
    transport = FakeTransport(response=_json_response(_success()))
    result = _client(transport).create_booking(**_create_kwargs())
    assert result.booking_id == _BOOKING_UUID
    assert result.slot_id == _SLOT_ID
    assert result.starts_at == _STARTS_AT
    assert result.idempotent_replay is False


def test_successful_idempotent_replay() -> None:
    transport = FakeTransport(response=_json_response(_success(idempotentReplay=True)))
    result = _client(transport).create_booking(**_create_kwargs())
    assert result.idempotent_replay is True
    assert result.booking_id == _BOOKING_UUID


def test_success_starts_at_must_match_slot_id_time() -> None:
    """F1: parser binds startsAt to date/time encoded in slotId."""

    request = build_booking_create_remote_request(**_create_kwargs())
    parts = parse_bot_slot_id(_SLOT_ID)
    assert expected_canonical_starts_at_from_slot_parts(parts) == _STARTS_AT

    ok = parse_booking_create_success_payload(_success(), request=request)
    assert ok is not None
    assert ok.starts_at == _STARTS_AT

    replay = parse_booking_create_success_payload(
        _success(idempotentReplay=True), request=request
    )
    assert replay is not None
    assert replay.idempotent_replay is True

    # Same slotId (10:00) but different minute in startsAt must fail closed.
    assert (
        parse_booking_create_success_payload(
            _success(startsAt="2026-08-06T10:30:00+05:00"),
            request=request,
        )
        is None
    )
    assert (
        parse_booking_create_success_payload(
            _success(startsAt="2026-08-07T10:00:00+05:00"),
            request=request,
        )
        is None
    )
    assert (
        parse_booking_create_success_payload(
            _success(startsAt="2026-08-06T10:00:00+00:00"),
            request=request,
        )
        is None
    )
    assert (
        parse_booking_create_success_payload(
            _success(startsAt="2026-08-06T10:00:01+05:00"),
            request=request,
        )
        is None
    )

    transport = FakeTransport(
        response=_json_response(_success(startsAt="2026-08-06T10:30:00+05:00"))
    )
    with pytest.raises(BookingCreateHttpError) as exc_info:
        _client(transport).create_booking(**_create_kwargs())
    assert exc_info.value.code == "RESPONSE_INVALID"
    rendered = f"{exc_info.value!s}{exc_info.value!r}"
    _assert_no_secrets(rendered)
    assert _STARTS_AT not in rendered
    assert "10:30" not in rendered


def test_max_valid_public_request_under_byte_bound() -> None:
    """Architectural fact: valid max fields cannot exceed the request byte cap."""

    request = build_booking_create_remote_request(
        idempotency_key=_IDEMPOTENCY_KEY,
        slot_id=_SLOT_ID,
        client_name="A" * 256,
        phone="+" + ("1" * 15),
        personal_data_consent=True,
        offer_acknowledgement=True,
    )
    body = encode_booking_create_request_body(request.to_json_object())
    assert len(body) < MAX_BOOKING_CREATE_REQUEST_BYTES
    assert len(body) < 4096


def test_encode_helper_rejects_oversized_body_without_pii_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense-in-depth bound: oversized synthetic body fails before transport."""

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("uuid must not be generated for oversized encode")

    monkeypatch.setattr(uuid, "uuid4", _boom)
    secret_name = "Иван Секретный"
    secret_phone = "+79009998877"
    oversized = {
        "idempotencyKey": _IDEMPOTENCY_KEY,
        "slotId": _SLOT_ID,
        "clientName": secret_name,
        "phone": secret_phone,
        "personalDataConsent": True,
        "offerAcknowledgement": True,
        "pad": "x" * (MAX_BOOKING_CREATE_REQUEST_BYTES + 64),
    }
    with pytest.raises(BookingCreateHttpError) as exc_info:
        encode_booking_create_request_body(oversized)
    assert exc_info.value.code == "REQUEST_INVALID"
    rendered = f"{exc_info.value!s}{exc_info.value!r}"
    assert secret_name not in rendered
    assert secret_phone not in rendered
    assert _SLOT_ID not in rendered
    assert _IDEMPOTENCY_KEY not in rendered
    assert "pad" not in rendered
    assert "xxxx" not in rendered


@pytest.mark.parametrize(
    "overrides",
    [
        {"bookingId": "not-uuid"},
        {"bookingId": "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"},
        {"slotId": f"bs1.{_SERVICE_UUID}.{_MASTER_UUID}.{_DATE}.1030"},
        {"status": "CANCELLED"},
        {"status": "scheduled"},
        {"startsAt": "2026-08-06T10:00:00Z"},
        {"startsAt": "2026-08-06T10:00:01+05:00"},
        {"startsAt": "2026-08-06T10:30:00+05:00"},
        {"idempotentReplay": "false"},
        {"idempotentReplay": 0},
        {"ok": False},
        {"ok": "true"},
        {"extra": True},
    ],
)
def test_success_fields_strictly_validated(overrides: dict[str, Any]) -> None:
    payload = _success()
    if "extra" in overrides:
        payload["extra"] = overrides.pop("extra")
    payload.update(overrides)
    transport = FakeTransport(response=_json_response(payload))
    with pytest.raises(BookingCreateHttpError) as exc_info:
        _client(transport).create_booking(**_create_kwargs())
    assert exc_info.value.code == "RESPONSE_INVALID"


def test_success_missing_key_fail_closed() -> None:
    payload = _success()
    del payload["bookingId"]
    transport = FakeTransport(response=_json_response(payload))
    with pytest.raises(BookingCreateHttpError) as exc_info:
        _client(transport).create_booking(**_create_kwargs())
    assert exc_info.value.code == "RESPONSE_INVALID"


def test_wrong_content_type_fail_closed() -> None:
    body = json.dumps(_success()).encode("utf-8")
    transport = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={"Content-Type": "text/plain", "Content-Length": str(len(body))},
            body=body,
        )
    )
    with pytest.raises(BookingCreateHttpError) as exc_info:
        _client(transport).create_booking(**_create_kwargs())
    assert exc_info.value.code == "RESPONSE_INVALID"


def test_oversized_response_fail_closed() -> None:
    transport = FakeTransport(error=S2sHttpTransportError("RESPONSE_TOO_LARGE"))
    with pytest.raises(BookingCreateHttpError) as exc_info:
        _client(transport).create_booking(**_create_kwargs())
    assert exc_info.value.code == "RESPONSE_TOO_LARGE"


def test_empty_body_fail_closed() -> None:
    transport = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json", "Content-Length": "0"},
            body=b"",
        )
    )
    with pytest.raises(BookingCreateHttpError) as exc_info:
        _client(transport).create_booking(**_create_kwargs())
    assert exc_info.value.code == "RESPONSE_INVALID"


def test_invalid_utf8_fail_closed() -> None:
    transport = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json", "Content-Length": "2"},
            body=b"\xff\xfe",
        )
    )
    with pytest.raises(BookingCreateHttpError) as exc_info:
        _client(transport).create_booking(**_create_kwargs())
    assert exc_info.value.code == "RESPONSE_INVALID"


def test_invalid_json_fail_closed() -> None:
    transport = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=b"{not-json",
        )
    )
    with pytest.raises(BookingCreateHttpError) as exc_info:
        _client(transport).create_booking(**_create_kwargs())
    assert exc_info.value.code == "RESPONSE_INVALID"


def test_html_instead_of_json_fail_closed() -> None:
    html = b"<html><body>secret</body></html>"
    transport = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={"Content-Type": "text/html", "Content-Length": str(len(html))},
            body=html,
        )
    )
    with pytest.raises(BookingCreateHttpError) as exc_info:
        _client(transport).create_booking(**_create_kwargs())
    assert exc_info.value.code == "RESPONSE_INVALID"
    _assert_no_secrets(f"{exc_info.value!s}{exc_info.value!r}", body_fragment="secret")


# ---------------------------------------------------------------------------
# Error envelopes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "remote_code", "expected"),
    [
        (400, "VALIDATION_ERROR", "VALIDATION_ERROR"),
        (400, "SLOT_INVALID", "SLOT_INVALID"),
        (400, "SERVICE_UNAVAILABLE", "SERVICE_UNAVAILABLE"),
        (400, "MASTER_UNAVAILABLE", "MASTER_UNAVAILABLE"),
        (400, "SERVICE_MASTER_MISMATCH", "SERVICE_MASTER_MISMATCH"),
        (400, "BOOKING_REQUEST_INVALID", "BOOKING_REQUEST_INVALID"),
        (401, "UNAUTHORIZED", "UNAUTHORIZED"),
        (409, "IDEMPOTENCY_CONFLICT", "IDEMPOTENCY_CONFLICT"),
        (409, "IDEMPOTENCY_IN_PROGRESS", "IDEMPOTENCY_IN_PROGRESS"),
        (409, "SLOT_NO_LONGER_AVAILABLE", "SLOT_NO_LONGER_AVAILABLE"),
        (409, "CLIENT_AMBIGUOUS", "CLIENT_AMBIGUOUS"),
        (409, "BOOKING_REQUEST_CONFLICT", "BOOKING_REQUEST_CONFLICT"),
        (409, "BOOKING_CONFLICT", "BOOKING_CONFLICT"),
        (413, "PAYLOAD_TOO_LARGE", "PAYLOAD_TOO_LARGE"),
        (429, "RATE_LIMITED", "RATE_LIMITED"),
        (500, "INTERNAL_ERROR", "INTERNAL_ERROR"),
    ],
)
def test_allowed_status_code_error_envelopes(
    status: int, remote_code: str, expected: str
) -> None:
    secret = "upstream secret detail must not leak"
    transport = FakeTransport(
        response=_json_response(
            {"ok": False, "code": remote_code, "error": secret},
            status=status,
        )
    )
    with pytest.raises(BookingCreateHttpError) as exc_info:
        _client(transport).create_booking(**_create_kwargs())
    assert exc_info.value.code == expected
    rendered = f"{exc_info.value!s}{exc_info.value!r}"
    _assert_no_secrets(rendered, body_fragment=secret)


@pytest.mark.parametrize(
    ("status", "remote_code"),
    [
        (400, "INTERNAL_ERROR"),
        (409, "VALIDATION_ERROR"),
        (401, "RATE_LIMITED"),
        (502, "INTERNAL_ERROR"),
        (418, "VALIDATION_ERROR"),
        (500, "UNKNOWN_CODE"),
    ],
)
def test_unknown_status_code_pair_fail_closed(status: int, remote_code: str) -> None:
    transport = FakeTransport(
        response=_json_response(
            {"ok": False, "code": remote_code, "error": "x"},
            status=status,
        )
    )
    with pytest.raises(BookingCreateHttpError) as exc_info:
        _client(transport).create_booking(**_create_kwargs())
    assert exc_info.value.code == "REMOTE_REJECTED"


def test_timeout_maps_to_timeout() -> None:
    transport = FakeTransport(error=S2sHttpTransportError("TIMEOUT"))
    with pytest.raises(BookingCreateHttpError) as exc_info:
        _client(transport).create_booking(**_create_kwargs())
    assert exc_info.value.code == "TIMEOUT"


def test_transport_error_maps() -> None:
    transport = FakeTransport(error=S2sHttpTransportError("TRANSPORT_ERROR"))
    with pytest.raises(BookingCreateHttpError) as exc_info:
        _client(transport).create_booking(**_create_kwargs())
    assert exc_info.value.code == "TRANSPORT_ERROR"


def test_redirects_forbidden_on_request() -> None:
    transport = FakeTransport(response=_json_response(_success()))
    _client(transport).create_booking(**_create_kwargs())
    assert transport.calls[0].allow_redirects is False


def test_logs_and_repr_exclude_pii(caplog: pytest.LogCaptureFixture) -> None:
    secret = "do-not-log-this-body"
    transport = FakeTransport(
        response=_json_response(
            {"ok": False, "code": "UNAUTHORIZED", "error": secret},
            status=401,
        )
    )
    with caplog.at_level(logging.INFO):
        with pytest.raises(BookingCreateHttpError) as exc_info:
            _client(transport).create_booking(**_create_kwargs())
    joined = " ".join(record.getMessage() for record in caplog.records)
    rendered = f"{exc_info.value!s}{exc_info.value!r}{joined}"
    _assert_no_secrets(rendered, body_fragment=secret)
    req = build_booking_create_remote_request(**_create_kwargs())
    _assert_no_secrets(repr(req))


def test_factory_builds_create_client_without_io() -> None:
    settings = Settings.from_env(
        {
            "BOOKING_ELIGIBILITY_BASE_URL": "https://eligibility.example",
            "BOOKING_ELIGIBILITY_BEARER_TOKEN": _VALID_TOKEN,
        }
    )
    transport = FakeTransport(response=_json_response(_success()))
    client = build_booking_create_client(settings, transport=transport)
    assert client is not None
    assert transport.calls == []
    clients = build_booking_s2s_clients(settings, transport=transport)
    assert clients.booking_create is not None
    assert clients.booking_create._transport is transport
    assert build_booking_s2s_config(settings) is not None


def test_unconfigured_create_client_is_none() -> None:
    settings = Settings.from_env({})
    assert build_booking_create_client(settings) is None
    clients = build_booking_s2s_clients(settings)
    assert clients.eligibility is None
    assert clients.availability is None
    assert clients.booking_create is None
    assert clients.transport is None


def test_route_path_canonical() -> None:
    assert BOOKINGS_ROUTE_PATH == "/api/internal/bot/v1/bookings"
    assert _config().bookings_url.endswith(BOOKINGS_ROUTE_PATH)
