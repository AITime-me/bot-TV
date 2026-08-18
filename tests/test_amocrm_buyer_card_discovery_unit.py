"""IR-3 read-only amoCRM Buyer Card (Customer) discovery unit coverage."""

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
    parse_contact_with_customers_body,
    parse_customer_inspect_body,
)
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.amocrm_crm_rest_http import (
    AmoCrmCrmRestOutcome,
    AmoCrmCrmTokenRefreshResult,
)
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.services.amocrm_buyer_card_discovery import (
    MAX_LINKED_CUSTOMERS_PER_DISCOVERY,
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
    customer_ids: list[int],
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
        "_embedded": {
            "customers": [{"id": cid, "name": f"Customer {cid}"} for cid in customer_ids]
        },
    }


def _customer_payload(
    customer_id: int,
    contact_ids: list[int],
    *,
    name: str = "Buyer Customer Name",
) -> dict:
    return {
        "id": customer_id,
        "name": name,
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


def _queue_contact_and_customers(
    transport: _FakeTransport,
    *,
    contact_id: int,
    customers: list[dict],
) -> None:
    customer_ids = [int(item["id"]) for item in customers]
    transport.responses.append(
        _json_response(200, _contact_payload(contact_id, customer_ids))
    )
    by_id = {int(item["id"]): item for item in customers}
    for cid in sorted(set(customer_ids)):
        transport.responses.append(_json_response(200, by_id[cid]))


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
    assert result.eligible_customer_ids == ()
    text = repr(result)
    assert _NAME not in text
    assert _PHONE not in text


@pytest.mark.asyncio
async def test_contact_without_customers_not_found() -> None:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [])))
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND
    assert result.contact_id == str(_CONTACT_ID)
    assert result.eligible_customer_ids == ()
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_one_linked_customer_found_candidate() -> None:
    transport = _FakeTransport()
    _queue_contact_and_customers(
        transport,
        contact_id=_CONTACT_ID,
        customers=[_customer_payload(7, [_CONTACT_ID])],
    )
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE
    assert result.eligible_customer_ids == ("7",)
    assert transport.calls[0].method == "GET"
    assert "with=customers" in transport.calls[0].url
    assert "with=leads" not in transport.calls[0].url
    assert "/customers/7" in transport.calls[1].url
    assert "with=contacts" in transport.calls[1].url
    mapped = buyer_card_reconcile_candidates_from_discovery(result)
    assert mapped is not None
    assert mapped.candidate_buyer_card_ids == ("7",)
    assert mapped.candidate_technical_deal_ids == ()


@pytest.mark.asyncio
async def test_two_linked_customers_ambiguous() -> None:
    transport = _FakeTransport()
    _queue_contact_and_customers(
        transport,
        contact_id=_CONTACT_ID,
        customers=[
            _customer_payload(10, [_CONTACT_ID]),
            _customer_payload(20, [_CONTACT_ID]),
        ],
    )
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.AMBIGUOUS
    assert result.eligible_customer_ids == ("10", "20")
    mapped = buyer_card_reconcile_candidates_from_discovery(result)
    assert mapped is not None
    assert mapped.candidate_buyer_card_ids == ("10", "20")


@pytest.mark.asyncio
async def test_customer_response_id_mismatch() -> None:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [7])))
    transport.responses.append(_json_response(200, _customer_payload(99, [_CONTACT_ID])))
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_CUSTOMER_ID_MISMATCH"
    assert result.eligible_customer_ids == ()


@pytest.mark.asyncio
async def test_customer_no_longer_linked_to_contact_incomplete() -> None:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [7])))
    transport.responses.append(_json_response(200, _customer_payload(7, [999])))
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.INCOMPLETE
    assert result.error_code == "AMOCRM_BUYER_CARD_CUSTOMER_CONTACT_UNLINKED"
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
async def test_malformed_customer_permanent() -> None:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [7])))
    transport.responses.append(
        S2sHttpResponse(status_code=200, headers={}, body=b"{not-json")
    )
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_CUSTOMER_BODY_INVALID"
    assert result.outcome is not AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE
    assert result.outcome is not AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND


async def _discover_one_customer(payload: dict) -> AmoCrmBuyerCardDiscoveryResult:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [7])))
    transport.responses.append(_json_response(200, payload))
    service = await _make_service(transport)
    return await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))


def _assert_customer_body_invalid(result: AmoCrmBuyerCardDiscoveryResult) -> None:
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_CUSTOMER_BODY_INVALID"
    assert result.outcome is not AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE
    assert result.outcome is not AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND
    assert result.eligible_customer_ids == ()


@pytest.mark.asyncio
async def test_missing_embedded_contacts_malformed() -> None:
    payload = {"id": 7, "name": "Buyer Customer Name"}
    result = await _discover_one_customer(payload)
    _assert_customer_body_invalid(result)


@pytest.mark.asyncio
async def test_customer_status_fields_are_ignored() -> None:
    payload = _customer_payload(7, [_CONTACT_ID])
    payload["is_deleted"] = True
    payload["closed_at"] = 1_700_000_000
    result = await _discover_one_customer(payload)
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE
    assert result.eligible_customer_ids == ("7",)


@pytest.mark.asyncio
async def test_missing_embedded_customers_malformed() -> None:
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
async def test_customer_5xx_does_not_found_or_not_found() -> None:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [7])))
    transport.responses.append(S2sHttpResponse(status_code=503, headers={}, body=b""))
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.TRANSIENT_ERROR
    assert result.eligible_customer_ids == ()


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
            _json_response(200, _customer_payload(7, [_CONTACT_ID])),
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
            _json_response(200, _customer_payload(7, [_CONTACT_ID])),
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
async def test_over_max_linked_customers_incomplete() -> None:
    transport = _FakeTransport()
    too_many = list(range(1, MAX_LINKED_CUSTOMERS_PER_DISCOVERY + 2))
    transport.responses.append(
        _json_response(200, _contact_payload(_CONTACT_ID, too_many))
    )
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.INCOMPLETE
    assert result.error_code == "AMOCRM_BUYER_CARD_LINKED_CUSTOMERS_LIMIT"
    assert result.eligible_customer_ids == ()
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
async def test_duplicate_linked_customer_ids_deduped() -> None:
    transport = _FakeTransport()
    payload = _contact_payload(_CONTACT_ID, [7, 7, 7])
    transport.responses.append(_json_response(200, payload))
    transport.responses.append(_json_response(200, _customer_payload(7, [_CONTACT_ID])))
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE
    assert result.eligible_customer_ids == ("7",)
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_customer_missing_incomplete() -> None:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [7])))
    transport.responses.append(S2sHttpResponse(status_code=404, headers={}, body=b""))
    service = await _make_service(transport)
    result = await service.discover_buyer_card_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmBuyerCardDiscoveryOutcome.INCOMPLETE
    assert result.error_code == "AMOCRM_CRM_HTTP_404"


def test_result_repr_no_pii() -> None:
    result = AmoCrmBuyerCardDiscoveryResult(
        outcome=AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE,
        contact_id="42",
        eligible_customer_ids=("7",),
    )
    text = repr(result)
    assert "42" in text
    assert "7" in text
    assert _NAME not in text
    assert _PHONE not in text
    assert _EMAIL not in text
    parsed = parse_contact_with_customers_body(
        json.dumps(_contact_payload(42, [7], name=_NAME)).encode()
    )
    assert parsed is not None
    assert _NAME not in repr(parsed)
    assert _PHONE not in repr(parsed)
    customer = parse_customer_inspect_body(
        json.dumps(_customer_payload(7, [42])).encode()
    )
    assert customer is not None
    assert "Buyer Customer Name" not in repr(customer)
    assert "vip" not in repr(customer)


def test_http_client_has_no_write_methods() -> None:
    client = AmoCrmBuyerCardHttpClient(_enabled_config(), transport=_FakeTransport())
    names = dir(client)
    assert "create_lead" not in names
    assert "create_customer" not in names
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


def test_buyer_card_discovery_path_has_no_lead_calls() -> None:
    service_src = (_REPO / "app/services/amocrm_buyer_card_discovery.py").read_text(
        encoding="utf-8"
    )
    types_src = (_REPO / "app/core/amocrm_buyer_card_discovery.py").read_text(
        encoding="utf-8"
    )
    for src in (service_src, types_src):
        assert "GET_LEAD_" not in src
        assert "with=leads" not in src
        assert "get_lead_with_contacts" not in src
        assert "get_contact_with_leads" not in src
        assert "closed_at" not in src
        assert "is_deleted" not in src
    assert "GET_CUSTOMER_" not in service_src
    assert "get_customer_with_contacts" in service_src
    assert "get_contact_with_customers" in service_src


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
        error_code="AMOCRM_BUYER_CARD_LINKED_CUSTOMERS_LIMIT",
    )
    assert buyer_card_reconcile_candidates_from_discovery(result) is None
