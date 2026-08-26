"""Unit tests for amoCRM analytics field contract + safe write-if-empty."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from app.core.amocrm_analytics_fields import (
    AMOCRM_ANALYTICS_CHANNEL_FIELD_ID,
    AmoCrmAnalyticsApplyDecision,
    AmoCrmAnalyticsBookingMethodEnum,
    AmoCrmAnalyticsFieldId,
    AmoCrmAnalyticsSourcePrimaryEnum,
    assert_enum_allowed_for_field,
    assert_writable_analytics_field_id,
)
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.amocrm_crm_writes_http import (
    AmoCrmAnalyticsApplyReceipt,
    AmoCrmCrmWriteOutcome,
    AmoCrmCrmWritesHttpClient,
)
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.services.teya_request_crm import (
    TeyaCrmActionOutcome,
    TeyaRequestCrmService,
)

_TEST_PIPELINE = 1001
_TEST_STATUS = 2002
_TEST_MANAGER = 3003
_TEST_TASK_TYPE = 4004


@dataclass
class _FakeTransport:
    responses: list[S2sHttpResponse]
    calls: list[S2sHttpRequest]

    def __init__(self, responses: list[S2sHttpResponse]) -> None:
        self.responses = list(responses)
        self.calls = []

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        if not self.responses:
            raise AssertionError("unexpected HTTP call")
        return self.responses.pop(0)


def _json_response(status: int, payload: object) -> S2sHttpResponse:
    body = json.dumps(payload).encode("utf-8")
    return S2sHttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        body=body,
    )


def _config() -> AmoCrmCrmRestConfig:
    return AmoCrmCrmRestConfig(
        enabled=True,
        client_id="crm-client-id-001",
        client_secret="crm-secret-xxxxxxxxxx",
        api_base_url="https://example.amocrm.ru",
        redirect_uri="https://example.com/oauth",
        connection_scope="default",
    )


def _writes(transport: _FakeTransport) -> AmoCrmCrmWritesHttpClient:
    return AmoCrmCrmWritesHttpClient(
        _config(),
        transport=transport,
        pipeline_id=_TEST_PIPELINE,
        open_status_id=_TEST_STATUS,
        manager_id=_TEST_MANAGER,
        task_type_id=_TEST_TASK_TYPE,
    )


def _lead_payload(*, field_id: int | None = None, enum_id: int | None = None) -> dict:
    custom: list[dict] = []
    if field_id is not None and enum_id is not None:
        custom.append(
            {
                "field_id": field_id,
                "values": [{"enum_id": enum_id}],
            }
        )
    return {
        "id": 55,
        "pipeline_id": _TEST_PIPELINE,
        "status_id": _TEST_STATUS,
        "custom_fields_values": custom,
    }


class _Token:
    async def access_token(self) -> str | None:
        return "token-abc"

    async def refresh_access_token(self) -> str | None:
        return None


class _TokenRefreshOnce(_Token):
    def __init__(self) -> None:
        self.refreshed = 0

    async def refresh_access_token(self) -> str | None:
        self.refreshed += 1
        return "token-refreshed"


class _TechIds:
    def __init__(self, ids: tuple[str, ...] = ()) -> None:
        self._ids = ids

    async def list_active_technical_deal_ids(self) -> tuple[str, ...]:
        return self._ids


def test_channel_field_never_writable() -> None:
    with pytest.raises(ValueError, match="CHANNEL_WRITE_FORBIDDEN"):
        assert_writable_analytics_field_id(AMOCRM_ANALYTICS_CHANNEL_FIELD_ID)
    with pytest.raises(ValueError, match="CHANNEL_WRITE_FORBIDDEN"):
        assert_enum_allowed_for_field(AMOCRM_ANALYTICS_CHANNEL_FIELD_ID, 851491)


def test_contract_ids_match_live_proof() -> None:
    assert int(AmoCrmAnalyticsFieldId.SOURCE_PRIMARY) == 1258095
    assert int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD) == 1321305
    assert int(AmoCrmAnalyticsBookingMethodEnum.TEYA) == 851491
    assert AMOCRM_ANALYTICS_CHANNEL_FIELD_ID == 1321303


def test_cross_field_enum_rejected() -> None:
    with pytest.raises(ValueError, match="ENUM_NOT_ALLOWED_FOR_FIELD"):
        assert_enum_allowed_for_field(
            int(AmoCrmAnalyticsFieldId.SOURCE_PRIMARY),
            int(AmoCrmAnalyticsBookingMethodEnum.TEYA),
        )
    with pytest.raises(ValueError, match="ENUM_NOT_ALLOWED_FOR_FIELD"):
        assert_enum_allowed_for_field(
            int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD),
            int(AmoCrmAnalyticsSourcePrimaryEnum.SITE),
        )
    with pytest.raises(ValueError, match="ENUM_NOT_ALLOWED_FOR_FIELD"):
        assert_enum_allowed_for_field(
            int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD),
            999999,
        )


def test_empty_booking_method_patches_teya_enum() -> None:
    field = int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD)
    enum_id = int(AmoCrmAnalyticsBookingMethodEnum.TEYA)
    transport = _FakeTransport(
        [
            _json_response(200, _lead_payload()),
            _json_response(200, {"_embedded": {"leads": [{"id": 55}]}}),
            _json_response(200, _lead_payload(field_id=field, enum_id=enum_id)),
        ]
    )
    client = _writes(transport)
    receipt = client.ensure_lead_analytics_enum_if_empty(
        lead_id="55",
        field_id=field,
        enum_id=enum_id,
        access_token="token",
    )
    assert receipt.outcome is AmoCrmCrmWriteOutcome.VERIFIED
    assert receipt.decision is AmoCrmAnalyticsApplyDecision.APPLIED
    assert [c.method for c in transport.calls] == ["GET", "PATCH", "GET"]
    patch_body = json.loads(transport.calls[1].body.decode("utf-8"))
    assert patch_body[0]["custom_fields_values"][0]["field_id"] == field
    assert patch_body[0]["custom_fields_values"][0]["values"][0]["enum_id"] == enum_id
    assert "1321303" not in transport.calls[1].body.decode("utf-8")


def test_same_booking_method_no_patch() -> None:
    field = int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD)
    enum_id = int(AmoCrmAnalyticsBookingMethodEnum.TEYA)
    transport = _FakeTransport(
        [_json_response(200, _lead_payload(field_id=field, enum_id=enum_id))]
    )
    receipt = _writes(transport).ensure_lead_analytics_enum_if_empty(
        lead_id="55", field_id=field, enum_id=enum_id, access_token="token"
    )
    assert receipt.decision is AmoCrmAnalyticsApplyDecision.ALREADY_SAME
    assert [c.method for c in transport.calls] == ["GET"]


def test_conflict_booking_method_no_overwrite() -> None:
    field = int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD)
    transport = _FakeTransport(
        [
            _json_response(
                200,
                _lead_payload(
                    field_id=field,
                    enum_id=int(AmoCrmAnalyticsBookingMethodEnum.MANAGER),
                ),
            )
        ]
    )
    receipt = _writes(transport).ensure_lead_analytics_enum_if_empty(
        lead_id="55",
        field_id=field,
        enum_id=int(AmoCrmAnalyticsBookingMethodEnum.TEYA),
        access_token="token",
    )
    assert receipt.decision is AmoCrmAnalyticsApplyDecision.CONFLICT_NONEMPTY
    assert [c.method for c in transport.calls] == ["GET"]


def test_adapter_rejects_cross_field_before_http() -> None:
    transport = _FakeTransport([])
    receipt = _writes(transport).ensure_lead_analytics_enum_if_empty(
        lead_id="55",
        field_id=int(AmoCrmAnalyticsFieldId.SOURCE_PRIMARY),
        enum_id=int(AmoCrmAnalyticsBookingMethodEnum.TEYA),
        access_token="token",
    )
    assert receipt.outcome is AmoCrmCrmWriteOutcome.FAILED
    assert receipt.error_code == "AMOCRM_ANALYTICS_ENUM_NOT_ALLOWED_FOR_FIELD"
    assert transport.calls == []


def test_patch_400_is_permanent_manual() -> None:
    field = int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD)
    enum_id = int(AmoCrmAnalyticsBookingMethodEnum.TEYA)
    transport = _FakeTransport(
        [
            _json_response(200, _lead_payload()),
            S2sHttpResponse(status_code=400, headers={}, body=b'{"detail":"bad"}'),
            _json_response(200, _lead_payload()),
        ]
    )
    receipt = _writes(transport).ensure_lead_analytics_enum_if_empty(
        lead_id="55", field_id=field, enum_id=enum_id, access_token="token"
    )
    assert receipt.decision is AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW
    assert receipt.error_code == "AMOCRM_ANALYTICS_PATCH_PERMANENT"
    assert [c.method for c in transport.calls] == ["GET", "PATCH", "GET"]


def test_patch_403_is_permanent_manual() -> None:
    field = int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD)
    enum_id = int(AmoCrmAnalyticsBookingMethodEnum.TEYA)
    transport = _FakeTransport(
        [
            _json_response(200, _lead_payload()),
            S2sHttpResponse(status_code=403, headers={}, body=b""),
            _json_response(200, _lead_payload()),
        ]
    )
    receipt = _writes(transport).ensure_lead_analytics_enum_if_empty(
        lead_id="55", field_id=field, enum_id=enum_id, access_token="token"
    )
    assert receipt.decision is AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW
    assert receipt.error_code == "AMOCRM_ANALYTICS_PATCH_PERMANENT"


def test_persistent_401_is_manual() -> None:
    field = int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD)
    enum_id = int(AmoCrmAnalyticsBookingMethodEnum.TEYA)
    transport = _FakeTransport(
        [S2sHttpResponse(status_code=401, headers={}, body=b"")]
    )
    receipt = _writes(transport).ensure_lead_analytics_enum_if_empty(
        lead_id="55", field_id=field, enum_id=enum_id, access_token="token"
    )
    assert receipt.decision is AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW
    assert receipt.error_code == "AMOCRM_ANALYTICS_UNAUTHORIZED"


@pytest.mark.asyncio
async def test_401_refresh_then_success() -> None:
    field = int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD)
    enum_id = int(AmoCrmAnalyticsBookingMethodEnum.TEYA)
    transport = _FakeTransport(
        [
            S2sHttpResponse(status_code=401, headers={}, body=b""),
            _json_response(200, _lead_payload()),
            _json_response(200, {"_embedded": {"leads": [{"id": 55}]}}),
            _json_response(200, _lead_payload(field_id=field, enum_id=enum_id)),
        ]
    )
    tokens = _TokenRefreshOnce()
    crm = TeyaRequestCrmService(
        identity_lookup=None,  # type: ignore[arg-type]
        deal_discovery=None,  # type: ignore[arg-type]
        writes=_writes(transport),
        tokens=tokens,
        technical_deal_ids=_TechIds(),
    )
    result = await crm.apply_lead_analytics_enum_if_empty(
        deal_id="55",
        field_id=field,
        enum_id=enum_id,
    )
    assert tokens.refreshed == 1
    assert result.outcome is TeyaCrmActionOutcome.READY
    assert result.analytics_decision == "APPLIED"


def test_429_then_verify_empty_retries() -> None:
    field = int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD)
    enum_id = int(AmoCrmAnalyticsBookingMethodEnum.TEYA)
    transport = _FakeTransport(
        [
            _json_response(200, _lead_payload()),
            S2sHttpResponse(status_code=429, headers={}, body=b""),
            _json_response(200, _lead_payload()),
        ]
    )
    receipt = _writes(transport).ensure_lead_analytics_enum_if_empty(
        lead_id="55", field_id=field, enum_id=enum_id, access_token="token"
    )
    assert receipt.decision is AmoCrmAnalyticsApplyDecision.TRANSIENT_RETRY
    assert receipt.error_code == "AMOCRM_ANALYTICS_PATCH_TRANSIENT"


def test_uncertain_patch_verifies_before_retry() -> None:
    field = int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD)
    enum_id = int(AmoCrmAnalyticsBookingMethodEnum.TEYA)
    transport = _FakeTransport(
        [
            _json_response(200, _lead_payload()),
            S2sHttpResponse(status_code=500, headers={}, body=b""),
            _json_response(200, _lead_payload(field_id=field, enum_id=enum_id)),
        ]
    )
    receipt = _writes(transport).ensure_lead_analytics_enum_if_empty(
        lead_id="55", field_id=field, enum_id=enum_id, access_token="token"
    )
    assert receipt.decision is AmoCrmAnalyticsApplyDecision.APPLIED
    assert [c.method for c in transport.calls] == ["GET", "PATCH", "GET"]


def test_uncertain_patch_empty_after_verify_retries() -> None:
    field = int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD)
    enum_id = int(AmoCrmAnalyticsBookingMethodEnum.TEYA)
    transport = _FakeTransport(
        [
            _json_response(200, _lead_payload()),
            S2sHttpResponse(status_code=500, headers={}, body=b""),
            _json_response(200, _lead_payload()),
        ]
    )
    receipt = _writes(transport).ensure_lead_analytics_enum_if_empty(
        lead_id="55", field_id=field, enum_id=enum_id, access_token="token"
    )
    assert receipt.decision is AmoCrmAnalyticsApplyDecision.TRANSIENT_RETRY
    assert receipt.error_code == "AMOCRM_ANALYTICS_PATCH_TRANSIENT"


@pytest.mark.asyncio
async def test_no_source_evidence_skips_write() -> None:
    transport = _FakeTransport([])
    crm = TeyaRequestCrmService(
        identity_lookup=None,  # type: ignore[arg-type]
        deal_discovery=None,  # type: ignore[arg-type]
        writes=_writes(transport),
        tokens=_Token(),
    )
    result = await crm.apply_lead_analytics_enum_if_empty(
        deal_id="55",
        field_id=int(AmoCrmAnalyticsFieldId.SOURCE_PRIMARY),
        enum_id=None,
    )
    assert result.outcome is TeyaCrmActionOutcome.READY
    assert result.analytics_decision == "SKIPPED_NO_EVIDENCE"
    assert transport.calls == []


def test_channel_rejected_by_adapter() -> None:
    transport = _FakeTransport([])
    receipt = _writes(transport).ensure_lead_analytics_enum_if_empty(
        lead_id="55",
        field_id=AMOCRM_ANALYTICS_CHANNEL_FIELD_ID,
        enum_id=851491,
        access_token="token",
    )
    assert receipt.outcome is AmoCrmCrmWriteOutcome.FAILED
    assert receipt.error_code == "AMOCRM_ANALYTICS_CHANNEL_WRITE_FORBIDDEN"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_ambiguous_deal_skips_analytics() -> None:
    transport = _FakeTransport([])
    crm = TeyaRequestCrmService(
        identity_lookup=None,  # type: ignore[arg-type]
        deal_discovery=None,  # type: ignore[arg-type]
        writes=_writes(transport),
        tokens=_Token(),
    )
    result = await crm.apply_lead_analytics_enum_if_empty(
        deal_id=None,
        field_id=int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD),
        enum_id=int(AmoCrmAnalyticsBookingMethodEnum.TEYA),
    )
    assert result.outcome is TeyaCrmActionOutcome.READY
    assert result.error_code == "ANALYTICS_SKIPPED_NO_DEAL"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_technical_deal_forbidden_for_analytics() -> None:
    transport = _FakeTransport([])
    crm = TeyaRequestCrmService(
        identity_lookup=None,  # type: ignore[arg-type]
        deal_discovery=None,  # type: ignore[arg-type]
        writes=_writes(transport),
        tokens=_Token(),
        technical_deal_ids=_TechIds(("99",)),
    )
    result = await crm.apply_lead_analytics_enum_if_empty(
        deal_id="99",
        field_id=int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD),
        enum_id=int(AmoCrmAnalyticsBookingMethodEnum.TEYA),
    )
    assert result.outcome is TeyaCrmActionOutcome.FAIL_CLOSED
    assert result.error_code == "ANALYTICS_TECHNICAL_DEAL_FORBIDDEN"
    assert transport.calls == []


def test_receipt_repr_has_no_pii() -> None:
    receipt = AmoCrmAnalyticsApplyReceipt(
        outcome=AmoCrmCrmWriteOutcome.VERIFIED,
        decision=AmoCrmAnalyticsApplyDecision.APPLIED,
        field_id=1321305,
        enum_id=851491,
        lead_id="55",
    )
    text = repr(receipt)
    assert "phone" not in text.lower()
    assert "+" not in text
