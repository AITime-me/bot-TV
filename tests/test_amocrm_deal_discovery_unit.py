"""Read-only amoCRM business Deal (Lead) discovery unit coverage."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.amocrm_crm_rest_http import (
    AmoCrmCrmRestOutcome,
    AmoCrmCrmTokenRefreshResult,
)
from app.core.amocrm_deal_discovery import (
    AMOCRM_SYSTEM_LEAD_STATUS_SUCCESS,
    AMOCRM_SYSTEM_LEAD_STATUS_UNREALIZED,
    AmoCrmDealDiscoveryOutcome,
)
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.services.amocrm_deal_discovery import (
    MAX_LINKED_LEADS_PER_DISCOVERY,
    AmoCrmDealDiscoveryService,
)
from tests.docker_runtime_allowlist import (
    DEAL_DISCOVERY_DOCKER_RUNTIME_PATHS,
    assert_canonical_docker_runtime_allowlist,
    is_included_in_docker_build_context,
)

_REPO = Path(__file__).resolve().parents[1]
_CONTACT_ID = 42
_NAME = "Secret Person"
_CUSTOM_ACTIVE_STATUS = 555001
_CLOSED_AT = 1_700_000_000


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


def _contact_payload(contact_id: int, lead_ids: list[int]) -> dict:
    return {
        "id": contact_id,
        "name": _NAME,
        "_embedded": {"leads": [{"id": lid, "name": f"Lead {lid}"} for lid in lead_ids]},
    }


def _lead_payload(
    lead_id: int,
    contact_ids: list[int],
    *,
    status_id: int = _CUSTOM_ACTIVE_STATUS,
    closed_at: int | None = None,
    is_deleted: bool = False,
    name: str = "Business Deal Name",
) -> dict:
    return {
        "id": lead_id,
        "name": name,
        "status_id": status_id,
        "is_deleted": is_deleted,
        "closed_at": closed_at,
        "tags": [{"name": "vip"}],
        "_embedded": {"contacts": [{"id": cid} for cid in contact_ids]},
    }


async def _make_service(transport: _FakeTransport) -> AmoCrmDealDiscoveryService:
    token_box = {"access": "access-1"}
    oauth = _StubOauth(token_box=token_box, outcome=AmoCrmCrmRestOutcome.SUCCESS)

    async def _resolve() -> str | None:
        return token_box["access"]

    return AmoCrmDealDiscoveryService(
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
async def test_contact_without_leads_not_found() -> None:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [])))
    service = await _make_service(transport)
    result = await service.discover_deal_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmDealDiscoveryOutcome.NOT_FOUND
    assert result.business_active_lead_ids == ()
    assert result.reanimation_candidate_lead_ids == ()


@pytest.mark.asyncio
async def test_active_custom_status_closed_at_null_is_business_active() -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[
            _lead_payload(
                7,
                [_CONTACT_ID],
                status_id=_CUSTOM_ACTIVE_STATUS,
                closed_at=None,
            )
        ],
    )
    service = await _make_service(transport)
    result = await service.discover_deal_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmDealDiscoveryOutcome.FOUND
    assert result.business_active_lead_ids == ("7",)
    assert result.reanimation_candidate_lead_ids == ()
    assert result.successfully_closed_lead_ids == ()
    assert result.technical_lead_ids == ()
    assert "with=leads" in transport.calls[0].url
    assert "GET_LEAD_7" in result.http_calls


@pytest.mark.asyncio
async def test_status_143_closed_at_is_reanimation_candidate() -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[
            _lead_payload(
                8,
                [_CONTACT_ID],
                status_id=AMOCRM_SYSTEM_LEAD_STATUS_UNREALIZED,
                closed_at=_CLOSED_AT,
            )
        ],
    )
    service = await _make_service(transport)
    result = await service.discover_deal_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmDealDiscoveryOutcome.FOUND
    assert result.business_active_lead_ids == ()
    assert result.reanimation_candidate_lead_ids == ("8",)
    assert result.successfully_closed_lead_ids == ()
    assert result.technical_lead_ids == ()


@pytest.mark.asyncio
async def test_status_142_closed_at_is_successfully_closed_not_reanimation() -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[
            _lead_payload(
                8,
                [_CONTACT_ID],
                status_id=AMOCRM_SYSTEM_LEAD_STATUS_SUCCESS,
                closed_at=_CLOSED_AT,
            )
        ],
    )
    service = await _make_service(transport)
    result = await service.discover_deal_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmDealDiscoveryOutcome.FOUND
    assert result.business_active_lead_ids == ()
    assert result.reanimation_candidate_lead_ids == ()
    assert result.successfully_closed_lead_ids == ("8",)
    assert result.technical_lead_ids == ()


@pytest.mark.asyncio
async def test_deleted_lead_not_reanimation_candidate() -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[_lead_payload(8, [_CONTACT_ID], is_deleted=True)],
    )
    service = await _make_service(transport)
    result = await service.discover_deal_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmDealDiscoveryOutcome.FOUND
    assert result.business_active_lead_ids == ()
    assert result.reanimation_candidate_lead_ids == ()
    assert result.successfully_closed_lead_ids == ()


@pytest.mark.asyncio
async def test_deleted_status_143_not_reanimation() -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[
            _lead_payload(
                8,
                [_CONTACT_ID],
                status_id=AMOCRM_SYSTEM_LEAD_STATUS_UNREALIZED,
                closed_at=_CLOSED_AT,
                is_deleted=True,
            )
        ],
    )
    service = await _make_service(transport)
    result = await service.discover_deal_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmDealDiscoveryOutcome.FOUND
    assert result.reanimation_candidate_lead_ids == ()
    assert result.successfully_closed_lead_ids == ()
    assert result.business_active_lead_ids == ()


@pytest.mark.asyncio
async def test_technical_lead_excluded_from_business_candidates() -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[
            _lead_payload(2, [_CONTACT_ID]),
            _lead_payload(3, [_CONTACT_ID]),
            _lead_payload(
                8,
                [_CONTACT_ID],
                status_id=AMOCRM_SYSTEM_LEAD_STATUS_UNREALIZED,
                closed_at=_CLOSED_AT,
            ),
        ],
    )
    service = await _make_service(transport)
    result = await service.discover_deal_candidates(
        contact_id=str(_CONTACT_ID),
        known_technical_deal_ids=("2",),
    )
    assert result.outcome is AmoCrmDealDiscoveryOutcome.FOUND
    assert result.technical_lead_ids == ("2",)
    assert result.business_active_lead_ids == ("3",)
    assert result.reanimation_candidate_lead_ids == ("8",)
    assert result.known_technical_deal_ids == ("2",)


@pytest.mark.parametrize(
    "status_id",
    [AMOCRM_SYSTEM_LEAD_STATUS_SUCCESS, AMOCRM_SYSTEM_LEAD_STATUS_UNREALIZED],
)
@pytest.mark.asyncio
async def test_technical_status_142_or_143_remains_technical(status_id: int) -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[
            _lead_payload(
                9,
                [_CONTACT_ID],
                status_id=status_id,
                closed_at=_CLOSED_AT,
            )
        ],
    )
    service = await _make_service(transport)
    result = await service.discover_deal_candidates(
        contact_id=str(_CONTACT_ID),
        known_technical_deal_ids=("9",),
    )
    assert result.technical_lead_ids == ("9",)
    assert result.reanimation_candidate_lead_ids == ()
    assert result.successfully_closed_lead_ids == ()
    assert result.business_active_lead_ids == ()


@pytest.mark.asyncio
async def test_over_max_linked_leads_incomplete() -> None:
    transport = _FakeTransport()
    too_many = list(range(1, MAX_LINKED_LEADS_PER_DISCOVERY + 2))
    transport.responses.append(
        _json_response(200, _contact_payload(_CONTACT_ID, too_many))
    )
    service = await _make_service(transport)
    result = await service.discover_deal_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmDealDiscoveryOutcome.INCOMPLETE
    assert result.error_code == "AMOCRM_DEAL_LINKED_LEADS_LIMIT"
    assert result.business_active_lead_ids == ()


@pytest.mark.asyncio
async def test_lead_unlinked_from_contact_incomplete() -> None:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [7])))
    transport.responses.append(_json_response(200, _lead_payload(7, [999])))
    service = await _make_service(transport)
    result = await service.discover_deal_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmDealDiscoveryOutcome.INCOMPLETE
    assert result.error_code == "AMOCRM_DEAL_LEAD_CONTACT_UNLINKED"


@pytest.mark.asyncio
async def test_malformed_lead_permanent() -> None:
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [7])))
    transport.responses.append(
        S2sHttpResponse(status_code=200, headers={}, body=b"{not-json")
    )
    service = await _make_service(transport)
    result = await service.discover_deal_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmDealDiscoveryOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_LEAD_BODY_INVALID"


@pytest.mark.asyncio
async def test_missing_closed_at_permanent() -> None:
    payload = _lead_payload(7, [_CONTACT_ID])
    del payload["closed_at"]
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [7])))
    transport.responses.append(_json_response(200, payload))
    service = await _make_service(transport)
    result = await service.discover_deal_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmDealDiscoveryOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_LEAD_BODY_INVALID"


@pytest.mark.asyncio
async def test_missing_status_id_permanent() -> None:
    payload = _lead_payload(7, [_CONTACT_ID])
    del payload["status_id"]
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [7])))
    transport.responses.append(_json_response(200, payload))
    service = await _make_service(transport)
    result = await service.discover_deal_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmDealDiscoveryOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_LEAD_BODY_INVALID"


@pytest.mark.parametrize("bad_status", ["143", True, 0, -1, 1.5, []])
@pytest.mark.asyncio
async def test_malformed_status_id_permanent(bad_status: object) -> None:
    payload = _lead_payload(7, [_CONTACT_ID])
    payload["status_id"] = bad_status
    transport = _FakeTransport()
    transport.responses.append(_json_response(200, _contact_payload(_CONTACT_ID, [7])))
    transport.responses.append(_json_response(200, payload))
    service = await _make_service(transport)
    result = await service.discover_deal_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmDealDiscoveryOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_LEAD_BODY_INVALID"


@pytest.mark.asyncio
async def test_non_system_status_with_closed_at_fail_closed() -> None:
    transport = _FakeTransport()
    _queue_contact_and_leads(
        transport,
        contact_id=_CONTACT_ID,
        leads=[
            _lead_payload(
                7,
                [_CONTACT_ID],
                status_id=_CUSTOM_ACTIVE_STATUS,
                closed_at=_CLOSED_AT,
            )
        ],
    )
    service = await _make_service(transport)
    result = await service.discover_deal_candidates(contact_id=str(_CONTACT_ID))
    assert result.outcome is AmoCrmDealDiscoveryOutcome.INCOMPLETE
    assert result.error_code == "AMOCRM_DEAL_LEAD_STATUS_CLOSED_INCONSISTENT"
    assert result.business_active_lead_ids == ()
    assert result.reanimation_candidate_lead_ids == ()
    assert result.successfully_closed_lead_ids == ()


@pytest.mark.asyncio
async def test_invalid_technical_id_no_http() -> None:
    transport = _FakeTransport()
    service = await _make_service(transport)
    result = await service.discover_deal_candidates(
        contact_id="1",
        known_technical_deal_ids=("not-id",),
    )
    assert result.outcome is AmoCrmDealDiscoveryOutcome.INVALID_INPUT
    assert transport.calls == []


@pytest.mark.asyncio
async def test_disabled_zero_http() -> None:
    transport = _FakeTransport()
    service = AmoCrmDealDiscoveryService(
        session_factory=object(),  # type: ignore[arg-type]
        config=AmoCrmCrmRestConfig(enabled=False),
        transport=transport,
    )
    result = await service.discover_deal_candidates(contact_id="1")
    assert result.outcome is AmoCrmDealDiscoveryOutcome.DISABLED
    assert transport.calls == []


def test_deal_discovery_not_wired_into_webhook_worker() -> None:
    for rel in (
        "app/amocrm_chat_webhook.py",
        "app/worker.py",
        "app/services/worker_runtime.py",
        "app/services/inbound.py",
        "app/services/identity_glue.py",
        "app/services/amocrm_buyer_card_read_flow.py",
        "app/services/amocrm_buyer_card_bind.py",
    ):
        source = (_REPO / rel).read_text(encoding="utf-8")
        assert "amocrm_deal_discovery" not in source
        assert "AmoCrmDealDiscoveryService" not in source
        assert "discover_deal_candidates" not in source


def test_no_mutating_http_in_deal_discovery() -> None:
    paths = [
        _REPO / "app/services/amocrm_deal_discovery.py",
        _REPO / "app/core/amocrm_deal_discovery.py",
    ]
    forbidden = {"POST", "PATCH", "PUT", "DELETE"}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and type(node.value) is str:
                if node.value in forbidden:
                    raise AssertionError(f"{path.name} contains {node.value!r}")


def test_docker_allowlist_includes_deal_discovery() -> None:
    assert_canonical_docker_runtime_allowlist()
    for rel in DEAL_DISCOVERY_DOCKER_RUNTIME_PATHS:
        assert is_included_in_docker_build_context(rel)
        assert (_REPO / rel).is_file()
