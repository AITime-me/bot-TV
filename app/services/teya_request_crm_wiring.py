"""Compose production CRM adapters for TeyaRequestOrchestrator (fail-closed).

Never sends outbound messages. Uses existing identity lookup + deal discovery
+ AmoCrmCrmWritesHttpClient. Technical deals are never treated as business.
Business pipeline/status/manager/task come from env config only.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_crm_business_write_config import (
    AmoCrmBusinessWriteConfigError,
    load_business_write_config_fail_closed,
)
from app.core.amocrm_crm_oauth_keys import EnvAmoCrmOauthKeyProvider
from app.core.amocrm_crm_rest_http import (
    AmoCrmCrmRestHttpClient,
    AmoCrmCrmRestOutcome,
    _CrmHttpStdlibTransport,
)
from app.core.amocrm_crm_writes_http import AmoCrmCrmWritesHttpClient
from app.core.amocrm_identity_lookup import AmoCrmIdentityLookupResult
from app.db.session import session_scope
from app.repositories import amocrm_crm_oauth_tokens as oauth_repo
from app.repositories import amocrm_entity_links as entity_links
from app.services.amocrm_deal_discovery import AmoCrmDealDiscoveryService
from app.services.amocrm_identity_lookup import AmoCrmIdentityLookupService
from app.services.teya_request_crm import TeyaRequestCrmService


class _IdentityLookupAdapter:
    def __init__(self, service: AmoCrmIdentityLookupService) -> None:
        self._service = service

    async def lookup_by_phone(
        self, *, phone_e164: str
    ) -> AmoCrmIdentityLookupResult:
        return await self._service.lookup_contact_by_phone(phone=phone_e164)


class _DealDiscoveryAdapter:
    def __init__(self, service: AmoCrmDealDiscoveryService) -> None:
        self._service = service

    async def discover_deal_candidates(
        self,
        *,
        contact_id: str,
        known_technical_deal_ids: tuple[str, ...] = (),
    ):
        return await self._service.discover_deal_candidates(
            contact_id=contact_id,
            known_technical_deal_ids=known_technical_deal_ids,
        )


class _TechnicalDealIdsAdapter:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_active_technical_deal_ids(self) -> tuple[str, ...]:
        async with session_scope(self._session_factory) as session:
            return await entity_links.list_active_technical_deal_external_ids(
                session
            )


class _OauthTokenAdapter:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        connection_scope: str,
        key_provider: EnvAmoCrmOauthKeyProvider,
        oauth: AmoCrmCrmRestHttpClient,
    ) -> None:
        self._session_factory = session_factory
        self._connection_scope = connection_scope
        self._key_provider = key_provider
        self._oauth = oauth

    async def access_token(self) -> str | None:
        async with session_scope(self._session_factory) as session:
            row = await oauth_repo.get_by_scope(
                session,
                connection_scope=self._connection_scope,
            )
            if row is None:
                return None
            tokens = oauth_repo.decrypt_row(row, key_provider=self._key_provider)
            return tokens.access_token

    async def refresh_access_token(self) -> str | None:
        """One bounded OAuth refresh via existing token-store fencing."""

        refreshed = await self._oauth.refresh_tokens()
        if refreshed.outcome is not AmoCrmCrmRestOutcome.SUCCESS:
            return None
        return await self.access_token()


def build_teya_request_crm_service(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    worker_id: str,
) -> TeyaRequestCrmService | None:
    """Return CRM service when business-write + CRM REST are enabled; else None."""

    business = load_business_write_config_fail_closed()
    if not business.enabled or business.rest is None:
        return None
    try:
        business.require_runtime()
    except AmoCrmBusinessWriteConfigError:
        return None

    assert business.pipeline_id is not None
    assert business.open_status_id is not None
    assert business.manager_id is not None
    assert business.task_type_id is not None

    config = business.rest
    transport = _CrmHttpStdlibTransport()
    key_provider = EnvAmoCrmOauthKeyProvider()
    oauth = AmoCrmCrmRestHttpClient(
        config,
        session_factory=session_factory,
        key_provider=key_provider,
        transport=transport,
        worker_id=f"{worker_id}-teya-crm",
    )
    identity = AmoCrmIdentityLookupService(
        session_factory=session_factory,
        config=config,
        key_provider=key_provider,
        transport=transport,
        oauth=oauth,
        worker_id=f"{worker_id}-teya-id",
    )
    deals = AmoCrmDealDiscoveryService(
        session_factory=session_factory,
        config=config,
        key_provider=key_provider,
        transport=transport,
        oauth=oauth,
        worker_id=f"{worker_id}-teya-deal",
    )
    writes = AmoCrmCrmWritesHttpClient(
        config,
        transport=transport,
        pipeline_id=business.pipeline_id,
        open_status_id=business.open_status_id,
        manager_id=business.manager_id,
        task_type_id=business.task_type_id,
    )
    return TeyaRequestCrmService(
        identity_lookup=_IdentityLookupAdapter(identity),
        deal_discovery=_DealDiscoveryAdapter(deals),
        writes=writes,
        tokens=_OauthTokenAdapter(
            session_factory=session_factory,
            connection_scope=config.connection_scope,
            key_provider=key_provider,
            oauth=oauth,
        ),
        technical_deal_ids=_TechnicalDealIdsAdapter(session_factory),
    )
