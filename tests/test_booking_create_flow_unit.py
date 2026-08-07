"""Application-boundary tests for CURSOR-25 booking create confirm path."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.config import Settings
from app.core.booking_create_http import BookingCreateHttpClient, BookingCreateHttpError
from app.core.booking_create_remote import (
    BookingCreateConfirmedResult,
    BookingCreateMachineOutcome,
    BookingCreateRejectedResult,
    BookingCreateRemoteSuccess,
)
from app.core.booking_eligibility_factory import build_booking_flow_from_settings
from app.core.booking_eligibility_http import BookingEligibilityHttpConfig
from app.core.booking_types import AvailableSlot
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse, S2sHttpTransportError
from app.services.booking_eligibility_flow import BookingEligibilityFlowService
from app.services.booking_flow import BookingFlowService

_TOKEN = "t" * 32
_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_BOOKING = "33333333-3333-4333-8333-333333333333"
_KEY = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_DATE = "2026-08-06"
_SLOT_ID = f"bs1.{_SERVICE}.{_MASTER}.{_DATE}.1000"
_STARTS = "2026-08-06T10:00:00+05:00"
_STUDIO_TZ = timezone(timedelta(hours=5), name="Asia/Yekaterinburg")
_NAME = "Иван Тестов"
_PHONE = "+79001234567"


class FakeTransport:
    def __init__(
        self,
        *,
        response: S2sHttpResponse | None = None,
        error: BaseException | None = None,
        responses: list[S2sHttpResponse] | None = None,
    ) -> None:
        self._response = response
        self._error = error
        self._responses = list(responses or [])
        self.calls: list[S2sHttpRequest] = []

    def request(self, request: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        if self._responses:
            return self._responses.pop(0)
        if self._response is None:
            raise S2sHttpTransportError("TRANSPORT_ERROR")
        return self._response


class RecordingCreateClient:
    def __init__(
        self,
        *,
        result: BookingCreateRemoteSuccess | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def create_booking(self, **kwargs: Any) -> BookingCreateRemoteSuccess:
        self.calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("result not configured")
        return self.result


def _slot(**overrides: Any) -> AvailableSlot:
    values: dict[str, Any] = {
        "slot_id": _SLOT_ID,
        "starts_at": datetime(2026, 8, 6, 10, 0, tzinfo=_STUDIO_TZ),
        "master_id": _MASTER,
        "service_id": _SERVICE,
    }
    values.update(overrides)
    return AvailableSlot(**values)


def _success_remote(**overrides: Any) -> BookingCreateRemoteSuccess:
    values: dict[str, Any] = {
        "booking_id": _BOOKING,
        "slot_id": _SLOT_ID,
        "starts_at": _STARTS,
        "idempotent_replay": False,
    }
    values.update(overrides)
    return BookingCreateRemoteSuccess(**values)


def _confirm(
    flow: BookingFlowService,
    slot: AvailableSlot | None = None,
    **overrides: Any,
):
    values: dict[str, Any] = {
        "idempotency_key": _KEY,
        "client_name": _NAME,
        "phone": _PHONE,
        "personal_data_consent": True,
        "offer_acknowledgement": True,
    }
    values.update(overrides)
    return flow.confirm_selected_slot(slot if slot is not None else _slot(), **values)


def test_confirmed_only_on_valid_success() -> None:
    client = RecordingCreateClient(result=_success_remote())
    flow = BookingFlowService(None, None, client)
    result = _confirm(flow)
    assert type(result) is BookingCreateConfirmedResult
    assert result.outcome is BookingCreateMachineOutcome.CONFIRMED
    assert result.booking_id == _BOOKING
    assert result.slot_id == _SLOT_ID
    assert result.idempotent_replay is False
    assert len(client.calls) == 1


def test_confirmed_contains_booking_id() -> None:
    client = RecordingCreateClient(result=_success_remote())
    result = _confirm(BookingFlowService(None, None, client))
    assert isinstance(result, BookingCreateConfirmedResult)
    assert result.booking_id
    assert type(result.booking_id) is str


def test_idempotent_replay_remains_confirmed() -> None:
    client = RecordingCreateClient(result=_success_remote(idempotent_replay=True))
    result = _confirm(BookingFlowService(None, None, client))
    assert type(result) is BookingCreateConfirmedResult
    assert result.idempotent_replay is True


@pytest.mark.parametrize(
    ("code", "outcome"),
    [
        ("SLOT_NO_LONGER_AVAILABLE", BookingCreateMachineOutcome.SLOT_RESELECT_REQUIRED),
        ("BOOKING_CONFLICT", BookingCreateMachineOutcome.SLOT_RESELECT_REQUIRED),
        ("CLIENT_AMBIGUOUS", BookingCreateMachineOutcome.MANAGER_HANDOFF),
        ("IDEMPOTENCY_IN_PROGRESS", BookingCreateMachineOutcome.RETRY_LATER),
        ("INTERNAL_ERROR", BookingCreateMachineOutcome.RETRY_LATER),
        ("IDEMPOTENCY_CONFLICT", BookingCreateMachineOutcome.FAIL_CLOSED),
        ("VALIDATION_ERROR", BookingCreateMachineOutcome.FAIL_CLOSED),
        ("SERVICE_UNAVAILABLE", BookingCreateMachineOutcome.SERVICE_UNAVAILABLE),
    ],
)
def test_remote_errors_map_to_machine_outcomes(
    code: str, outcome: BookingCreateMachineOutcome
) -> None:
    client = RecordingCreateClient(error=BookingCreateHttpError(code))
    result = _confirm(BookingFlowService(None, None, client))
    assert type(result) is BookingCreateRejectedResult
    assert result.outcome is outcome
    assert result.internal_reason_code == code
    assert result.idempotency_key == _KEY


def test_idempotency_in_progress_preserves_key() -> None:
    client = RecordingCreateClient(
        error=BookingCreateHttpError("IDEMPOTENCY_IN_PROGRESS")
    )
    result = _confirm(BookingFlowService(None, None, client))
    assert result.outcome is BookingCreateMachineOutcome.RETRY_LATER
    assert result.idempotency_key == _KEY
    assert client.calls[0]["idempotency_key"] == _KEY


def test_idempotency_conflict_does_not_generate_other_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("must not mint a new idempotency key")

    monkeypatch.setattr(uuid, "uuid4", _boom)
    client = RecordingCreateClient(error=BookingCreateHttpError("IDEMPOTENCY_CONFLICT"))
    result = _confirm(BookingFlowService(None, None, client))
    assert result.outcome is BookingCreateMachineOutcome.FAIL_CLOSED
    assert result.idempotency_key == _KEY


def test_booking_request_conflict_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2: BOOKING_REQUEST_CONFLICT is not proven slot conflict → FAIL_CLOSED."""

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("must not mint a new idempotency key")

    monkeypatch.setattr(uuid, "uuid4", _boom)
    client = RecordingCreateClient(
        error=BookingCreateHttpError("BOOKING_REQUEST_CONFLICT")
    )
    result = _confirm(BookingFlowService(None, None, client))
    assert type(result) is BookingCreateRejectedResult
    assert result.outcome is BookingCreateMachineOutcome.FAIL_CLOSED
    assert result.internal_reason_code == "BOOKING_REQUEST_CONFLICT"
    assert result.idempotency_key == _KEY
    assert len(client.calls) == 1
    assert client.calls[0]["idempotency_key"] == _KEY
    assert not isinstance(result, BookingCreateConfirmedResult)


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("port exploded with secrets"),
        TypeError("bad internal contract"),
    ],
)
def test_unknown_port_exception_is_fail_closed(
    exc: BaseException,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F3: untyped exceptions are defects, not RETRY_LATER."""

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("must not mint a new idempotency key")

    monkeypatch.setattr(uuid, "uuid4", _boom)
    client = RecordingCreateClient(error=exc)
    with caplog.at_level("INFO"):
        result = _confirm(BookingFlowService(None, None, client))
    assert type(result) is BookingCreateRejectedResult
    assert result.outcome is BookingCreateMachineOutcome.FAIL_CLOSED
    assert result.internal_reason_code == "UNEXPECTED_ERROR"
    assert result.idempotency_key == _KEY
    assert len(client.calls) == 1
    assert not isinstance(result, BookingCreateConfirmedResult)
    joined = " ".join(record.getMessage() for record in caplog.records)
    rendered = f"{result!s}{result!r}{joined}"
    assert _NAME not in rendered
    assert _PHONE not in rendered
    assert _SLOT_ID not in rendered
    assert _BOOKING not in rendered
    assert _TOKEN not in rendered
    assert "port exploded" not in rendered
    assert "bad internal contract" not in rendered


@pytest.mark.parametrize(
    "code",
    ["TIMEOUT", "TRANSPORT_ERROR", "INTERNAL_ERROR", "RESPONSE_TOO_LARGE"],
)
def test_typed_transient_errors_remain_retry_later(code: str) -> None:
    client = RecordingCreateClient(error=BookingCreateHttpError(code))
    result = _confirm(BookingFlowService(None, None, client))
    assert type(result) is BookingCreateRejectedResult
    assert result.outcome is BookingCreateMachineOutcome.RETRY_LATER
    assert result.internal_reason_code == code
    assert result.idempotency_key == _KEY


def test_remote_success_other_slot_id_fail_closed() -> None:
    other = f"bs1.{_SERVICE}.{_MASTER}.{_DATE}.1030"
    client = RecordingCreateClient(result=_success_remote(slot_id=other))
    result = _confirm(BookingFlowService(None, None, client))
    assert type(result) is BookingCreateRejectedResult
    assert result.outcome is BookingCreateMachineOutcome.FAIL_CLOSED
    assert result.internal_reason_code == "RESPONSE_INVALID"


def test_remote_success_other_starts_at_fail_closed() -> None:
    client = RecordingCreateClient(
        result=_success_remote(starts_at="2026-08-06T10:30:00+05:00")
    )
    result = _confirm(BookingFlowService(None, None, client))
    assert type(result) is BookingCreateRejectedResult
    assert result.outcome is BookingCreateMachineOutcome.FAIL_CLOSED


def test_one_application_call_makes_one_remote_call() -> None:
    client = RecordingCreateClient(result=_success_remote())
    _confirm(BookingFlowService(None, None, client))
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"personal_data_consent": False},
        {"offer_acknowledgement": False},
        {"personal_data_consent": True, "offer_acknowledgement": False},
    ],
)
def test_no_create_without_both_consents(kwargs: dict[str, Any]) -> None:
    client = RecordingCreateClient(result=_success_remote())
    result = _confirm(BookingFlowService(None, None, client), **kwargs)
    assert type(result) is BookingCreateRejectedResult
    assert result.outcome is BookingCreateMachineOutcome.FAIL_CLOSED
    assert client.calls == []


def test_no_create_for_non_available_slot() -> None:
    client = RecordingCreateClient(result=_success_remote())
    flow = BookingFlowService(None, None, client)
    result = flow.confirm_selected_slot(  # type: ignore[arg-type]
        {"slot_id": _SLOT_ID},  # type: ignore[arg-type]
        idempotency_key=_KEY,
        client_name=_NAME,
        phone=_PHONE,
        personal_data_consent=True,
        offer_acknowledgement=True,
    )
    assert type(result) is BookingCreateRejectedResult
    assert client.calls == []


def test_unconfigured_create_fail_closed() -> None:
    flow = BookingFlowService(BookingEligibilityFlowService(None), None, None)
    result = _confirm(flow)
    assert type(result) is BookingCreateRejectedResult
    assert result.outcome is BookingCreateMachineOutcome.FAIL_CLOSED
    assert result.internal_reason_code == "CONFIG_INVALID"


def test_factory_wires_create_client_into_flow() -> None:
    settings = Settings.from_env(
        {
            "BOOKING_ELIGIBILITY_BASE_URL": "https://eligibility.example",
            "BOOKING_ELIGIBILITY_BEARER_TOKEN": _TOKEN,
        }
    )
    transport = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=(
                b'{"ok":true,"bookingId":"%s","slotId":"%s",'
                b'"status":"SCHEDULED","startsAt":"%s","idempotentReplay":false}'
                % (_BOOKING.encode(), _SLOT_ID.encode(), _STARTS.encode())
            ),
        )
    )
    flow = build_booking_flow_from_settings(settings, transport=transport)
    assert flow._booking_create_client is not None
    assert type(flow._booking_create_client) is BookingCreateHttpClient
    assert transport.calls == []
    result = _confirm(flow)
    assert type(result) is BookingCreateConfirmedResult
    assert len(transport.calls) == 1


def test_construction_does_not_perform_http_io() -> None:
    settings = Settings.from_env(
        {
            "BOOKING_ELIGIBILITY_BASE_URL": "https://eligibility.example",
            "BOOKING_ELIGIBILITY_BEARER_TOKEN": _TOKEN,
        }
    )
    transport = FakeTransport(error=AssertionError("must not call network"))
    flow = build_booking_flow_from_settings(settings, transport=transport)
    assert flow._booking_create_client is not None
    assert transport.calls == []


def test_confirmed_result_repr_hides_pii() -> None:
    client = RecordingCreateClient(result=_success_remote())
    result = _confirm(BookingFlowService(None, None, client))
    rendered = repr(result)
    assert _NAME not in rendered
    assert _PHONE not in rendered
    assert _BOOKING not in rendered
    assert _SLOT_ID not in rendered
    assert _KEY not in rendered


def test_config_url_property() -> None:
    config = BookingEligibilityHttpConfig(
        base_url="https://eligibility.example",
        bearer_token=_TOKEN,
    )
    assert config.bookings_url == "https://eligibility.example/api/internal/bot/v1/bookings"
