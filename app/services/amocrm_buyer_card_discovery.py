"""Read-only amoCRM Buyer Card (Customer) candidate discovery (IR-3).

Collects linked-customer evidence for a later
IdentityResolutionService.reconcile_buyer_card call. No Lead status, no
attach, no CRM writes, no webhook/worker wiring. OAuth refresh reuses the
existing token-store fencing with a one-refresh budget per discovery.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_buyer_card_discovery import (
    AmoCrmBuyerCardDiscoveryOutcome,
    AmoCrmBuyerCardDiscoveryResult,
)
from app.core.amocrm_crm_buyer_card_http import (
    AmoCrmBuyerCardHttpClient,
    AmoCrmContactWithCustomersRecord,
    AmoCrmCustomerInspectRecord,
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
from app.db.session import session_scope
from app.repositories import amocrm_crm_oauth_tokens as oauth_repo

__all__ = (
    "AmoCrmBuyerCardDiscoveryService",
    "MAX_LINKED_CUSTOMERS_PER_DISCOVERY",
)

MAX_LINKED_CUSTOMERS_PER_DISCOVERY: Final[int] = 20

_ResolveAccessToken = Callable[[], Awaitable[str | None]]


class _OauthRefreshBudget:
    """One remote OAuth refresh per top-level discovery operation."""

    __slots__ = ("spent",)

    def __init__(self) -> None:
        self.spent = False


class AmoCrmBuyerCardDiscoveryService:
    """Fail-closed read-only Buyer Card (Customer) candidate discovery."""

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
        http_client: AmoCrmBuyerCardHttpClient | None = None,
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
        self._worker_id = worker_id or f"crm-buyer-disc-{uuid.uuid4().hex[:12]}"
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
        self._http = http_client or AmoCrmBuyerCardHttpClient(
            self._config,
            transport=self._transport,
        )

    async def discover_buyer_card_candidates(
        self,
        *,
        contact_id: object,
    ) -> AmoCrmBuyerCardDiscoveryResult:
        if not self._config.enabled:
            return AmoCrmBuyerCardDiscoveryResult(
                outcome=AmoCrmBuyerCardDiscoveryOutcome.DISABLED,
                error_code="AMOCRM_CRM_REST_DISABLED",
            )
        cid = self._normalize_entity_id(contact_id)
        if cid is None:
            return AmoCrmBuyerCardDiscoveryResult(
                outcome=AmoCrmBuyerCardDiscoveryOutcome.INVALID_INPUT,
                error_code="AMOCRM_CONTACT_ID_INVALID",
            )
        budget = _OauthRefreshBudget()
        access = await self._resolve_access_token(budget)
        if access is None:
            return AmoCrmBuyerCardDiscoveryResult(
                outcome=AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR,
                error_code="AMOCRM_CRM_OAUTH_NOT_FOUND",
                http_calls=tuple(self._http.http_calls),
            )
        contact_result = await self._with_401_retry(
            self._http.get_contact_with_customers,
            access_token=access,
            budget=budget,
            contact_id=cid,
        )
        mapped_contact = self._map_contact_error(contact_result)
        if mapped_contact is not None:
            return mapped_contact
        contact = getattr(contact_result, "contact", None)
        if not isinstance(contact, AmoCrmContactWithCustomersRecord):
            return AmoCrmBuyerCardDiscoveryResult(
                outcome=AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR,
                error_code="AMOCRM_CRM_CONTACT_BODY_INVALID",
                http_calls=tuple(self._http.http_calls),
            )
        reloaded = await self._load_access_token()
        if reloaded is not None:
            access = reloaded
        linked = contact.linked_customer_ids
        if len(linked) > MAX_LINKED_CUSTOMERS_PER_DISCOVERY:
            return AmoCrmBuyerCardDiscoveryResult(
                outcome=AmoCrmBuyerCardDiscoveryOutcome.INCOMPLETE,
                contact_id=contact.contact_id,
                error_code="AMOCRM_BUYER_CARD_LINKED_CUSTOMERS_LIMIT",
                http_calls=tuple(self._http.http_calls),
            )
        eligible: list[str] = []
        for customer_id in linked:
            customer_result = await self._with_401_retry(
                self._http.get_customer_with_contacts,
                access_token=access,
                budget=budget,
                customer_id=customer_id,
            )
            mapped_customer = self._map_customer_inspect_error(
                customer_result,
                contact_id=contact.contact_id,
            )
            if mapped_customer is not None:
                return mapped_customer
            customer = getattr(customer_result, "customer", None)
            if not isinstance(customer, AmoCrmCustomerInspectRecord):
                return AmoCrmBuyerCardDiscoveryResult(
                    outcome=AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR,
                    contact_id=contact.contact_id,
                    error_code="AMOCRM_CRM_CUSTOMER_BODY_INVALID",
                    http_calls=tuple(self._http.http_calls),
                )
            if contact.contact_id not in customer.linked_contact_ids:
                return AmoCrmBuyerCardDiscoveryResult(
                    outcome=AmoCrmBuyerCardDiscoveryOutcome.INCOMPLETE,
                    contact_id=contact.contact_id,
                    error_code="AMOCRM_BUYER_CARD_CUSTOMER_CONTACT_UNLINKED",
                    http_calls=tuple(self._http.http_calls),
                )
            eligible.append(customer.customer_id)
            reloaded = await self._load_access_token()
            if reloaded is not None:
                access = reloaded
        return self._finish_eligible(
            contact_id=contact.contact_id,
            eligible=tuple(eligible),
        )

    def _finish_eligible(
        self,
        *,
        contact_id: str,
        eligible: tuple[str, ...],
    ) -> AmoCrmBuyerCardDiscoveryResult:
        calls = tuple(self._http.http_calls)
        if len(eligible) == 0:
            return AmoCrmBuyerCardDiscoveryResult(
                outcome=AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND,
                contact_id=contact_id,
                http_calls=calls,
            )
        if len(eligible) == 1:
            return AmoCrmBuyerCardDiscoveryResult(
                outcome=AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE,
                contact_id=contact_id,
                eligible_customer_ids=eligible,
                http_calls=calls,
            )
        return AmoCrmBuyerCardDiscoveryResult(
            outcome=AmoCrmBuyerCardDiscoveryOutcome.AMBIGUOUS,
            contact_id=contact_id,
            eligible_customer_ids=eligible,
            http_calls=calls,
        )

    def _map_contact_error(
        self,
        result: object,
    ) -> AmoCrmBuyerCardDiscoveryResult | None:
        calls = tuple(self._http.http_calls)
        outcome = getattr(result, "outcome", None)
        if getattr(result, "not_found", False):
            return AmoCrmBuyerCardDiscoveryResult(
                outcome=AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND,
                error_code=getattr(result, "error_code", None),
                http_calls=calls,
            )
        if outcome is AmoCrmCrmRestOutcome.DISABLED:
            return AmoCrmBuyerCardDiscoveryResult(
                outcome=AmoCrmBuyerCardDiscoveryOutcome.DISABLED,
                error_code="AMOCRM_CRM_REST_DISABLED",
                http_calls=calls,
            )
        if getattr(result, "unauthorized", False):
            return AmoCrmBuyerCardDiscoveryResult(
                outcome=AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR,
                error_code=getattr(result, "error_code", None) or "AMOCRM_CRM_HTTP_401",
                http_calls=calls,
            )
        if outcome is AmoCrmCrmRestOutcome.SUCCESS:
            return None
        if outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR:
            return AmoCrmBuyerCardDiscoveryResult(
                outcome=AmoCrmBuyerCardDiscoveryOutcome.TRANSIENT_ERROR,
                error_code=getattr(result, "error_code", None),
                http_calls=calls,
            )
        return AmoCrmBuyerCardDiscoveryResult(
            outcome=AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR,
            error_code=getattr(result, "error_code", None),
            http_calls=calls,
        )

    def _map_customer_inspect_error(
        self,
        result: object,
        *,
        contact_id: str,
    ) -> AmoCrmBuyerCardDiscoveryResult | None:
        calls = tuple(self._http.http_calls)
        outcome = getattr(result, "outcome", None)
        if getattr(result, "not_found", False):
            return AmoCrmBuyerCardDiscoveryResult(
                outcome=AmoCrmBuyerCardDiscoveryOutcome.INCOMPLETE,
                contact_id=contact_id,
                error_code=getattr(result, "error_code", None)
                or "AMOCRM_BUYER_CARD_CUSTOMER_MISSING",
                http_calls=calls,
            )
        if outcome is AmoCrmCrmRestOutcome.DISABLED:
            return AmoCrmBuyerCardDiscoveryResult(
                outcome=AmoCrmBuyerCardDiscoveryOutcome.DISABLED,
                contact_id=contact_id,
                error_code="AMOCRM_CRM_REST_DISABLED",
                http_calls=calls,
            )
        if getattr(result, "unauthorized", False):
            return AmoCrmBuyerCardDiscoveryResult(
                outcome=AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR,
                contact_id=contact_id,
                error_code=getattr(result, "error_code", None) or "AMOCRM_CRM_HTTP_401",
                http_calls=calls,
            )
        if outcome is AmoCrmCrmRestOutcome.SUCCESS:
            return None
        if outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR:
            return AmoCrmBuyerCardDiscoveryResult(
                outcome=AmoCrmBuyerCardDiscoveryOutcome.TRANSIENT_ERROR,
                contact_id=contact_id,
                error_code=getattr(result, "error_code", None),
                http_calls=calls,
            )
        return AmoCrmBuyerCardDiscoveryResult(
            outcome=AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR,
            contact_id=contact_id,
            error_code=getattr(result, "error_code", None),
            http_calls=calls,
        )

    @staticmethod
    def _normalize_entity_id(value: object) -> str | None:
        if type(value) is int and not isinstance(value, bool) and value > 0:
            return str(value)
        if type(value) is not str or not value:
            return None
        if not value.isdigit() or value.startswith("0"):
            return None
        return value

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
        if not await self._try_remote_refresh(
            budget,
            rejected_access_token=access_token,
        ):
            return result
        new_access = await self._load_access_token()
        if new_access is None:
            return result
        return fn(access_token=new_access, **kwargs)

    async def _try_remote_refresh(
        self,
        budget: _OauthRefreshBudget,
        *,
        rejected_access_token: str,
    ) -> bool:
        if budget.spent or self._oauth is None:
            return False
        budget.spent = True
        refreshed = await self._oauth.refresh_tokens(
            if_still_access_token=rejected_access_token,
        )
        return refreshed.outcome is AmoCrmCrmRestOutcome.SUCCESS

    async def _resolve_access_token(
        self,
        budget: _OauthRefreshBudget,
    ) -> str | None:
        if self._resolve_access_token_override is not None:
            return await self._resolve_access_token_override()
        if not self._config.enabled:
            return None
        return await self._load_access_token()

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
