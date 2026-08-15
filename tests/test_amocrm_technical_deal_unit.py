"""AMO-01B2 technical-deal projection unit coverage."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.amocrm_crm_deal_create_config import (
    AmoCrmDealCreateConfig,
    AmoCrmDealCreateConfigError,
    load_deal_create_config_fail_closed,
)
from app.core.amocrm_crm_leads_http import AmoCrmLeadHttpClient
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.services.amocrm_technical_deal import (
    TechnicalDealOutcome,
    TechnicalDealProjectionService,
    coerce_conversation_uuid,
)
from tests.docker_runtime_allowlist import (
    AMO01B2_DOCKER_RUNTIME_PATHS,
    assert_canonical_docker_runtime_allowlist,
    is_included_in_docker_build_context,
)

_REPO = Path(__file__).resolve().parents[1]


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[S2sHttpRequest] = []
        self.responses: list[S2sHttpResponse] = []

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        if not self.responses:
            raise AssertionError("no fake response queued")
        return self.responses.pop(0)


def _valid_deal_env(**extra: str) -> dict[str, str]:
    env = {
        "AMOCRM_CRM_REST_ENABLED": "true",
        "AMOCRM_CLIENT_ID": "crm-client-id-001",
        "AMOCRM_CLIENT_SECRET": "crm-secret-xxxxxxxxxx",
        "AMOCRM_CRM_API_BASE_URL": "https://example.amocrm.ru",
        "AMOCRM_CRM_REDIRECT_URI": "https://example.com/oauth",
        "AMOCRM_CRM_DEAL_CREATE_ENABLED": "true",
        "AMOCRM_CRM_DEAL_PIPELINE_ID": "1001",
        "AMOCRM_CRM_DEAL_STATUS_ID": "2002",
    }
    env.update(extra)
    return env


def test_deal_create_default_off() -> None:
    config = AmoCrmDealCreateConfig.from_env({})
    assert config.enabled is False


def test_deal_create_enabled_missing_pipeline_fail_closed() -> None:
    with pytest.raises(AmoCrmDealCreateConfigError, match="PIPELINE_ID_REQUIRED"):
        AmoCrmDealCreateConfig.from_env(
            {
                "AMOCRM_CRM_REST_ENABLED": "true",
                "AMOCRM_CLIENT_ID": "crm-client-id-001",
                "AMOCRM_CLIENT_SECRET": "crm-secret-xxxxxxxxxx",
                "AMOCRM_CRM_API_BASE_URL": "https://example.amocrm.ru",
                "AMOCRM_CRM_REDIRECT_URI": "https://example.com/oauth",
                "AMOCRM_CRM_DEAL_CREATE_ENABLED": "true",
                "AMOCRM_CRM_DEAL_STATUS_ID": "2002",
            }
        )
    cfg = load_deal_create_config_fail_closed(
        {"AMOCRM_CRM_DEAL_CREATE_ENABLED": "true"}
    )
    assert cfg.enabled is False


@pytest.mark.asyncio
async def test_disabled_ensure_zero_http() -> None:
    transport = _FakeTransport()
    service = TechnicalDealProjectionService(
        session_factory=object(),  # type: ignore[arg-type]
        config=AmoCrmDealCreateConfig(enabled=False),
        transport=transport,
    )
    result = await service.ensure_technical_deal(uuid4())
    assert result.outcome is TechnicalDealOutcome.DISABLED
    assert transport.calls == []


def test_coerce_conversation_uuid_accepts_stdlib_and_subclass() -> None:
    import uuid as uuid_mod

    std = uuid_mod.uuid4()
    assert coerce_conversation_uuid(std) == std
    assert type(coerce_conversation_uuid(std)) is uuid_mod.UUID

    class _DriverUuid(uuid_mod.UUID):
        """Stand-in for asyncpg.pgproto.UUID (subclass, not exact type)."""

    driver = _DriverUuid(str(std))
    assert type(driver) is not uuid_mod.UUID
    coerced = coerce_conversation_uuid(driver)
    assert coerced == std
    assert type(coerced) is uuid_mod.UUID
    assert coerce_conversation_uuid(str(std)) == std


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "not-a-uuid",
        " 11111111-1111-1111-1111-111111111111",
        12345,
        True,
        False,
        b"11111111-1111-1111-1111-111111111111",
        {"id": "x"},
        object(),
    ],
)
def test_coerce_conversation_uuid_rejects_malformed(bad: object) -> None:
    assert coerce_conversation_uuid(bad) is None


@pytest.mark.asyncio
async def test_hostile_uuid_subclass_int_raises_fail_closed_zero_http() -> None:
    import uuid as uuid_mod

    class _HostileUuid(uuid_mod.UUID):
        def __getattribute__(self, name: str) -> object:
            if name == "int":
                raise RuntimeError("hostile-int")
            return super().__getattribute__(name)

    hostile = _HostileUuid("00000000-0000-0000-0000-000000000000")
    assert isinstance(hostile, uuid_mod.UUID)
    assert type(hostile) is not uuid_mod.UUID
    assert coerce_conversation_uuid(hostile) is None

    transport = _FakeTransport()
    service = TechnicalDealProjectionService(
        session_factory=object(),  # type: ignore[arg-type]
        config=AmoCrmDealCreateConfig(
            enabled=True,
            pipeline_id=1,
            status_id=2,
            rest=AmoCrmCrmRestConfig(
                enabled=True,
                client_id="c",
                client_secret="secret12",
                api_base_url="https://example.amocrm.ru",
                redirect_uri="https://example.com/oauth",
            ),
        ),
        transport=transport,
    )
    result = await service.ensure_technical_deal(hostile)
    assert result.outcome is TechnicalDealOutcome.PERMANENT_ERROR
    assert result.error_code == "CONVERSATION_ID_INVALID"
    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [None, "", "not-a-uuid", 12345, True, object()],
)
async def test_ensure_rejects_invalid_conversation_id_fail_closed(bad: object) -> None:
    transport = _FakeTransport()
    service = TechnicalDealProjectionService(
        session_factory=object(),  # type: ignore[arg-type]
        config=AmoCrmDealCreateConfig(
            enabled=True,
            pipeline_id=1,
            status_id=2,
            rest=AmoCrmCrmRestConfig(
                enabled=True,
                client_id="c",
                client_secret="secret12",
                api_base_url="https://example.amocrm.ru",
                redirect_uri="https://example.com/oauth",
            ),
        ),
        transport=transport,
    )
    result = await service.ensure_technical_deal(bad)  # type: ignore[arg-type]
    assert result.outcome is TechnicalDealOutcome.PERMANENT_ERROR
    assert result.error_code == "CONVERSATION_ID_INVALID"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_ensure_accepts_uuid_subclass_without_crm_when_disabled() -> None:
    import uuid as uuid_mod

    class _DriverUuid(uuid_mod.UUID):
        pass

    transport = _FakeTransport()
    service = TechnicalDealProjectionService(
        session_factory=object(),  # type: ignore[arg-type]
        config=AmoCrmDealCreateConfig(enabled=False),
        transport=transport,
    )
    result = await service.ensure_technical_deal(_DriverUuid(str(uuid4())))
    assert result.outcome is TechnicalDealOutcome.DISABLED
    assert transport.calls == []


def test_lead_create_payload_is_v4_shape() -> None:
    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(
            status_code=200,
            headers={},
            body=json.dumps({"_embedded": {"leads": [{"id": 555}]}}).encode(),
        )
    )
    client = AmoCrmLeadHttpClient(
        AmoCrmCrmRestConfig(
            enabled=True,
            client_id="c",
            client_secret="secret12",
            api_base_url="https://example.amocrm.ru",
            redirect_uri="https://example.com/oauth",
        ),
        transport=transport,
    )
    result = client.create_lead(
        name="bot-tv:x",
        pipeline_id=1001,
        status_id=2002,
        access_token="tok",
        contact_id="77",
    )
    assert result.lead_id == "555"
    body = json.loads(transport.calls[0].body.decode())
    assert body == [
        {
            "name": "bot-tv:x",
            "pipeline_id": 1001,
            "status_id": 2002,
            "_embedded": {"contacts": [{"id": 77, "is_main": True}]},
        }
    ]
    assert transport.calls[0].headers["Authorization"] == "Bearer tok"
    assert "tok" not in repr(transport.calls[0])


def test_ambiguous_create_marks_flag() -> None:
    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(status_code=503, headers={}, body=b"unavailable")
    )
    client = AmoCrmLeadHttpClient(
        AmoCrmCrmRestConfig(
            enabled=True,
            client_id="c",
            client_secret="secret12",
            api_base_url="https://example.amocrm.ru",
            redirect_uri="https://example.com/oauth",
        ),
        transport=transport,
    )
    result = client.create_lead(
        name="bot-tv:x",
        pipeline_id=1,
        status_id=2,
        access_token="tok",
    )
    assert result.ambiguous is True


def test_no_contact_create_helper() -> None:
    names = dir(AmoCrmLeadHttpClient)
    assert "create_contact" not in names
    assert "create_lead" in names


def _lead_client(transport: _FakeTransport) -> AmoCrmLeadHttpClient:
    return AmoCrmLeadHttpClient(
        AmoCrmCrmRestConfig(
            enabled=True,
            client_id="c",
            client_secret="secret12",
            api_base_url="https://example.amocrm.ru",
            redirect_uri="https://example.com/oauth",
        ),
        transport=transport,
    )


@pytest.mark.parametrize(
    ("status_code", "outcome", "not_found", "unauthorized"),
    [
        (200, "SUCCESS", False, False),
        (404, "PERMANENT_ERROR", True, False),
        (401, "UNAUTHORIZED", False, True),
        (402, "TRANSIENT_ERROR", False, False),
        (403, "TRANSIENT_ERROR", False, False),
        (429, "TRANSIENT_ERROR", False, False),
        (500, "TRANSIENT_ERROR", False, False),
        (503, "TRANSIENT_ERROR", False, False),
    ],
)
def test_get_lead_http_taxonomy(
    status_code: int,
    outcome: str,
    not_found: bool,
    unauthorized: bool,
) -> None:
    from app.core.amocrm_crm_rest_http import AmoCrmCrmRestOutcome

    transport = _FakeTransport()
    body = b'{"id": 42}' if status_code == 200 else b"{}"
    transport.responses.append(
        S2sHttpResponse(status_code=status_code, headers={}, body=body)
    )
    result = _lead_client(transport).get_lead(lead_id="42", access_token="tok")
    assert result.outcome is AmoCrmCrmRestOutcome(outcome)
    assert result.not_found is not_found
    assert result.unauthorized is unauthorized
    assert result.status_code == status_code
    assert transport.calls[0].url.endswith("/api/v4/leads/42?with=contacts")


@pytest.mark.parametrize(
    ("status_code", "outcome", "unauthorized", "ambiguous"),
    [
        (401, "UNAUTHORIZED", True, False),
        (400, "PERMANENT_ERROR", False, False),
        (404, "PERMANENT_ERROR", False, False),
        (422, "PERMANENT_ERROR", False, False),
        (402, "TRANSIENT_ERROR", False, True),
        (403, "TRANSIENT_ERROR", False, True),
        (429, "TRANSIENT_ERROR", False, True),
        (503, "TRANSIENT_ERROR", False, True),
    ],
)
def test_create_lead_http_taxonomy(
    status_code: int,
    outcome: str,
    unauthorized: bool,
    ambiguous: bool,
) -> None:
    from app.core.amocrm_crm_rest_http import AmoCrmCrmRestOutcome

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(status_code=status_code, headers={}, body=b"{}")
    )
    result = _lead_client(transport).create_lead(
        name="bot-tv:x",
        pipeline_id=1,
        status_id=2,
        access_token="tok",
    )
    assert result.outcome is AmoCrmCrmRestOutcome(outcome)
    assert result.unauthorized is unauthorized
    assert result.ambiguous is ambiguous
    assert result.lead_id is None


def test_deal_paths_allowlisted() -> None:
    assert_canonical_docker_runtime_allowlist()
    for rel in (
        "app/core/amocrm_crm_deal_create_config.py",
        "app/core/amocrm_crm_leads_http.py",
        "app/services/amocrm_technical_deal.py",
        "app/services/amocrm_crm_mirror_adapter.py",
        "alembic/versions/20260813_25_amo_deal_reserve.py",
    ):
        assert is_included_in_docker_build_context(rel, repo_root=_REPO)
        assert (_REPO / rel).is_file()
    for rel in AMO01B2_DOCKER_RUNTIME_PATHS:
        assert is_included_in_docker_build_context(rel, repo_root=_REPO)


def test_chat_hmac_not_in_deal_modules() -> None:
    for rel in (
        "app/core/amocrm_crm_deal_create_config.py",
        "app/core/amocrm_crm_leads_http.py",
        "app/services/amocrm_technical_deal.py",
        "app/services/amocrm_crm_mirror_adapter.py",
    ):
        text = (_REPO / rel).read_text(encoding="utf-8")
        assert "AMOCRM_CHAT_" not in text or "separate" in text.lower()
        assert "channel_secret" not in text
        assert "X-Signature" not in text
