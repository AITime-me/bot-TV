"""Read-only amoCRM business Deal (Lead) discovery.

Classifies linked Leads without treating them as Buyer Cards (Customers).
Only amoCRM system status 143 (closed/unrealized) is a reanimation candidate.
Status 142 is successful history, not reanimation. Known AMOCRM_TECHNICAL_DEAL
ids are excluded from business candidates. No auto-reanimation. No CRM writes.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_crm_buyer_card_http import (
    AmoCrmBuyerCardHttpClient,
    AmoCrmContactWithLeadsRecord,
    AmoCrmLeadInspectRecord,
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
from app.core.amocrm_deal_discovery import (
    AMOCRM_SYSTEM_LEAD_STATUS_SUCCESS,
    AMOCRM_SYSTEM_LEAD_STATUS_UNREALIZED,
    AmoCrmDealDiscoveryOutcome,
    AmoCrmDealDiscoveryResult,
)
from app.db.session import session_scope
from app.repositories import amocrm_crm_oauth_tokens as oauth_repo

__all__ = (
    "AmoCrmDealDiscoveryService",
    "MAX_LINKED_LEADS_PER_DISCOVERY",
)

MAX_LINKED_LEADS_PER_DISCOVERY: Final[int] = 20

_ResolveAccessToken = Callable[[], Awaitable[str | None]]


class _OauthRefreshBudget:
    """One remote OAuth refresh per top-level discovery operation."""

    __slots__ = ("spent",)

    def __init__(self) -> None:
        self.spent = False


class AmoCrmDealDiscoveryService:
    """Fail-closed read-only business Deal (Lead) classification."""

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
        self._worker_id = worker_id or f"crm-deal-disc-{uuid.uuid4().hex[:12]}"
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

    async def discover_deal_candidates(
        self,
        *,
        contact_id: object,
        known_technical_deal_ids: Sequence[object] = (),
    ) -> AmoCrmDealDiscoveryResult:
        if not self._config.enabled:
            return AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.DISABLED,
                error_code="AMOCRM_CRM_REST_DISABLED",
            )
        cid = self._normalize_entity_id(contact_id)
        if cid is None:
            return AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.INVALID_INPUT,
                error_code="AMOCRM_CONTACT_ID_INVALID",
            )
        tech_ids = self._normalize_id_tuple(known_technical_deal_ids)
        if tech_ids is None:
            return AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.INVALID_INPUT,
                error_code="AMOCRM_TECHNICAL_DEAL_ID_INVALID",
            )
        tech_set = set(tech_ids)
        budget = _OauthRefreshBudget()
        access = await self._resolve_access_token(budget)
        if access is None:
            return AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.PERMANENT_ERROR,
                known_technical_deal_ids=tech_ids,
                error_code="AMOCRM_CRM_OAUTH_NOT_FOUND",
                http_calls=tuple(self._http.http_calls),
            )
        contact_result = await self._with_401_retry(
            self._http.get_contact_with_leads,
            access_token=access,
            budget=budget,
            contact_id=cid,
        )
        mapped_contact = self._map_contact_error(contact_result, tech_ids)
        if mapped_contact is not None:
            return mapped_contact
        contact = getattr(contact_result, "contact", None)
        if not isinstance(contact, AmoCrmContactWithLeadsRecord):
            return AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.PERMANENT_ERROR,
                known_technical_deal_ids=tech_ids,
                error_code="AMOCRM_CRM_CONTACT_BODY_INVALID",
                http_calls=tuple(self._http.http_calls),
            )
        reloaded = await self._load_access_token()
        if reloaded is not None:
            access = reloaded
        linked = contact.linked_lead_ids
        if len(linked) > MAX_LINKED_LEADS_PER_DISCOVERY:
            return AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.INCOMPLETE,
                contact_id=contact.contact_id,
                known_technical_deal_ids=tech_ids,
                error_code="AMOCRM_DEAL_LINKED_LEADS_LIMIT",
                http_calls=tuple(self._http.http_calls),
            )
        if len(linked) == 0:
            return AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.NOT_FOUND,
                contact_id=contact.contact_id,
                known_technical_deal_ids=tech_ids,
                http_calls=tuple(self._http.http_calls),
            )
        business_active: list[str] = []
        reanimation: list[str] = []
        successfully_closed: list[str] = []
        technical: list[str] = []
        for lead_id in linked:
            lead_result = await self._with_401_retry(
                self._http.get_lead_with_contacts,
                access_token=access,
                budget=budget,
                lead_id=lead_id,
            )
            mapped_lead = self._map_lead_inspect_error(
                lead_result,
                contact_id=contact.contact_id,
                tech_ids=tech_ids,
            )
            if mapped_lead is not None:
                return mapped_lead
            lead = getattr(lead_result, "lead", None)
            if not isinstance(lead, AmoCrmLeadInspectRecord):
                return AmoCrmDealDiscoveryResult(
                    outcome=AmoCrmDealDiscoveryOutcome.PERMANENT_ERROR,
                    contact_id=contact.contact_id,
                    known_technical_deal_ids=tech_ids,
                    error_code="AMOCRM_CRM_LEAD_BODY_INVALID",
                    http_calls=tuple(self._http.http_calls),
                )
            if contact.contact_id not in lead.linked_contact_ids:
                return AmoCrmDealDiscoveryResult(
                    outcome=AmoCrmDealDiscoveryOutcome.INCOMPLETE,
                    contact_id=contact.contact_id,
                    known_technical_deal_ids=tech_ids,
                    error_code="AMOCRM_DEAL_LEAD_CONTACT_UNLINKED",
                    http_calls=tuple(self._http.http_calls),
                )
            classified = self._classify_lead(
                lead,
                tech_set=tech_set,
                contact_id=contact.contact_id,
                tech_ids=tech_ids,
            )
            if isinstance(classified, AmoCrmDealDiscoveryResult):
                return classified
            bucket, lead_token = classified
            if bucket == "technical":
                technical.append(lead_token)
            elif bucket == "reanimation":
                reanimation.append(lead_token)
            elif bucket == "successfully_closed":
                successfully_closed.append(lead_token)
            elif bucket == "business_active":
                business_active.append(lead_token)
            reloaded = await self._load_access_token()
            if reloaded is not None:
                access = reloaded
        return AmoCrmDealDiscoveryResult(
            outcome=AmoCrmDealDiscoveryOutcome.FOUND,
            contact_id=contact.contact_id,
            business_active_lead_ids=tuple(business_active),
            reanimation_candidate_lead_ids=tuple(reanimation),
            successfully_closed_lead_ids=tuple(successfully_closed),
            technical_lead_ids=tuple(technical),
            known_technical_deal_ids=tech_ids,
            http_calls=tuple(self._http.http_calls),
        )

    def _classify_lead(
        self,
        lead: AmoCrmLeadInspectRecord,
        *,
        tech_set: set[str],
        contact_id: str,
        tech_ids: tuple[str, ...],
    ) -> tuple[str, str] | AmoCrmDealDiscoveryResult:
        if lead.lead_id in tech_set:
            return "technical", lead.lead_id
        if lead.is_deleted:
            return "deleted", lead.lead_id
        if lead.status_id == AMOCRM_SYSTEM_LEAD_STATUS_UNREALIZED:
            return "reanimation", lead.lead_id
        if lead.status_id == AMOCRM_SYSTEM_LEAD_STATUS_SUCCESS:
            return "successfully_closed", lead.lead_id
        if lead.closed_at is None:
            return "business_active", lead.lead_id
        return AmoCrmDealDiscoveryResult(
            outcome=AmoCrmDealDiscoveryOutcome.INCOMPLETE,
            contact_id=contact_id,
            known_technical_deal_ids=tech_ids,
            error_code="AMOCRM_DEAL_LEAD_STATUS_CLOSED_INCONSISTENT",
            http_calls=tuple(self._http.http_calls),
        )

    def _map_contact_error(
        self,
        result: object,
        tech_ids: tuple[str, ...],
    ) -> AmoCrmDealDiscoveryResult | None:
        calls = tuple(self._http.http_calls)
        outcome = getattr(result, "outcome", None)
        if getattr(result, "not_found", False):
            return AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.NOT_FOUND,
                known_technical_deal_ids=tech_ids,
                error_code=getattr(result, "error_code", None),
                http_calls=calls,
            )
        if outcome is AmoCrmCrmRestOutcome.DISABLED:
            return AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.DISABLED,
                known_technical_deal_ids=tech_ids,
                error_code="AMOCRM_CRM_REST_DISABLED",
                http_calls=calls,
            )
        if getattr(result, "unauthorized", False):
            return AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.PERMANENT_ERROR,
                known_technical_deal_ids=tech_ids,
                error_code=getattr(result, "error_code", None) or "AMOCRM_CRM_HTTP_401",
                http_calls=calls,
            )
        if outcome is AmoCrmCrmRestOutcome.SUCCESS:
            return None
        if outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR:
            return AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.TRANSIENT_ERROR,
                known_technical_deal_ids=tech_ids,
                error_code=getattr(result, "error_code", None),
                http_calls=calls,
            )
        return AmoCrmDealDiscoveryResult(
            outcome=AmoCrmDealDiscoveryOutcome.PERMANENT_ERROR,
            known_technical_deal_ids=tech_ids,
            error_code=getattr(result, "error_code", None),
            http_calls=calls,
        )

    def _map_lead_inspect_error(
        self,
        result: object,
        *,
        contact_id: str,
        tech_ids: tuple[str, ...],
    ) -> AmoCrmDealDiscoveryResult | None:
        calls = tuple(self._http.http_calls)
        outcome = getattr(result, "outcome", None)
        if getattr(result, "not_found", False):
            return AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.INCOMPLETE,
                contact_id=contact_id,
                known_technical_deal_ids=tech_ids,
                error_code=getattr(result, "error_code", None)
                or "AMOCRM_DEAL_LEAD_MISSING",
                http_calls=calls,
            )
        if outcome is AmoCrmCrmRestOutcome.DISABLED:
            return AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.DISABLED,
                contact_id=contact_id,
                known_technical_deal_ids=tech_ids,
                error_code="AMOCRM_CRM_REST_DISABLED",
                http_calls=calls,
            )
        if getattr(result, "unauthorized", False):
            return AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.PERMANENT_ERROR,
                contact_id=contact_id,
                known_technical_deal_ids=tech_ids,
                error_code=getattr(result, "error_code", None) or "AMOCRM_CRM_HTTP_401",
                http_calls=calls,
            )
        if outcome is AmoCrmCrmRestOutcome.SUCCESS:
            return None
        if outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR:
            return AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.TRANSIENT_ERROR,
                contact_id=contact_id,
                known_technical_deal_ids=tech_ids,
                error_code=getattr(result, "error_code", None),
                http_calls=calls,
            )
        return AmoCrmDealDiscoveryResult(
            outcome=AmoCrmDealDiscoveryOutcome.PERMANENT_ERROR,
            contact_id=contact_id,
            known_technical_deal_ids=tech_ids,
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

    def _normalize_id_tuple(self, values: Sequence[object]) -> tuple[str, ...] | None:
        if type(values) is not tuple and type(values) is not list:
            return None
        out: list[str] = []
        seen: set[str] = set()
        for item in values:
            token = self._normalize_entity_id(item)
            if token is None:
                return None
            if token in seen:
                continue
            seen.add(token)
            out.append(token)
        return tuple(sorted(out, key=lambda value: int(value)))

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
