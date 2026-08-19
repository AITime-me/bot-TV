"""CLIENTREF-03: clientRef propagation and identity-aware booking create bridge."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.core.booking_create_http import BookingCreateHttpClient, BookingCreateHttpError
from app.core.booking_create_remote import (
    BookingCreateConfirmedResult,
    BookingCreateMachineOutcome,
    BookingCreateRejectedResult,
    BookingCreateRemoteSuccess,
    build_booking_create_remote_request,
)
from app.core.client_ref_resolution import (
    ClientRefResolutionOutcome,
    ClientRefResolutionResult,
)
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse, S2sHttpTransportError
from app.core.booking_eligibility_http import BookingEligibilityHttpConfig
from app.core.booking_types import AvailableSlot
from app.services.booking_flow import (
    BookingFlowService,
    confirm_selected_slot_for_conversation,
)

_TOKEN = "t" * 32
_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_BOOKING = "33333333-3333-4333-8333-333333333333"
_CLIENT_REF = "44444444-4444-4444-8444-444444444444"
_KEY = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_DATE = "2026-08-06"
_SLOT_ID = f"bs1.{_SERVICE}.{_MASTER}.{_DATE}.1000"
_STARTS = "2026-08-06T10:00:00+05:00"
_STUDIO_TZ = timezone(timedelta(hours=5), name="Asia/Yekaterinburg")
_NAME = "Иван Тестов"
_PHONE = "+79001234567"
_CONVERSATION_ID = "55555555-5555-4555-8555-555555555555"


class FakeTransport:
    def __init__(self, *, response: S2sHttpResponse) -> None:
        self._response = response
        self.calls: list[S2sHttpRequest] = []

    def request(self, request: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(request)
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


class FakeClientRefResolver:
    def __init__(self, result: ClientRefResolutionResult) -> None:
        self._result = result
        self.calls: list[object] = []

    async def resolve_for_conversation(
        self,
        *,
        conversation_id: object,
    ) -> ClientRefResolutionResult:
        self.calls.append(conversation_id)
        return self._result


def _config() -> BookingEligibilityHttpConfig:
    return BookingEligibilityHttpConfig(
        base_url="https://eligibility.example",
        bearer_token=_TOKEN,
    )


def _success_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "bookingId": _BOOKING,
        "slotId": _SLOT_ID,
        "status": "SCHEDULED",
        "startsAt": _STARTS,
        "idempotentReplay": False,
    }
    payload.update(overrides)
    return payload


def _json_response(payload: dict[str, Any]) -> S2sHttpResponse:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return S2sHttpResponse(
        status_code=200,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        body=body,
    )


def _create_kwargs(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "idempotency_key": _KEY,
        "slot_id": _SLOT_ID,
        "client_name": _NAME,
        "phone": _PHONE,
        "personal_data_consent": True,
        "offer_acknowledgement": True,
    }
    values.update(overrides)
    return values


def _slot() -> AvailableSlot:
    return AvailableSlot(
        slot_id=_SLOT_ID,
        starts_at=datetime(2026, 8, 6, 10, 0, tzinfo=_STUDIO_TZ),
        master_id=_MASTER,
        service_id=_SERVICE,
    )


def _success_remote() -> BookingCreateRemoteSuccess:
    return BookingCreateRemoteSuccess(
        booking_id=_BOOKING,
        slot_id=_SLOT_ID,
        starts_at=_STARTS,
        idempotent_replay=False,
    )


# ---------------------------------------------------------------------------
# Wire DTO propagation / validation
# ---------------------------------------------------------------------------


def test_legacy_request_json_omits_client_ref() -> None:
    request = build_booking_create_remote_request(**_create_kwargs())
    body = request.to_json_object()
    assert "clientRef" not in body
    assert body == {
        "idempotencyKey": _KEY,
        "slotId": _SLOT_ID,
        "clientName": _NAME,
        "phone": _PHONE,
        "personalDataConsent": True,
        "offerAcknowledgement": True,
    }


def test_client_ref_json_propagation_exact() -> None:
    request = build_booking_create_remote_request(
        **_create_kwargs(client_ref=_CLIENT_REF)
    )
    body = request.to_json_object()
    assert body["clientRef"] == _CLIENT_REF
    assert set(body) == {
        "idempotencyKey",
        "slotId",
        "clientName",
        "phone",
        "personalDataConsent",
        "offerAcknowledgement",
        "clientRef",
    }


@pytest.mark.parametrize(
    "client_ref",
    [
        "UPPER-0000-4000-8000-000000000000",
        "not-a-uuid",
        "",
        "44444444-4444-4444-8444-44444444444g",
    ],
)
def test_invalid_client_ref_rejected_before_http(client_ref: str) -> None:
    transport = FakeTransport(response=_json_response(_success_payload()))
    with pytest.raises(BookingCreateHttpError) as exc_info:
        BookingCreateHttpClient(_config(), transport).create_booking(
            **_create_kwargs(client_ref=client_ref)
        )
    assert exc_info.value.code == "REQUEST_INVALID"
    assert transport.calls == []


def test_http_client_emits_client_ref_on_wire() -> None:
    transport = FakeTransport(response=_json_response(_success_payload()))
    BookingCreateHttpClient(_config(), transport).create_booking(
        **_create_kwargs(client_ref=_CLIENT_REF)
    )
    body = json.loads(transport.calls[0].body.decode("utf-8"))
    assert body["clientRef"] == _CLIENT_REF
    assert len(transport.calls) == 1


def test_request_repr_redacts_client_ref() -> None:
    request = build_booking_create_remote_request(
        **_create_kwargs(client_ref=_CLIENT_REF)
    )
    rendered = repr(request)
    assert _CLIENT_REF not in rendered
    assert "client_ref=<redacted>" in rendered


# ---------------------------------------------------------------------------
# Identity-aware application bridge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_found_makes_one_create_call_with_client_ref() -> None:
    create_client = RecordingCreateClient(result=_success_remote())
    flow = BookingFlowService(None, None, create_client)
    resolver = FakeClientRefResolver(
        ClientRefResolutionResult(
            outcome=ClientRefResolutionOutcome.FOUND,
            client_ref=_CLIENT_REF,
        )
    )
    result = await confirm_selected_slot_for_conversation(
        flow,
        resolver,
        _slot(),
        conversation_id=_CONVERSATION_ID,
        idempotency_key=_KEY,
        client_name=_NAME,
        phone=_PHONE,
        personal_data_consent=True,
        offer_acknowledgement=True,
    )
    assert type(result) is BookingCreateConfirmedResult
    assert len(create_client.calls) == 1
    assert create_client.calls[0]["client_ref"] == _CLIENT_REF
    assert resolver.calls == [_CONVERSATION_ID]


@pytest.mark.parametrize(
    ("resolution", "expected_reason"),
    [
        (
            ClientRefResolutionResult(outcome=ClientRefResolutionOutcome.NOT_FOUND),
            "CLIENT_REF_NOT_FOUND",
        ),
        (
            ClientRefResolutionResult(
                outcome=ClientRefResolutionOutcome.REFUSED,
                reason_code="CONVERSATION_MISSING",
            ),
            "CONVERSATION_MISSING",
        ),
        (
            ClientRefResolutionResult(
                outcome=ClientRefResolutionOutcome.INVALID_INPUT,
                error_code="CONVERSATION_ID_INVALID",
            ),
            "CONVERSATION_ID_INVALID",
        ),
    ],
)
@pytest.mark.asyncio
async def test_non_found_outcomes_make_zero_create_calls(
    resolution: ClientRefResolutionResult,
    expected_reason: str,
) -> None:
    create_client = RecordingCreateClient(result=_success_remote())
    flow = BookingFlowService(None, None, create_client)
    resolver = FakeClientRefResolver(resolution)
    result = await confirm_selected_slot_for_conversation(
        flow,
        resolver,
        _slot(),
        conversation_id=_CONVERSATION_ID,
        idempotency_key=_KEY,
        client_name=_NAME,
        phone=_PHONE,
        personal_data_consent=True,
        offer_acknowledgement=True,
    )
    assert type(result) is BookingCreateRejectedResult
    assert result.outcome is BookingCreateMachineOutcome.FAIL_CLOSED
    assert result.internal_reason_code == expected_reason
    assert create_client.calls == []


@pytest.mark.asyncio
async def test_non_found_does_not_fall_back_to_phone_name_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolver NOT_FOUND must not reach create even when phone/name are present."""

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("must not mint identity from phone/name")

    monkeypatch.setattr(uuid, "uuid4", _boom)
    create_client = RecordingCreateClient(result=_success_remote())
    flow = BookingFlowService(None, None, create_client)
    resolver = FakeClientRefResolver(
        ClientRefResolutionResult(outcome=ClientRefResolutionOutcome.NOT_FOUND)
    )
    result = await confirm_selected_slot_for_conversation(
        flow,
        resolver,
        _slot(),
        conversation_id=_CONVERSATION_ID,
        idempotency_key=_KEY,
        client_name=_NAME,
        phone=_PHONE,
        personal_data_consent=True,
        offer_acknowledgement=True,
    )
    assert result.outcome is BookingCreateMachineOutcome.FAIL_CLOSED
    assert create_client.calls == []
