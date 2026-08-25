"""Unit tests for Teya CRM action layer (fake transports, no live amoCRM)."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.amocrm_crm_writes_http import (
    AmoCrmCrmWriteOutcome,
    AmoCrmCrmWritesHttpClient,
)
from app.core.amocrm_deal_discovery import (
    AmoCrmDealDiscoveryOutcome,
    AmoCrmDealDiscoveryResult,
)
from app.core.amocrm_identity_lookup import (
    AmoCrmIdentityLookupOutcome,
    AmoCrmIdentityLookupResult,
)
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.services.teya_request_crm import (
    TeyaCrmActionOutcome,
    TeyaRequestCrmService,
)

# Test-only fixture IDs (not production account constants).
_TEST_PIPELINE = 1001
_TEST_STATUS = 2002
_TEST_MANAGER = 3003
_TEST_TASK_TYPE = 4004


def _writes(transport: _FakeTransport) -> AmoCrmCrmWritesHttpClient:
    return AmoCrmCrmWritesHttpClient(
        _config(),
        transport=transport,
        pipeline_id=_TEST_PIPELINE,
        open_status_id=_TEST_STATUS,
        manager_id=_TEST_MANAGER,
        task_type_id=_TEST_TASK_TYPE,
    )


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


class _Identity:
    def __init__(self, result: AmoCrmIdentityLookupResult) -> None:
        self._result = result

    async def lookup_by_phone(self, *, phone_e164: str) -> AmoCrmIdentityLookupResult:
        assert phone_e164.startswith("+")
        return self._result


class _Deals:
    def __init__(self, result: AmoCrmDealDiscoveryResult) -> None:
        self._result = result

    async def discover_deal_candidates(
        self, *, contact_id: str
    ) -> AmoCrmDealDiscoveryResult:
        return self._result


class _Tokens:
    async def access_token(self) -> str | None:
        return "token"


@pytest.mark.asyncio
async def test_existing_unique_contact_reuses_active_deal() -> None:
    writes = _writes(_FakeTransport([]))
    crm = TeyaRequestCrmService(
        identity_lookup=_Identity(
            AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.FOUND,
                contact_id="100",
            )
        ),
        deal_discovery=_Deals(
            AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.FOUND,
                contact_id="100",
                business_active_lead_ids=("200",),
            )
        ),
        writes=writes,
        tokens=_Tokens(),
    )
    result = await crm.ensure_contact_and_deal(
        phone_e164="+79001234567", client_name="Test"
    )
    assert result.outcome is TeyaCrmActionOutcome.READY
    assert result.contact_id == "100"
    assert result.deal_id == "200"
    assert writes.http_calls == []


@pytest.mark.asyncio
async def test_ambiguous_identity_manual_review_no_creates() -> None:
    transport = _FakeTransport([])
    writes = _writes(transport)
    crm = TeyaRequestCrmService(
        identity_lookup=_Identity(
            AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.AMBIGUOUS,
                contact_ids=("1", "2"),
            )
        ),
        deal_discovery=_Deals(
            AmoCrmDealDiscoveryResult(outcome=AmoCrmDealDiscoveryOutcome.NOT_FOUND)
        ),
        writes=writes,
        tokens=_Tokens(),
    )
    result = await crm.ensure_contact_and_deal(
        phone_e164="+79001234567", client_name="Test"
    )
    assert result.outcome is TeyaCrmActionOutcome.MANUAL_REVIEW
    assert result.error_code == "IDENTITY_AMBIGUOUS"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_new_contact_and_lead_with_postcheck() -> None:
    transport = _FakeTransport(
        [
            _json_response(200, {"_embedded": {"contacts": [{"id": 101}]}}),
            _json_response(200, {"id": 101}),
            _json_response(200, {"_embedded": {"leads": [{"id": 201}]}}),
            _json_response(
                200,
                {
                    "id": 201,
                    "pipeline_id": _TEST_PIPELINE,
                    "status_id": _TEST_STATUS,
                },
            ),
        ]
    )
    writes = _writes(transport)
    crm = TeyaRequestCrmService(
        identity_lookup=_Identity(
            AmoCrmIdentityLookupResult(outcome=AmoCrmIdentityLookupOutcome.NOT_FOUND)
        ),
        deal_discovery=_Deals(
            AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.NOT_FOUND,
                contact_id="101",
            )
        ),
        writes=writes,
        tokens=_Tokens(),
    )
    result = await crm.ensure_contact_and_deal(
        phone_e164="+79001234567", client_name="Test"
    )
    assert result.outcome is TeyaCrmActionOutcome.READY
    assert result.contact_id == "101"
    assert result.deal_id == "201"
    assert "POST_CONTACT" in writes.http_calls
    assert "GET_CONTACT_POSTCHECK" in writes.http_calls
    assert "POST_LEAD" in writes.http_calls
    assert "GET_LEAD_POSTCHECK" in writes.http_calls


@pytest.mark.asyncio
async def test_reanimation_of_status_143() -> None:
    transport = _FakeTransport(
        [
            _json_response(
                200,
                {"id": 301, "pipeline_id": 1, "status_id": 143},
            ),
            _json_response(200, {"id": 301}),
            _json_response(
                200,
                {
                    "id": 301,
                    "pipeline_id": _TEST_PIPELINE,
                    "status_id": _TEST_STATUS,
                },
            ),
        ]
    )
    writes = _writes(transport)
    crm = TeyaRequestCrmService(
        identity_lookup=_Identity(
            AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.FOUND,
                contact_id="100",
            )
        ),
        deal_discovery=_Deals(
            AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.FOUND,
                contact_id="100",
                reanimation_candidate_lead_ids=("301",),
            )
        ),
        writes=writes,
        tokens=_Tokens(),
    )
    result = await crm.ensure_contact_and_deal(
        phone_e164="+79001234567", client_name="Test"
    )
    assert result.outcome is TeyaCrmActionOutcome.READY
    assert result.deal_id == "301"
    assert "PATCH_LEAD_REANIMATE" in writes.http_calls


@pytest.mark.asyncio
async def test_no_duplicate_active_deal() -> None:
    writes = _writes(_FakeTransport([]))
    crm = TeyaRequestCrmService(
        identity_lookup=_Identity(
            AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.FOUND,
                contact_id="100",
            )
        ),
        deal_discovery=_Deals(
            AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.FOUND,
                contact_id="100",
                business_active_lead_ids=("200", "201"),
            )
        ),
        writes=writes,
        tokens=_Tokens(),
    )
    result = await crm.ensure_contact_and_deal(
        phone_e164="+79001234567", client_name="Test"
    )
    assert result.outcome is TeyaCrmActionOutcome.MANUAL_REVIEW
    assert result.error_code == "ACTIVE_DEAL_AMBIGUOUS"


@pytest.mark.asyncio
async def test_note_and_task_postcheck_and_dedupe() -> None:
    note_text = "type=MANAGER_REQUEST"
    transport = _FakeTransport(
        [
            _json_response(200, {"_embedded": {"notes": []}}),
            _json_response(200, {"_embedded": {"notes": [{"id": 9}]}}),
            _json_response(200, {"id": 9}),
            _json_response(200, {"_embedded": {"tasks": []}}),
            _json_response(200, {"_embedded": {"tasks": [{"id": 77}]}}),
            _json_response(
                200,
                {
                    "_embedded": {
                        "tasks": [
                            {
                                "id": 77,
                                "text": "Обработать заявку из онлайн-записи",
                                "task_type_id": _TEST_TASK_TYPE,
                                "responsible_user_id": _TEST_MANAGER,
                            }
                        ]
                    }
                },
            ),
        ]
    )
    writes = _writes(transport)
    crm = TeyaRequestCrmService(
        identity_lookup=_Identity(
            AmoCrmIdentityLookupResult(outcome=AmoCrmIdentityLookupOutcome.NOT_FOUND)
        ),
        deal_discovery=_Deals(
            AmoCrmDealDiscoveryResult(outcome=AmoCrmDealDiscoveryOutcome.NOT_FOUND)
        ),
        writes=writes,
        tokens=_Tokens(),
    )
    result = await crm.attach_note_and_task(
        deal_id="200",
        note_text=note_text,
    )
    assert result.outcome is TeyaCrmActionOutcome.READY
    assert result.note_id == "9"
    assert result.task_id == "77"

    # Second call reuses fingerprint — list only, no POST.
    transport2 = _FakeTransport(
        [
            _json_response(
                200,
                {
                    "_embedded": {
                        "notes": [
                            {"id": 10, "params": {"text": note_text}}
                        ]
                    }
                },
            ),
            _json_response(
                200,
                {
                    "_embedded": {
                        "tasks": [
                            {
                                "id": 77,
                                "text": "Обработать заявку из онлайн-записи",
                                "task_type_id": _TEST_TASK_TYPE,
                                "responsible_user_id": _TEST_MANAGER,
                            }
                        ]
                    }
                },
            ),
        ]
    )
    writes2 = _writes(transport2)
    crm2 = TeyaRequestCrmService(
        identity_lookup=_Identity(
            AmoCrmIdentityLookupResult(outcome=AmoCrmIdentityLookupOutcome.NOT_FOUND)
        ),
        deal_discovery=_Deals(
            AmoCrmDealDiscoveryResult(outcome=AmoCrmDealDiscoveryOutcome.NOT_FOUND)
        ),
        writes=writes2,
        tokens=_Tokens(),
    )
    reused = await crm2.attach_note_and_task(
        deal_id="200",
        note_text=note_text,
    )
    assert reused.outcome is TeyaCrmActionOutcome.READY
    assert reused.note_id == "10"
    assert reused.task_id == "77"
    assert "POST_NOTE" not in writes2.http_calls
    assert "POST_TASK" not in writes2.http_calls
