"""AMO-01B2 PostgreSQL: encrypted OAuth store, refresh fencing, entity links."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_crm_oauth_keys import EnvAmoCrmOauthKeyProvider
from app.core.amocrm_crm_oauth_types import KEY_SIZE_BYTES, AmoCrmCrmOauthError
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.amocrm_crm_rest_http import (
    AmoCrmCrmRestHttpClient,
    AmoCrmCrmRestOutcome,
)
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.db.session import session_scope
from app.models.amocrm_crm_oauth_token import AmocrmCrmOauthToken
from app.models.amocrm_entity_link import (
    AmocrmEntityKind,
    AmocrmEntityLink,
    AmocrmEntityLinkStatus,
)
from app.models.conversation import Channel, Conversation
from app.repositories import amocrm_crm_oauth_tokens as oauth_repo
from app.repositories import amocrm_entity_links as entity_links
from app.repositories import conversations as conversation_repo
from app.repositories.amocrm_entity_links import AmocrmEntityLinkConflictError
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


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[S2sHttpRequest] = []
        self.responses: list[S2sHttpResponse] = []

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        if not self.responses:
            raise AssertionError("no fake response queued")
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


async def _seed_conversation(
    session: AsyncSession,
) -> Conversation:
    conversation, _ = await conversation_repo.get_or_create(
        session,
        channel=Channel.SYNTHETIC,
        external_conversation_id=f"ext-{uuid4().hex[:12]}",
    )
    return conversation


@pytest.mark.asyncio
async def test_encrypted_token_persistence_at_rest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = _provider()
    access = "access-plain-AAA"
    refresh = "refresh-plain-BBB"
    async with session_scope(session_factory) as session:
        row = await oauth_repo.upsert_token_pair(
            session,
            access_token=access,
            refresh_token=refresh,
            key_provider=provider,
            connection_scope=_SCOPE,
        )
        row_id = row.id

    async with session_factory() as session:
        async with session.begin():
            stored = await session.get(AmocrmCrmOauthToken, row_id)
            assert stored is not None
            assert access.encode("utf-8") not in stored.access_ciphertext
            assert refresh.encode("utf-8") not in stored.refresh_ciphertext
            raw = (
                await session.execute(
                    text(
                        "SELECT encode(access_ciphertext, 'escape'), "
                        "encode(refresh_ciphertext, 'escape') "
                        "FROM amocrm_crm_oauth_tokens WHERE id = :id"
                    ),
                    {"id": row_id},
                )
            ).one()
            joined = f"{raw[0]}|{raw[1]}"
            assert access not in joined
            assert refresh not in joined
            decrypted = oauth_repo.decrypt_row(stored, key_provider=provider)
            assert decrypted.access_token == access
            assert decrypted.refresh_token == refresh


@pytest.mark.asyncio
async def test_concurrent_refresh_fencing_rejects_stale_lease(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = _provider()
    async with session_scope(session_factory) as session:
        await oauth_repo.upsert_token_pair(
            session,
            access_token="access-1",
            refresh_token="refresh-1",
            key_provider=provider,
            connection_scope=_SCOPE,
        )

    async with session_scope(session_factory) as session:
        lease_a = await oauth_repo.claim_refresh_lease(
            session,
            worker_id="worker-a",
            connection_scope=_SCOPE,
        )

    async with session_scope(session_factory) as session:
        with pytest.raises(AmoCrmCrmOauthError, match="STALE_LEASE"):
            await oauth_repo.claim_refresh_lease(
                session,
                worker_id="worker-b",
                connection_scope=_SCOPE,
            )

    async with session_scope(session_factory) as session:
        with pytest.raises(AmoCrmCrmOauthError, match="STALE_LEASE"):
            await oauth_repo.rotate_tokens_with_lease(
                session,
                lease=oauth_repo.OauthRefreshLease(
                    token_row_id=lease_a.token_row_id,
                    connection_scope=_SCOPE,
                    lease_owner="worker-a",
                    lease_token=uuid4(),
                    lease_version=lease_a.lease_version,
                    lease_until=lease_a.lease_until,
                ),
                access_token="access-stale",
                refresh_token="refresh-stale",
                key_provider=provider,
            )

    async with session_scope(session_factory) as session:
        await oauth_repo.rotate_tokens_with_lease(
            session,
            lease=lease_a,
            access_token="access-2",
            refresh_token="refresh-2",
            key_provider=provider,
        )
        row = await oauth_repo.get_by_scope(session, connection_scope=_SCOPE)
        assert row is not None
        tokens = oauth_repo.decrypt_row(row, key_provider=provider)
        assert tokens.access_token == "access-2"
        assert tokens.refresh_token == "refresh-2"
        assert row.lease_token is None


@pytest.mark.asyncio
async def test_rotated_pair_replaces_old_atomically(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = _provider()
    async with session_scope(session_factory) as session:
        await oauth_repo.upsert_token_pair(
            session,
            access_token="old-access",
            refresh_token="old-refresh",
            key_provider=provider,
            connection_scope=_SCOPE,
        )
        lease = await oauth_repo.claim_refresh_lease(
            session,
            worker_id="rotator",
            connection_scope=_SCOPE,
        )
        await oauth_repo.rotate_tokens_with_lease(
            session,
            lease=lease,
            access_token="new-access",
            refresh_token="new-refresh",
            key_provider=provider,
            access_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        rows = (
            await session.scalars(select(AmocrmCrmOauthToken))
        ).all()
        assert len(rows) == 1
        tokens = oauth_repo.decrypt_row(rows[0], key_provider=provider)
        assert tokens.access_token == "new-access"
        assert tokens.refresh_token == "new-refresh"
        assert "old-access" not in tokens.access_token


@pytest.mark.asyncio
async def test_http_refresh_uses_lease_and_fake_transport(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = _provider()
    async with session_scope(session_factory) as session:
        await oauth_repo.upsert_token_pair(
            session,
            access_token="access-before",
            refresh_token="refresh-before",
            key_provider=provider,
            connection_scope=_SCOPE,
        )

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "access_token": "access-after",
                    "refresh_token": "refresh-after",
                    "expires_in": 3600,
                }
            ).encode("utf-8"),
        )
    )
    client = AmoCrmCrmRestHttpClient(
        AmoCrmCrmRestConfig(
            enabled=True,
            client_id="cid",
            client_secret="csecret12",
            api_base_url="https://example.amocrm.ru",
            redirect_uri="https://example.com/oauth",
            connection_scope=_SCOPE,
        ),
        session_factory=session_factory,
        key_provider=provider,
        transport=transport,
        worker_id="crm-http-1",
    )
    result = await client.refresh_tokens()
    assert result.outcome is AmoCrmCrmRestOutcome.SUCCESS
    assert len(transport.calls) == 1
    assert transport.calls[0].url.endswith("/oauth2/access_token")
    body = transport.calls[0].body.decode("utf-8")
    assert "refresh-before" in body
    payload = json.loads(body)
    assert payload["redirect_uri"] == "https://example.com/oauth"
    assert "csecret12" not in repr(transport.calls[0])

    async with session_scope(session_factory) as session:
        row = await oauth_repo.get_by_scope(session, connection_scope=_SCOPE)
        assert row is not None
        tokens = oauth_repo.decrypt_row(row, key_provider=provider)
        assert tokens.access_token == "access-after"
        assert tokens.refresh_token == "refresh-after"


@pytest.mark.asyncio
async def test_post_200_recover_when_refresh_lease_expired(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """HTTP 200 then expired lease: guarded recovery persists without second POST."""

    provider = _provider()
    async with session_scope(session_factory) as session:
        await oauth_repo.upsert_token_pair(
            session,
            access_token="access-before",
            refresh_token="refresh-before",
            key_provider=provider,
            connection_scope=_SCOPE,
        )

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "access_token": "access-recovered",
                    "refresh_token": "refresh-recovered",
                    "expires_in": 3600,
                }
            ).encode("utf-8"),
        )
    )

    client = AmoCrmCrmRestHttpClient(
        AmoCrmCrmRestConfig(
            enabled=True,
            client_id="cid",
            client_secret="csecret12",
            api_base_url="https://example.amocrm.ru",
            redirect_uri="https://example.com/oauth",
            connection_scope=_SCOPE,
        ),
        session_factory=session_factory,
        key_provider=provider,
        transport=transport,
        worker_id="crm-recover-1",
    )

    original_persist = client._persist_rotated_tokens_after_200

    async def _persist_after_expire(**kwargs):  # type: ignore[no-untyped-def]
        async with session_scope(session_factory) as session:
            await session.execute(
                text(
                    "UPDATE amocrm_crm_oauth_tokens "
                    "SET lease_until = statement_timestamp() - interval '5 seconds' "
                    "WHERE connection_scope = :scope"
                ),
                {"scope": _SCOPE},
            )
        return await original_persist(**kwargs)

    client._persist_rotated_tokens_after_200 = _persist_after_expire  # type: ignore[method-assign]

    result = await client.refresh_tokens()
    assert result.outcome is AmoCrmCrmRestOutcome.SUCCESS
    assert len(transport.calls) == 1
    assert result.error_code is None
    async with session_scope(session_factory) as session:
        row = await oauth_repo.get_by_scope(session, connection_scope=_SCOPE)
        assert row is not None
        tokens = oauth_repo.decrypt_row(row, key_provider=provider)
        assert tokens.refresh_token == "refresh-recovered"
        assert tokens.access_token == "access-recovered"
        assert row.lease_token is None


@pytest.mark.asyncio
async def test_post_200_superseded_when_pre_refresh_no_longer_in_db(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = _provider()
    async with session_scope(session_factory) as session:
        await oauth_repo.upsert_token_pair(
            session,
            access_token="access-before",
            refresh_token="refresh-before",
            key_provider=provider,
            connection_scope=_SCOPE,
        )

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(
            status_code=200,
            headers={},
            body=json.dumps(
                {
                    "access_token": "access-orphan",
                    "refresh_token": "refresh-orphan",
                    "expires_in": 3600,
                }
            ).encode("utf-8"),
        )
    )
    client = AmoCrmCrmRestHttpClient(
        AmoCrmCrmRestConfig(
            enabled=True,
            client_id="cid",
            client_secret="csecret12",
            api_base_url="https://example.amocrm.ru",
            redirect_uri="https://example.com/oauth",
            connection_scope=_SCOPE,
        ),
        session_factory=session_factory,
        key_provider=provider,
        transport=transport,
        worker_id="crm-super-1",
    )
    original_persist = client._persist_rotated_tokens_after_200

    async def _persist_after_reseed(**kwargs):  # type: ignore[no-untyped-def]
        async with session_scope(session_factory) as session:
            await oauth_repo.upsert_token_pair(
                session,
                access_token="access-reseed",
                refresh_token="refresh-reseed",
                key_provider=provider,
                connection_scope=_SCOPE,
            )
        return await original_persist(**kwargs)

    client._persist_rotated_tokens_after_200 = _persist_after_reseed  # type: ignore[method-assign]
    result = await client.refresh_tokens()
    assert result.outcome is AmoCrmCrmRestOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_OAUTH_ROTATE_SUPERSEDED"
    assert len(transport.calls) == 1
    async with session_scope(session_factory) as session:
        row = await oauth_repo.get_by_scope(session, connection_scope=_SCOPE)
        assert row is not None
        tokens = oauth_repo.decrypt_row(row, key_provider=provider)
        assert tokens.refresh_token == "refresh-reseed"
        assert "refresh-orphan" not in tokens.refresh_token


@pytest.mark.asyncio
async def test_entity_link_uniqueness_revoke_rebind(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        conv_a = await _seed_conversation(session)
        conv_b = await _seed_conversation(session)
        link, created = await entity_links.insert_active_if_absent(
            session,
            conversation_id=conv_a.id,
            entity_kind=AmocrmEntityKind.CONTACT,
            external_id="contact-100",
        )
        assert created is True
        again, created2 = await entity_links.insert_active_if_absent(
            session,
            conversation_id=conv_a.id,
            entity_kind=AmocrmEntityKind.CONTACT,
            external_id="contact-100",
        )
        assert created2 is False
        assert again.id == link.id

        with pytest.raises(AmocrmEntityLinkConflictError):
            await entity_links.insert_active_if_absent(
                session,
                conversation_id=conv_a.id,
                entity_kind=AmocrmEntityKind.CONTACT,
                external_id="contact-200",
            )

        with pytest.raises(AmocrmEntityLinkConflictError):
            await entity_links.insert_active_if_absent(
                session,
                conversation_id=conv_b.id,
                entity_kind=AmocrmEntityKind.CONTACT,
                external_id="contact-100",
            )

        revoked = await entity_links.revoke_active(
            session,
            conversation_id=conv_a.id,
            entity_kind=AmocrmEntityKind.CONTACT,
        )
        assert revoked is not None
        assert revoked.status == AmocrmEntityLinkStatus.REVOKED.value
        assert (
            await entity_links.get_active(
                session,
                conversation_id=conv_a.id,
                entity_kind=AmocrmEntityKind.CONTACT,
            )
            is None
        )

        rebound = await entity_links.rebind_active(
            session,
            conversation_id=conv_a.id,
            entity_kind=AmocrmEntityKind.CONTACT,
            external_id="contact-300",
        )
        assert rebound.status == AmocrmEntityLinkStatus.ACTIVE.value
        assert rebound.external_id == "contact-300"

        deal, deal_created = await entity_links.insert_active_if_absent(
            session,
            conversation_id=conv_a.id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
            external_id="deal-9",
        )
        assert deal_created is True
        assert deal.entity_kind == AmocrmEntityKind.TECHNICAL_DEAL.value

        rows = (
            await session.scalars(
                select(AmocrmEntityLink).where(
                    AmocrmEntityLink.conversation_id == conv_a.id
                )
            )
        ).all()
        assert len(rows) == 3  # revoked contact + active contact + deal


@pytest.mark.asyncio
async def test_concurrent_entity_link_insert_one_wins(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        conv = await _seed_conversation(session)
        conv_id = conv.id

    async def _attempt() -> str:
        try:
            async with session_scope(session_factory) as session:
                await entity_links.insert_active_if_absent(
                    session,
                    conversation_id=conv_id,
                    entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
                    external_id="deal-same",
                )
            return "ok"
        except AmocrmEntityLinkConflictError:
            return "conflict"

    results = await asyncio.gather(_attempt(), _attempt())
    assert "ok" in results
    async with session_scope(session_factory) as session:
        active_rows = (
            await session.scalars(
                select(AmocrmEntityLink).where(
                    AmocrmEntityLink.entity_kind
                    == AmocrmEntityKind.TECHNICAL_DEAL.value,
                    AmocrmEntityLink.status == AmocrmEntityLinkStatus.ACTIVE.value,
                    AmocrmEntityLink.external_id == "deal-same",
                )
            )
        ).all()
        assert len(active_rows) == 1
        assert active_rows[0].conversation_id == conv_id
