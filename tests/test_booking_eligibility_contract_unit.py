"""Contract-focused tests locked to online-zapis-tv PR A eligibility API.

Source of truth: online-zapis-tv
  - src/app/api/internal/bot/v1/eligibility/route.ts
  - src/lib/bot-api/eligibility-types.ts
  - src/lib/bot-api/evaluate-eligibility.ts
  - src/lib/auth/bot-internal-auth.ts
  - docs/architecture/bot-internal-api-pr-a.md

No live network. Transport is injected.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import Settings
from app.core.booking_eligibility_http import (
    ELIGIBILITY_ROUTE_PATH,
    BookingEligibilityHttpClient,
    BookingEligibilityHttpConfig,
    BookingEligibilityHttpError,
)
from app.core.booking_types import (
    BookingEligibilityOutcome,
    SelectedMaster,
    SelectedService,
)
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransportError,
)

_SERVICE_UUID = "11111111-1111-4111-8111-111111111111"
_MASTER_UUID = "22222222-2222-4222-8222-222222222222"
_ALT_UUID = "33333333-3333-4333-8333-333333333333"
_TOKEN = "t" * 32
_SERVICE = SelectedService(_SERVICE_UUID)
_MASTER = SelectedMaster(_MASTER_UUID)


class _RecordingTransport:
    def __init__(self, *, response: S2sHttpResponse | None = None) -> None:
        self.calls: list[S2sHttpRequest] = []
        self._response = response

    def request(self, request: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(request)
        if self._response is None:
            raise S2sHttpTransportError("TRANSPORT_ERROR")
        return self._response


def _config(**overrides: Any) -> BookingEligibilityHttpConfig:
    values: dict[str, Any] = {
        "base_url": "https://booking.internal.example",
        "bearer_token": _TOKEN,
    }
    values.update(overrides)
    return BookingEligibilityHttpConfig(**values)


def _client(
    transport: _RecordingTransport, **config_overrides: Any
) -> BookingEligibilityHttpClient:
    return BookingEligibilityHttpClient(
        _config(**config_overrides),
        transport,
        settings=Settings.from_env(
            {
                "BOT_MODE": "AUTO_READ",
                "EMERGENCY_LOCK": "false",
            }
        ),
    )


def _json_response(
    payload: object,
    *,
    status: int = 200,
    content_type: str = "application/json; charset=utf-8",
) -> S2sHttpResponse:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return S2sHttpResponse(
        status_code=status,
        headers={"Content-Type": content_type, "Content-Length": str(len(body))},
        body=body,
    )


def _eligible_pair(**overrides: Any) -> dict[str, Any]:
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


def _ineligible_pair(*, reason: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "outcome": "MANAGER_HANDOFF",
        "reasonCode": reason,
        "selectedPairAllowed": False,
        "serviceOnlineInGeneral": True,
        "otherOnlineMasterCount": 0,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Wire request
# ---------------------------------------------------------------------------


def test_contract_endpoint_method_path_and_bearer_only() -> None:
    transport = _RecordingTransport(response=_json_response(_eligible_pair()))
    _client(transport).check_eligibility(_SERVICE, _MASTER)
    call = transport.calls[0]
    assert call.method == "POST"
    assert call.url.endswith(ELIGIBILITY_ROUTE_PATH)
    assert call.headers["Authorization"] == f"Bearer {_TOKEN}"
    assert call.headers["Content-Type"] == "application/json"
    assert call.allow_redirects is False
    assert "Idempotency-Key" not in call.headers
    assert "X-Signature" not in call.headers
    assert "HMAC" not in str(call.headers).upper()


def test_contract_request_body_fields_and_defaults() -> None:
    transport = _RecordingTransport(response=_json_response(_eligible_pair()))
    _client(transport).check_eligibility(_SERVICE, _MASTER)
    body = json.loads(transport.calls[0].body.decode("utf-8"))
    assert set(body) == {"serviceId", "masterId", "includeAlternatives"}
    assert body["serviceId"] == _SERVICE_UUID
    assert body["masterId"] == _MASTER_UUID
    assert body["includeAlternatives"] is False


def test_contract_request_omits_master_and_keeps_explicit_alternatives_flag() -> None:
    transport = _RecordingTransport(
        response=_json_response(
            _eligible_pair(selectedPairAllowed=None, otherOnlineMasterCount=0)
        )
    )
    _client(transport).check_eligibility(_SERVICE, None)
    body = json.loads(transport.calls[0].body.decode("utf-8"))
    assert set(body) == {"serviceId", "includeAlternatives"}
    assert body["includeAlternatives"] is False


def test_contract_request_body_under_backend_4096_limit() -> None:
    transport = _RecordingTransport(response=_json_response(_eligible_pair()))
    _client(transport).check_eligibility(_SERVICE, _MASTER, include_alternatives=True)
    assert len(transport.calls[0].body) <= 4096


# ---------------------------------------------------------------------------
# Success semantics (HTTP 200 for both eligible and ineligible)
# ---------------------------------------------------------------------------


def test_contract_eligible_pair_is_http_200() -> None:
    transport = _RecordingTransport(response=_json_response(_eligible_pair()))
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    assert result.internal_reason_code is None


def test_contract_ineligible_pair_is_still_http_200_manager_handoff() -> None:
    transport = _RecordingTransport(
        response=_json_response(_ineligible_pair(reason="STUDIO_ONLINE_DISABLED"))
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.MANAGER_HANDOFF
    assert result.internal_reason_code == "STUDIO_ONLINE_DISABLED"


@pytest.mark.parametrize(
    "reason",
    [
        "STUDIO_ONLINE_DISABLED",
        "SERVICE_INACTIVE",
        "MASTER_INACTIVE",
        "ONLINE_DISABLED",
        "MASTER_SERVICE_UNAVAILABLE",
        "MANAGER_ONLY",
    ],
)
def test_contract_known_reason_codes_accepted(reason: str) -> None:
    transport = _RecordingTransport(
        response=_json_response(_ineligible_pair(reason=reason))
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.MANAGER_HANDOFF
    assert result.internal_reason_code == reason


def test_contract_alternatives_only_when_requested() -> None:
    transport = _RecordingTransport(
        response=_json_response(
            _eligible_pair(
                otherOnlineMasterCount=1,
                otherOnlineMasters=[{"id": _ALT_UUID, "publicName": "Alt"}],
            )
        )
    )
    result = _client(transport).check_eligibility(
        _SERVICE, _MASTER, include_alternatives=True
    )
    assert result.other_online_master_ids == (_ALT_UUID,)
    assert "Alt" not in repr(result)


def test_contract_charset_content_type_accepted() -> None:
    transport = _RecordingTransport(
        response=_json_response(
            _eligible_pair(),
            content_type="application/json; charset=utf-8",
        )
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED


# ---------------------------------------------------------------------------
# Error HTTP codes — fail-closed, no body echo, no retries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error_body"),
    [
        (401, {"ok": False, "code": "UNAUTHORIZED", "error": "Unauthorized"}),
        (400, {"ok": False, "code": "VALIDATION_ERROR", "error": "Invalid serviceId"}),
        (413, {"ok": False, "code": "PAYLOAD_TOO_LARGE", "error": "Payload too large"}),
        (429, {"ok": False, "code": "RATE_LIMITED", "error": "Too many requests"}),
        (500, {"ok": False, "code": "INTERNAL_ERROR", "error": "Internal error"}),
    ],
)
def test_contract_backend_error_statuses_fail_closed_without_echo(
    status: int, error_body: dict[str, object]
) -> None:
    transport = _RecordingTransport(
        response=_json_response(error_body, status=status)
    )
    result = _client(transport).check_eligibility(_SERVICE, _MASTER)
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert result.internal_reason_code == "REMOTE_REJECTED"
    assert len(transport.calls) == 1  # no retry
    rendered = f"{result!r}{result!s}"
    assert "Unauthorized" not in rendered
    assert "Invalid serviceId" not in rendered
    assert "Payload too large" not in rendered
    assert "RATE_LIMITED" not in rendered
    assert "INTERNAL_ERROR" not in rendered
    assert _TOKEN not in rendered


# ---------------------------------------------------------------------------
# Token rules aligned with bot-internal-auth.ts
# ---------------------------------------------------------------------------


def test_contract_token_rejects_non_printable_and_oversize() -> None:
    with pytest.raises(BookingEligibilityHttpError):
        _config(bearer_token="a" * 31 + "\u0440")
    with pytest.raises(BookingEligibilityHttpError):
        _config(bearer_token="a" * 513)
