"""Read-only amoCRM contact identity lookup (IR-2).

Live GET adapter for Identity Resolver. No CRM entity writes, no attach,
no webhook/worker wiring. OAuth refresh reuses existing token-store fencing.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_crm_contacts_http import (
    AmoCrmContactRecord,
    AmoCrmContactsHttpClient,
    contact_has_exact_phone,
)
from app.core.amocrm_crm_oauth_keys import (
    AmoCrmOauthKeyProvider,
    EnvAmoCrmOauthKeyProvider,
)
from app.core.amocrm_crm_rest_config import (
    AmoCrmCrmRestConfig,
    load_crm_rest_config_fail_closed,
)
from app.core.amocrm_crm_rest_http import (
    AmoCrmCrmRestHttpClient,
    AmoCrmCrmRestOutcome,
    AmoCrmCrmRestTransport,
    _CrmHttpStdlibTransport,
)
from app.core.amocrm_identity_lookup import (
    AMOCRM_IDENTITY_PROVIDER,
    AmoCrmIdentityLookupOutcome,
    AmoCrmIdentityLookupResult,
)
from app.core.identity_provider_port import ExternalEntityRef
from app.core.identity_resolution import (
    IdentityEntityKind,
    IdentityResolutionError,
    normalize_phone_e164,
)
from app.db.clock import resolve_moment
from app.db.session import session_scope
from app.repositories import amocrm_crm_oauth_tokens as oauth_repo

__all__ = (
    "AmoCrmIdentityLookupService",
    "MAX_CONTACT_QUERY_PAGES",
)

_REFRESH_SKEW = timedelta(seconds=60)
MAX_CONTACT_QUERY_PAGES: Final[int] = 5

_ResolveAccessToken = Callable[[], Awaitable[str | None]]


class _OauthRefreshBudget:
    """One remote OAuth refresh per top-level lookup operation."""

    __slots__ = ("spent",)

    def __init__(self) -> None:
        self.spent = False


class AmoCrmIdentityLookupService:
    """Fail-closed amoCRM contact lookup. Primary API returns typed outcomes."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: AmoCrmCrmRestConfig | None = None,
        key_provider: AmoCrmOauthKeyProvider | None = None,
        transport: AmoCrmCrmRestTransport | None = None,
        worker_id: str | None = None,
        oauth: AmoCrmCrmRestHttpClient | None = None,
        resolve_access_token: _ResolveAccessToken | None = None,
        contacts_client: AmoCrmContactsHttpClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = (
            config if config is not None else load_crm_rest_config_fail_closed()
        )
        self._key_provider = (
            key_provider if key_provider is not None else EnvAmoCrmOauthKeyProvider()
        )
        self._transport = (
            transport if transport is not None else _CrmHttpStdlibTransport()
        )
        self._worker_id = worker_id or f"crm-id-lookup-{uuid.uuid4().hex[:12]}"
        if oauth is not None:
            self._oauth = oauth
        elif self._config.enabled:
            self._oauth = AmoCrmCrmRestHttpClient(
                self._config,
                session_factory=session_factory,
                key_provider=self._key_provider,
                transport=self._transport,
                worker_id=self._worker_id,
            )
        else:
            self._oauth = None
        self._resolve_access_token_override = resolve_access_token
        self._contacts = contacts_client or AmoCrmContactsHttpClient(
            self._config,
            transport=self._transport,
        )

    async def lookup_contact_by_id(
        self,
        *,
        contact_id: object,
    ) -> AmoCrmIdentityLookupResult:
        if not self._config.enabled:
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.DISABLED,
                error_code="AMOCRM_CRM_REST_DISABLED",
            )
        cid = self._normalize_contact_id(contact_id)
        if cid is None:
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.INVALID_INPUT,
                error_code="AMOCRM_CONTACT_ID_INVALID",
            )
        budget = _OauthRefreshBudget()
        access = await self._resolve_access_token(budget)
        if access is None:
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.PERMANENT_ERROR,
                error_code="AMOCRM_CRM_OAUTH_NOT_FOUND",
                http_calls=tuple(self._contacts.http_calls),
            )
        result = await self._with_401_retry(
            self._contacts.get_contact_by_id,
            access_token=access,
            budget=budget,
            contact_id=cid,
        )
        return self._map_by_id_result(result)

    async def lookup_contact_by_phone(
        self,
        *,
        phone: object,
    ) -> AmoCrmIdentityLookupResult:
        if not self._config.enabled:
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.DISABLED,
                error_code="AMOCRM_CRM_REST_DISABLED",
            )
        try:
            normalized = normalize_phone_e164(phone)
        except IdentityResolutionError:
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.INVALID_INPUT,
                error_code="PHONE_INVALID",
            )
        budget = _OauthRefreshBudget()
        access = await self._resolve_access_token(budget)
        if access is None:
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.PERMANENT_ERROR,
                error_code="AMOCRM_CRM_OAUTH_NOT_FOUND",
                http_calls=tuple(self._contacts.http_calls),
            )
        exact_ids: set[str] = set()
        page = 1
        while page <= MAX_CONTACT_QUERY_PAGES:
            page_result = await self._with_401_retry(
                self._contacts.query_contacts_page,
                access_token=access,
                budget=budget,
                query=normalized,
                page=page,
            )
            mapped_err = self._map_query_transport_error(page_result)
            if mapped_err is not None:
                return mapped_err
            for contact in page_result.contacts:
                if contact_has_exact_phone(
                    contact,
                    normalized_phone=normalized,
                    normalize_fn=normalize_phone_e164,
                ):
                    exact_ids.add(contact.contact_id)
            if len(exact_ids) >= 2 and not page_result.has_next_page:
                return AmoCrmIdentityLookupResult(
                    outcome=AmoCrmIdentityLookupOutcome.AMBIGUOUS,
                    contact_ids=self._sorted_ids(exact_ids),
                    http_calls=tuple(self._contacts.http_calls),
                )
            if len(exact_ids) >= 2 and page_result.has_next_page:
                # Already ambiguous; further pages cannot clear ambiguity.
                return AmoCrmIdentityLookupResult(
                    outcome=AmoCrmIdentityLookupOutcome.AMBIGUOUS,
                    contact_ids=self._sorted_ids(exact_ids),
                    http_calls=tuple(self._contacts.http_calls),
                )
            if not page_result.has_next_page:
                return self._finish_phone_exact(exact_ids)
            page += 1
            # Reload access in case 401-retry rotated tokens mid-loop.
            reloaded = await self._load_access_token()
            if reloaded is not None:
                access = reloaded
        # Exhausted bounded pages while more may exist.
        if len(exact_ids) >= 2:
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.AMBIGUOUS,
                contact_ids=self._sorted_ids(exact_ids),
                http_calls=tuple(self._contacts.http_calls),
            )
        return AmoCrmIdentityLookupResult(
            outcome=AmoCrmIdentityLookupOutcome.INCOMPLETE,
            error_code="AMOCRM_CRM_CONTACTS_PAGE_INCOMPLETE",
            http_calls=tuple(self._contacts.http_calls),
        )

    async def lookup_by_external_id(
        self,
        *,
        provider: str,
        connection_scope: str,
        entity_kind: IdentityEntityKind,
        external_id: str,
    ) -> ExternalEntityRef | None:
        """Protocol surface: FOUND → ref; everything else → None (lossy)."""

        if provider != AMOCRM_IDENTITY_PROVIDER:
            return None
        if connection_scope != self._config.connection_scope:
            return None
        if entity_kind is not IdentityEntityKind.AMOCRM_CONTACT:
            return None
        result = await self.lookup_contact_by_id(contact_id=external_id)
        if result.outcome is not AmoCrmIdentityLookupOutcome.FOUND:
            return None
        assert result.contact_id is not None
        return ExternalEntityRef(
            provider=AMOCRM_IDENTITY_PROVIDER,
            connection_scope=self._config.connection_scope,
            entity_kind=IdentityEntityKind.AMOCRM_CONTACT,
            external_id=result.contact_id,
        )

    def _finish_phone_exact(
        self,
        exact_ids: set[str],
    ) -> AmoCrmIdentityLookupResult:
        calls = tuple(self._contacts.http_calls)
        if len(exact_ids) == 0:
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.NOT_FOUND,
                http_calls=calls,
            )
        if len(exact_ids) == 1:
            only = next(iter(exact_ids))
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.FOUND,
                contact_id=only,
                http_calls=calls,
            )
        return AmoCrmIdentityLookupResult(
            outcome=AmoCrmIdentityLookupOutcome.AMBIGUOUS,
            contact_ids=self._sorted_ids(exact_ids),
            http_calls=calls,
        )

    def _map_by_id_result(self, result: object) -> AmoCrmIdentityLookupResult:
        calls = tuple(self._contacts.http_calls)
        outcome = getattr(result, "outcome", None)
        if getattr(result, "not_found", False):
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.NOT_FOUND,
                error_code=getattr(result, "error_code", None),
                http_calls=calls,
            )
        if outcome is AmoCrmCrmRestOutcome.DISABLED:
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.DISABLED,
                error_code="AMOCRM_CRM_REST_DISABLED",
                http_calls=calls,
            )
        if getattr(result, "unauthorized", False):
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.PERMANENT_ERROR,
                error_code=getattr(result, "error_code", None) or "AMOCRM_CRM_HTTP_401",
                http_calls=calls,
            )
        if outcome is AmoCrmCrmRestOutcome.SUCCESS:
            contact = getattr(result, "contact", None)
            if not isinstance(contact, AmoCrmContactRecord):
                return AmoCrmIdentityLookupResult(
                    outcome=AmoCrmIdentityLookupOutcome.PERMANENT_ERROR,
                    error_code="AMOCRM_CRM_CONTACT_BODY_INVALID",
                    http_calls=calls,
                )
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.FOUND,
                contact_id=contact.contact_id,
                http_calls=calls,
            )
        if outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR:
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.TRANSIENT_ERROR,
                error_code=getattr(result, "error_code", None),
                http_calls=calls,
            )
        return AmoCrmIdentityLookupResult(
            outcome=AmoCrmIdentityLookupOutcome.PERMANENT_ERROR,
            error_code=getattr(result, "error_code", None),
            http_calls=calls,
        )

    def _map_query_transport_error(
        self,
        result: object,
    ) -> AmoCrmIdentityLookupResult | None:
        calls = tuple(self._contacts.http_calls)
        outcome = getattr(result, "outcome", None)
        if outcome is AmoCrmCrmRestOutcome.SUCCESS:
            return None
        if outcome is AmoCrmCrmRestOutcome.DISABLED:
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.DISABLED,
                error_code="AMOCRM_CRM_REST_DISABLED",
                http_calls=calls,
            )
        if getattr(result, "unauthorized", False):
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.PERMANENT_ERROR,
                error_code=getattr(result, "error_code", None) or "AMOCRM_CRM_HTTP_401",
                http_calls=calls,
            )
        if outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR:
            return AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.TRANSIENT_ERROR,
                error_code=getattr(result, "error_code", None),
                http_calls=calls,
            )
        return AmoCrmIdentityLookupResult(
            outcome=AmoCrmIdentityLookupOutcome.PERMANENT_ERROR,
            error_code=getattr(result, "error_code", None),
            http_calls=calls,
        )

    @staticmethod
    def _normalize_contact_id(value: object) -> str | None:
        if type(value) is int and not isinstance(value, bool) and value > 0:
            return str(value)
        if type(value) is not str or not value:
            return None
        if not value.isdigit() or value.startswith("0"):
            return None
        return value

    @staticmethod
    def _sorted_ids(ids: set[str]) -> tuple[str, ...]:
        return tuple(sorted(ids, key=lambda x: int(x)))

    async def _with_401_retry(
        self,
        fn: Callable[..., object],
        *,
        access_token: str,
        budget: _OauthRefreshBudget,
        **kwargs: object,
    ) -> object:
        result = fn(access_token=access_token, **kwargs)
        if not getattr(result, "unauthorized", False):
            return result
        if not await self._try_remote_refresh(budget):
            return result
        new_access = await self._load_access_token()
        if new_access is None:
            return result
        return fn(access_token=new_access, **kwargs)

    async def _try_remote_refresh(self, budget: _OauthRefreshBudget) -> bool:
        if budget.spent or self._oauth is None:
            return False
        budget.spent = True
        refreshed = await self._oauth.refresh_tokens()
        return refreshed.outcome is AmoCrmCrmRestOutcome.SUCCESS

    async def _resolve_access_token(
        self,
        budget: _OauthRefreshBudget,
    ) -> str | None:
        if self._resolve_access_token_override is not None:
            return await self._resolve_access_token_override()
        if not self._config.enabled:
            return None
        need_refresh = False
        async with session_scope(self._session_factory) as session:
            row = await oauth_repo.get_by_scope(
                session,
                connection_scope=self._config.connection_scope,
            )
            if row is None:
                return None
            tokens = oauth_repo.decrypt_row(row, key_provider=self._key_provider)
            moment = await resolve_moment(session, None)
            if (
                row.access_expires_at is not None
                and row.access_expires_at <= moment + _REFRESH_SKEW
            ):
                need_refresh = True
            access = tokens.access_token
        if need_refresh:
            if await self._try_remote_refresh(budget):
                reloaded = await self._load_access_token()
                if reloaded is not None:
                    return reloaded
        return access

    async def _load_access_token(self) -> str | None:
        if self._resolve_access_token_override is not None:
            return await self._resolve_access_token_override()
        async with session_scope(self._session_factory) as session:
            row = await oauth_repo.get_by_scope(
                session,
                connection_scope=self._config.connection_scope,
            )
            if row is None:
                return None
            tokens = oauth_repo.decrypt_row(row, key_provider=self._key_provider)
            return tokens.access_token
