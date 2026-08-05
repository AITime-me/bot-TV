"""Unit tests for CURSOR-15 booking eligibility HTTP adapter.

Uses a fake S2sHttpTransport only. No live network, env, Docker, or pipeline.
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
_SERVICE = SelectedService("service-1")
_MASTER = SelectedMaster("master-a")


class FakeTransport:
    """Records a single programmed response. Never opens a network socket."""

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
        service_id="service-1",
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_accepts_http_and_https() -> None:
    http_cfg = _config(base_url="http://eligibility.example")
    https_cfg = _config(base_url="https://eligibility.example")
    assert http_cfg.eligibility_url == "http://eligibility.example" + ELIGIBILITY_ROUTE_PATH
    assert https_cfg.eligibility_url == "https://eligibility.example" + ELIGIBILITY_ROUTE_PATH


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://eligibility.example",
        "https://user:pass@eligibility.example",
        "https://eligibility.example?x=1",
        "https://eligibility.example#frag",
        "https://eligibility.example/api",
        "https://example.com:abc",
        "https://example.com:65536",
        r"https://example.com\@evil.com",
        "https://example.com\n.evil.com",
        "not-a-url",
        "",
    ],
)
def test_config_rejects_invalid_base_url(base_url: str) -> None:
    with pytest.raises(BookingEligibilityHttpError) as exc_info:
        _config(base_url=base_url)
    assert exc_info.value.code == "CONFIG_INVALID"
    assert _VALID_TOKEN not in str(exc_info.value)
    assert "user:pass" not in str(exc_info.value)


def test_config_accepts_ipv6_and_trailing_slash() -> None:
    ipv6 = _config(base_url="https://[::1]:8443")
    assert ipv6.eligibility_url == "https://[::1]:8443" + ELIGIBILITY_ROUTE_PATH
    slash = _config(base_url="https://eligibility.example/")
    assert slash.eligibility_url == "https://eligibility.example" + ELIGIBILITY_ROUTE_PATH


@pytest.mark.parametrize(
    "token",
    ["", "short", " " * 32, "a" * 31, ("a" * 31) + "\x00", ("b" * 31) + "\n"],
)
def test_config_rejects_invalid_token(token: str) -> None:
    with pytest.raises(BookingEligibilityHttpError) as exc_info:
        _config(bearer_token=token)
    assert exc_info.value.code == "CONFIG_INVALID"
    assert "CONFIG_INVALID" == str(exc_info.value)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), "3", None])
def test_config_rejects_invalid_timeout(timeout: object) -> None:
    with pytest.raises(BookingEligibilityHttpError) as exc_info:
        BookingEligibilityHttpConfig(
            base_url="https://eligibility.example",
            bearer_token=_VALID_TOKEN,
            timeout_seconds=timeout,  # type: ignore[arg-type]
        )
    assert exc_info.value.code == "CONFIG_INVALID"


@pytest.mark.parametrize("limit", [0, -5, True, 1_000_001, 3.5, "4096"])
def test_config_rejects_invalid_response_limit(limit: object) -> None:
    with pytest.raises(BookingEligibilityHttpError) as exc_info:
        BookingEligibilityHttpConfig(
            base_url="https://eligibility.example",
            bearer_token=_VALID_TOKEN,
            max_response_bytes=limit,  # type: ignore[arg-type]
        )
    assert exc_info.value.code == "CONFIG_INVALID"


def test_config_repr_redacts_token() -> None:
    text = repr(_config())
    assert _VALID_TOKEN not in text
    assert "bearer_token=<redacted>" in text


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def test_request_is_exact_post_json_with_auth_header_only() -> None:
    transport = FakeTransport(response=_json_response(_success_payload()))
    client = _client(transport)
    result = client.check_eligibility(_SERVICE, _MASTER, include_alternatives=True)

    assert result.outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call.method == "POST"
    assert call.allow_redirects is False
    assert call.timeout_seconds == 3.5
    parts = urlsplit(call.url)
    assert parts.scheme == "https"
    assert parts.hostname == "eligibility.example"
    assert parts.path == ELIGIBILITY_ROUTE_PATH
    assert parts.query == ""
    assert parts.fragment == ""
    assert parts.username is None
    assert _VALID_TOKEN not in call.url

    body = json.loads(call.body.decode("utf-8"))
    assert body == {
        "serviceId": "service-1",
        "masterId": "master-a",
        "includeAlternatives": True,
    }
    assert _VALID_TOKEN not in call.body.decode("utf-8")
    assert call.headers["Authorization"] == f"Bearer {_VALID_TOKEN}"
    assert call.headers["Content-Type"] == "application/json"
    assert call.headers["Accept"] == "application/json"
    assert "Authorization" not in repr(call)
    assert _VALID_TOKEN not in repr(call)


def test_request_omits_master_id_when_master_absent() -> None:
    transport = FakeTransport(response=_json_response(_success_payload(selectedPairAllowed=None)))
    client = _client(transport)
    client.check_eligibility(_SERVICE, None, include_alternatives=False)
    body = json.loads(transport.calls[0].body.decode("utf-8"))
    assert body == {
        "serviceId": "service-1",
        "includeAlternatives": False,
    }
    assert "masterId" not in body


def test_no_retry_on_transport_error() -> None:
    transport = FakeTransport(error=S2sHttpTransportError("TRANSPORT_ERROR"))
    client = _client(transport)
    result = client.check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# Success parsing
# ---------------------------------------------------------------------------


def test_success_self_booking_allowed() -> None:
    transport = FakeTransport(response=_json_response(_success_payload()))
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    assert result.selected_service == _SERVICE
    assert result.selected_master == _MASTER
    assert result.other_online_master_ids == ()


def test_success_manager_handoff() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                outcome="MANAGER_HANDOFF",
                reasonCode="MANAGER_ONLY",
                selectedPairAllowed=False,
            )
        )
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.MANAGER_HANDOFF
    assert result.internal_reason_code == "MANAGER_ONLY"


@pytest.mark.parametrize("selected_pair", [True, False])
def test_selected_pair_allowed_bool_with_master(selected_pair: bool) -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                outcome="MANAGER_HANDOFF" if selected_pair is False else "SELF_BOOKING_ALLOWED",
                reasonCode="ONLINE_DISABLED" if selected_pair is False else None,
                selectedPairAllowed=selected_pair,
            )
        )
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    if selected_pair:
        assert result.outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    else:
        assert result.outcome is BookingEligibilityOutcome.MANAGER_HANDOFF


def test_self_booking_with_selected_pair_false_fails_closed() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                outcome="SELF_BOOKING_ALLOWED",
                selectedPairAllowed=False,
            )
        )
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert (
        result.internal_reason_code
        == BookingEligibilityAdapterReasonCode.RESPONSE_INVALID.value
    )


def test_selected_pair_null_with_master_fails_closed() -> None:
    transport = FakeTransport(
        response=_json_response(_success_payload(selectedPairAllowed=None))
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE


def test_selected_pair_bool_without_master_fails_closed() -> None:
    transport = FakeTransport(
        response=_json_response(_success_payload(selectedPairAllowed=True))
    )
    result = _client(transport).check_eligibility(_SERVICE, None)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE


def test_selected_pair_null_without_master_ok() -> None:
    transport = FakeTransport(
        response=_json_response(_success_payload(selectedPairAllowed=None))
    )
    result = _client(transport).check_eligibility(_SERVICE, None)
    assert result.outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED


def test_alternatives_absent_and_present() -> None:
    absent = FakeTransport(response=_json_response(_success_payload(otherOnlineMasterCount=2)))
    result_absent = _client(absent).check_eligibility(_SERVICE, _MASTER, include_alternatives=True)
    assert result_absent.other_online_master_ids == ()

    present = FakeTransport(
        response=_json_response(
            _success_payload(
                otherOnlineMasterCount=2,
                otherOnlineMasters=[
                    {"id": "master-b", "publicName": "B"},
                    {"id": "master-c", "publicName": "C"},
                ],
            )
        )
    )
    result_present = _client(present).check_eligibility(
        _SERVICE, _MASTER, include_alternatives=True
    )
    assert result_present.other_online_master_ids == ("master-b", "master-c")


def test_duplicate_alternative_ids_are_deduped_deterministically() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                otherOnlineMasterCount=3,
                otherOnlineMasters=[
                    {"id": "master-b", "publicName": "B1"},
                    {"id": "master-c", "publicName": "C"},
                    {"id": "master-b", "publicName": "B2"},
                ],
            )
        )
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.other_online_master_ids == ("master-b", "master-c")


def test_selected_master_excluded_from_alternatives() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                otherOnlineMasterCount=2,
                otherOnlineMasters=[
                    {"id": "master-a", "publicName": "Self"},
                    {"id": "master-b", "publicName": "B"},
                ],
            )
        )
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.other_online_master_ids == ("master-b",)


def test_include_alternatives_false_ignores_list_and_count() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                otherOnlineMasterCount=5,
                otherOnlineMasters=[{"id": "master-b", "publicName": "B"}],
            )
        )
    )
    result = _client(transport).check_eligibility(
        _SERVICE, _MASTER, include_alternatives=False
    )
    assert result.other_online_master_ids == ()


def test_unknown_reason_code_fails_closed() -> None:
    transport = FakeTransport(
        response=_json_response(_success_payload(reasonCode="SOME_NEW_REASON"))
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert result.outcome is not BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    assert (
        result.internal_reason_code
        == BookingEligibilityAdapterReasonCode.RESPONSE_INVALID.value
    )


def test_known_reason_with_self_booking_fails_closed() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                outcome="SELF_BOOKING_ALLOWED",
                reasonCode="MANAGER_ONLY",
            )
        )
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE


@pytest.mark.parametrize(
    "payload",
    [
        _success_payload(otherOnlineMasterCount=0, otherOnlineMasters=[{"id": "master-b", "publicName": "B"}]),
        _success_payload(
            otherOnlineMasterCount=1,
            otherOnlineMasters=[
                {"id": "master-b", "publicName": "B"},
                {"id": "master-c", "publicName": "C"},
            ],
        ),
        _success_payload(
            otherOnlineMasterCount=5,
            otherOnlineMasters=[{"id": "master-b", "publicName": "B"}],
        ),
    ],
)
def test_count_list_mismatch_fails_closed(payload: dict[str, Any]) -> None:
    transport = FakeTransport(response=_json_response(payload))
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert (
        result.internal_reason_code
        == BookingEligibilityAdapterReasonCode.RESPONSE_INVALID.value
    )


def test_unknown_outcome_fails_closed() -> None:
    transport = FakeTransport(
        response=_json_response(_success_payload(outcome="TOTALLY_UNKNOWN"))
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert result.outcome is not BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    assert (
        result.internal_reason_code
        == BookingEligibilityAdapterReasonCode.RESPONSE_INVALID.value
    )


@pytest.mark.parametrize(
    "payload",
    [
        _success_payload(ok=False),
        _success_payload(serviceOnlineInGeneral="yes"),
        _success_payload(otherOnlineMasterCount=-1),
        _success_payload(otherOnlineMasterCount=True),
        _success_payload(selectedPairAllowed="yes"),
        {"ok": True, "outcome": "SELF_BOOKING_ALLOWED"},
        _success_payload(otherOnlineMasters=[{"id": "master-b"}]),
        _success_payload(otherOnlineMasters=[{"publicName": "B"}]),
        _success_payload(otherOnlineMasters=[{"id": "bad id", "publicName": "B"}]),
        _success_payload(otherOnlineMasters="master-b"),
        _success_payload(otherOnlineMasters=None),
        _success_payload(reasonCode=123),
    ],
)
def test_malformed_success_payload_fails_closed(payload: dict[str, Any]) -> None:
    transport = FakeTransport(response=_json_response(payload))
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert (
        result.internal_reason_code
        == BookingEligibilityAdapterReasonCode.RESPONSE_INVALID.value
    )


def test_invalid_json_wrong_content_type_empty_and_oversized() -> None:
    invalid_json = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=b"{not-json",
        )
    )
    invalid_utf8 = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=b"\xff\xfe{",
        )
    )
    wrong_type = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={"Content-Type": "text/plain"},
            body=json.dumps(_success_payload()).encode("utf-8"),
        )
    )
    empty = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=b"",
        )
    )
    # Unicode body: character count below limit, byte length above.
    unicode_payload = {"pad": "ы" * 3000}
    unicode_body = json.dumps(unicode_payload, ensure_ascii=False).encode("utf-8")
    assert len(unicode_body) > 4096
    unicode_oversize = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(unicode_body)),
            },
            body=unicode_body,
        )
    )
    # Lying Content-Length smaller than real body still blocked by body len.
    real_oversize = b"{" + (b"a" * 5000) + b"}"
    lying_cl = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={
                "Content-Type": "application/json",
                "Content-Length": "100",
            },
            body=real_oversize,
        )
    )
    # Exact boundary accepted; +1 rejected.
    valid_body = json.dumps(_success_payload(), ensure_ascii=False).encode("utf-8")
    boundary_ok = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(valid_body)),
            },
            body=valid_body,
        )
    )
    boundary_plus = FakeTransport(
        response=S2sHttpResponse(
            status_code=200,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(valid_body) + 1),
            },
            body=valid_body + b"\n",
        )
    )

    for transport, code in (
        (invalid_json, "RESPONSE_INVALID"),
        (invalid_utf8, "RESPONSE_INVALID"),
        (wrong_type, "RESPONSE_INVALID"),
        (empty, "RESPONSE_INVALID"),
        (unicode_oversize, "RESPONSE_TOO_LARGE"),
        (lying_cl, "RESPONSE_TOO_LARGE"),
    ):
        result = _client(transport, max_response_bytes=4096).check_eligibility(_SERVICE, _MASTER)
        assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
        assert result.internal_reason_code == code

    ok = _client(boundary_ok, max_response_bytes=len(valid_body)).check_eligibility(
        _SERVICE, _MASTER
    )
    assert ok.outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    plus = _client(boundary_plus, max_response_bytes=len(valid_body)).check_eligibility(
        _SERVICE, _MASTER
    )
    assert plus.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert plus.internal_reason_code == "RESPONSE_TOO_LARGE"


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (S2sHttpTransportError("TIMEOUT"), "TIMEOUT"),
        (S2sHttpTransportError("TRANSPORT_ERROR"), "TRANSPORT_ERROR"),
        (ConnectionError("boom"), "TRANSPORT_ERROR"),
    ],
)
def test_transport_failures_fail_closed(error: BaseException, code: str) -> None:
    transport = FakeTransport(error=error)
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert result.internal_reason_code == code


@pytest.mark.parametrize("status", [201, 204, 400, 401, 413, 429, 500])
def test_non_200_statuses_fail_closed(status: int) -> None:
    transport = FakeTransport(
        response=_json_response({"error": "x"}, status=status)
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert (
        result.internal_reason_code
        == BookingEligibilityAdapterReasonCode.REMOTE_REJECTED.value
    )


def test_secrets_and_ids_not_in_exception_or_log_output(caplog: pytest.LogCaptureFixture) -> None:
    secret = "S" * 40
    transport = FakeTransport(error=S2sHttpTransportError("TIMEOUT"))
    with caplog.at_level(logging.INFO):
        result = _client(transport, bearer_token=secret).check_eligibility(
            SelectedService("secret-service-id"),
            SelectedMaster("secret-master-id"),
        )
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    log_blob = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in log_blob
    assert "secret-service-id" not in log_blob
    assert "secret-master-id" not in log_blob
    assert "Authorization" not in log_blob
    assert "Bearer" not in log_blob
    assert result.internal_reason_code == "TIMEOUT"
    assert "booking_eligibility_http_fail_closed code=TIMEOUT" in log_blob

    # Config / transport errors expose only fixed codes.
    with pytest.raises(BookingEligibilityHttpError) as exc_info:
        _config(bearer_token="short-token-value-not-32")
    assert secret not in str(exc_info.value)
    assert "short-token-value-not-32" not in str(exc_info.value)
    assert exc_info.value.code == "CONFIG_INVALID"
    assert _VALID_TOKEN not in repr(_config())
    assert secret not in repr(transport.calls[0])
    assert "Authorization" not in repr(transport.calls[0])


# ---------------------------------------------------------------------------
# Domain integration
# ---------------------------------------------------------------------------


def test_mapped_result_compatible_with_decide_booking_dialog() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(
                otherOnlineMasterCount=1,
                otherOnlineMasters=[{"id": "master-b", "publicName": "B"}],
            )
        )
    )
    eligibility = _client(transport).check_eligibility(_SERVICE, _MASTER)
    now = datetime(2026, 8, 5, 7, 0, tzinfo=timezone(timedelta(hours=5)))
    decision = decide_booking_dialog(
        eligibility,
        (_slot(slot_id="b1", master_id="master-b"),),
        now=now,
        alternate_master_consent=True,
    )
    assert decision.action.value == "OFFER_SLOTS"
    assert [slot.master_id for slot in decision.offered_slots] == ["master-b"]


def test_without_allowlist_consent_does_not_open_arbitrary_slots() -> None:
    transport = FakeTransport(
        response=_json_response(_success_payload(otherOnlineMasterCount=3))
    )
    eligibility = _client(transport).check_eligibility(
        _SERVICE, _MASTER, include_alternatives=True
    )
    assert eligibility.other_online_master_ids == ()
    now = datetime(2026, 8, 5, 7, 0, tzinfo=timezone(timedelta(hours=5)))
    decision = decide_booking_dialog(
        eligibility,
        (
            _slot(slot_id="hidden", master_id="master-hidden"),
            _slot(slot_id="b1", master_id="master-b", minute=1),
        ),
        now=now,
        alternate_master_consent=True,
    )
    assert isinstance(decision, ManagerHandoffDecision)
    assert (
        decision.internal_reason_code
        == BookingInternalReasonCode.NO_VALID_SLOTS.value
    )


def test_internal_reason_not_in_client_message() -> None:
    transport = FakeTransport(
        response=_json_response(
            _success_payload(outcome="MANAGER_HANDOFF", reasonCode="MANAGER_ONLY")
        )
    )
    eligibility = _client(transport).check_eligibility(_SERVICE, _MASTER)
    now = datetime(2026, 8, 5, 7, 0, tzinfo=timezone(timedelta(hours=5)))
    decision = decide_booking_dialog(eligibility, (), now=now)
    text = client_message_for_decision(decision)
    assert "MANAGER_ONLY" not in text
    assert eligibility.internal_reason_code == "MANAGER_ONLY"


def test_transport_exception_with_secret_does_not_leak(caplog: pytest.LogCaptureFixture) -> None:
    secret = "leak-token-value-" + ("Z" * 32)
    transport = FakeTransport(error=RuntimeError(f"Authorization Bearer {secret}"))
    with caplog.at_level(logging.INFO):
        result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert result.internal_reason_code == "TRANSPORT_ERROR"
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in blob
    assert "Authorization" not in blob


def test_request_and_response_repr_omit_sensitive_material() -> None:
    req = S2sHttpRequest(
        method="POST",
        url="https://eligibility.example/api/internal/bot/v1/eligibility",
        headers={"Authorization": f"Bearer {_VALID_TOKEN}", "Content-Type": "application/json"},
        body=b'{"serviceId":"service-1"}',
        timeout_seconds=1.0,
        allow_redirects=False,
    )
    resp = S2sHttpResponse(
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=b'{"ok":true,"publicName":"Hidden"}',
    )
    assert _VALID_TOKEN not in repr(req)
    assert "Authorization" not in repr(req)
    assert "service-1" not in repr(req)
    assert "eligibility.example" not in repr(req)
    assert "publicName" not in repr(resp)
    assert "Hidden" not in repr(resp)
    assert "ok" not in repr(resp)


def test_all_failure_paths_never_self_booking() -> None:
    cases = [
        FakeTransport(error=S2sHttpTransportError("TIMEOUT")),
        FakeTransport(response=_json_response({"error": "x"}, status=401)),
        FakeTransport(response=_json_response(_success_payload(outcome="NOPE"))),
        FakeTransport(response=_json_response(_success_payload(reasonCode="UNKNOWN_X"))),
        FakeTransport(
            response=_json_response(
                _success_payload(
                    outcome="SELF_BOOKING_ALLOWED",
                    selectedPairAllowed=False,
                )
            )
        ),
        FakeTransport(
            response=S2sHttpResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body=b"",
            )
        ),
    ]
    for transport in cases:
        result = _client(transport).check_eligibility(_SERVICE, _MASTER)
        assert result.outcome is not BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
