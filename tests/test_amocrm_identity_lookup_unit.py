"""IR-2 read-only amoCRM identity lookup unit coverage."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from app.core.amocrm_crm_contacts_http import (
    AmoCrmContactRecord,
    AmoCrmContactsHttpClient,
    contact_has_exact_phone,
    extract_contact_phone_raw_values,
    parse_contact_body,
    parse_contacts_query_page,
)
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.amocrm_crm_rest_http import (
    AmoCrmCrmRestOutcome,
    AmoCrmCrmTokenRefreshResult,
)
from app.core.amocrm_identity_lookup import (
    AmoCrmIdentityLookupOutcome,
    AmoCrmIdentityLookupResult,
)
from app.core.identity_resolution import normalize_phone_e164
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.services.amocrm_identity_lookup import (
    MAX_CONTACT_QUERY_PAGES,
    AmoCrmIdentityLookupService,
)
from tests.docker_runtime_allowlist import (
    IR2_DOCKER_RUNTIME_PATHS,
    assert_canonical_docker_runtime_allowlist,
    is_included_in_docker_build_context,
)

_REPO = Path(__file__).resolve().parents[1]
_PHONE = "+79001234567"
_PHONE_ALT_FORMATS = (
    "+7 (900) 123-45-67",
    "8 (900) 123-45-67",
    "9001234567",
    "79001234567",
)


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


def _phone_fields(*phones: str) -> list[dict]:
    return [
        {
            "field_code": "PHONE",
            "values": [{"value": p} for p in phones],
        }
    ]


def _contact_payload(contact_id: int, *phones: str, name: str = "Secret Name") -> dict:
    return {
        "id": contact_id,
        "name": name,
        "custom_fields_values": _phone_fields(*phones) if phones else [],
    }


def _json_response(status: int, payload: object) -> S2sHttpResponse:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return S2sHttpResponse(status_code=status, headers={}, body=body)


def _list_payload(
    contacts: list[dict],
    *,
    has_next: bool = False,
    include_links: bool = True,
) -> dict:
    payload: dict = {"_embedded": {"contacts": contacts}}
    if include_links:
        if has_next:
            payload["_links"] = {
                "next": {"href": "https://example.amocrm.ru/api/v4/contacts?page=2"}
            }
        else:
            payload["_links"] = {
                "self": {"href": "https://example.amocrm.ru/api/v4/contacts?page=1"}
            }
    return payload


async def _make_service(
    transport: _FakeTransport,
    *,
    token: str = "access-1",
    oauth: _StubOauth | None = None,
) -> AmoCrmIdentityLookupService:
    token_box = {"access": token}
    if oauth is None:
        oauth = _StubOauth(token_box=token_box, outcome=AmoCrmCrmRestOutcome.SUCCESS)

    async def _resolve() -> str | None:
        return token_box["access"]

    return AmoCrmIdentityLookupService(
        session_factory=object(),  # type: ignore[arg-type]
        config=_enabled_config(),
        transport=transport,
        oauth=oauth,  # type: ignore[arg-type]
        resolve_access_token=_resolve,
    )


@pytest.mark.asyncio
async def test_lookup_by_id_found() -> None:
    transport = _FakeTransport()
    transport.responses.append(
        _json_response(200, _contact_payload(42, _PHONE, name="Hidden"))
    )
    service = await _make_service(transport)
    result = await service.lookup_contact_by_id(contact_id="42")
    assert result.outcome is AmoCrmIdentityLookupOutcome.FOUND
    assert result.contact_id == "42"
    assert len(transport.calls) == 1
    assert transport.calls[0].method == "GET"
    assert transport.calls[0].url.endswith("/api/v4/contacts/42")
    text = repr(result)
    assert "Hidden" not in text
    assert _PHONE not in text


@pytest.mark.asyncio
async def test_lookup_by_id_404_not_found() -> None:
    transport = _FakeTransport()
    transport.responses.append(S2sHttpResponse(status_code=404, headers={}, body=b""))
    service = await _make_service(transport)
    result = await service.lookup_contact_by_id(contact_id="99")
    assert result.outcome is AmoCrmIdentityLookupOutcome.NOT_FOUND
    assert result.error_code == "AMOCRM_CRM_HTTP_404"


@pytest.mark.asyncio
async def test_lookup_by_id_204_not_found() -> None:
    transport = _FakeTransport()
    transport.responses.append(S2sHttpResponse(status_code=204, headers={}, body=b""))
    service = await _make_service(transport)
    result = await service.lookup_contact_by_id(contact_id="99")
    assert result.outcome is AmoCrmIdentityLookupOutcome.NOT_FOUND
    assert result.error_code == "AMOCRM_CRM_HTTP_204"
    assert result.contact_id is None


@pytest.mark.asyncio
async def test_lookup_by_id_response_id_mismatch_fail_closed() -> None:
    transport = _FakeTransport()
    transport.responses.append(
        _json_response(200, _contact_payload(99, _PHONE, name="Hidden Person"))
    )
    service = await _make_service(transport)
    result = await service.lookup_contact_by_id(contact_id="42")
    assert result.outcome is AmoCrmIdentityLookupOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_CONTACT_ID_MISMATCH"
    assert result.contact_id is None
    text = repr(result)
    assert "Hidden Person" not in text
    assert _PHONE not in text
    assert result.error_code is not None
    assert _PHONE not in result.error_code


@pytest.mark.asyncio
async def test_lookup_by_id_malformed_fail_closed() -> None:
    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(status_code=200, headers={}, body=b"{not-json")
    )
    service = await _make_service(transport)
    result = await service.lookup_contact_by_id(contact_id="1")
    assert result.outcome is AmoCrmIdentityLookupOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_CONTACT_BODY_INVALID"


@pytest.mark.asyncio
async def test_lookup_by_phone_zero_exact_not_found() -> None:
    transport = _FakeTransport()
    transport.responses.append(
        _json_response(200, _list_payload([_contact_payload(1, "+79009999999")]))
    )
    service = await _make_service(transport)
    result = await service.lookup_contact_by_phone(phone=_PHONE)
    assert result.outcome is AmoCrmIdentityLookupOutcome.NOT_FOUND


@pytest.mark.asyncio
async def test_lookup_by_phone_one_exact_found() -> None:
    transport = _FakeTransport()
    transport.responses.append(
        _json_response(200, _list_payload([_contact_payload(7, "8 (900) 123-45-67")]))
    )
    service = await _make_service(transport)
    result = await service.lookup_contact_by_phone(phone=_PHONE)
    assert result.outcome is AmoCrmIdentityLookupOutcome.FOUND
    assert result.contact_id == "7"


@pytest.mark.asyncio
async def test_lookup_by_phone_multiple_exact_ambiguous() -> None:
    transport = _FakeTransport()
    transport.responses.append(
        _json_response(
            200,
            _list_payload(
                [
                    _contact_payload(10, _PHONE),
                    _contact_payload(20, "9001234567"),
                ]
            ),
        )
    )
    service = await _make_service(transport)
    result = await service.lookup_contact_by_phone(phone=_PHONE)
    assert result.outcome is AmoCrmIdentityLookupOutcome.AMBIGUOUS
    assert result.contact_ids == ("10", "20")
    assert result.contact_id is None


@pytest.mark.asyncio
async def test_query_false_positive_dropped_by_exact_phone_filter() -> None:
    transport = _FakeTransport()
    # Discovery hit: name contains digits, PHONE is different — must NOT match.
    transport.responses.append(
        _json_response(
            200,
            _list_payload(
                [
                    _contact_payload(
                        5,
                        "+79001112233",
                        name=f"Client {_PHONE}",
                    )
                ]
            ),
        )
    )
    service = await _make_service(transport)
    result = await service.lookup_contact_by_phone(phone=_PHONE)
    assert result.outcome is AmoCrmIdentityLookupOutcome.NOT_FOUND


@pytest.mark.parametrize("raw", _PHONE_ALT_FORMATS)
@pytest.mark.asyncio
async def test_phone_formats_normalize_to_exact_match(raw: str) -> None:
    assert normalize_phone_e164(raw) == _PHONE
    transport = _FakeTransport()
    transport.responses.append(
        _json_response(200, _list_payload([_contact_payload(3, _PHONE)]))
    )
    service = await _make_service(transport)
    result = await service.lookup_contact_by_phone(phone=raw)
    assert result.outcome is AmoCrmIdentityLookupOutcome.FOUND
    assert result.contact_id == "3"


@pytest.mark.asyncio
async def test_invalid_phone_no_http() -> None:
    transport = _FakeTransport()
    service = await _make_service(transport)
    result = await service.lookup_contact_by_phone(phone="not-a-phone")
    assert result.outcome is AmoCrmIdentityLookupOutcome.INVALID_INPUT
    assert transport.calls == []


@pytest.mark.asyncio
async def test_invalid_contact_id_no_http() -> None:
    transport = _FakeTransport()
    service = await _make_service(transport)
    result = await service.lookup_contact_by_id(contact_id="abc")
    assert result.outcome is AmoCrmIdentityLookupOutcome.INVALID_INPUT
    assert transport.calls == []


@pytest.mark.asyncio
async def test_401_one_refresh_then_retry_get() -> None:
    transport = _FakeTransport()
    token_box = {"access": "access-old"}
    oauth = _StubOauth(token_box=token_box, outcome=AmoCrmCrmRestOutcome.SUCCESS)
    transport.responses.extend(
        [
            S2sHttpResponse(status_code=401, headers={}, body=b""),
            _json_response(200, _contact_payload(8, _PHONE)),
        ]
    )

    async def _resolve() -> str | None:
        return token_box["access"]

    service = AmoCrmIdentityLookupService(
        session_factory=object(),  # type: ignore[arg-type]
        config=_enabled_config(),
        transport=transport,
        oauth=oauth,  # type: ignore[arg-type]
        resolve_access_token=_resolve,
    )
    result = await service.lookup_contact_by_id(contact_id="8")
    assert result.outcome is AmoCrmIdentityLookupOutcome.FOUND
    assert result.contact_id == "8"
    assert oauth.refresh_count == 1
    assert oauth.refresh_count <= 1
    assert len(transport.calls) == 2
    assert transport.calls[1].headers["Authorization"] == "Bearer access-after-refresh"


@pytest.mark.asyncio
async def test_second_401_fail_closed() -> None:
    transport = _FakeTransport()
    token_box = {"access": "access-old"}
    oauth = _StubOauth(token_box=token_box, outcome=AmoCrmCrmRestOutcome.SUCCESS)
    transport.responses.extend(
        [
            S2sHttpResponse(status_code=401, headers={}, body=b""),
            S2sHttpResponse(status_code=401, headers={}, body=b""),
        ]
    )

    async def _resolve() -> str | None:
        return token_box["access"]

    service = AmoCrmIdentityLookupService(
        session_factory=object(),  # type: ignore[arg-type]
        config=_enabled_config(),
        transport=transport,
        oauth=oauth,  # type: ignore[arg-type]
        resolve_access_token=_resolve,
    )
    result = await service.lookup_contact_by_id(contact_id="8")
    assert result.outcome is AmoCrmIdentityLookupOutcome.PERMANENT_ERROR
    assert oauth.refresh_count == 1
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_phone_401_page1_refresh_once_later_page_no_second_refresh() -> None:
    transport = _FakeTransport()
    token_box = {"access": "access-old"}
    oauth = _StubOauth(token_box=token_box, outcome=AmoCrmCrmRestOutcome.SUCCESS)
    transport.responses.extend(
        [
            S2sHttpResponse(status_code=401, headers={}, body=b""),
            _json_response(
                200,
                _list_payload([_contact_payload(7, _PHONE)], has_next=True),
            ),
            S2sHttpResponse(status_code=401, headers={}, body=b""),
        ]
    )

    async def _resolve() -> str | None:
        return token_box["access"]

    service = AmoCrmIdentityLookupService(
        session_factory=object(),  # type: ignore[arg-type]
        config=_enabled_config(),
        transport=transport,
        oauth=oauth,  # type: ignore[arg-type]
        resolve_access_token=_resolve,
    )
    result = await service.lookup_contact_by_phone(phone=_PHONE)
    assert result.outcome is AmoCrmIdentityLookupOutcome.PERMANENT_ERROR
    assert result.outcome is not AmoCrmIdentityLookupOutcome.FOUND
    assert oauth.refresh_count == 1
    assert len(transport.calls) == 3


@pytest.mark.asyncio
async def test_proactive_refresh_then_401_no_second_refresh() -> None:
    transport = _FakeTransport()
    token_box = {"access": "access-old"}
    oauth = _StubOauth(token_box=token_box, outcome=AmoCrmCrmRestOutcome.SUCCESS)
    transport.responses.append(S2sHttpResponse(status_code=401, headers={}, body=b""))

    async def _resolve() -> str | None:
        return token_box["access"]

    class _Proactive(AmoCrmIdentityLookupService):
        async def _resolve_access_token(self, budget):  # type: ignore[override]
            refreshed = await self._try_remote_refresh(budget)
            assert refreshed is True
            loaded = await self._load_access_token()
            return loaded

    service = _Proactive(
        session_factory=object(),  # type: ignore[arg-type]
        config=_enabled_config(),
        transport=transport,
        oauth=oauth,  # type: ignore[arg-type]
        resolve_access_token=_resolve,
    )
    result = await service.lookup_contact_by_id(contact_id="8")
    assert result.outcome is AmoCrmIdentityLookupOutcome.PERMANENT_ERROR
    assert oauth.refresh_count == 1
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [402, 403])
@pytest.mark.asyncio
async def test_402_403_permanent_not_transient(status: int) -> None:
    transport = _FakeTransport()
    transport.responses.append(S2sHttpResponse(status_code=status, headers={}, body=b""))
    service = await _make_service(transport)
    result = await service.lookup_contact_by_id(contact_id="1")
    assert result.outcome is AmoCrmIdentityLookupOutcome.PERMANENT_ERROR
    assert result.error_code == f"AMOCRM_CRM_HTTP_{status}"


@pytest.mark.asyncio
async def test_transport_and_5xx_transient() -> None:
    from app.core.s2s_http_transport import S2sHttpTransportError

    class _Boom(_FakeTransport):
        def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
            self.calls.append(req)
            raise S2sHttpTransportError("TRANSPORT_ERROR")

    boom = _Boom()
    service = await _make_service(boom)
    result = await service.lookup_contact_by_id(contact_id="1")
    assert result.outcome is AmoCrmIdentityLookupOutcome.TRANSIENT_ERROR

    transport = _FakeTransport()
    transport.responses.append(S2sHttpResponse(status_code=503, headers={}, body=b""))
    service2 = await _make_service(transport)
    result2 = await service2.lookup_contact_by_id(contact_id="1")
    assert result2.outcome is AmoCrmIdentityLookupOutcome.TRANSIENT_ERROR


@pytest.mark.asyncio
async def test_pagination_incomplete_fail_closed() -> None:
    transport = _FakeTransport()
    for page in range(MAX_CONTACT_QUERY_PAGES):
        transport.responses.append(
            _json_response(
                200,
                _list_payload(
                    [_contact_payload(1000 + page, "+79009999999")],
                    has_next=True,
                ),
            )
        )
    service = await _make_service(transport)
    result = await service.lookup_contact_by_phone(phone=_PHONE)
    assert result.outcome is AmoCrmIdentityLookupOutcome.INCOMPLETE
    assert result.error_code == "AMOCRM_CRM_CONTACTS_PAGE_INCOMPLETE"
    assert len(transport.calls) == MAX_CONTACT_QUERY_PAGES


@pytest.mark.asyncio
async def test_204_list_not_found() -> None:
    transport = _FakeTransport()
    transport.responses.append(S2sHttpResponse(status_code=204, headers={}, body=b""))
    service = await _make_service(transport)
    result = await service.lookup_contact_by_phone(phone=_PHONE)
    assert result.outcome is AmoCrmIdentityLookupOutcome.NOT_FOUND


@pytest.mark.asyncio
async def test_missing_embedded_fail_closed() -> None:
    transport = _FakeTransport()
    transport.responses.append(
        _json_response(200, {"_links": {"self": {"href": "https://example.amocrm.ru/x"}}})
    )
    service = await _make_service(transport)
    result = await service.lookup_contact_by_phone(phone=_PHONE)
    assert result.outcome is AmoCrmIdentityLookupOutcome.PERMANENT_ERROR
    assert result.outcome not in {
        AmoCrmIdentityLookupOutcome.FOUND,
        AmoCrmIdentityLookupOutcome.NOT_FOUND,
    }


@pytest.mark.asyncio
async def test_missing_contacts_fail_closed() -> None:
    transport = _FakeTransport()
    transport.responses.append(
        _json_response(
            200,
            {
                "_embedded": {},
                "_links": {"self": {"href": "https://example.amocrm.ru/x"}},
            },
        )
    )
    service = await _make_service(transport)
    result = await service.lookup_contact_by_phone(phone=_PHONE)
    assert result.outcome is AmoCrmIdentityLookupOutcome.PERMANENT_ERROR
    assert result.outcome not in {
        AmoCrmIdentityLookupOutcome.FOUND,
        AmoCrmIdentityLookupOutcome.NOT_FOUND,
    }


@pytest.mark.asyncio
async def test_malformed_links_fail_closed() -> None:
    transport = _FakeTransport()
    transport.responses.append(
        _json_response(
            200,
            {
                "_embedded": {"contacts": [_contact_payload(7, _PHONE)]},
                "_links": "not-an-object",
            },
        )
    )
    service = await _make_service(transport)
    result = await service.lookup_contact_by_phone(phone=_PHONE)
    assert result.outcome is AmoCrmIdentityLookupOutcome.PERMANENT_ERROR
    assert result.outcome is not AmoCrmIdentityLookupOutcome.FOUND


@pytest.mark.asyncio
async def test_missing_pagination_envelope_one_exact_not_found() -> None:
    transport = _FakeTransport()
    transport.responses.append(
        _json_response(
            200,
            _list_payload([_contact_payload(7, _PHONE)], include_links=False),
        )
    )
    service = await _make_service(transport)
    result = await service.lookup_contact_by_phone(phone=_PHONE)
    assert result.outcome is AmoCrmIdentityLookupOutcome.PERMANENT_ERROR
    assert result.outcome is not AmoCrmIdentityLookupOutcome.FOUND
    assert result.contact_id is None


@pytest.mark.asyncio
async def test_valid_last_page_without_next_found() -> None:
    transport = _FakeTransport()
    transport.responses.append(
        _json_response(200, _list_payload([_contact_payload(7, _PHONE)], has_next=False))
    )
    service = await _make_service(transport)
    result = await service.lookup_contact_by_phone(phone=_PHONE)
    assert result.outcome is AmoCrmIdentityLookupOutcome.FOUND
    assert result.contact_id == "7"


@pytest.mark.asyncio
async def test_valid_last_page_without_next_not_found() -> None:
    transport = _FakeTransport()
    transport.responses.append(
        _json_response(
            200,
            _list_payload([_contact_payload(1, "+79009999999")], has_next=False),
        )
    )
    service = await _make_service(transport)
    result = await service.lookup_contact_by_phone(phone=_PHONE)
    assert result.outcome is AmoCrmIdentityLookupOutcome.NOT_FOUND


@pytest.mark.asyncio
async def test_disabled_zero_http() -> None:
    transport = _FakeTransport()
    service = AmoCrmIdentityLookupService(
        session_factory=object(),  # type: ignore[arg-type]
        config=AmoCrmCrmRestConfig(enabled=False),
        transport=transport,
    )
    result = await service.lookup_contact_by_phone(phone=_PHONE)
    assert result.outcome is AmoCrmIdentityLookupOutcome.DISABLED
    assert transport.calls == []


def test_result_and_record_repr_no_pii() -> None:
    result = AmoCrmIdentityLookupResult(
        outcome=AmoCrmIdentityLookupOutcome.FOUND,
        contact_id="123",
    )
    text = repr(result)
    assert "123" in text
    assert _PHONE not in text
    assert "@" not in text
    record = AmoCrmContactRecord(
        contact_id="55",
        phone_raw_values=(_PHONE, "secret@example.com"),
    )
    rtext = repr(record)
    assert "55" in rtext
    assert _PHONE not in rtext
    assert "secret@example.com" not in rtext
    assert "phone_raw_count=2" in rtext
    assert "Hidden Person" not in rtext


def test_parse_helpers_and_exact_phone() -> None:
    body = json.dumps(_contact_payload(9, "8-900-123-45-67")).encode()
    parsed = parse_contact_body(body)
    assert parsed is not None
    assert parsed.contact_id == "9"
    assert contact_has_exact_phone(
        parsed,
        normalized_phone=_PHONE,
        normalize_fn=normalize_phone_e164,
    )
    page = parse_contacts_query_page(
        json.dumps(_list_payload([_contact_payload(1, _PHONE)], has_next=True)).encode(),
        status_code=200,
    )
    assert page is not None
    contacts, has_next = page
    assert has_next is True
    assert len(contacts) == 1
    assert extract_contact_phone_raw_values({"name": "x", "custom_fields_values": []}) == ()
    assert (
        parse_contacts_query_page(
            json.dumps({"_links": {"self": {"href": "https://example.amocrm.ru/x"}}}).encode(),
            status_code=200,
        )
        is None
    )
    last = parse_contacts_query_page(
        json.dumps(_list_payload([_contact_payload(1, _PHONE)], has_next=False)).encode(),
        status_code=200,
    )
    assert last is not None
    last_contacts, last_next = last
    assert last_next is False
    assert len(last_contacts) == 1


def test_contacts_http_rejects_non_get() -> None:
    client = AmoCrmContactsHttpClient(_enabled_config(), transport=_FakeTransport())
    with pytest.raises(Exception):
        client._request(  # noqa: SLF001
            method="POST",
            path="/api/v4/contacts",
            access_token="t",
            call_label="BAD",
        )


def test_no_entity_mutating_http_in_ir2_modules() -> None:
    paths = [
        _REPO / "app/core/amocrm_crm_contacts_http.py",
        _REPO / "app/services/amocrm_identity_lookup.py",
        _REPO / "app/core/amocrm_identity_lookup.py",
    ]
    forbidden = {"POST", "PATCH", "PUT", "DELETE"}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and type(node.value) is str:
                if node.value in forbidden:
                    # method= literals only; allow words in comments via Constant scan
                    # of source strings like method="GET" — POST must not appear.
                    raise AssertionError(f"{path.name} contains {node.value!r}")


def test_ir2_not_wired_into_webhook_or_worker() -> None:
    for rel in (
        "app/amocrm_chat_webhook.py",
        "app/worker.py",
        "app/services/worker_runtime.py",
        "app/services/inbound.py",
        "app/services/identity_glue.py",
        "app/services/identity_resolution.py",
    ):
        source = (_REPO / rel).read_text(encoding="utf-8")
        assert "amocrm_identity_lookup" not in source
        assert "AmoCrmIdentityLookupService" not in source


def test_docker_allowlist_includes_ir2() -> None:
    assert_canonical_docker_runtime_allowlist()
    for rel in IR2_DOCKER_RUNTIME_PATHS:
        assert is_included_in_docker_build_context(rel)


def test_protocol_surface_exists() -> None:
    assert hasattr(AmoCrmIdentityLookupService, "lookup_by_external_id")
    source = inspect.getsource(AmoCrmIdentityLookupService)
    assert "normalize_phone_e164" in source
    assert "refresh_tokens" in source
