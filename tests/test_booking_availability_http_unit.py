"""Mutation-sensitive unit tests for CURSOR-22 booking availability S2S client.

Uses a fake S2sHttpTransport only. No live network, env, Docker, channels,
or booking writes. Empty success collections are valid; malformed responses
must raise and never become empty success.
"""

from __future__ import annotations

import json
import logging
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import pytest

from app.config import Settings
from app.core.booking_availability_http import (
    AVAILABLE_DAYS_ROUTE_PATH,
    SLOTS_ROUTE_PATH,
    BookingAvailabilityHttpClient,
    BookingAvailabilityHttpError,
    require_calendar_date,
    require_calendar_month,
)
from app.core.booking_availability_remote import (
    AvailableDaysResult,
    AvailableSlotsResult,
)
from app.core.booking_eligibility_factory import (
    build_booking_availability_client,
    build_booking_eligibility_client,
    build_booking_s2s_config,
)
from app.core.booking_eligibility_http import (
    ELIGIBILITY_ROUTE_PATH,
    BookingEligibilityHttpClient,
    BookingEligibilityHttpConfig,
)
from app.core.booking_types import AvailableSlot
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransportError,
)

_VALID_TOKEN = "t" * 32
_SERVICE_UUID = "11111111-1111-4111-8111-111111111111"
_MASTER_UUID = "22222222-2222-4222-8222-222222222222"
_MONTH = "2026-08"
_DATE = "2026-08-06"
_STUDIO_TODAY = "2026-08-05"
_STUDIO_TZ = timezone(timedelta(hours=5), name="Asia/Yekaterinburg")


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


def _client(transport: FakeTransport, **config_overrides: Any) -> BookingAvailabilityHttpClient:
    return BookingAvailabilityHttpClient(_config(**config_overrides), transport)


def _slot_payload(
    *,
    slot_id: str,
    starts_at: str,
    service_id: str = _SERVICE_UUID,
    master_id: str = _MASTER_UUID,
) -> dict[str, object]:
    return {
        "slotId": slot_id,
        "serviceId": service_id,
        "masterId": master_id,
        "startsAt": starts_at,
    }


def _days_success(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "serviceId": _SERVICE_UUID,
        "masterId": _MASTER_UUID,
        "month": _MONTH,
        "studioToday": _STUDIO_TODAY,
        "dateKeys": ["2026-08-06", "2026-08-07"],
    }
    payload.update(overrides)
    return payload


def _slots_success(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "serviceId": _SERVICE_UUID,
        "masterId": _MASTER_UUID,
        "date": _DATE,
        "studioToday": _STUDIO_TODAY,
        "slots": [
            _slot_payload(
                slot_id=f"bs1.{_SERVICE_UUID}.{_MASTER_UUID}.{_DATE}.1000",
                starts_at="2026-08-06T10:00:00+05:00",
            ),
            _slot_payload(
                slot_id=f"bs1.{_SERVICE_UUID}.{_MASTER_UUID}.{_DATE}.1030",
                starts_at="2026-08-06T10:30:00+05:00",
            ),
        ],
    }
    payload.update(overrides)
    return payload


def _assert_no_secrets(text: str, *, body_fragment: str | None = None) -> None:
    assert _VALID_TOKEN not in text
    assert "Authorization" not in text
    assert "https://eligibility.example" not in text
    if body_fragment is not None:
        assert body_fragment not in text


# ---------------------------------------------------------------------------
# Calendar / request validation (before network)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "month",
    ["2026-13", "2026-00", "26-08", "2026-8", "2026-08-01", "not-a-month", ""],
)
def test_invalid_month_rejected_before_network(month: str) -> None:
    transport = FakeTransport(response=_json_response(_days_success()))
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport).get_available_days(
            service_id=_SERVICE_UUID,
            master_id=_MASTER_UUID,
            month=month,
        )
    assert exc_info.value.code == "REQUEST_INVALID"
    assert transport.calls == []
    if month:
        assert month not in str(exc_info.value)


@pytest.mark.parametrize(
    "day",
    [
        "2026-08-32",
        "2026-02-30",
        "2026-8-6",
        "08-06-2026",
        "not-a-date",
        "",
        "2026-02-29",
    ],
)
def test_invalid_date_rejected_before_network(day: str) -> None:
    transport = FakeTransport(response=_json_response(_slots_success()))
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport).get_available_slots(
            service_id=_SERVICE_UUID,
            master_id=_MASTER_UUID,
            date=day,
        )
    assert exc_info.value.code == "REQUEST_INVALID"
    assert transport.calls == []


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-uuid",
        "{11111111-1111-4111-8111-111111111111}",
        "urn:uuid:11111111-1111-4111-8111-111111111111",
    ],
)
def test_invalid_uuid_rejected_before_network(raw: str) -> None:
    transport = FakeTransport(response=_json_response(_days_success()))
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport).get_available_days(
            service_id=raw,
            master_id=_MASTER_UUID,
            month=_MONTH,
        )
    assert exc_info.value.code == "REQUEST_INVALID"
    assert transport.calls == []


def test_leap_day_accepted_as_local_request_date() -> None:
    transport = FakeTransport(
        response=_json_response(
            _slots_success(
                date="2024-02-29",
                slots=[
                    _slot_payload(
                        slot_id="leap",
                        starts_at="2024-02-29T10:00:00+05:00",
                    )
                ],
            )
        )
    )
    result = _client(transport).get_available_slots(
        service_id=_SERVICE_UUID,
        master_id=_MASTER_UUID,
        date="2024-02-29",
    )
    assert result.date == "2024-02-29"
    assert len(transport.calls) == 1


def test_invalid_client_config_remains_config_invalid() -> None:
    transport = FakeTransport(response=_json_response(_days_success()))
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        BookingAvailabilityHttpClient("not-a-config", transport)  # type: ignore[arg-type]
    assert exc_info.value.code == "CONFIG_INVALID"
    assert transport.calls == []
    with pytest.raises(BookingAvailabilityHttpError) as exc_info2:
        BookingAvailabilityHttpClient(_config(), None)  # type: ignore[arg-type]
    assert exc_info2.value.code == "CONFIG_INVALID"


def test_require_calendar_helpers() -> None:
    assert require_calendar_month("2026-08") == "2026-08"
    assert require_calendar_date("2026-08-06") == "2026-08-06"
    assert require_calendar_date("2024-02-29") == "2024-02-29"
    with pytest.raises(BookingAvailabilityHttpError) as exc_month:
        require_calendar_month("2026-13")
    assert exc_month.value.code == "REQUEST_INVALID"
    with pytest.raises(BookingAvailabilityHttpError) as exc_date:
        require_calendar_date("2026-02-29")
    assert exc_date.value.code == "REQUEST_INVALID"


# ---------------------------------------------------------------------------
# Request shape / shared transport config
# ---------------------------------------------------------------------------


def test_available_days_request_is_exact_post_json_bearer_only() -> None:
    transport = FakeTransport(response=_json_response(_days_success()))
    _client(transport).get_available_days(
        service_id=_SERVICE_UUID.upper(),
        master_id=_MASTER_UUID.upper(),
        month=_MONTH,
    )
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call.method == "POST"
    parts = urlsplit(call.url)
    assert parts.path == AVAILABLE_DAYS_ROUTE_PATH
    assert parts.query == ""
    assert call.allow_redirects is False
    assert call.timeout_seconds == 3.5
    assert call.max_response_bytes == 4096
    assert call.headers == {
        "Authorization": f"Bearer {_VALID_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = json.loads(call.body.decode("utf-8"))
    assert body == {
        "serviceId": _SERVICE_UUID,
        "masterId": _MASTER_UUID,
        "month": _MONTH,
    }
    assert set(body) == {"serviceId", "masterId", "month"}


def test_available_slots_request_is_exact_post_json_bearer_only() -> None:
    transport = FakeTransport(response=_json_response(_slots_success()))
    _client(transport).get_available_slots(
        service_id=_SERVICE_UUID,
        master_id=_MASTER_UUID,
        date=_DATE,
    )
    call = transport.calls[0]
    assert call.method == "POST"
    assert urlsplit(call.url).path == SLOTS_ROUTE_PATH
    assert urlsplit(call.url).query == ""
    body = json.loads(call.body.decode("utf-8"))
    assert body == {
        "serviceId": _SERVICE_UUID,
        "masterId": _MASTER_UUID,
        "date": _DATE,
    }


def test_availability_shares_config_urls_and_token_with_eligibility() -> None:
    cfg = _config()
    assert cfg.eligibility_url.endswith(ELIGIBILITY_ROUTE_PATH)
    assert cfg.available_days_url.endswith(AVAILABLE_DAYS_ROUTE_PATH)
    assert cfg.available_slots_url.endswith(SLOTS_ROUTE_PATH)
    assert cfg.available_days_url.startswith(cfg.base_url)
    assert cfg.bearer_token == _VALID_TOKEN
    assert cfg.timeout_seconds == 3.5
    assert cfg.max_response_bytes == 4096


def test_eligibility_client_delegates_availability_methods() -> None:
    transport = FakeTransport(response=_json_response(_days_success()))
    client = BookingEligibilityHttpClient(_config(), transport)
    result = client.get_available_days(
        service_id=_SERVICE_UUID,
        master_id=_MASTER_UUID,
        month=_MONTH,
    )
    assert isinstance(result, AvailableDaysResult)
    assert result.date_keys == ("2026-08-06", "2026-08-07")


def test_factory_builds_availability_from_same_settings() -> None:
    settings = Settings.from_env(
        {
            "BOT_MODE": "OFF",
            "EMERGENCY_LOCK": "true",
            "BOOKING_ELIGIBILITY_BASE_URL": "https://eligibility.example",
            "BOOKING_ELIGIBILITY_BEARER_TOKEN": _VALID_TOKEN,
            "BOOKING_ELIGIBILITY_TIMEOUT_SECONDS": "4",
            "BOOKING_ELIGIBILITY_MAX_RESPONSE_BYTES": "8192",
        }
    )
    shared = build_booking_s2s_config(settings)
    assert shared is not None
    transport = FakeTransport(response=_json_response(_days_success()))
    availability = build_booking_availability_client(settings, transport=transport)
    eligibility = build_booking_eligibility_client(settings, transport=transport)
    assert availability is not None
    assert eligibility is not None
    assert availability._config.base_url == eligibility._config.base_url
    assert availability._config.bearer_token == eligibility._config.bearer_token
    assert availability._config.timeout_seconds == eligibility._config.timeout_seconds
    assert (
        availability._config.max_response_bytes
        == eligibility._config.max_response_bytes
    )


# ---------------------------------------------------------------------------
# Success parsing
# ---------------------------------------------------------------------------


def test_available_days_success_parsing_immutable() -> None:
    transport = FakeTransport(response=_json_response(_days_success(dateKeys=[])))
    result = _client(transport).get_available_days(
        service_id=_SERVICE_UUID,
        master_id=_MASTER_UUID,
        month=_MONTH,
    )
    assert result.service_id == _SERVICE_UUID
    assert result.master_id == _MASTER_UUID
    assert result.month == _MONTH
    assert result.studio_today == _STUDIO_TODAY
    assert result.date_keys == ()
    assert type(result.date_keys) is tuple
    with pytest.raises(FrozenInstanceError):
        result.date_keys = ("2026-08-01",)  # type: ignore[misc]


def test_available_slots_success_preserves_server_ids_and_tz() -> None:
    slot_id = f"bs1.{_SERVICE_UUID}.{_MASTER_UUID}.{_DATE}.1015"
    transport = FakeTransport(
        response=_json_response(
            _slots_success(
                slots=[
                    _slot_payload(
                        slot_id=slot_id,
                        starts_at="2026-08-06T10:15:00+05:00",
                    )
                ]
            )
        )
    )
    result = _client(transport).get_available_slots(
        service_id=_SERVICE_UUID,
        master_id=_MASTER_UUID,
        date=_DATE,
    )
    assert isinstance(result, AvailableSlotsResult)
    assert len(result.slots) == 1
    slot = result.slots[0]
    assert type(slot) is AvailableSlot
    assert slot.slot_id == slot_id
    assert slot.service_id == _SERVICE_UUID
    assert slot.master_id == _MASTER_UUID
    assert slot.starts_at.tzinfo is not None
    assert slot.starts_at.utcoffset() == timedelta(hours=5)
    assert slot.starts_at == datetime(2026, 8, 6, 10, 15, tzinfo=_STUDIO_TZ)
    assert type(result.slots) is tuple
    with pytest.raises(FrozenInstanceError):
        result.slots = ()  # type: ignore[misc]


def test_unsorted_date_keys_fail_closed_not_silently_sorted() -> None:
    transport = FakeTransport(
        response=_json_response(
            _days_success(dateKeys=["2026-08-07", "2026-08-06"])
        )
    )
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport).get_available_days(
            service_id=_SERVICE_UUID,
            master_id=_MASTER_UUID,
            month=_MONTH,
        )
    assert exc_info.value.code == "RESPONSE_INVALID"


def test_unsorted_slots_fail_closed_not_silently_sorted() -> None:
    transport = FakeTransport(
        response=_json_response(
            _slots_success(
                slots=[
                    _slot_payload(
                        slot_id="later",
                        starts_at="2026-08-06T11:00:00+05:00",
                    ),
                    _slot_payload(
                        slot_id="earlier",
                        starts_at="2026-08-06T10:00:00+05:00",
                    ),
                ]
            )
        )
    )
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport).get_available_slots(
            service_id=_SERVICE_UUID,
            master_id=_MASTER_UUID,
            date=_DATE,
        )
    assert exc_info.value.code == "RESPONSE_INVALID"


# ---------------------------------------------------------------------------
# Contradictory / malformed success bodies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"serviceId": "33333333-3333-4333-8333-333333333333"},
        {"masterId": "33333333-3333-4333-8333-333333333333"},
        {"month": "2026-09"},
        {"studioToday": "2026-13-01"},
        {"studioToday": "not-a-date"},
        {"dateKeys": ["2026-09-01"]},
        {"dateKeys": ["2026-08-06", "2026-08-06"]},
        {"dateKeys": ["2026-08-07", "2026-08-06"]},
        {"dateKeys": [f"2026-08-{day:02d}" for day in range(1, 33)]},
        {"ok": False},
        {"ok": "true"},
        {"ok": 1},
        {"extra": 1},
    ],
)
def test_available_days_contradictory_response_fail_closed(
    overrides: dict[str, Any],
) -> None:
    payload = _days_success()
    if "extra" in overrides:
        payload["extra"] = overrides.pop("extra")
    payload.update(overrides)
    # Drop keys intentionally for missing-field cases handled elsewhere.
    transport = FakeTransport(response=_json_response(payload))
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport).get_available_days(
            service_id=_SERVICE_UUID,
            master_id=_MASTER_UUID,
            month=_MONTH,
        )
    assert exc_info.value.code == "RESPONSE_INVALID"
    _assert_no_secrets(repr(exc_info.value))


def test_success_non_json_content_type_is_response_invalid() -> None:
    body = json.dumps(_days_success(), ensure_ascii=False).encode("utf-8")
    transport = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={"Content-Type": "text/plain", "Content-Length": str(len(body))},
            body=body,
        )
    )
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport).get_available_days(
            service_id=_SERVICE_UUID,
            master_id=_MASTER_UUID,
            month=_MONTH,
        )
    assert exc_info.value.code == "RESPONSE_INVALID"


def test_html_error_body_does_not_leak_and_maps_safely() -> None:
    html = b"<html><body>upstream secret detail must not leak</body></html>"
    transport = FakeTransport(
        response=S2sHttpResponse(
            status_code=500,
            headers={"Content-Type": "text/html", "Content-Length": str(len(html))},
            body=html,
        )
    )
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport).get_available_slots(
            service_id=_SERVICE_UUID,
            master_id=_MASTER_UUID,
            date=_DATE,
        )
    assert exc_info.value.code == "REMOTE_REJECTED"
    rendered = f"{exc_info.value!s}{exc_info.value!r}"
    assert "upstream secret" not in rendered
    assert "<html>" not in rendered


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "string",
        1,
        True,
    ],
)
def test_available_days_non_object_json_fail_closed(payload: object) -> None:
    transport = FakeTransport(response=_json_response(payload))
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport).get_available_days(
            service_id=_SERVICE_UUID,
            master_id=_MASTER_UUID,
            month=_MONTH,
        )
    assert exc_info.value.code == "RESPONSE_INVALID"


def test_available_days_missing_field_fail_closed() -> None:
    payload = _days_success()
    del payload["dateKeys"]
    transport = FakeTransport(response=_json_response(payload))
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport).get_available_days(
            service_id=_SERVICE_UUID,
            master_id=_MASTER_UUID,
            month=_MONTH,
        )
    assert exc_info.value.code == "RESPONSE_INVALID"


@pytest.mark.parametrize(
    "overrides",
    [
        {"serviceId": "33333333-3333-4333-8333-333333333333"},
        {"masterId": "33333333-3333-4333-8333-333333333333"},
        {"date": "2026-08-07"},
        {"studioToday": "2026-02-30"},
        {"ok": False},
        {
            "slots": [
                _slot_payload(
                    slot_id="a",
                    starts_at="2026-08-06T10:00:00",  # naive
                )
            ]
        },
        {
            "slots": [
                _slot_payload(
                    slot_id="a",
                    starts_at="2026-08-06T10:00:00Z",
                )
            ]
        },
        {
            "slots": [
                _slot_payload(
                    slot_id="a",
                    starts_at="2026-08-07T10:00:00+05:00",
                )
            ]
        },
        {
            "slots": [
                _slot_payload(
                    slot_id="dup",
                    starts_at="2026-08-06T10:00:00+05:00",
                ),
                _slot_payload(
                    slot_id="dup",
                    starts_at="2026-08-06T10:30:00+05:00",
                ),
            ]
        },
        {
            "slots": [
                _slot_payload(
                    slot_id="a",
                    starts_at="2026-08-06T10:00:00+05:00",
                ),
                _slot_payload(
                    slot_id="b",
                    starts_at="2026-08-06T10:00:00+05:00",
                ),
            ]
        },
        {
            "slots": [
                _slot_payload(
                    slot_id="a",
                    starts_at="2026-08-06T11:00:00+05:00",
                ),
                _slot_payload(
                    slot_id="b",
                    starts_at="2026-08-06T10:00:00+05:00",
                ),
            ]
        },
        {
            "slots": [
                {
                    "slotId": "a",
                    "serviceId": _SERVICE_UUID,
                    "masterId": _MASTER_UUID,
                    "startsAt": "2026-08-06T10:00:00+05:00",
                    "extra": True,
                }
            ]
        },
        {
            "slots": [
                _slot_payload(
                    slot_id="   ",
                    starts_at="2026-08-06T10:00:00+05:00",
                )
            ]
        },
    ],
)
def test_available_slots_contradictory_response_fail_closed(
    overrides: dict[str, Any],
) -> None:
    payload = _slots_success()
    payload.update(overrides)
    transport = FakeTransport(response=_json_response(payload))
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport).get_available_slots(
            service_id=_SERVICE_UUID,
            master_id=_MASTER_UUID,
            date=_DATE,
        )
    assert exc_info.value.code == "RESPONSE_INVALID"


def test_available_slots_more_than_288_fail_closed() -> None:
    payload = _slots_success(
        slots=[
            _slot_payload(
                slot_id=f"s{i}",
                starts_at="2026-08-06T10:00:00+05:00",
            )
            for i in range(289)
        ]
    )
    transport = FakeTransport(response=_json_response(payload))
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport, max_response_bytes=1_000_000).get_available_slots(
            service_id=_SERVICE_UUID,
            master_id=_MASTER_UUID,
            date=_DATE,
        )
    assert exc_info.value.code == "RESPONSE_INVALID"


def test_malformed_json_fail_closed() -> None:
    transport = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=b"{not-json",
        )
    )
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport).get_available_days(
            service_id=_SERVICE_UUID,
            master_id=_MASTER_UUID,
            month=_MONTH,
        )
    assert exc_info.value.code == "RESPONSE_INVALID"


def test_oversized_response_fail_closed() -> None:
    transport = FakeTransport(
        error=S2sHttpTransportError("RESPONSE_TOO_LARGE")
    )
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport, max_response_bytes=64).get_available_days(
            service_id=_SERVICE_UUID,
            master_id=_MASTER_UUID,
            month=_MONTH,
        )
    assert exc_info.value.code == "RESPONSE_TOO_LARGE"


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "remote_code", "expected"),
    [
        (400, "SERVICE_UNAVAILABLE", "SERVICE_UNAVAILABLE"),
        (400, "VALIDATION_ERROR", "VALIDATION_ERROR"),
        (413, "PAYLOAD_TOO_LARGE", "REMOTE_REJECTED"),
        (401, "UNAUTHORIZED", "UNAUTHORIZED"),
        (429, "RATE_LIMITED", "RATE_LIMITED"),
        (500, "INTERNAL_ERROR", "TRANSPORT_ERROR"),
        (502, "INTERNAL_ERROR", "REMOTE_REJECTED"),
    ],
)
def test_error_status_mapping_no_body_echo(
    status: int, remote_code: str, expected: str
) -> None:
    secret_error = "upstream secret detail must not leak"
    transport = FakeTransport(
        response=_json_response(
            {"ok": False, "code": remote_code, "error": secret_error},
            status=status,
        )
    )
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport).get_available_slots(
            service_id=_SERVICE_UUID,
            master_id=_MASTER_UUID,
            date=_DATE,
        )
    assert exc_info.value.code == expected
    assert len(transport.calls) == 1
    rendered = f"{exc_info.value!s}{exc_info.value!r}"
    _assert_no_secrets(rendered, body_fragment=secret_error)
    assert remote_code not in rendered or remote_code == expected


def test_timeout_maps_to_timeout() -> None:
    transport = FakeTransport(error=S2sHttpTransportError("TIMEOUT"))
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport).get_available_days(
            service_id=_SERVICE_UUID,
            master_id=_MASTER_UUID,
            month=_MONTH,
        )
    assert exc_info.value.code == "TIMEOUT"


def test_dns_connection_failure_maps_to_transport_error() -> None:
    transport = FakeTransport(error=S2sHttpTransportError("TRANSPORT_ERROR"))
    with pytest.raises(BookingAvailabilityHttpError) as exc_info:
        _client(transport).get_available_days(
            service_id=_SERVICE_UUID,
            master_id=_MASTER_UUID,
            month=_MONTH,
        )
    assert exc_info.value.code == "TRANSPORT_ERROR"


def test_error_logging_does_not_include_secrets(caplog: pytest.LogCaptureFixture) -> None:
    secret_error = "do-not-log-this-body"
    transport = FakeTransport(
        response=_json_response(
            {"ok": False, "code": "UNAUTHORIZED", "error": secret_error},
            status=401,
        )
    )
    with caplog.at_level(logging.INFO):
        with pytest.raises(BookingAvailabilityHttpError):
            _client(transport).get_available_days(
                service_id=_SERVICE_UUID,
                master_id=_MASTER_UUID,
                month=_MONTH,
            )
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert secret_error not in joined
    assert _VALID_TOKEN not in joined
    assert "UNAUTHORIZED" in joined or "booking_availability_http_fail_closed" in joined
