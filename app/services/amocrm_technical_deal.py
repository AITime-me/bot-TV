"""Ensure one amoCRM TECHNICAL_DEAL per Bot Core conversation (AMO-01B2).

Durable create reservation before POST. Ambiguous create → RECONCILE_REQUIRED.
CONTACT: reuse deterministic ACTIVE link only; never create/guess contacts.
HTTP 401 refreshes OAuth once under token-store fencing, then retries once.
GET 404 is the only remote miss that revokes an ACTIVE link.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_crm_deal_create_config import (
    AmoCrmDealCreateConfig,
    AmoCrmDealCreateConfigError,
    load_deal_create_config_fail_closed,
)
from app.core.amocrm_crm_leads_http import AmoCrmLeadHttpClient
from app.core.amocrm_crm_oauth_keys import (
    AmoCrmOauthKeyProvider,
    EnvAmoCrmOauthKeyProvider,
)
from app.core.amocrm_crm_rest_http import (
    AmoCrmCrmRestHttpClient,
    AmoCrmCrmRestOutcome,
    AmoCrmCrmRestTransport,
    _CrmHttpStdlibTransport,
)
from app.db.session import session_scope
from app.models.amocrm_entity_link import AmocrmEntityKind, AmocrmEntityLinkStatus
from app.repositories import amocrm_crm_oauth_tokens as oauth_repo
from app.repositories import amocrm_entity_links as entity_links
from app.repositories.amocrm_entity_links import (
    AmocrmEntityLinkConflictError,
    AmocrmEntityLinkStaleLeaseError,
)

__all__ = (
    "TechnicalDealEnsureResult",
    "TechnicalDealOutcome",
    "TechnicalDealProjectionService",
    "coerce_conversation_uuid",
    "load_deal_create_config_fail_closed",
)

_T = TypeVar("_T")


def coerce_conversation_uuid(value: object) -> uuid.UUID | None:
    """Normalize a conversation id from ORM/asyncpg without guessing.

    Accepts ``uuid.UUID`` and UUID subclasses returned by PostgreSQL drivers
    (asyncpg). Rejects ints, bools, arbitrary objects, and malformed strings.
    """

    if type(value) is uuid.UUID:
        return value
    if isinstance(value, uuid.UUID):
        # asyncpg.pgproto.UUID subclasses uuid.UUID; normalize to stdlib UUID.
        # Hostile/broken subclasses must fail closed, not leak exceptions.
        try:
            return uuid.UUID(int=value.int)
        except Exception:
            return None
    if type(value) is str:
        if not value or any(ch.isspace() for ch in value):
            return None
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


class TechnicalDealOutcome(str, Enum):
    ENSURED = "ENSURED"
    DISABLED = "DISABLED"
    BUSY = "BUSY"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    PERMANENT_ERROR = "PERMANENT_ERROR"


@dataclass(frozen=True, slots=True, repr=False)
class TechnicalDealEnsureResult:
    outcome: TechnicalDealOutcome
    external_deal_id: str | None = None
    error_code: str | None = None
    http_calls: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "TechnicalDealEnsureResult("
            f"outcome={self.outcome!r}, "
            f"external_deal_id={self.external_deal_id!r}, "
            f"error_code={self.error_code!r}, "
            f"http_calls={self.http_calls!r})"
        )


class TechnicalDealProjectionService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: AmoCrmDealCreateConfig | None = None,
        key_provider: AmoCrmOauthKeyProvider | None = None,
        transport: AmoCrmCrmRestTransport | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = (
            config if config is not None else load_deal_create_config_fail_closed()
        )
        self._key_provider = (
            key_provider if key_provider is not None else EnvAmoCrmOauthKeyProvider()
        )
        self._transport = (
            transport if transport is not None else _CrmHttpStdlibTransport()
        )
        self._worker_id = worker_id or f"crm-deal-{uuid.uuid4().hex[:12]}"
        rest = self._config.rest
        self._oauth = (
            AmoCrmCrmRestHttpClient(
                rest,
                session_factory=session_factory,
                key_provider=self._key_provider,
                transport=self._transport,
                worker_id=self._worker_id,
            )
            if rest is not None
            else None
        )

    async def ensure_technical_deal(
        self,
        conversation_id: uuid.UUID,
    ) -> TechnicalDealEnsureResult:
        normalized_id = coerce_conversation_uuid(conversation_id)
        if normalized_id is None:
            return TechnicalDealEnsureResult(
                outcome=TechnicalDealOutcome.PERMANENT_ERROR,
                error_code="CONVERSATION_ID_INVALID",
            )
        conversation_id = normalized_id
        if not self._config.enabled:
            return TechnicalDealEnsureResult(outcome=TechnicalDealOutcome.DISABLED)
        try:
            self._config.require_runtime()
        except AmoCrmDealCreateConfigError as exc:
            return TechnicalDealEnsureResult(
                outcome=TechnicalDealOutcome.DISABLED,
                error_code=str(exc.args[0]),
            )
        assert self._config.rest is not None
        assert self._config.pipeline_id is not None
        assert self._config.status_id is not None

        leads = AmoCrmLeadHttpClient(self._config.rest, transport=self._transport)
        access = await self._resolve_access_token()
        if access is None:
            return TechnicalDealEnsureResult(
                outcome=TechnicalDealOutcome.TRANSIENT_ERROR,
                error_code="AMOCRM_CRM_OAUTH_NOT_FOUND",
                http_calls=tuple(leads.http_calls),
            )

        open_status, open_external_id = await self._snapshot_open_deal(conversation_id)
        if open_status == AmocrmEntityLinkStatus.RECONCILE_REQUIRED.value:
            return TechnicalDealEnsureResult(
                outcome=TechnicalDealOutcome.RECONCILE_REQUIRED,
                error_code="ENTITY_LINK_RECONCILE_REQUIRED",
                http_calls=tuple(leads.http_calls),
            )
        if open_status == AmocrmEntityLinkStatus.ACTIVE.value:
            assert open_external_id is not None
            validated = await self._validate_active_deal(
                conversation_id=conversation_id,
                external_id=open_external_id,
                leads=leads,
                access_token=access,
            )
            if validated is not None:
                return validated

        contact_id = await self._deterministic_contact_id(
            conversation_id=conversation_id,
            leads=leads,
            access_token=access,
        )
        access = await self._load_access_token() or access

        async with session_scope(self._session_factory) as session:
            try:
                reservation = await entity_links.claim_deal_create_reservation(
                    session,
                    conversation_id=conversation_id,
                    worker_id=self._worker_id,
                )
            except AmocrmEntityLinkStaleLeaseError as exc:
                code = str(exc)
                if code == "ENTITY_LINK_RECONCILE_REQUIRED":
                    return TechnicalDealEnsureResult(
                        outcome=TechnicalDealOutcome.RECONCILE_REQUIRED,
                        error_code=code,
                        http_calls=tuple(leads.http_calls),
                    )
                return TechnicalDealEnsureResult(
                    outcome=TechnicalDealOutcome.BUSY,
                    error_code=code,
                    http_calls=tuple(leads.http_calls),
                )
            except AmocrmEntityLinkConflictError as exc:
                return TechnicalDealEnsureResult(
                    outcome=TechnicalDealOutcome.ENSURED
                    if str(exc) == "ENTITY_LINK_ALREADY_ACTIVE"
                    else TechnicalDealOutcome.BUSY,
                    error_code=str(exc),
                    http_calls=tuple(leads.http_calls),
                )
            try:
                await entity_links.mark_create_submitted(
                    session, reservation=reservation
                )
            except AmocrmEntityLinkStaleLeaseError:
                return TechnicalDealEnsureResult(
                    outcome=TechnicalDealOutcome.BUSY,
                    error_code="ENTITY_LINK_STALE_LEASE",
                    http_calls=tuple(leads.http_calls),
                )

        create = await self._with_401_retry(
            leads.create_lead,
            access_token=access,
            name=f"bot-tv:{conversation_id}",
            pipeline_id=self._config.pipeline_id,
            status_id=self._config.status_id,
            contact_id=contact_id,
        )
        if create.outcome is AmoCrmCrmRestOutcome.SUCCESS and create.lead_id:
            async with session_scope(self._session_factory) as session:
                try:
                    row = await entity_links.complete_reservation_to_active(
                        session,
                        reservation=reservation,
                        external_id=create.lead_id,
                    )
                except AmocrmEntityLinkStaleLeaseError:
                    return TechnicalDealEnsureResult(
                        outcome=TechnicalDealOutcome.RECONCILE_REQUIRED,
                        error_code="ENTITY_LINK_STALE_AFTER_CREATE",
                        http_calls=tuple(leads.http_calls),
                    )
            return TechnicalDealEnsureResult(
                outcome=TechnicalDealOutcome.ENSURED,
                external_deal_id=row.external_id,
                http_calls=tuple(leads.http_calls),
            )

        if create.unauthorized:
            async with session_scope(self._session_factory) as session:
                await entity_links.release_reservation_for_retry(
                    session,
                    reservation=reservation,
                    allow_after_submit=True,
                )
            return TechnicalDealEnsureResult(
                outcome=TechnicalDealOutcome.TRANSIENT_ERROR,
                error_code=create.error_code or "AMOCRM_CRM_HTTP_401",
                http_calls=tuple(leads.http_calls),
            )

        if create.ambiguous or create.outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR:
            async with session_scope(self._session_factory) as session:
                try:
                    await entity_links.mark_reservation_reconcile_required(
                        session, reservation=reservation
                    )
                except AmocrmEntityLinkStaleLeaseError:
                    pass
            return TechnicalDealEnsureResult(
                outcome=TechnicalDealOutcome.RECONCILE_REQUIRED,
                error_code=create.error_code or "AMOCRM_CRM_CREATE_AMBIGUOUS",
                http_calls=tuple(leads.http_calls),
            )

        async with session_scope(self._session_factory) as session:
            await entity_links.release_reservation_for_retry(
                session,
                reservation=reservation,
                allow_after_submit=True,
            )
        return TechnicalDealEnsureResult(
            outcome=TechnicalDealOutcome.PERMANENT_ERROR
            if create.outcome is AmoCrmCrmRestOutcome.PERMANENT_ERROR
            else TechnicalDealOutcome.TRANSIENT_ERROR,
            error_code=create.error_code,
            http_calls=tuple(leads.http_calls),
        )

    async def _snapshot_open_deal(
        self, conversation_id: uuid.UUID
    ) -> tuple[str | None, str | None]:
        async with session_scope(self._session_factory) as session:
            open_row = await entity_links.get_open(
                session,
                conversation_id=conversation_id,
                entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
            )
            if open_row is None:
                return None, None
            return open_row.status, open_row.external_id

    async def _validate_active_deal(
        self,
        *,
        conversation_id: uuid.UUID,
        external_id: str,
        leads: AmoCrmLeadHttpClient,
        access_token: str,
    ) -> TechnicalDealEnsureResult | None:
        got = await self._with_401_retry(
            leads.get_lead,
            access_token=access_token,
            lead_id=external_id,
        )
        if got.outcome is AmoCrmCrmRestOutcome.SUCCESS:
            attach = await self._attach_contact_if_needed(
                conversation_id=conversation_id,
                deal_id=got.lead_id or external_id,
                linked_contact_ids=got.contact_ids,
                leads=leads,
                access_token=access_token,
            )
            if attach is not None:
                return attach
            return TechnicalDealEnsureResult(
                outcome=TechnicalDealOutcome.ENSURED,
                external_deal_id=got.lead_id or external_id,
                http_calls=tuple(leads.http_calls),
            )
        if got.not_found:
            async with session_scope(self._session_factory) as session:
                await entity_links.revoke_active(
                    session,
                    conversation_id=conversation_id,
                    entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
                )
            return None
        # 401 after retry, 402/403/429/5xx/transport: never revoke or recreate.
        return TechnicalDealEnsureResult(
            outcome=TechnicalDealOutcome.TRANSIENT_ERROR,
            error_code=got.error_code,
            http_calls=tuple(leads.http_calls),
        )

    async def _attach_contact_if_needed(
        self,
        *,
        conversation_id: uuid.UUID,
        deal_id: str,
        linked_contact_ids: tuple[str, ...],
        leads: AmoCrmLeadHttpClient,
        access_token: str,
    ) -> TechnicalDealEnsureResult | None:
        contact_id = await self._local_contact_id(conversation_id)
        if contact_id is None or contact_id in linked_contact_ids:
            return None
        linked = await self._with_401_retry(
            leads.link_contact_to_lead,
            access_token=access_token,
            lead_id=deal_id,
            contact_id=contact_id,
        )
        if linked.outcome is AmoCrmCrmRestOutcome.SUCCESS:
            return None
        if linked.status_code == 400:
            # Idempotent: already linked or unusable metadata — deal still valid.
            return None
        if linked.outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR or linked.unauthorized:
            return TechnicalDealEnsureResult(
                outcome=TechnicalDealOutcome.TRANSIENT_ERROR,
                error_code=linked.error_code,
                http_calls=tuple(leads.http_calls),
            )
        return None

    async def _deterministic_contact_id(
        self,
        *,
        conversation_id: uuid.UUID,
        leads: AmoCrmLeadHttpClient,
        access_token: str,
    ) -> str | None:
        contact_id = await self._local_contact_id(conversation_id)
        if contact_id is None:
            return None
        probed = await self._with_401_retry(
            leads.contact_exists,
            access_token=access_token,
            contact_id=contact_id,
        )
        if probed.exists:
            return contact_id
        return None

    async def _local_contact_id(self, conversation_id: uuid.UUID) -> str | None:
        async with session_scope(self._session_factory) as session:
            contact = await entity_links.get_active(
                session,
                conversation_id=conversation_id,
                entity_kind=AmocrmEntityKind.CONTACT,
            )
        if contact is None or not contact.external_id:
            return None
        if not contact.external_id.isdigit():
            return None
        return contact.external_id

    async def _with_401_retry(
        self,
        fn: Callable[..., _T],
        *,
        access_token: str,
        **kwargs: object,
    ) -> _T:
        result = fn(access_token=access_token, **kwargs)
        unauthorized = getattr(result, "unauthorized", False)
        if not unauthorized:
            return result
        if self._oauth is None:
            return result
        refreshed = await self._oauth.refresh_tokens(
            if_still_access_token=access_token,
        )
        if refreshed.outcome is not AmoCrmCrmRestOutcome.SUCCESS:
            return result
        new_access = await self._load_access_token()
        if new_access is None:
            return result
        return fn(access_token=new_access, **kwargs)

    async def _resolve_access_token(self) -> str | None:
        return await self._load_access_token()

    async def _load_access_token(self) -> str | None:
        assert self._config.rest is not None
        async with session_scope(self._session_factory) as session:
            row = await oauth_repo.get_by_scope(
                session,
                connection_scope=self._config.rest.connection_scope,
            )
            if row is None:
                return None
            tokens = oauth_repo.decrypt_row(row, key_provider=self._key_provider)
            return tokens.access_token
