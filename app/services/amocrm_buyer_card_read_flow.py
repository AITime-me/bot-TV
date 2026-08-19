"""Read-only Buyer Card orchestration (IR-4).

Local graph snapshot → IR-2/IR-3 HTTP → fresh local re-check →
IdentityResolutionService.reconcile_buyer_card. No attach, no CRM writes,
no webhook/worker wiring. IdentityResolutionService stays network-free.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_buyer_card_discovery import (
    AmoCrmBuyerCardDiscoveryOutcome,
    AmoCrmBuyerCardDiscoveryResult,
    BuyerCardReconcileCandidates,
    buyer_card_reconcile_candidates_from_discovery,
)
from app.core.amocrm_buyer_card_read_flow import (
    AmoCrmBuyerCardReadOutcome,
    AmoCrmBuyerCardReadResult,
    BuyerCardContactSource,
)
from app.core.amocrm_crm_oauth_keys import AmoCrmOauthKeyProvider
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.amocrm_crm_rest_http import AmoCrmCrmRestTransport
from app.core.amocrm_identity_lookup import (
    AmoCrmIdentityLookupOutcome,
    AmoCrmIdentityLookupResult,
)
from app.core.identity_resolution import (
    CanonicalIdentityStatus,
    IdentityEntityKind,
    IdentityResolutionError,
    ReconcileBuyerCardOutcome,
    ReconcileBuyerCardResult,
    require_canonical_identity_id,
)
from app.db.session import session_scope
from app.repositories import identity_resolution as identity_repo
from app.services.amocrm_buyer_card_discovery import AmoCrmBuyerCardDiscoveryService
from app.services.amocrm_identity_lookup import AmoCrmIdentityLookupService
from app.services.identity_resolution import IdentityResolutionService

__all__ = ("AmoCrmBuyerCardReadFlowService",)


@dataclass(frozen=True, slots=True)
class _GraphSnapshot:
    canonical_id: uuid.UUID
    active: bool
    contact_ids: tuple[str, ...]
    technical_deal_ids: tuple[str, ...]


_LoadSnapshot = Callable[[uuid.UUID], Awaitable[_GraphSnapshot | None]]
_ReconcileFn = Callable[..., Awaitable[ReconcileBuyerCardResult]]


class AmoCrmBuyerCardReadFlowService:
    """Fail-closed read-only Buyer Card reuse check. Never attaches."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        lookup: AmoCrmIdentityLookupService | None = None,
        discovery: AmoCrmBuyerCardDiscoveryService | None = None,
        config: AmoCrmCrmRestConfig | None = None,
        key_provider: AmoCrmOauthKeyProvider | None = None,
        transport: AmoCrmCrmRestTransport | None = None,
        load_snapshot: _LoadSnapshot | None = None,
        reconcile: _ReconcileFn | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._lookup = lookup or AmoCrmIdentityLookupService(
            session_factory=session_factory,
            config=config,
            key_provider=key_provider,
            transport=transport,
        )
        self._discovery = discovery or AmoCrmBuyerCardDiscoveryService(
            session_factory=session_factory,
            config=config,
            key_provider=key_provider,
            transport=transport,
        )
        self._load_snapshot_override = load_snapshot
        self._reconcile_override = reconcile

    async def read_buyer_card(
        self,
        *,
        canonical_identity_id: object,
        phone: object | None = None,
    ) -> AmoCrmBuyerCardReadResult:
        try:
            cid = uuid.UUID(require_canonical_identity_id(canonical_identity_id))
        except (IdentityResolutionError, ValueError, TypeError):
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.INVALID_INPUT,
                error_code="CANONICAL_IDENTITY_ID_INVALID",
            )

        snapshot = await self._load_snapshot(cid)
        if snapshot is None:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.NOT_FOUND,
                canonical_identity_id=cid,
                error_code="CANONICAL_NOT_FOUND",
            )
        if not snapshot.active:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.NOT_FOUND,
                canonical_identity_id=cid,
                error_code="CANONICAL_NOT_ACTIVE",
            )

        resolved = await self._resolve_contact(snapshot, phone=phone)
        if isinstance(resolved, AmoCrmBuyerCardReadResult):
            return resolved
        contact_id, source, http_calls = resolved

        discovery = await self._discovery.discover_buyer_card_candidates(
            contact_id=contact_id,
        )
        http_calls = http_calls + discovery.http_calls
        if discovery.outcome is AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND:
            raced = await self._recheck_after_http(
                canonical_id=cid,
                contact_id=contact_id,
                source=source,
                http_calls=http_calls,
            )
            if raced is not None:
                return raced
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.NOT_FOUND,
                canonical_identity_id=cid,
                contact_id=contact_id,
                contact_source=source,
                error_code=discovery.error_code or "AMOCRM_BUYER_CARD_NO_ELIGIBLE",
                http_calls=http_calls,
            )
        mapped_discovery = self._map_discovery(
            discovery,
            canonical_id=cid,
            contact_id=contact_id,
            source=source,
            http_calls=http_calls,
        )
        if mapped_discovery is not None:
            return mapped_discovery
        candidates = buyer_card_reconcile_candidates_from_discovery(discovery)
        if candidates is None:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.PERMANENT_ERROR,
                canonical_identity_id=cid,
                contact_id=contact_id,
                contact_source=source,
                error_code="AMOCRM_BUYER_CARD_DISCOVERY_UNMAPPED",
                http_calls=http_calls,
            )
        return await self._finalize(
            canonical_id=cid,
            contact_id=contact_id,
            source=source,
            candidates=candidates,
            http_calls=http_calls,
        )

    async def _resolve_contact(
        self,
        snapshot: _GraphSnapshot,
        *,
        phone: object | None,
    ) -> tuple[str, BuyerCardContactSource, tuple[str, ...]] | AmoCrmBuyerCardReadResult:
        contacts = snapshot.contact_ids
        if len(contacts) > 1:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.MANUAL_REVIEW_REQUIRED,
                canonical_identity_id=snapshot.canonical_id,
                reason="AMBIGUOUS_AMOCRM_CONTACTS",
            )
        if len(contacts) == 1:
            durable_id = contacts[0]
            looked = await self._lookup.lookup_contact_by_id(contact_id=durable_id)
            mapped = self._map_lookup(
                looked,
                canonical_id=snapshot.canonical_id,
                expected_contact_id=durable_id,
                source=BuyerCardContactSource.DURABLE_LINK,
            )
            if mapped is not None:
                return mapped
            if looked.contact_id != durable_id:
                return AmoCrmBuyerCardReadResult(
                    outcome=AmoCrmBuyerCardReadOutcome.PERMANENT_ERROR,
                    canonical_identity_id=snapshot.canonical_id,
                    contact_source=BuyerCardContactSource.DURABLE_LINK,
                    error_code="AMOCRM_CRM_CONTACT_ID_MISMATCH",
                    http_calls=looked.http_calls,
                )
            return (
                durable_id,
                BuyerCardContactSource.DURABLE_LINK,
                looked.http_calls,
            )

        if phone is None:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.NOT_FOUND,
                canonical_identity_id=snapshot.canonical_id,
                error_code="CONTACT_NOT_RESOLVED",
            )
        looked = await self._lookup.lookup_contact_by_phone(phone=phone)
        mapped = self._map_lookup(
            looked,
            canonical_id=snapshot.canonical_id,
            expected_contact_id=None,
            source=BuyerCardContactSource.PHONE_LOOKUP,
        )
        if mapped is not None:
            return mapped
        assert looked.contact_id is not None
        return (
            looked.contact_id,
            BuyerCardContactSource.PHONE_LOOKUP,
            looked.http_calls,
        )

    def _map_lookup(
        self,
        looked: AmoCrmIdentityLookupResult,
        *,
        canonical_id: uuid.UUID,
        expected_contact_id: str | None,
        source: BuyerCardContactSource,
    ) -> AmoCrmBuyerCardReadResult | None:
        outcome = looked.outcome
        if outcome is AmoCrmIdentityLookupOutcome.FOUND:
            return None
        if outcome is AmoCrmIdentityLookupOutcome.NOT_FOUND:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.NOT_FOUND,
                canonical_identity_id=canonical_id,
                contact_source=source if expected_contact_id is not None else None,
                error_code=looked.error_code or "CONTACT_NOT_FOUND",
                http_calls=looked.http_calls,
            )
        if outcome is AmoCrmIdentityLookupOutcome.AMBIGUOUS:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.MANUAL_REVIEW_REQUIRED,
                canonical_identity_id=canonical_id,
                reason="AMBIGUOUS_PHONE_CONTACTS",
                error_code=looked.error_code,
                http_calls=looked.http_calls,
            )
        if outcome is AmoCrmIdentityLookupOutcome.INVALID_INPUT:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.INVALID_INPUT,
                canonical_identity_id=canonical_id,
                error_code=looked.error_code or "INVALID_INPUT",
                http_calls=looked.http_calls,
            )
        if outcome is AmoCrmIdentityLookupOutcome.INCOMPLETE:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.INCOMPLETE,
                canonical_identity_id=canonical_id,
                contact_source=source if expected_contact_id is not None else None,
                error_code=looked.error_code,
                http_calls=looked.http_calls,
            )
        if outcome is AmoCrmIdentityLookupOutcome.DISABLED:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.DISABLED,
                canonical_identity_id=canonical_id,
                error_code=looked.error_code or "AMOCRM_CRM_REST_DISABLED",
                http_calls=looked.http_calls,
            )
        if outcome is AmoCrmIdentityLookupOutcome.TRANSIENT_ERROR:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.TRANSIENT_ERROR,
                canonical_identity_id=canonical_id,
                error_code=looked.error_code,
                http_calls=looked.http_calls,
            )
        return AmoCrmBuyerCardReadResult(
            outcome=AmoCrmBuyerCardReadOutcome.PERMANENT_ERROR,
            canonical_identity_id=canonical_id,
            error_code=looked.error_code,
            http_calls=looked.http_calls,
        )

    def _map_discovery(
        self,
        discovery: AmoCrmBuyerCardDiscoveryResult,
        *,
        canonical_id: uuid.UUID,
        contact_id: str,
        source: BuyerCardContactSource,
        http_calls: tuple[str, ...],
    ) -> AmoCrmBuyerCardReadResult | None:
        outcome = discovery.outcome
        if outcome is AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE:
            return None
        if outcome is AmoCrmBuyerCardDiscoveryOutcome.AMBIGUOUS:
            return None
        if outcome is AmoCrmBuyerCardDiscoveryOutcome.INCOMPLETE:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.INCOMPLETE,
                canonical_identity_id=canonical_id,
                contact_id=contact_id,
                contact_source=source,
                error_code=discovery.error_code,
                http_calls=http_calls,
            )
        if outcome is AmoCrmBuyerCardDiscoveryOutcome.INVALID_INPUT:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.INVALID_INPUT,
                canonical_identity_id=canonical_id,
                contact_id=contact_id,
                contact_source=source,
                error_code=discovery.error_code,
                http_calls=http_calls,
            )
        if outcome is AmoCrmBuyerCardDiscoveryOutcome.DISABLED:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.DISABLED,
                canonical_identity_id=canonical_id,
                contact_id=contact_id,
                contact_source=source,
                error_code=discovery.error_code or "AMOCRM_CRM_REST_DISABLED",
                http_calls=http_calls,
            )
        if outcome is AmoCrmBuyerCardDiscoveryOutcome.TRANSIENT_ERROR:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.TRANSIENT_ERROR,
                canonical_identity_id=canonical_id,
                contact_id=contact_id,
                contact_source=source,
                error_code=discovery.error_code,
                http_calls=http_calls,
            )
        return AmoCrmBuyerCardReadResult(
            outcome=AmoCrmBuyerCardReadOutcome.PERMANENT_ERROR,
            canonical_identity_id=canonical_id,
            contact_id=contact_id,
            contact_source=source,
            error_code=discovery.error_code,
            http_calls=http_calls,
        )

    async def _recheck_after_http(
        self,
        *,
        canonical_id: uuid.UUID,
        contact_id: str,
        source: BuyerCardContactSource,
        http_calls: tuple[str, ...],
    ) -> AmoCrmBuyerCardReadResult | None:
        # Fresh graph only. IR-3 NOT_FOUND must never reach reconcile_buyer_card.
        fresh = await self._load_snapshot(canonical_id)
        return self._race_result(
            fresh,
            canonical_id=canonical_id,
            contact_id=contact_id,
            source=source,
            http_calls=http_calls,
        )

    async def _finalize(
        self,
        *,
        canonical_id: uuid.UUID,
        contact_id: str,
        source: BuyerCardContactSource,
        candidates: BuyerCardReconcileCandidates,
        http_calls: tuple[str, ...],
    ) -> AmoCrmBuyerCardReadResult:
        if self._load_snapshot_override is not None or self._reconcile_override is not None:
            fresh = await self._load_snapshot(canonical_id)
            raced = self._race_result(
                fresh,
                canonical_id=canonical_id,
                contact_id=contact_id,
                source=source,
                http_calls=http_calls,
            )
            if raced is not None:
                return raced
            reconciled = await self._reconcile(
                canonical_identity_id=canonical_id,
                candidate_buyer_card_ids=candidates.candidate_buyer_card_ids,
                candidate_technical_deal_ids=candidates.candidate_technical_deal_ids,
            )
            return self._map_reconcile(
                reconciled,
                canonical_id=canonical_id,
                contact_id=contact_id,
                source=source,
                http_calls=http_calls,
            )
        async with session_scope(self._session_factory) as session:
            fresh = await self._read_graph(session, canonical_id)
            raced = self._race_result(
                fresh,
                canonical_id=canonical_id,
                contact_id=contact_id,
                source=source,
                http_calls=http_calls,
            )
            if raced is not None:
                return raced
            reconciled = await IdentityResolutionService(session).reconcile_buyer_card(
                canonical_identity_id=canonical_id,
                candidate_buyer_card_ids=candidates.candidate_buyer_card_ids,
                candidate_technical_deal_ids=candidates.candidate_technical_deal_ids,
            )
            return self._map_reconcile(
                reconciled,
                canonical_id=canonical_id,
                contact_id=contact_id,
                source=source,
                http_calls=http_calls,
            )

    def _race_result(
        self,
        fresh: _GraphSnapshot | None,
        *,
        canonical_id: uuid.UUID,
        contact_id: str,
        source: BuyerCardContactSource,
        http_calls: tuple[str, ...],
    ) -> AmoCrmBuyerCardReadResult | None:
        if fresh is None or not fresh.active:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.MANUAL_REVIEW_REQUIRED,
                canonical_identity_id=canonical_id,
                contact_id=contact_id,
                contact_source=source,
                reason="CANONICAL_CONTEXT_CHANGED",
                http_calls=http_calls,
            )
        contacts = fresh.contact_ids
        if source is BuyerCardContactSource.DURABLE_LINK:
            if len(contacts) != 1 or contacts[0] != contact_id:
                return AmoCrmBuyerCardReadResult(
                    outcome=AmoCrmBuyerCardReadOutcome.MANUAL_REVIEW_REQUIRED,
                    canonical_identity_id=canonical_id,
                    contact_id=contact_id,
                    contact_source=source,
                    reason="DURABLE_CONTACT_CHANGED",
                    http_calls=http_calls,
                )
            return None
        if len(contacts) == 0:
            return None
        if len(contacts) == 1 and contacts[0] == contact_id:
            return None
        return AmoCrmBuyerCardReadResult(
            outcome=AmoCrmBuyerCardReadOutcome.MANUAL_REVIEW_REQUIRED,
            canonical_identity_id=canonical_id,
            contact_id=contact_id,
            contact_source=source,
            reason="PHONE_CONTACT_CONFLICTS_DURABLE",
            http_calls=http_calls,
        )

    def _map_reconcile(
        self,
        reconciled: ReconcileBuyerCardResult,
        *,
        canonical_id: uuid.UUID,
        contact_id: str,
        source: BuyerCardContactSource,
        http_calls: tuple[str, ...],
    ) -> AmoCrmBuyerCardReadResult:
        outcome = reconciled.outcome
        if outcome is ReconcileBuyerCardOutcome.REUSED:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.REUSED,
                canonical_identity_id=reconciled.canonical_identity_id or canonical_id,
                contact_id=contact_id,
                buyer_card_external_id=reconciled.buyer_card_external_id,
                contact_source=source,
                reason=reconciled.reason,
                http_calls=http_calls,
            )
        if outcome is ReconcileBuyerCardOutcome.MANUAL_REVIEW_REQUIRED:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.MANUAL_REVIEW_REQUIRED,
                canonical_identity_id=canonical_id,
                contact_id=contact_id,
                contact_source=source,
                reason=reconciled.reason,
                http_calls=http_calls,
            )
        if outcome is ReconcileBuyerCardOutcome.INVALID_INPUT:
            return AmoCrmBuyerCardReadResult(
                outcome=AmoCrmBuyerCardReadOutcome.INVALID_INPUT,
                canonical_identity_id=canonical_id,
                contact_id=contact_id,
                contact_source=source,
                error_code="RECONCILE_INVALID_INPUT",
                http_calls=http_calls,
            )
        return AmoCrmBuyerCardReadResult(
            outcome=AmoCrmBuyerCardReadOutcome.NOT_FOUND,
            canonical_identity_id=canonical_id,
            contact_id=contact_id,
            contact_source=source,
            reason=reconciled.reason,
            error_code=None,
            http_calls=http_calls,
        )

    async def _load_snapshot(self, canonical_id: uuid.UUID) -> _GraphSnapshot | None:
        if self._load_snapshot_override is not None:
            return await self._load_snapshot_override(canonical_id)
        async with session_scope(self._session_factory) as session:
            return await self._read_graph(session, canonical_id)

    async def _reconcile(
        self,
        *,
        canonical_identity_id: uuid.UUID,
        candidate_buyer_card_ids: tuple[str, ...],
        candidate_technical_deal_ids: tuple[str, ...],
    ) -> ReconcileBuyerCardResult:
        if self._reconcile_override is not None:
            return await self._reconcile_override(
                canonical_identity_id=canonical_identity_id,
                candidate_buyer_card_ids=candidate_buyer_card_ids,
                candidate_technical_deal_ids=candidate_technical_deal_ids,
            )
        async with session_scope(self._session_factory) as session:
            return await IdentityResolutionService(session).reconcile_buyer_card(
                canonical_identity_id=canonical_identity_id,
                candidate_buyer_card_ids=candidate_buyer_card_ids,
                candidate_technical_deal_ids=candidate_technical_deal_ids,
            )

    @staticmethod
    async def _read_graph(
        session: AsyncSession,
        canonical_id: uuid.UUID,
    ) -> _GraphSnapshot | None:
        identity = await identity_repo.get_canonical(session, identity_id=canonical_id)
        if identity is None:
            return None
        links = await identity_repo.list_links_for_canonical(
            session,
            canonical_identity_id=canonical_id,
            active_only=True,
        )
        return _GraphSnapshot(
            canonical_id=canonical_id,
            active=identity.status == CanonicalIdentityStatus.ACTIVE.value,
            contact_ids=_unique_ids_for_kind(
                links, IdentityEntityKind.AMOCRM_CONTACT
            ),
            technical_deal_ids=_unique_ids_for_kind(
                links, IdentityEntityKind.AMOCRM_TECHNICAL_DEAL
            ),
        )


def _unique_ids_for_kind(links: Sequence[object], kind: IdentityEntityKind) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    expected = kind.value
    for link in links:
        if getattr(link, "entity_kind", None) != expected:
            continue
        token = getattr(link, "external_id", None)
        if type(token) is not str or not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return tuple(sorted(out))
