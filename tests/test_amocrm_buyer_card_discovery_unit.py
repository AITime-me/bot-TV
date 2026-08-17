"""IR-3 read-only amoCRM Buyer Card discovery unit coverage."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.core.amocrm_buyer_card_discovery import (
    AmoCrmBuyerCardDiscoveryOutcome,
    AmoCrmBuyerCardDiscoveryResult,
    buyer_card_reconcile_candidates_from_discovery,
)
from app.core.amocrm_crm_buyer_card_http import (
    AmoCrmBuyerCardHttpClient,
    parse_contact_with_leads_body,
    parse_lead_inspect_body,
)
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.amocrm_crm_rest_http import (
    AmoCrmCrmRestOutcome,
    AmoCrmCrmTokenRefreshResult,
)
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.services.amocrm_buyer_card_discovery import (
    MAX_LINKED_LEADS_PER_DISCOVERY,
    AmoCrmBuyerCardDiscoveryService,
)
from tests.docker_runtime_allowlist import (
    IR3_DOCKER_RUNTIME_PATHS,
    assert_canonical_docker_runtime_allowlist,
    is_included_in_docker_build_context,
)

_REPO = Path(__file__).resolve().parents[1]
_CONTACT_ID = 42
_PHONE = "+79001234567"
_NAME = "Secret Person"
_EMAIL = "hidden@example.com"


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[S2sHttpRequest] = []
        self.responses: list[S2sHttpResponse] = []

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        if not self.responses:
            raise AssertionError("no fake response queued")
        return self.responses.pop(0)


class _StubOauth:
    def __init__(self, *, token_box: dict[str, str], outcome: AmoCrmCrmRestOutcome) -> None:
        self.token_box = token_box
        self.outcome = outcome
        self.refresh_count = 0

    async def refresh_tokens(self) -> AmoCrmCrmTokenRefreshResult:
        self.refresh_count += 1
        if self.outcome is AmoCrmCrmRestOutcome.SUCCESS:
            self.token_box["access"] = "access-after-refresh"
        return AmoCrmCrmTokenRefreshResult(outcome=self.outcome)


def _enabled_config() -> AmoCrmCrmRestConfig:
    return AmoCrmCrmRestConfig(
        enabled=True,
        client_id="crm-client-id-001",
        client_secret="crm-secret-xxxxxxxxxx",
        api_base_url="https://example.amocrm.ru",
        redirect_uri="https://example.com/oauth",
        connection_scope="default",
    )


def _json_response(status: int, payload: object) -> S2sHttpResponse:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return S2sHttpResponse(status_code=status, headers={}, body=body)


def _contact_payload(
    contact_id: int,
    lead_ids: list[int],
    *,
    name: str = _NAME,
) -> dict:
    return {
        "id": contact_id,
        "name": name,
        "custom_fields_values": [
            {"field_code": "EMAIL", "values": [{"value": _EMAIL}]},
            {"field_code": "PHONE", "values": [{"value": _PHONE}]},
        ],
        "_embedded": {"leads": [{"id": lid, "name": f"Lead {lid}"} for lid in lead_ids]},
    }


def _lead_payload(
    lead_id: int,
    contact_ids: list[int],
    *,
    closed_at: int | None = None,
    is_deleted: bool = False,
    name: str = "Buyer Deal Name",
) -> dict:
    return {
        "id": lead_id,
        "name": name,
        "is_deleted": is_deleted,
        "closed_at": closed_at,
        "tags": [{"name": "vip"}],
        "_embedded": {"contacts": [{"id": cid} for cid in contact_ids]},
    }


async def _make_service(
    transport: _FakeTransport,
    *,
    token: str = "access-1",
    oauth: _StubOauth | None = None,
) -> AmoCrmBuyerCardDiscoveryService:
    token_box = {"access": token}
    if oauth is None:
        oauth = _StubOauth(token_box=token_box, outcome=AmoCrmCrmRestOutcome.SUCCESS)

    async def _resolve() -> str | None:
        return token_box["access"]

    return AmoCrmBuyerCardDiscoveryService(
        session_factory=object(),  # type: ignore[arg-type]
        config=_enabled_config(),
        transport=transport,
        oauth=oauth,  # type: ignore[arg-type]
        resolve_access_token=_resolve,
    )


def _queue_contact_and_leads(
    transport: _FakeTransport,
    *,
    contact_id: int,
    leads: list[dict],
) -> None:
    lead_ids = [int(item["id"]) for item in leads]
    transport.responses.append(_json_response(200, _contact_payload(contact_id, lead_ids)))
    by_id = {int(item["id"]): item for item in leads}
    for lid in sorted(set(lead_ids)):
        transport.responses.append(_json_response(200, by_id[lid]))


@pytest.mark.asyncio
async def test_contact_204_not_found() -> None:
    transport = _FakeTransport()
    transport.responses.append(S2sHttpResponse(status_code=204, headers={}, body=b""))
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND
    assert result.error_code == "AMOCRM_CRM_HTTP_204"
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_contact_404_not_found() -> None:
    transport = _FakeTransport()
    transport.responses.append(S2sHttpResponse(status_code=404, headers={}, body=b""))
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND
    assert result.error_code == "AMOCRM_CRM_HTTP_404"


@pytest.mark.asyncio
async def test_contact_id_mismatch_permanent() -> None:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(99, [1])))
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id="42")
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_CONTACT_ID_MISMATCH"
    assert result.eligible_lead_ids == ()
    text = repr(result)
    assert _NAME not in text
    assert _PHONE not in text


@pytest.mark.asyncio
async def test_contact_without_leads_not_found() -> None:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [])))
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND
    assert result.contact_id == str(_CONTACT_ID)
    assert result.eligible_lead_ids == ()
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_one_open_linked_lead_found_candidate() -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[_lead_payload(7, [_CONTACT_ID])],
    )
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE
    assert result.eligible_lead_ids == ("7",)
    assert transport.calls[0].method == "GET"
    assert "with=leads" in transport.calls[0].url
    assert "with=contacts" in transport.calls[1].url
    mapped = buyer_card_reconcile_candidates_from_discovery(result)
    assert mapped is not None
    assert mapped.candidate_buyer_card_ids == ("7",)
    assert mapped.candidate_technical_deal_ids == ()


@pytest.mark.asyncio
async def test_two_open_linked_leads_ambiguous() -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[
            _lead_payload(10, [_CONTACT_ID]),
            _lead_payload(20, [_CONTACT_ID]),
        ],
    )
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.AMBIGUOUS
    assert result.eligible_lead_ids == ("10", "20")
    mapped = buyer_card_reconcile_candidates_from_discovery(result)
    assert mapped is not None
    assert mapped.candidate_buyer_card_ids == ("10", "20")


@pytest.mark.asyncio
async def test_technical_id_excluded() -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[_lead_payload(9, [_CONTACT_ID])],
    )
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(
        contact_id=str(_CONTACT_ID),
        known_technical_deal_ids=("9",),
    )
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND
    assert result.eligible_lead_ids == ()
    assert result.known_technical_deal_ids == ("9",)
    mapped = buyer_card_reconcile_candidates_from_discovery(result)
    assert mapped is not None
    assert mapped.candidate_buyer_card_ids == ()
    assert mapped.candidate_technical_deal_ids == ("9",)


@pytest.mark.asyncio
async def test_closed_lead_excluded() -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[_lead_payload(8, [_CONTACT_ID], closed_at=1_700_000_000)],
    )
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND


@pytest.mark.asyncio
async def test_deleted_lead_excluded() -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[_lead_payload(8, [_CONTACT_ID], is_deleted=True)],
    )
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND


@pytest.mark.asyncio
async def test_closed_technical_and_one_open_exactly_one_candidate() -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[
            _lead_payload(1, [_CONTACT_ID], closed_at=1_700_000_000),
            _lead_payload(2, [_CONTACT_ID]),
            _lead_payload(3, [_CONTACT_ID]),
        ],
    )
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(
        contact_id=str(_CONTACT_ID),
        known_technical_deal_ids=("2",),
    )
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE
    assert result.eligible_lead_ids == ("3",)
    mapped = buyer_card_reconcile_candidates_from_discovery(result)
    assert mapped is not None
    assert mapped.candidate_buyer_card_ids == ("3",)
    assert mapped.candidate_technical_deal_ids == ("2",)


@pytest.mark.asyncio
async def test_lead_response_id_mismatch() -> None:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [7])))
    transport.responses.append(_json_response(200, _lead_payload(99, [_CONTACT_ID])))
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_LEAD_ID_MISMATCH"
    assert result.eligible_lead_ids == ()


@pytest.mark.asyncio
async def test_lead_no_longer_linked_to_contact_incomplete() -> None:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [7])))
    transport.responses.append(_json_response(200, _lead_payload(7, [999])))
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.INCOMPLETE
    assert result.error_code == "AMOCRM_BUYER_CARD_LEAD_CONTACT_UNLINKED"
    assert buyer_card_reconcile_candidates_from_discovery(result) is None


@pytest.mark.asyncio
async def test_malformed_contact_permanent() -> None:
    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(status_code=200, headers={}, body=b"{not-json")
    )
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id="1")
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_CONTACT_BODY_INVALID"


@pytest.mark.asyncio
async def test_malformed_lead_permanent() -> None:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [7])))
    transport.responses.append(
        S2sHttpResponse(status_code=200, headers={}, body=b"{not-json")
    )
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_LEAD_BODY_INVALID"
    assert result.outcome is not AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE
    assert result.outcome is not AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND


async def _discover_one_lead(payload: dict) -> AmoCrmBuyerCardDiscoveryResult:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [7])))
    transport.responses.append(_json_response(200, payload))
    service = await _make_service(transport)
    return await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))


def _assert_lead_body_invalid(result: AmoCrmBuyerCardDiscoveryResult) -> None:
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_LEAD_BODY_INVALID"
    assert result.outcome is not AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE
    assert result.outcome is not AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND
    assert result.eligible_lead_ids == ()


@pytest.mark.asyncio
async def test_missing_is_deleted_permanent() -> None:
    payload = _lead_payload(7, [_CONTACT_ID])
    del payload["is_deleted"]
    result = await _discover_one_lead(payload)
    _assert_lead_body_invalid(result)


@pytest.mark.asyncio
async def test_missing_closed_at_permanent() -> None:
    payload = _lead_payload(7, [_CONTACT_ID])
    del payload["closed_at"]
    result = await _discover_one_lead(payload)
    _assert_lead_body_invalid(result)


@pytest.mark.asyncio
async def test_invalid_is_deleted_type_permanent() -> None:
    payload = _lead_payload(7, [_CONTACT_ID])
    payload["is_deleted"] = 0
    result = await _discover_one_lead(payload)
    _assert_lead_body_invalid(result)


@pytest.mark.parametrize("bad_closed_at", ["1700000000", True, 1.5, []])
@pytest.mark.asyncio
async def test_invalid_closed_at_type_permanent(bad_closed_at: object) -> None:
    payload = _lead_payload(7, [_CONTACT_ID])
    payload["closed_at"] = bad_closed_at
    result = await _discover_one_lead(payload)
    _assert_lead_body_invalid(result)


@pytest.mark.asyncio
async def test_closed_at_null_and_is_deleted_false_eligible() -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[_lead_payload(7, [_CONTACT_ID], closed_at=None, is_deleted=False)],
    )
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE
    assert result.eligible_lead_ids == ("7",)


@pytest.mark.asyncio
async def test_closed_at_int_excluded() -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[_lead_payload(8, [_CONTACT_ID], closed_at=1_700_000_000, is_deleted=False)],
    )
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND


@pytest.mark.asyncio
async def test_is_deleted_true_excluded() -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[_lead_payload(8, [_CONTACT_ID], closed_at=None, is_deleted=True)],
    )
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND


@pytest.mark.asyncio
async def test_missing_embedded_leads_malformed() -> None:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, {"id": _CONTACT_ID, "name": _NAME}))
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR
    assert result.outcome is not AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND


@pytest.mark.asyncio
async def test_transport_and_5xx_transient() -> None:
    from app.core.s2s_http_transport import S2sHttpTransportError

    class _Boom(_FakeTransport):
        def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
            self.calls.append(req)
            raise S2sHttpTransportError("TRANSPORT_ERROR")

    service = await _make_service(_Boom())
    result = await service.discover_buyer_card_candidates(contact_id="1")
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.TRANSIENT_ERROR

    transport = _FakeTransport()
    transport.responses.append(S2sHttpResponse(status_code=503, headers={}, body=b""))
    service2 = await _make_service(transport)
    result2 = await service2.discover_buyer_card_candidates(contact_id="1")
    assert result2.outcome is AmoCrmBuyerCardDiscoveryOutcome.TRANSIENT_ERROR


@pytest.mark.asyncio
async def test_lead_5xx_does_not_found_or_not_found() -> None:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [7])))
    transport.responses.append(S2sHttpResponse(status_code=503, headers={}, body=b""))
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.TRANSIENT_ERROR
    assert result.eligible_lead_ids == ()


@pytest.mark.parametrize("status", [402, 403])
@pytest.mark.asyncio
async def test_402_403_permanent(status: int) -> None:
    transport = _FakeTransport()
    transport.responses.append(S2sHttpResponse(status_code=status, headers={}, body=b""))
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id="1")
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR
    assert result.error_code == f"AMOCRM_CRM_HTTP_{status}"


@pytest.mark.asyncio
async def test_401_one_refresh_then_continue() -> None:
    transport = _FakeTransport()
    token_box = {"access": "access-old"}
    oauth = _StubOauth(token_box=token_box, outcome=AmoCrmCrmRestOutcome.SUCCESS)
    transport.responses.extend(
        [
            S2sHttpResponse(status_code=401, headers={}, body=b""),
            _json_response(200, _contact_payload(_CONTACT_ID, [7])),
            _json_response(200, _lead_payload(7, [_CONTACT_ID])),
        ]
    )

    async def _resolve() -> str | None:
        return token_box["access"]

    service = AmoCrmBuyerCardDiscoveryService(
        session_factory=object(),  # type: ignore[arg-type]
        config=_enabled_config(),
        transport=transport,
        oauth=oauth,  # type: ignore[arg-type]
        resolve_access_token=_resolve,
    )
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE
    assert oauth.refresh_count == 1
    assert transport.calls[1].headers["Authorization"] == "Bearer access-after-refresh"


@pytest.mark.asyncio
async def test_later_401_does_not_second_refresh() -> None:
    transport = _FakeTransport()
    token_box = {"access": "access-old"}
    oauth = _StubOauth(token_box=token_box, outcome=AmoCrmCrmRestOutcome.SUCCESS)
    transport.responses.extend(
        [
            S2sHttpResponse(status_code=401, headers={}, body=b""),
            _json_response(200, _contact_payload(_CONTACT_ID, [7, 8])),
            _json_response(200, _lead_payload(7, [_CONTACT_ID])),
            S2sHttpResponse(status_code=401, headers={}, body=b""),
        ]
    )

    async def _resolve() -> str | None:
        return token_box["access"]

    service = AmoCrmBuyerCardDiscoveryService(
        session_factory=object(),  # type: ignore[arg-type]
        config=_enabled_config(),
        transport=transport,
        oauth=oauth,  # type: ignore[arg-type]
        resolve_access_token=_resolve,
    )
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR
    assert result.outcome is not AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE
    assert oauth.refresh_count == 1


@pytest.mark.asyncio
async def test_proactive_refresh_then_later_401_no_second_refresh() -> None:
    transport = _FakeTransport()
    token_box = {"access": "access-old"}
    oauth = _StubOauth(token_box=token_box, outcome=AmoCrmCrmRestOutcome.SUCCESS)
    transport.responses.append(S2sHttpResponse(status_code=401, headers={}, body=b""))

    async def _resolve() -> str | None:
        return token_box["access"]

    class _Proactive(AmoCrmBuyerCardDiscoveryService):
        async def _resolve_access_token(self, budget):  # type: ignore[override]
            refreshed = await self._try_remote_refresh(budget)
            assert refreshed is True
            return await self._load_access_token()

    service = _Proactive(
        session_factory=object(),  # type: ignore[arg-type]
        config=_enabled_config(),
        transport=transport,
        oauth=oauth,  # type: ignore[arg-type]
        resolve_access_token=_resolve,
    )
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR
    assert oauth.refresh_count == 1
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_over_max_linked_leads_incomplete() -> None:
    transport = _FakeTransport()
    too_many = list(range(1, MAX_LINKED_LEADS_PER_DISCOVERY + 2))
    transport.responses.append(
        _json_response(200, _contact_payload(_CONTACT_ID, too_many))
    )
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.INCOMPLETE
    assert result.error_code == "AMOCRM_BUYER_CARD_LINKED_LEADS_LIMIT"
    assert result.eligible_lead_ids == ()
    assert len(transport.calls) == 1
    assert buyer_card_reconcile_candidates_from_discovery(result) is None


@pytest.mark.asyncio
async def test_invalid_contact_id_no_http() -> None:
    transport = _FakeTransport()
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id="abc")
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.INVALID_INPUT
    assert transport.calls == []


@pytest.mark.asyncio
async def test_invalid_technical_id_no_http() -> None:
    transport = _FakeTransport()
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(
        contact_id="1",
        known_technical_deal_ids=("not-id",),
    )
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.INVALID_INPUT
    assert transport.calls == []


@pytest.mark.asyncio
async def test_disabled_zero_http() -> None:
    transport = _FakeTransport()
    service = AmoCrmBuyerCardDiscoveryService(
        session_factory=object(),  # type: ignore[arg-type]
        config=AmoCrmCrmRestConfig(enabled=False),
        transport=transport,
    )
    result = await service.discover_buyer_card_candidates(contact_id="1")
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.DISABLED
    assert transport.calls == []


@pytest.mark.asyncio
async def test_duplicate_linked_lead_ids_deduped() -> None:
    transport = _FakeTransport()
    payload = _contact_payload(_CONTACT_ID, [7, 7, 7])
    transport.responses.append(_json_response(200, payload))
    transport.responses.append(_json_response(200, _lead_payload(7, [_CONTACT_ID])))
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE
    assert result.eligible_lead_ids == ("7",)
    assert len(transport.calls) == 2


def test_result_repr_no_pii() -> None:
    result = AmoCrmBuyerCardDiscoveryResult(
        outcome=AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE,
        contact_id="42",
        eligible_lead_ids=("7",),
        known_technical_deal_ids=("9",),
    )
    text = repr(result)
    assert "42" in text
    assert "7" in text
    assert _NAME not in text
    assert _PHONE not in text
    assert _EMAIL not in text
    parsed = parse_contact_with_leads_body(
        json.dumps(_contact_payload(42, [7], name=_NAME)).encode()
    )
    assert parsed is not None
    assert _NAME not in repr(parsed)
    assert _PHONE not in repr(parsed)
    lead = parse_lead_inspect_body(json.dumps(_lead_payload(7, [42])).encode())
    assert lead is not None
    assert "Buyer Deal Name" not in repr(lead)
    assert "vip" not in repr(lead)


def test_http_client_has_no_write_methods() -> None:
    client = AmoCrmBuyerCardHttpClient(_enabled_config(), transport=_FakeTransport())
    names = dir(client)
    assert "create_lead" not in names
    assert "link_contact" not in names
    assert not any(name.startswith("post") for name in names)


def test_no_entity_mutating_http_in_ir3_modules() -> None:
    paths = [
        _REPO / "app/core/amocrm_crm_buyer_card_http.py",
        _REPO / "app/services/amocrm_buyer_card_discovery.py",
        _REPO / "app/core/amocrm_buyer_card_discovery.py",
    ]
    forbidden = {"POST", "PATCH", "PUT", "DELETE"}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and type(node.value) is str:
                if node.value in forbidden:
                    raise AssertionError(f"{path.name} contains {node.value!r}")


def test_ir3_not_wired_into_webhook_worker_or_glue() -> None:
    for rel in (
        "app/amocrm_chat_webhook.py",
        "app/worker.py",
        "app/services/worker_runtime.py",
        "app/services/inbound.py",
        "app/services/identity_glue.py",
        "app/services/identity_resolution.py",
        "app/services/amocrm_technical_deal.py",
    ):
        source = (_REPO / rel).read_text(encoding="utf-8")
        assert "amocrm_buyer_card_discovery" not in source
        assert "AmoCrmBuyerCardDiscoveryService" not in source
        assert "discover_buyer_card_candidates" not in source


def test_ir3_does_not_import_write_capable_leads_client() -> None:
    service_src = (_REPO / "app/services/amocrm_buyer_card_discovery.py").read_text(
        encoding="utf-8"
    )
    http_src = (_REPO / "app/core/amocrm_crm_buyer_card_http.py").read_text(
        encoding="utf-8"
    )
    assert "amocrm_crm_leads_http" not in service_src
    assert "amocrm_crm_leads_http" not in http_src
    assert "AmoCrmLeadHttpClient" not in service_src
    assert "from app.services.identity_resolution" not in service_src


def test_docker_allowlist_includes_ir3() -> None:
    assert_canonical_docker_runtime_allowlist()
    for rel in IR3_DOCKER_RUNTIME_PATHS:
        assert is_included_in_docker_build_context(rel)


def test_mapper_skips_incomplete() -> None:
    result = AmoCrmBuyerCardDiscoveryResult(
        outcome=AmoCrmBuyerCardDiscoveryOutcome.INCOMPLETE,
        contact_id="42",
        error_code="AMOCRM_BUYER_CARD_LINKED_LEADS_LIMIT",
    )
    assert buyer_card_reconcile_candidates_from_discovery(result) is None
