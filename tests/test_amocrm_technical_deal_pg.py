"""AMO-01B2 PostgreSQL: TECHNICAL_DEAL ensure fencing + ambiguity."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_crm_deal_create_config import AmoCrmDealCreateConfig
from app.core.amocrm_crm_oauth_keys import EnvAmoCrmOauthKeyProvider
from app.core.amocrm_crm_oauth_types import KEY_SIZE_BYTES
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.db.session import session_scope
from app.models.amocrm_entity_link import (
    AmocrmEntityKind,
    AmocrmEntityLink,
    AmocrmEntityLinkStatus,
)
from app.models.conversation import Channel
from app.repositories import amocrm_crm_oauth_tokens as oauth_repo
from app.repositories import amocrm_entity_links as entity_links
from app.repositories import conversations as conversation_repo
from app.repositories.amocrm_entity_links import AmocrmEntityLinkStaleLeaseError
from app.services.amocrm_technical_deal import (
    TechnicalDealOutcome,
    TechnicalDealProjectionService,
    coerce_conversation_uuid,
)
from tests.pg_harness import truncate_foundation_tables

_KEY = secrets.token_bytes(KEY_SIZE_BYTES)
_KEY_B64 = base64.urlsafe_b64encode(_KEY).decode("ascii")
_SCOPE = "default"
_PIPELINE = 1001
_STATUS = 2002


def _provider() -> EnvAmoCrmOauthKeyProvider:
    return EnvAmoCrmOauthKeyProvider(
        {
            "AMOCRM_CRM_OAUTH_ACTIVE_KEY_ID": "K1",
            "AMOCRM_CRM_OAUTH_KEY_K1": _KEY_B64,
        }
    )


def _deal_config() -> AmoCrmDealCreateConfig:
    return AmoCrmDealCreateConfig(
        enabled=True,
        pipeline_id=_PIPELINE,
        status_id=_STATUS,
        rest=AmoCrmCrmRestConfig(
            enabled=True,
            client_id="cid",
            client_secret="csecret12",
            api_base_url="https://example.amocrm.ru",
            redirect_uri="https://example.com/oauth",
            connection_scope=_SCOPE,
        ),
    )


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[S2sHttpRequest] = []
        self.responses: list[S2sHttpResponse] = []

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        if not self.responses:
            raise AssertionError(f"no response for {req.method} {req.url}")
        return self.responses.pop(0)


def _http_path(url: str) -> str:
    return urlsplit(url).path


def _install_oauth_refresh_entry_gate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    parties: int = 2,
    timeout_seconds: float = 5.0,
) -> None:
    """Hold both reactive-401 callers until they enter refresh, before any DB session."""

    import app.core.amocrm_crm_rest_http as rest_http

    ready = asyncio.Event()
    arrived = 0
    lock = asyncio.Lock()
    real_refresh = rest_http.AmoCrmCrmRestHttpClient.refresh_tokens

    async def gated_refresh(self, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal arrived
        async with lock:
            arrived += 1
            if arrived >= parties:
                ready.set()
        await asyncio.wait_for(ready.wait(), timeout=timeout_seconds)
        return await real_refresh(self, **kwargs)

    monkeypatch.setattr(
        rest_http.AmoCrmCrmRestHttpClient,
        "refresh_tokens",
        gated_refresh,
    )


@pytest_asyncio.fixture(autouse=True)
async def cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


async def _seed_conversation(session: AsyncSession):
    conversation, _ = await conversation_repo.get_or_create(
        session,
        channel=Channel.SYNTHETIC,
        external_conversation_id=f"ext-{uuid4().hex[:12]}",
    )
    return conversation


async def _seed_oauth(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(session_factory) as session:
        await oauth_repo.upsert_token_pair(
            session,
            access_token="access-live",
            refresh_token="refresh-live",
            key_provider=_provider(),
            connection_scope=_SCOPE,
        )


def _lead_created(lead_id: int = 9001) -> S2sHttpResponse:
    return S2sHttpResponse(
        status_code=200,
        headers={},
        body=json.dumps({"_embedded": {"leads": [{"id": lead_id}]}}).encode(),
    )


def _lead_get(lead_id: int = 9001) -> S2sHttpResponse:
    return S2sHttpResponse(
        status_code=200,
        headers={},
        body=json.dumps({"id": lead_id}).encode(),
    )


@pytest.mark.asyncio
async def test_pg_loaded_conversation_id_is_accepted_by_ensure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression: asyncpg UUID subclass must not trip CONVERSATION_ID_INVALID."""

    await _seed_oauth(session_factory)
    async with session_scope(session_factory) as session:
        conv = await _seed_conversation(session)
        conv_id = conv.id
        # Force a round-trip load so the value is whatever the PG driver returns.
        raw = (
            await session.execute(
                text("SELECT id FROM conversations WHERE id = :id"),
                {"id": conv_id},
            )
        ).scalar_one()

    coerced = coerce_conversation_uuid(raw)
    assert coerced is not None
    assert type(coerced) is uuid.UUID
    # Driver may return a subclass; exact-type check must not gate ensure.
    if type(raw) is not uuid.UUID:
        assert isinstance(raw, uuid.UUID)

    transport = _FakeTransport()
    transport.responses.append(_lead_created(9001))
    service = TechnicalDealProjectionService(
        session_factory=session_factory,
        config=_deal_config(),
        key_provider=_provider(),
        transport=transport,
        worker_id="pg-uuid",
    )
    result = await service.ensure_technical_deal(raw)  # type: ignore[arg-type]
    assert result.outcome is TechnicalDealOutcome.ENSURED
    assert result.external_deal_id == "9001"
    assert result.error_code != "CONVERSATION_ID_INVALID"
    assert len([c for c in transport.calls if c.method == "POST"]) == 1


@pytest.mark.asyncio
async def test_concurrent_creators_one_post_maximum(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_oauth(session_factory)
    async with session_scope(session_factory) as session:
        conv = await _seed_conversation(session)
        conv_id = conv.id

    transport = _FakeTransport()
    # Winner creates; loser may validate ACTIVE via GET.
    transport.responses.append(_lead_created(9001))
    transport.responses.extend([_lead_get(9001), _lead_get(9001), _lead_get(9001)])

    async def _run(worker: str) -> TechnicalDealOutcome:
        service = TechnicalDealProjectionService(
            session_factory=session_factory,
            config=_deal_config(),
            key_provider=_provider(),
            transport=transport,
            worker_id=worker,
        )
        result = await service.ensure_technical_deal(conv_id)
        return result.outcome

    outcomes = await asyncio.gather(_run("w-a"), _run("w-b"))
    post_calls = [c for c in transport.calls if c.method == "POST" and c.url.endswith("/api/v4/leads")]
    assert len(post_calls) == 1
    assert TechnicalDealOutcome.ENSURED in outcomes
    assert set(outcomes) <= {
        TechnicalDealOutcome.ENSURED,
        TechnicalDealOutcome.BUSY,
    }
    async with session_scope(session_factory) as session:
        active = await entity_links.get_active(
            session,
            conversation_id=conv_id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
        )
        assert active is not None
        assert active.external_id == "9001"


@pytest.mark.asyncio
async def test_existing_active_deal_zero_create(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_oauth(session_factory)
    async with session_scope(session_factory) as session:
        conv = await _seed_conversation(session)
        await entity_links.insert_active_if_absent(
            session,
            conversation_id=conv.id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
            external_id="4242",
        )
        conv_id = conv.id

    transport = _FakeTransport()
    transport.responses.append(_lead_get(4242))
    service = TechnicalDealProjectionService(
        session_factory=session_factory,
        config=_deal_config(),
        key_provider=_provider(),
        transport=transport,
        worker_id="reuse",
    )
    result = await service.ensure_technical_deal(conv_id)
    assert result.outcome is TechnicalDealOutcome.ENSURED
    assert result.external_deal_id == "4242"
    assert not any(c.method == "POST" for c in transport.calls)


@pytest.mark.asyncio
async def test_missing_linked_deal_revokes_and_recreates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_oauth(session_factory)
    async with session_scope(session_factory) as session:
        conv = await _seed_conversation(session)
        await entity_links.insert_active_if_absent(
            session,
            conversation_id=conv.id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
            external_id="111",
        )
        conv_id = conv.id

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(status_code=404, headers={}, body=b"{}")
    )
    transport.responses.append(_lead_created(222))
    service = TechnicalDealProjectionService(
        session_factory=session_factory,
        config=_deal_config(),
        key_provider=_provider(),
        transport=transport,
        worker_id="recreate",
    )
    result = await service.ensure_technical_deal(conv_id)
    assert result.outcome is TechnicalDealOutcome.ENSURED
    assert result.external_deal_id == "222"
    async with session_scope(session_factory) as session:
        active = await entity_links.get_active(
            session,
            conversation_id=conv_id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
        )
        assert active is not None
        assert active.external_id == "222"
        revoked = (
            await session.scalars(
                select(AmocrmEntityLink).where(
                    AmocrmEntityLink.conversation_id == conv_id,
                    AmocrmEntityLink.status == AmocrmEntityLinkStatus.REVOKED.value,
                )
            )
        ).all()
        assert any(r.external_id == "111" for r in revoked)


@pytest.mark.asyncio
async def test_ambiguous_create_no_second_post(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_oauth(session_factory)
    async with session_scope(session_factory) as session:
        conv = await _seed_conversation(session)
        conv_id = conv.id

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(status_code=503, headers={}, body=b"boom")
    )
    service = TechnicalDealProjectionService(
        session_factory=session_factory,
        config=_deal_config(),
        key_provider=_provider(),
        transport=transport,
        worker_id="ambig",
    )
    first = await service.ensure_technical_deal(conv_id)
    assert first.outcome is TechnicalDealOutcome.RECONCILE_REQUIRED
    assert sum(1 for c in transport.calls if c.method == "POST") == 1

    second = await service.ensure_technical_deal(conv_id)
    assert second.outcome is TechnicalDealOutcome.RECONCILE_REQUIRED
    assert sum(1 for c in transport.calls if c.method == "POST") == 1


@pytest.mark.asyncio
async def test_stale_reservation_fence_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        conv = await _seed_conversation(session)
        reserved = await entity_links.claim_deal_create_reservation(
            session,
            conversation_id=conv.id,
            worker_id="owner-a",
        )
        conv_id = conv.id
        lease_version = reserved.lease_version

    async with session_scope(session_factory) as session:
        with pytest.raises(AmocrmEntityLinkStaleLeaseError):
            await entity_links.claim_deal_create_reservation(
                session,
                conversation_id=conv_id,
                worker_id="owner-b",
            )

    async with session_scope(session_factory) as session:
        with pytest.raises(AmocrmEntityLinkStaleLeaseError):
            await entity_links.complete_reservation_to_active(
                session,
                reservation=entity_links.DealCreateReservation(
                    link_id=reserved.link_id,
                    conversation_id=conv_id,
                    lease_owner="owner-a",
                    lease_token=uuid4(),
                    lease_version=lease_version,
                    lease_until=reserved.lease_until,
                ),
                external_id="999",
            )


@pytest.mark.asyncio
async def test_handoff_does_not_create_second_deal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_oauth(session_factory)
    async with session_scope(session_factory) as session:
        conv = await _seed_conversation(session)
        conv_id = conv.id

    transport = _FakeTransport()
    transport.responses.append(_lead_created(700))
    transport.responses.append(_lead_get(700))
    service = TechnicalDealProjectionService(
        session_factory=session_factory,
        config=_deal_config(),
        key_provider=_provider(),
        transport=transport,
        worker_id="handoff",
    )
    first = await service.ensure_technical_deal(conv_id)
    assert first.outcome is TechnicalDealOutcome.ENSURED

    async with session_scope(session_factory) as session:
        await session.execute(
            text(
                "UPDATE conversations SET ownership = 'MANAGER', "
                "status = 'HANDOFF', handoff_state = 'HUMAN_ACTIVE', "
                "handoff_deadline_at = statement_timestamp() + interval '1 hour', "
                "manager_takeover_at = statement_timestamp() "
                "WHERE id = :id"
            ),
            {"id": conv_id},
        )

    second = await service.ensure_technical_deal(conv_id)
    assert second.outcome is TechnicalDealOutcome.ENSURED
    assert second.external_deal_id == "700"
    assert sum(1 for c in transport.calls if c.url.endswith("/api/v4/leads") and c.method == "POST") == 1


@pytest.mark.asyncio
async def test_deterministic_contact_linked_absent_skips_contact_create(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_oauth(session_factory)
    async with session_scope(session_factory) as session:
        conv = await _seed_conversation(session)
        await entity_links.insert_active_if_absent(
            session,
            conversation_id=conv.id,
            entity_kind=AmocrmEntityKind.CONTACT,
            external_id="55",
        )
        conv_id = conv.id

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(
            status_code=200,
            headers={},
            body=json.dumps({"id": 55}).encode(),
        )
    )
    transport.responses.append(_lead_created(880))
    service = TechnicalDealProjectionService(
        session_factory=session_factory,
        config=_deal_config(),
        key_provider=_provider(),
        transport=transport,
        worker_id="contact-ok",
    )
    result = await service.ensure_technical_deal(conv_id)
    assert result.outcome is TechnicalDealOutcome.ENSURED
    post = next(c for c in transport.calls if c.method == "POST")
    payload = json.loads(post.body.decode())
    assert payload[0]["_embedded"]["contacts"][0]["id"] == 55
    assert not any("/api/v4/contacts" in c.url and c.method == "POST" for c in transport.calls)


@pytest.mark.asyncio
async def test_absent_contact_creates_deal_without_contact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_oauth(session_factory)
    async with session_scope(session_factory) as session:
        conv = await _seed_conversation(session)
        conv_id = conv.id

    transport = _FakeTransport()
    transport.responses.append(_lead_created(881))
    service = TechnicalDealProjectionService(
        session_factory=session_factory,
        config=_deal_config(),
        key_provider=_provider(),
        transport=transport,
        worker_id="no-contact",
    )
    result = await service.ensure_technical_deal(conv_id)
    assert result.outcome is TechnicalDealOutcome.ENSURED
    payload = json.loads(transport.calls[0].body.decode())
    assert "_embedded" not in payload[0]
    assert not any("contacts" in c.url and c.method == "POST" for c in transport.calls)


def _oauth_refresh_ok() -> S2sHttpResponse:
    return S2sHttpResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=json.dumps(
            {
                "access_token": "access-refreshed",
                "refresh_token": "refresh-refreshed",
                "expires_in": 3600,
            }
        ).encode(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [402, 403, 429, 500, 503])
async def test_existing_deal_transient_get_does_not_revoke(
    session_factory: async_sessionmaker[AsyncSession],
    status_code: int,
) -> None:
    await _seed_oauth(session_factory)
    async with session_scope(session_factory) as session:
        conv = await _seed_conversation(session)
        await entity_links.insert_active_if_absent(
            session,
            conversation_id=conv.id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
            external_id="4242",
        )
        conv_id = conv.id

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(status_code=status_code, headers={}, body=b"{}")
    )
    service = TechnicalDealProjectionService(
        session_factory=session_factory,
        config=_deal_config(),
        key_provider=_provider(),
        transport=transport,
        worker_id="no-revoke",
    )
    result = await service.ensure_technical_deal(conv_id)
    assert result.outcome is TechnicalDealOutcome.TRANSIENT_ERROR
    assert not any(c.method == "POST" for c in transport.calls)
    async with session_scope(session_factory) as session:
        active = await entity_links.get_active(
            session,
            conversation_id=conv_id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
        )
        assert active is not None
        assert active.external_id == "4242"


@pytest.mark.asyncio
async def test_get_401_refreshes_once_and_retries(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_oauth(session_factory)
    async with session_scope(session_factory) as session:
        conv = await _seed_conversation(session)
        await entity_links.insert_active_if_absent(
            session,
            conversation_id=conv.id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
            external_id="4242",
        )
        conv_id = conv.id

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(status_code=401, headers={}, body=b"{}")
    )
    transport.responses.append(_oauth_refresh_ok())
    transport.responses.append(_lead_get(4242))
    service = TechnicalDealProjectionService(
        session_factory=session_factory,
        config=_deal_config(),
        key_provider=_provider(),
        transport=transport,
        worker_id="retry-401",
    )
    result = await service.ensure_technical_deal(conv_id)
    assert result.outcome is TechnicalDealOutcome.ENSURED
    assert [c.method for c in transport.calls] == ["GET", "POST", "GET"]
    assert transport.calls[1].url.endswith("/oauth2/access_token")
    assert not any(
        c.method == "POST" and c.url.endswith("/api/v4/leads") for c in transport.calls
    )


@pytest.mark.asyncio
async def test_existing_contact_attaches_to_deal_without_contact_create(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_oauth(session_factory)
    async with session_scope(session_factory) as session:
        conv = await _seed_conversation(session)
        await entity_links.insert_active_if_absent(
            session,
            conversation_id=conv.id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
            external_id="9001",
        )
        await entity_links.insert_active_if_absent(
            session,
            conversation_id=conv.id,
            entity_kind=AmocrmEntityKind.CONTACT,
            external_id="55",
        )
        conv_id = conv.id

    transport = _FakeTransport()
    transport.responses.append(_lead_get(9001))
    transport.responses.append(
        S2sHttpResponse(status_code=200, headers={}, body=b"[]")
    )
    service = TechnicalDealProjectionService(
        session_factory=session_factory,
        config=_deal_config(),
        key_provider=_provider(),
        transport=transport,
        worker_id="attach-contact",
    )
    result = await service.ensure_technical_deal(conv_id)
    assert result.outcome is TechnicalDealOutcome.ENSURED
    assert any(c.url.endswith("/api/v4/leads/9001/link") for c in transport.calls)
    assert not any(
        "/api/v4/contacts" in c.url and c.method == "POST" for c in transport.calls
    )
    assert not any(
        c.method == "POST" and c.url.endswith("/api/v4/leads") for c in transport.calls
    )


@pytest.mark.asyncio
async def test_concurrent_reactive_401_oauth_refresh_is_fenced(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_oauth_refresh_entry_gate(monkeypatch)
    async with session_scope(session_factory) as session:
        conv = await _seed_conversation(session)
        conv_id = conv.id
        await entity_links.insert_active_if_absent(
            session,
            conversation_id=conv_id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
            external_id="4242",
        )
        await oauth_repo.upsert_token_pair(
            session,
            access_token="access-stale",
            refresh_token="refresh-live",
            key_provider=_provider(),
            connection_scope=_SCOPE,
        )

    transport = _FakeTransport()
    transport.responses.extend(
        [
            S2sHttpResponse(status_code=401, headers={}, body=b"{}"),
            S2sHttpResponse(status_code=401, headers={}, body=b"{}"),
        ]
    )
    transport.responses.append(_oauth_refresh_ok())
    transport.responses.extend([_lead_get(4242), _lead_get(4242)])

    async def _run(worker: str) -> TechnicalDealOutcome:
        service = TechnicalDealProjectionService(
            session_factory=session_factory,
            config=_deal_config(),
            key_provider=_provider(),
            transport=transport,
            worker_id=worker,
        )
        result = await service.ensure_technical_deal(conv_id)
        return result.outcome

    outcomes = await asyncio.gather(_run("oauth-a"), _run("oauth-b"))
    refresh_calls = [
        c for c in transport.calls if _http_path(c.url) == "/oauth2/access_token"
    ]
    deal_calls = [
        c
        for c in transport.calls
        if c.method == "GET" and _http_path(c.url) == "/api/v4/leads/4242"
    ]
    assert len(refresh_calls) == 1
    assert [
        c.headers["Authorization"] for c in deal_calls[:2]
    ] == ["Bearer access-stale", "Bearer access-stale"]
    assert any(
        c.headers["Authorization"] == "Bearer access-refreshed"
        for c in deal_calls[2:]
    )
    assert TechnicalDealOutcome.ENSURED in outcomes
    assert all(
        outcome
        in {
            TechnicalDealOutcome.ENSURED,
            TechnicalDealOutcome.TRANSIENT_ERROR,
        }
        for outcome in outcomes
    )
    assert not any(
        c.method == "POST" and _http_path(c.url) == "/api/v4/leads"
        for c in transport.calls
    )
    async with session_scope(session_factory) as session:
        active = await entity_links.get_active(
            session,
            conversation_id=conv_id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
        )
        assert active is not None
        assert active.external_id == "4242"
