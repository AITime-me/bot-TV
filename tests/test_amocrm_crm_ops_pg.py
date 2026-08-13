"""AMO-01B2-OPS PostgreSQL: OAuth bootstrap/reseed fencing + reconcile GET."""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_crm_oauth_keys import EnvAmoCrmOauthKeyProvider
from app.core.amocrm_crm_oauth_types import KEY_SIZE_BYTES
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.db.session import session_scope
from app.models.amocrm_crm_oauth_token import AmocrmCrmOauthToken
from app.models.amocrm_entity_link import (
    AmocrmEntityKind,
    AmocrmEntityLinkStatus,
)
from app.models.conversation import Channel
from app.repositories import amocrm_crm_oauth_tokens as oauth_repo
from app.repositories import amocrm_entity_links as entity_links
from app.repositories import conversations as conversation_repo
from app.repositories.amocrm_entity_links import AmocrmEntityLinkConflictError
from app.services.amocrm_crm_ops import AmoCrmCrmOpsService, AmoCrmOpsOutcome
from tests.pg_harness import truncate_foundation_tables

_KEY = secrets.token_bytes(KEY_SIZE_BYTES)
_KEY_B64 = base64.urlsafe_b64encode(_KEY).decode("ascii")
_SCOPE = "default"


def _provider() -> EnvAmoCrmOauthKeyProvider:
    return EnvAmoCrmOauthKeyProvider(
        {
            "AMOCRM_CRM_OAUTH_ACTIVE_KEY_ID": "K1",
            "AMOCRM_CRM_OAUTH_KEY_K1": _KEY_B64,
        }
    )


def _rest() -> AmoCrmCrmRestConfig:
    return AmoCrmCrmRestConfig(
        enabled=True,
        client_id="cid",
        client_secret="csecret12",
        api_base_url="https://example.amocrm.ru",
        connection_scope=_SCOPE,
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


@pytest_asyncio.fixture(autouse=True)
async def cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    transport: _FakeTransport | None = None,
    *,
    worker_id: str = "ops-1",
) -> AmoCrmCrmOpsService:
    return AmoCrmCrmOpsService(
        session_factory,
        key_provider=_provider(),
        rest_config=_rest(),
        transport=transport or _FakeTransport(),
        worker_id=worker_id,
        connection_scope=_SCOPE,
    )


async def _seed_reconcile(
    session_factory: async_sessionmaker[AsyncSession],
) -> object:
    async with session_scope(session_factory) as session:
        conv, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.SYNTHETIC,
            external_conversation_id=f"ext-{uuid4().hex[:12]}",
        )
        conv_id = conv.id
        reserved = await entity_links.claim_deal_create_reservation(
            session, conversation_id=conv_id, worker_id="w"
        )
        await entity_links.mark_create_submitted(session, reservation=reserved)
        await entity_links.mark_reservation_reconcile_required(
            session, reservation=reserved
        )
        return conv_id


@pytest.mark.asyncio
async def test_bootstrap_inserts_encrypted_and_refuses_second(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(session_factory)
    first = await service.bootstrap_oauth(
        access_token="access-plain-AAA",
        refresh_token="refresh-plain-BBB",
    )
    assert first.outcome is AmoCrmOpsOutcome.SEEDED

    async with session_factory() as session:
        async with session.begin():
            row = (await session.scalars(select(AmocrmCrmOauthToken))).one()
            assert b"access-plain-AAA" not in row.access_ciphertext
            assert b"refresh-plain-BBB" not in row.refresh_ciphertext
            raw = (
                await session.execute(
                    text(
                        "SELECT encode(access_ciphertext, 'escape'), "
                        "encode(refresh_ciphertext, 'escape') "
                        "FROM amocrm_crm_oauth_tokens WHERE id = :id"
                    ),
                    {"id": row.id},
                )
            ).one()
            joined = f"{raw[0]}|{raw[1]}"
            assert "access-plain-AAA" not in joined
            assert "refresh-plain-BBB" not in joined
            tokens = oauth_repo.decrypt_row(row, key_provider=_provider())
            assert tokens.access_token == "access-plain-AAA"
            assert tokens.refresh_token == "refresh-plain-BBB"

    second = await service.bootstrap_oauth(
        access_token="access-other",
        refresh_token="refresh-other",
    )
    assert second.outcome is AmoCrmOpsOutcome.ALREADY_PRESENT
    async with session_scope(session_factory) as session:
        row = await oauth_repo.get_by_scope(session, connection_scope=_SCOPE)
        assert row is not None
        tokens = oauth_repo.decrypt_row(row, key_provider=_provider())
        assert tokens.access_token == "access-plain-AAA"


@pytest.mark.asyncio
async def test_reseed_under_lease_and_stale_refuse(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service_a = _service(session_factory, worker_id="ops-a")
    seeded = await service_a.bootstrap_oauth(
        access_token="access-1",
        refresh_token="refresh-1",
    )
    assert seeded.outcome is AmoCrmOpsOutcome.SEEDED

    async with session_scope(session_factory) as session:
        lease = await oauth_repo.claim_refresh_lease(
            session,
            worker_id="worker-refresh",
            connection_scope=_SCOPE,
        )
        assert lease.lease_owner == "worker-refresh"

    refused = await service_a.reseed_oauth(
        access_token="access-2",
        refresh_token="refresh-2",
    )
    assert refused.outcome is AmoCrmOpsOutcome.REFUSED
    assert refused.error_code == "AMOCRM_CRM_OAUTH_STALE_LEASE"

    async with session_scope(session_factory) as session:
        await oauth_repo.release_refresh_lease(session, lease=lease)

    ok = await service_a.reseed_oauth(
        access_token="access-2",
        refresh_token="refresh-2",
    )
    assert ok.outcome is AmoCrmOpsOutcome.RESEEDED
    async with session_scope(session_factory) as session:
        row = await oauth_repo.get_by_scope(session, connection_scope=_SCOPE)
        assert row is not None
        tokens = oauth_repo.decrypt_row(row, key_provider=_provider())
        assert tokens.access_token == "access-2"
        assert tokens.refresh_token == "refresh-2"
        assert row.lease_token is None


@pytest.mark.asyncio
async def test_reseed_without_row_refuses(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(session_factory)
    result = await service.reseed_oauth(
        access_token="access-x",
        refresh_token="refresh-x",
    )
    assert result.outcome is AmoCrmOpsOutcome.REFUSED
    assert result.error_code == "AMOCRM_CRM_OAUTH_NOT_FOUND"


@pytest.mark.asyncio
async def test_resolve_reconcile_get_success_activates_no_post(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _service(session_factory).bootstrap_oauth(
        access_token="access-live",
        refresh_token="refresh-live",
    )
    conv_id = await _seed_reconcile(session_factory)

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(
            status_code=200,
            headers={},
            body=json.dumps({"id": 4242}).encode(),
        )
    )
    result = await _service(session_factory, transport).resolve_reconcile(
        conversation_id=conv_id,
        confirmed_deal_id="4242",
    )
    assert result.outcome is AmoCrmOpsOutcome.RECONCILE_ACTIVATED
    assert all(c.method == "GET" for c in transport.calls)
    assert not any(
        c.url.endswith("/api/v4/leads") and c.method == "POST" for c in transport.calls
    )

    async with session_scope(session_factory) as session:
        active = await entity_links.get_active(
            session,
            conversation_id=conv_id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
        )
        assert active is not None
        assert active.external_id == "4242"
        assert active.create_submitted_at is None
        assert active.lease_token is None


@pytest.mark.asyncio
async def test_resolve_get_404_leaves_reconcile_no_create(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _service(session_factory).bootstrap_oauth(
        access_token="access-live",
        refresh_token="refresh-live",
    )
    conv_id = await _seed_reconcile(session_factory)

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(status_code=404, headers={}, body=b"{}")
    )
    result = await _service(session_factory, transport).resolve_reconcile(
        conversation_id=conv_id,
        confirmed_deal_id="999",
    )
    assert result.outcome is AmoCrmOpsOutcome.REFUSED
    assert result.error_code == "AMOCRM_CRM_LEAD_NOT_FOUND"
    assert not any(c.method == "POST" for c in transport.calls)
    async with session_scope(session_factory) as session:
        open_row = await entity_links.get_open(
            session,
            conversation_id=conv_id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
        )
        assert open_row is not None
        assert open_row.status == AmocrmEntityLinkStatus.RECONCILE_REQUIRED.value


@pytest.mark.asyncio
async def test_resolve_get_503_leaves_reconcile_no_create(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _service(session_factory).bootstrap_oauth(
        access_token="access-live",
        refresh_token="refresh-live",
    )
    conv_id = await _seed_reconcile(session_factory)

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(status_code=503, headers={}, body=b"unavailable")
    )
    result = await _service(session_factory, transport).resolve_reconcile(
        conversation_id=conv_id,
        confirmed_deal_id="4242",
    )
    assert result.outcome is AmoCrmOpsOutcome.TRANSIENT_ERROR
    assert result.error_code == "AMOCRM_CRM_HTTP_503"
    assert all(c.method == "GET" for c in transport.calls)
    assert not any(
        c.url.endswith("/api/v4/leads") and c.method == "POST" for c in transport.calls
    )
    assert not any(c.method == "POST" for c in transport.calls)
    async with session_scope(session_factory) as session:
        open_row = await entity_links.get_open(
            session,
            conversation_id=conv_id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
        )
        assert open_row is not None
        assert open_row.status == AmocrmEntityLinkStatus.RECONCILE_REQUIRED.value
        assert open_row.external_id is None


@pytest.mark.asyncio
async def test_resolve_invalid_get_body_leaves_reconcile_no_create(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _service(session_factory).bootstrap_oauth(
        access_token="access-live",
        refresh_token="refresh-live",
    )
    conv_id = await _seed_reconcile(session_factory)

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(
            status_code=200,
            headers={},
            body=b'{"id":"not-an-int"}',
        )
    )
    result = await _service(session_factory, transport).resolve_reconcile(
        conversation_id=conv_id,
        confirmed_deal_id="4242",
    )
    assert result.outcome is AmoCrmOpsOutcome.TRANSIENT_ERROR
    assert result.error_code == "AMOCRM_CRM_LEAD_RESPONSE_INVALID"
    assert all(c.method == "GET" for c in transport.calls)
    assert not any(c.method == "POST" for c in transport.calls)
    async with session_scope(session_factory) as session:
        open_row = await entity_links.get_open(
            session,
            conversation_id=conv_id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
        )
        assert open_row is not None
        assert open_row.status == AmocrmEntityLinkStatus.RECONCILE_REQUIRED.value


@pytest.mark.asyncio
async def test_bootstrap_disabled_crm_uses_explicit_connection_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scope = "ops-custom-scope"
    service = AmoCrmCrmOpsService(
        session_factory,
        key_provider=_provider(),
        rest_config=AmoCrmCrmRestConfig(enabled=False, connection_scope=scope),
        worker_id="ops-scope",
    )
    assert service.connection_scope == scope
    seeded = await service.bootstrap_oauth(
        access_token="access-scoped",
        refresh_token="refresh-scoped",
    )
    assert seeded.outcome is AmoCrmOpsOutcome.SEEDED
    async with session_scope(session_factory) as session:
        row = await oauth_repo.get_by_scope(session, connection_scope=scope)
        assert row is not None
        tokens = oauth_repo.decrypt_row(row, key_provider=_provider())
        assert tokens.access_token == "access-scoped"
        default_row = await oauth_repo.get_by_scope(
            session, connection_scope=_SCOPE
        )
        assert default_row is None


@pytest.mark.asyncio
async def test_resolve_conflicting_active_external_fail_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _service(session_factory).bootstrap_oauth(
        access_token="access-live",
        refresh_token="refresh-live",
    )
    async with session_scope(session_factory) as session:
        other, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.SYNTHETIC,
            external_conversation_id=f"ext-{uuid4().hex[:12]}",
        )
        await entity_links.insert_active_if_absent(
            session,
            conversation_id=other.id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
            external_id="4242",
        )
    conv_id = await _seed_reconcile(session_factory)

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(
            status_code=200,
            headers={},
            body=json.dumps({"id": 4242}).encode(),
        )
    )
    result = await _service(session_factory, transport).resolve_reconcile(
        conversation_id=conv_id,
        confirmed_deal_id="4242",
    )
    assert result.outcome is AmoCrmOpsOutcome.REFUSED
    assert result.error_code == "ENTITY_LINK_EXTERNAL_ACTIVE_CONFLICT"
    async with session_scope(session_factory) as session:
        open_row = await entity_links.get_open(
            session,
            conversation_id=conv_id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
        )
        assert open_row is not None
        assert open_row.status == AmocrmEntityLinkStatus.RECONCILE_REQUIRED.value


@pytest.mark.asyncio
async def test_activate_reconcile_repo_requires_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        conv, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.SYNTHETIC,
            external_conversation_id=f"ext-{uuid4().hex[:12]}",
        )
        await entity_links.insert_active_if_absent(
            session,
            conversation_id=conv.id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
            external_id="1",
        )
        with pytest.raises(AmocrmEntityLinkConflictError):
            await entity_links.activate_reconcile_required(
                session,
                conversation_id=conv.id,
                external_id="2",
            )
