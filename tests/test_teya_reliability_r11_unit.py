"""R1.1 unit tests: CRM read-only reconciliation + OAuth MANUAL."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.amocrm_crm_writes_http import AmoCrmCrmWritesHttpClient
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

_TEST_PIPELINE = 1001
_TEST_STATUS = 2002
_TEST_MANAGER = 3003
_TEST_TASK_TYPE = 4004
_NOTE = "type=MANAGER_REQUEST; status=NEW"
_TASK = "Обработать заявку из онлайн-записи"


@dataclass
class _FakeTransport:
    responses: list[S2sHttpResponse | BaseException]
    calls: list[S2sHttpRequest] = field(default_factory=list)

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        if not self.responses:
            raise AssertionError("unexpected HTTP call")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _json_response(status: int, payload: object) -> S2sHttpResponse:
    return S2sHttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
    )


def _writes(transport: _FakeTransport) -> AmoCrmCrmWritesHttpClient:
    return AmoCrmCrmWritesHttpClient(
        AmoCrmCrmRestConfig(
            enabled=True,
            client_id="crm-client-id-001",
            client_secret="crm-secret-xxxxxxxxxx",
            api_base_url="https://example.amocrm.ru",
            redirect_uri="https://example.com/oauth",
            connection_scope="default",
        ),
        transport=transport,
        pipeline_id=_TEST_PIPELINE,
        open_status_id=_TEST_STATUS,
        manager_id=_TEST_MANAGER,
        task_type_id=_TEST_TASK_TYPE,
    )


@dataclass
class _Identity:
    result: AmoCrmIdentityLookupResult

    async def lookup_by_phone(self, *, phone_e164: str) -> AmoCrmIdentityLookupResult:
        return self.result


@dataclass
class _Deals:
    result: AmoCrmDealDiscoveryResult

    async def discover_deal_candidates(
        self, *, contact_id: str, known_technical_deal_ids: tuple[str, ...] = ()
    ):
        return self.result


class _Tokens:
    async def access_token(self) -> str | None:
        return "token"

    async def refresh_access_token(self, *, rejected_access_token: str) -> str | None:
        del rejected_access_token
        return None


def _crm(
    *,
    identity: AmoCrmIdentityLookupResult,
    deals: AmoCrmDealDiscoveryResult,
    transport: _FakeTransport | None = None,
) -> TeyaRequestCrmService:
    return TeyaRequestCrmService(
        identity_lookup=_Identity(identity),
        deal_discovery=_Deals(deals),
        writes=_writes(transport or _FakeTransport([])),
        tokens=_Tokens(),
    )


@pytest.mark.asyncio
async def test_recon_finds_exact_contact_and_deal() -> None:
    crm = _crm(
        identity=AmoCrmIdentityLookupResult(
            outcome=AmoCrmIdentityLookupOutcome.FOUND, contact_id="101"
        ),
        deals=AmoCrmDealDiscoveryResult(
            outcome=AmoCrmDealDiscoveryOutcome.FOUND,
            contact_id="101",
            business_active_lead_ids=("201",),
        ),
    )
    result = await crm.reconcile_readonly(phone_e164="+79001234567")
    assert result.outcome is TeyaCrmActionOutcome.READY
    assert result.contact_id == "101"
    assert result.deal_id == "201"


@pytest.mark.asyncio
async def test_recon_note_and_task_fingerprint_reuse_no_post() -> None:
    transport = _FakeTransport(
        [
            _json_response(
                200,
                {
                    "_embedded": {
                        "notes": [{"id": 9, "params": {"text": _NOTE}}]
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
                                "text": _TASK,
                                "task_type_id": _TEST_TASK_TYPE,
                                "responsible_user_id": _TEST_MANAGER,
                            }
                        ]
                    }
                },
            ),
        ]
    )
    crm = _crm(
        identity=AmoCrmIdentityLookupResult(
            outcome=AmoCrmIdentityLookupOutcome.FOUND, contact_id="101"
        ),
        deals=AmoCrmDealDiscoveryResult(
            outcome=AmoCrmDealDiscoveryOutcome.FOUND,
            contact_id="101",
            business_active_lead_ids=("201",),
        ),
        transport=transport,
    )
    result = await crm.reconcile_readonly(
        phone_e164="+79001234567", note_text=_NOTE, task_text=_TASK
    )
    assert result.outcome is TeyaCrmActionOutcome.READY
    assert result.note_id == "9"
    assert result.task_id == "77"
    assert all(c.method == "GET" for c in transport.calls)
    assert not any("/notes" in c.url and c.method == "POST" for c in transport.calls)


@pytest.mark.asyncio
async def test_recon_ambiguous_manual_zero_writes() -> None:
    transport = _FakeTransport([])
    crm = _crm(
        identity=AmoCrmIdentityLookupResult(
            outcome=AmoCrmIdentityLookupOutcome.AMBIGUOUS,
            contact_ids=("1", "2"),
        ),
        deals=AmoCrmDealDiscoveryResult(
            outcome=AmoCrmDealDiscoveryOutcome.NOT_FOUND
        ),
        transport=transport,
    )
    result = await crm.reconcile_readonly(phone_e164="+79001234567")
    assert result.outcome is TeyaCrmActionOutcome.MANUAL_REVIEW
    assert transport.calls == []


@pytest.mark.asyncio
async def test_recon_none_zero_writes() -> None:
    transport = _FakeTransport([])
    crm = _crm(
        identity=AmoCrmIdentityLookupResult(
            outcome=AmoCrmIdentityLookupOutcome.NOT_FOUND
        ),
        deals=AmoCrmDealDiscoveryResult(
            outcome=AmoCrmDealDiscoveryOutcome.NOT_FOUND
        ),
        transport=transport,
    )
    result = await crm.reconcile_readonly(phone_e164="+79001234567")
    assert result.outcome is TeyaCrmActionOutcome.NONE
    assert transport.calls == []


@pytest.mark.asyncio
async def test_recon_idempotent_repeat() -> None:
    identity = AmoCrmIdentityLookupResult(
        outcome=AmoCrmIdentityLookupOutcome.FOUND, contact_id="101"
    )
    deals = AmoCrmDealDiscoveryResult(
        outcome=AmoCrmDealDiscoveryOutcome.FOUND,
        contact_id="101",
        business_active_lead_ids=("201",),
    )
    first = await _crm(identity=identity, deals=deals).reconcile_readonly(
        phone_e164="+79001234567"
    )
    second = await _crm(identity=identity, deals=deals).reconcile_readonly(
        phone_e164="+79001234567"
    )
    assert first == second
    assert first.outcome is TeyaCrmActionOutcome.READY


@pytest.mark.asyncio
async def test_oauth_missing_manual_on_ensure() -> None:
    class _NoToken:
        async def access_token(self) -> str | None:
            return None

    crm = TeyaRequestCrmService(
        identity_lookup=_Identity(
            AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.NOT_FOUND
            )
        ),
        deal_discovery=_Deals(
            AmoCrmDealDiscoveryResult(outcome=AmoCrmDealDiscoveryOutcome.NOT_FOUND)
        ),
        writes=_writes(_FakeTransport([])),
        tokens=_NoToken(),
    )
    result = await crm.ensure_contact_and_deal(
        phone_e164="+79001234567", client_name="X"
    )
    assert result.outcome is TeyaCrmActionOutcome.MANUAL_REVIEW
    assert result.error_code == "AMOCRM_CRM_OAUTH_NOT_FOUND"
