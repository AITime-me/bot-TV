"""Unit tests for CURSOR-15 booking eligibility HTTP adapter.

Uses a fake S2sHttpTransport only. No live network, env, Docker, or pipeline.
Backend IDs are canonical UUIDs at the adapter boundary.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import pytest

from app.core.booking_dialog_policy import decide_booking_dialog
from app.core.booking_eligibility_http import (
    ELIGIBILITY_ROUTE_PATH,
    BookingEligibilityAdapterReasonCode,
    BookingEligibilityHttpClient,
    BookingEligibilityHttpConfig,
    BookingEligibilityHttpError,
    require_canonical_backend_uuid,
)
from app.core.booking_eligibility_remote import (
    EligibilityRemoteAlternativeMaster,
    EligibilityRemoteRequest,
)
from app.core.booking_types import (
    AvailableSlot,
    BookingEligibilityOutcome,
    BookingInternalReasonCode,
    ManagerHandoffDecision,
    SelectedMaster,
    SelectedService,
    client_message_for_decision,
)
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransportError,
)

_VALID_TOKEN = "t" * 32
_SERVICE_UUID = "11111111-1111-4111-8111-111111111111"
_MASTER_UUID = "22222222-2222-4222-8222-222222222222"
_ALT_B = "33333333-3333-4333-8333-333333333333"
_ALT_C = "44444444-4444-4444-8444-444444444444"
_SERVICE = SelectedService(_SERVICE_UUID)
_MASTER = SelectedMaster(_MASTER_UUID)


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
    extra_headers: dict[str, str] | None = None,
) -> S2sHttpResponse:
    body = b""
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
    if extra_headers:
        headers.update(extra_headers)
    return S2sHttpResponse(status_code=status, headers=headers, body=body)


def _success_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "outcome": "SELF_BOOKING_ALLOWED",
        "reasonCode": None,
        "selectedPairAllowed": True,
        "serviceOnlineInGeneral": True,
        "otherOnlineMasterCount": 0,
        "otherOnlineMasters": [],
    }
    payload.update(overrides)
    return payload


def _client(transport: FakeTransport, **config_overrides: Any) -> BookingEligibilityHttpClient:
    return BookingEligibilityHttpClient(_config(**config_overrides), transport)


def _utc(*parts: int) -> datetime:
    year, month, day, hour, minute = parts
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _slot(*, slot_id: str, master_id: str, minute: int = 0) -> AvailableSlot:
    return AvailableSlot(
        slot_id=slot_id,
        starts_at=_utc(2026, 8, 6, 5, minute),
        master_id=master_id,
        service_id=_SERVICE_UUID,
    )


# ---------------------------------------------------------------------------
# Config / UUID
# ---------------------------------------------------------------------------


def test_config_accepts_http_and_https() -> None:
    http_cfg = _config(base_url="http://eligibility.example")
    https_cfg = _config(base_url="https://eligibility.example")
    assert http_cfg.eligibility_url.endswith(ELIGIBILITY_ROUTE_PATH)
    assert https_cfg.eligibility_url.endswith(ELIGIBILITY_ROUTE_PATH)


def test_config_repr_redacts_base_url_and_bearer_token() -> None:
    secret_url = "https://internal-s2s.prod.example"
    secret_token = "secret-token-value-must-not-leak!!"
    cfg = _config(base_url=secret_url, bearer_token=secret_token)
    rendered = repr(cfg)
    assert secret_url not in rendered
    assert secret_token not in rendered
    assert "Authorization" not in rendered
    assert "base_url=<redacted>" in rendered
    assert "bearer_token=<redacted>" in rendered
    assert cfg.base_url == secret_url
    assert cfg.bearer_token == secret_token


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://eligibility.example",
        "https://user:pass@eligibility.example",
        "https://eligibility.example?x=1",
        "https://eligibility.example#frag",
        "https://eligibility.example/api",
        "https://example.com:abc",
        "",
    ],
)
def test_config_rejects_invalid_base_url(base_url: str) -> None:
    with pytest.raises(BookingEligibilityHttpError) as exc_info:
        _config(base_url=base_url)
    assert exc_info.value.code == "CONFIG_INVALID"


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-uuid",
        "11111111111141118111111111111111",
        "{11111111-1111-4111-8111-111111111111}",
        "urn:uuid:11111111-1111-4111-8111-111111111111",
    ],
)
def test_invalid_service_uuid_raises_before_transport(raw: str) -> None:
    transport = FakeTransport(response=_json_response(_success_payload()))
    with pytest.raises(BookingEligibilityHttpError) as exc_info:
        _client(transport).check_eligibility(SelectedService(raw), _MASTER)
    assert exc_info.value.code == "CONFIG_INVALID"
    assert raw not in str(exc_info.value)
    assert transport.calls == []


def test_uppercase_uuid_normalized_in_request_body() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(selectedPairAllowed=True, otherOnlineMasters=[])
        )
    )
    upper_service = SelectedService(_SERVICE_UUID.upper())
    upper_master = SelectedMaster(_MASTER_UUID.upper())
    result = _client(transport).check_eligibility(upper_service, upper_master)
    assert result.outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    body = json.loads(transport.calls[0].body.decode("utf-8"))
    assert body["serviceId"] == _SERVICE_UUID
    assert body["masterId"] == _MASTER_UUID


def test_require_canonical_uuid_helper() -> None:
    assert require_canonical_backend_uuid(_SERVICE_UUID.upper()) == _SERVICE_UUID
    with pytest.raises(BookingEligibilityHttpError):
        require_canonical_backend_uuid("nope")


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def test_request_is_exact_post_json_with_auth_header_only() -> None:
    transport = FakeTransport(response=_json_response(_success_payload()))
    result = _client(transport).check_eligibility(_SERVICE, _MASTER, include_alternatives=True)
    assert result.outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call.method == "POST"
    assert call.allow_redirects is False
    assert call.max_response_bytes == 4096
    parts = urlsplit(call.url)
    assert parts.path == ELIGIBILITY_ROUTE_PATH
    body = json.loads(call.body.decode("utf-8"))
    assert body == {
        "serviceId": _SERVICE_UUID,
        "masterId": _MASTER_UUID,
        "includeAlternatives": True,
    }
    assert call.headers["Authorization"] == f"Bearer {_VALID_TOKEN}"
    assert _VALID_TOKEN not in repr(call)
    assert len(call.body) <= 4096


def test_request_omits_master_id_when_master_absent() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(selectedPairAllowed=None, otherOnlineMasters=[])
        )
    )
    _client(transport).check_eligibility(_SERVICE, None, include_alternatives=True)
    body = json.loads(transport.calls[0].body.decode("utf-8"))
    assert "masterId" not in body
    assert body["includeAlternatives"] is True


# ---------------------------------------------------------------------------
# Success / invariants
# ---------------------------------------------------------------------------


def test_self_booking_with_master() -> None:
    transport = FakeTransport(response=_json_response(_success_payload()))
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    assert result.other_online_master_ids == ()


def test_self_booking_without_master() -> None:
    transport = FakeTransport(
        response=_json_response(_success_payload(selectedPairAllowed=None, otherOnlineMasters=[]))
    )
    result = _client(transport).check_eligibility(_SERVICE, None)
    assert result.outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED


def test_manager_handoff_with_master() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                outcome="MANAGER_HANDOFF",
                reasonCode="ONLINE_DISABLED",
                selectedPairAllowed=False,
                otherOnlineMasters=[],
            )
        )
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.MANAGER_HANDOFF
    assert result.internal_reason_code == "ONLINE_DISABLED"


def test_manager_handoff_null_reason_fails_closed() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                outcome="MANAGER_HANDOFF",
                reasonCode=None,
                selectedPairAllowed=False,
                otherOnlineMasters=[],
            )
        )
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE


def test_self_booking_service_offline_fails_closed() -> None:
    transport = FakeTransport(
        response=_json_response(_success_payload(serviceOnlineInGeneral=False))
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE


def test_self_booking_selected_pair_false_fails_closed() -> None:
    transport = FakeTransport(
        response=_json_response(_success_payload(selectedPairAllowed=False))
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE


def test_include_alternatives_true_requires_list() -> None:
    payload = _success_payload(otherOnlineMasterCount=2)
    del payload["otherOnlineMasters"]
    transport = FakeTransport(response=_json_response(payload))
    result = _client(transport).check_eligibility(_SERVICE, _MASTER, include_alternatives=True)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE


def test_alternatives_present_when_requested() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                otherOnlineMasterCount=2,
                otherOnlineMasters=[
                    {"id": _ALT_B, "publicName": "B"},
                    {"id": _ALT_C, "publicName": "C"},
                ],
            )
        )
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER, include_alternatives=True)
    assert result.other_online_master_ids == (_ALT_B, _ALT_C)


def test_include_alternatives_false_with_count_without_list() -> None:
    payload = _success_payload(otherOnlineMasterCount=3, selectedPairAllowed=True)
    del payload["otherOnlineMasters"]
    transport = FakeTransport(response=_json_response(payload))
    result = _client(transport).check_eligibility(_SERVICE, _MASTER, include_alternatives=False)
    assert result.outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    assert result.other_online_master_ids == ()


def test_include_alternatives_false_with_unexpected_list_fails() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                otherOnlineMasterCount=1,
                otherOnlineMasters=[{"id": _ALT_B, "publicName": "B"}],
            )
        )
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER, include_alternatives=False)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE


def test_duplicate_alternatives_fail_closed() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                otherOnlineMasterCount=2,
                otherOnlineMasters=[
                    {"id": _ALT_B, "publicName": "B1"},
                    {"id": _ALT_B, "publicName": "B2"},
                ],
            )
        )
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER, include_alternatives=True)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE


def test_selected_master_in_alternatives_fails_closed() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                otherOnlineMasterCount=2,
                otherOnlineMasters=[
                    {"id": _MASTER_UUID, "publicName": "Self"},
                    {"id": _ALT_B, "publicName": "B"},
                ],
            )
        )
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER, include_alternatives=True)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE


def test_count_list_mismatch_fails_closed() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                otherOnlineMasterCount=1,
                otherOnlineMasters=[
                    {"id": _ALT_B, "publicName": "B"},
                    {"id": _ALT_C, "publicName": "C"},
                ],
            )
        )
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER, include_alternatives=True)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE


def test_unknown_reason_and_outcome_fail_closed() -> None:
    for payload in (
        _success_payload(reasonCode="SOME_NEW_REASON"),
        _success_payload(outcome="TOTALLY_UNKNOWN"),
    ):
        result = _client(FakeTransport(response=_json_response(payload))).check_eligibility(
            _SERVICE, _MASTER
        )
        assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Failures / redaction / domain integration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [201, 301, 302, 400, 401, 413, 429, 500])
def test_non_200_fail_closed(status: int) -> None:
    result = _client(
        FakeTransport(response=_json_response({"error": "x"}, status=status))
    ).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert result.internal_reason_code == "REMOTE_REJECTED"


def test_transport_timeout_mapped() -> None:
    result = _client(
        FakeTransport(error=S2sHttpTransportError("TIMEOUT"))
    ).check_eligibility(_SERVICE, _MASTER)
    assert result.internal_reason_code == "TIMEOUT"


def test_secrets_not_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    secret = "S" * 40
    transport = FakeTransport(error=S2sHttpTransportError("TIMEOUT"))
    with caplog.at_level(logging.INFO):
        result = _client(transport, bearer_token=secret).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in blob
    assert _SERVICE_UUID not in blob
    assert "Authorization" not in blob


def test_remote_dto_repr_redacts_public_name() -> None:
    alt = EligibilityRemoteAlternativeMaster(id=_ALT_B, public_name="Visible Name")
    assert "Visible Name" not in repr(alt)
    assert "public_name=<redacted>" in repr(alt)
    req = EligibilityRemoteRequest(service_id=_SERVICE_UUID, master_id=_MASTER_UUID, include_alternatives=True)
    assert _SERVICE_UUID not in repr(req)
    assert "service_id=<redacted>" in repr(req)


def test_mapped_result_compatible_with_decide_booking_dialog() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                otherOnlineMasterCount=1,
                otherOnlineMasters=[{"id": _ALT_B, "publicName": "B"}],
            )
        )
    )
    eligibility = _client(transport).check_eligibility(_SERVICE, _MASTER, include_alternatives=True)
    now = datetime(2026, 8, 5, 7, 0, tzinfo=timezone(timedelta(hours=5)))
    decision = decide_booking_dialog(
        eligibility,
        (_slot(slot_id="b1", master_id=_ALT_B),),
        now=now,
        alternate_master_consent=True,
    )
    assert decision.action.value == "OFFER_SLOTS"
    assert [slot.master_id for slot in decision.offered_slots] == [_ALT_B]


def test_without_allowlist_consent_does_not_open_arbitrary_slots() -> None:
    payload = _success_payload(otherOnlineMasterCount=0, otherOnlineMasters=[])
    eligibility = _client(FakeTransport(response=_json_response(payload))).check_eligibility(
        _SERVICE, _MASTER, include_alternatives=True
    )
    now = datetime(2026, 8, 5, 7, 0, tzinfo=timezone(timedelta(hours=5)))
    decision = decide_booking_dialog(
        eligibility,
        (_slot(slot_id="hidden", master_id=_ALT_B),),
        now=now,
        alternate_master_consent=True,
    )
    assert isinstance(decision, ManagerHandoffDecision)
    assert decision.internal_reason_code == BookingInternalReasonCode.NO_VALID_SLOTS.value


def test_internal_reason_not_in_client_message() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                outcome="MANAGER_HANDOFF",
                reasonCode="MANAGER_ONLY",
                selectedPairAllowed=False,
                otherOnlineMasters=[],
            )
        )
    )
    eligibility = _client(transport).check_eligibility(_SERVICE, _MASTER)
    now = datetime(2026, 8, 5, 7, 0, tzinfo=timezone(timedelta(hours=5)))
    text = client_message_for_decision(decide_booking_dialog(eligibility, (), now=now))
    assert "MANAGER_ONLY" not in text
