"""Fail-closed manual amoCRM Buyer Card bind (IR-5).

Initial graph snapshot → IR-2 by-id → IR-3 discovery → fresh race
re-check + reconcile_buyer_card + attach in one DB session.
Local identity write only. No CRM writes, no phone fallback, no review-cases.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_buyer_card_bind import (
    AMOCRM_BUYER_CARD_BIND_SOURCE,
    AmoCrmBuyerCardBindOutcome,
    AmoCrmBuyerCardBindResult,
)
from app.core.amocrm_buyer_card_discovery import (
    AmoCrmBuyerCardDiscoveryOutcome,
    AmoCrmBuyerCardDiscoveryResult,
)
from app.core.amocrm_crm_oauth_keys import AmoCrmOauthKeyProvider
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.amocrm_crm_rest_http import AmoCrmCrmRestTransport
from app.core.amocrm_identity_lookup import (
    AMOCRM_IDENTITY_PROVIDER,
    AmoCrmIdentityLookupOutcome,
    AmoCrmIdentityLookupResult,
)
from app.core.identity_resolution import (
    DEFAULT_CONNECTION_SCOPE,
    AttachIdentityLinkOutcome,
    AttachIdentityLinkResult,
    CanonicalIdentityStatus,
    IdentityEntityKind,
    IdentityLinkConfidence,
    IdentityResolutionError,
    ReconcileBuyerCardOutcome,
    ReconcileBuyerCardResult,
    normalize_external_id,
    require_canonical_identity_id,
)
from app.db.session import session_scope
from app.repositories import identity_resolution as identity_repo
from app.services.amocrm_buyer_card_discovery import AmoCrmBuyerCardDiscoveryService
from app.services.amocrm_identity_lookup import AmoCrmIdentityLookupService
from app.services.identity_resolution import IdentityResolutionService

__all__ = ("AmoCrmBuyerCardBindService",)


@dataclass(frozen=True, slots=True)
class _GraphSnapshot:
    canonical_id: uuid.UUID
    active: bool
    contact_ids: tuple[str, ...]
    technical_deal_ids: tuple[str, ...]
    buyer_card_ids: tuple[str, ...]


_LoadSnapshot = Callable[[uuid.UUID], Awaitable[_GraphSnapshot | None]]
_ReconcileFn = Callable[..., Awaitable[ReconcileBuyerCardResult]]
_AttachFn = Callable[..., Awaitable[AttachIdentityLinkResult]]


class AmoCrmBuyerCardBindService:
    """Explicit one-card bind after live IR-2 + IR-3. Never creates canonical."""

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
        attach: _AttachFn | None = None,
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
        self._attach_override = attach

    async def bind_buyer_card(
        self,
        *,
        canonical_identity_id: object,
        contact_id: object,
        buyer_card_id: object,
    ) -> AmoCrmBuyerCardBindResult:
        try:
            cid = uuid.UUID(require_canonical_identity_id(canonical_identity_id))
            expected_contact = normalize_external_id(contact_id)
            expected_card = normalize_external_id(buyer_card_id)
        except (IdentityResolutionError, ValueError, TypeError):
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.INVALID_INPUT,
                error_code="BIND_INPUT_INVALID",
            )

        snapshot = await self._load_snapshot(cid)
        if snapshot is None:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.NOT_FOUND,
                canonical_identity_id=cid,
                error_code="CANONICAL_NOT_FOUND",
            )
        if not snapshot.active:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.NOT_FOUND,
                canonical_identity_id=cid,
                error_code="CANONICAL_NOT_ACTIVE",
            )

        blocked = self._durable_contact_gate(
            snapshot,
            canonical_id=cid,
            expected_contact=expected_contact,
        )
        if blocked is not None:
            return blocked

        looked = await self._lookup.lookup_contact_by_id(contact_id=expected_contact)
        mapped_lookup = self._map_lookup(
            looked,
            canonical_id=cid,
            expected_contact=expected_contact,
        )
        if mapped_lookup is not None:
            return mapped_lookup
        if looked.contact_id != expected_contact:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.PERMANENT_ERROR,
                canonical_identity_id=cid,
                contact_id=expected_contact,
                error_code="AMOCRM_CRM_CONTACT_ID_MISMATCH",
                http_calls=looked.http_calls,
            )

        discovery = await self._discovery.discover_buyer_card_candidates(
            contact_id=expected_contact,
        )
        http_calls = looked.http_calls + discovery.http_calls
        mapped_discovery = self._map_discovery(
            discovery,
            canonical_id=cid,
            expected_contact=expected_contact,
            expected_card=expected_card,
            http_calls=http_calls,
        )
        if mapped_discovery is not None:
            return mapped_discovery

        return await self._finalize(
            canonical_id=cid,
            expected_contact=expected_contact,
            expected_card=expected_card,
            http_calls=http_calls,
        )

    def _durable_contact_gate(
        self,
        snapshot: _GraphSnapshot,
        *,
        canonical_id: uuid.UUID,
        expected_contact: str,
    ) -> AmoCrmBuyerCardBindResult | None:
        contacts = snapshot.contact_ids
        if len(contacts) == 0:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.NOT_FOUND,
                canonical_identity_id=canonical_id,
                error_code="DURABLE_CONTACT_MISSING",
            )
        if len(contacts) > 1:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED,
                canonical_identity_id=canonical_id,
                reason="AMBIGUOUS_AMOCRM_CONTACTS",
            )
        if contacts[0] != expected_contact:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED,
                canonical_identity_id=canonical_id,
                reason="DURABLE_CONTACT_MISMATCH",
            )
        return None

    def _map_lookup(
        self,
        looked: AmoCrmIdentityLookupResult,
        *,
        canonical_id: uuid.UUID,
        expected_contact: str,
    ) -> AmoCrmBuyerCardBindResult | None:
        outcome = looked.outcome
        if outcome is AmoCrmIdentityLookupOutcome.FOUND:
            return None
        if outcome is AmoCrmIdentityLookupOutcome.NOT_FOUND:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.NOT_FOUND,
                canonical_identity_id=canonical_id,
                contact_id=expected_contact,
                error_code=looked.error_code or "CONTACT_NOT_FOUND",
                http_calls=looked.http_calls,
            )
        if outcome is AmoCrmIdentityLookupOutcome.AMBIGUOUS:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED,
                canonical_identity_id=canonical_id,
                contact_id=expected_contact,
                reason="AMBIGUOUS_PHONE_CONTACTS",
                error_code=looked.error_code,
                http_calls=looked.http_calls,
            )
        if outcome is AmoCrmIdentityLookupOutcome.INVALID_INPUT:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.INVALID_INPUT,
                canonical_identity_id=canonical_id,
                error_code=looked.error_code or "INVALID_INPUT",
                http_calls=looked.http_calls,
            )
        if outcome is AmoCrmIdentityLookupOutcome.INCOMPLETE:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.INCOMPLETE,
                canonical_identity_id=canonical_id,
                contact_id=expected_contact,
                error_code=looked.error_code,
                http_calls=looked.http_calls,
            )
        if outcome is AmoCrmIdentityLookupOutcome.DISABLED:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.DISABLED,
                canonical_identity_id=canonical_id,
                error_code=looked.error_code or "AMOCRM_CRM_REST_DISABLED",
                http_calls=looked.http_calls,
            )
        if outcome is AmoCrmIdentityLookupOutcome.TRANSIENT_ERROR:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.TRANSIENT_ERROR,
                canonical_identity_id=canonical_id,
                error_code=looked.error_code,
                http_calls=looked.http_calls,
            )
        return AmoCrmBuyerCardBindResult(
            outcome=AmoCrmBuyerCardBindOutcome.PERMANENT_ERROR,
            canonical_identity_id=canonical_id,
            error_code=looked.error_code,
            http_calls=looked.http_calls,
        )

    def _map_discovery(
        self,
        discovery: AmoCrmBuyerCardDiscoveryResult,
        *,
        canonical_id: uuid.UUID,
        expected_contact: str,
        expected_card: str,
        http_calls: tuple[str, ...],
    ) -> AmoCrmBuyerCardBindResult | None:
        outcome = discovery.outcome
        if outcome is AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE:
            eligible = discovery.eligible_customer_ids
            if len(eligible) != 1 or eligible[0] != expected_card:
                return AmoCrmBuyerCardBindResult(
                    outcome=AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED,
                    canonical_identity_id=canonical_id,
                    contact_id=expected_contact,
                    reason="EXPECTED_BUYER_CARD_MISMATCH",
                    http_calls=http_calls,
                )
            return None
        if outcome is AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.NOT_FOUND,
                canonical_identity_id=canonical_id,
                contact_id=expected_contact,
                error_code=discovery.error_code or "AMOCRM_BUYER_CARD_NO_ELIGIBLE",
                http_calls=http_calls,
            )
        if outcome is AmoCrmBuyerCardDiscoveryOutcome.AMBIGUOUS:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED,
                canonical_identity_id=canonical_id,
                contact_id=expected_contact,
                reason="AMBIGUOUS_BUYER_CARD_CANDIDATES",
                error_code=discovery.error_code,
                http_calls=http_calls,
            )
        if outcome is AmoCrmBuyerCardDiscoveryOutcome.INCOMPLETE:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.INCOMPLETE,
                canonical_identity_id=canonical_id,
                contact_id=expected_contact,
                error_code=discovery.error_code,
                http_calls=http_calls,
            )
        if outcome is AmoCrmBuyerCardDiscoveryOutcome.INVALID_INPUT:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.INVALID_INPUT,
                canonical_identity_id=canonical_id,
                contact_id=expected_contact,
                error_code=discovery.error_code,
                http_calls=http_calls,
            )
        if outcome is AmoCrmBuyerCardDiscoveryOutcome.DISABLED:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.DISABLED,
                canonical_identity_id=canonical_id,
                contact_id=expected_contact,
                error_code=discovery.error_code or "AMOCRM_CRM_REST_DISABLED",
                http_calls=http_calls,
            )
        if outcome is AmoCrmBuyerCardDiscoveryOutcome.TRANSIENT_ERROR:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.TRANSIENT_ERROR,
                canonical_identity_id=canonical_id,
                contact_id=expected_contact,
                error_code=discovery.error_code,
                http_calls=http_calls,
            )
        return AmoCrmBuyerCardBindResult(
            outcome=AmoCrmBuyerCardBindOutcome.PERMANENT_ERROR,
            canonical_identity_id=canonical_id,
            contact_id=expected_contact,
            error_code=discovery.error_code,
            http_calls=http_calls,
        )

    async def _finalize(
        self,
        *,
        canonical_id: uuid.UUID,
        expected_contact: str,
        expected_card: str,
        http_calls: tuple[str, ...],
    ) -> AmoCrmBuyerCardBindResult:
        if (
            self._load_snapshot_override is not None
            or self._reconcile_override is not None
            or self._attach_override is not None
        ):
            fresh = await self._load_snapshot(canonical_id)
            raced = self._race_result(
                fresh,
                canonical_id=canonical_id,
                expected_contact=expected_contact,
                http_calls=http_calls,
            )
            if raced is not None:
                return raced
            assert fresh is not None
            reconciled = await self._reconcile(
                canonical_identity_id=canonical_id,
                candidate_buyer_card_ids=(expected_card,),
            )
            mapped = self._map_reconcile(
                reconciled,
                canonical_id=canonical_id,
                expected_contact=expected_contact,
                expected_card=expected_card,
                http_calls=http_calls,
            )
            if mapped is not None:
                return mapped
            attached = await self._attach(
                canonical_identity_id=canonical_id,
                buyer_card_id=expected_card,
            )
            return self._map_attach(
                attached,
                canonical_id=canonical_id,
                expected_contact=expected_contact,
                expected_card=expected_card,
                http_calls=http_calls,
            )

        async with session_scope(self._session_factory) as session:
            locked = await identity_repo.lock_canonical(
                session, identity_id=canonical_id
            )
            if locked is None:
                return self._race_result(
                    None,
                    canonical_id=canonical_id,
                    expected_contact=expected_contact,
                    http_calls=http_calls,
                )
            fresh = await self._read_graph(session, canonical_id)
            raced = self._race_result(
                fresh,
                canonical_id=canonical_id,
                expected_contact=expected_contact,
                http_calls=http_calls,
            )
            if raced is not None:
                return raced
            identity = IdentityResolutionService(session)
            reconciled = await identity.reconcile_buyer_card(
                canonical_identity_id=canonical_id,
                candidate_buyer_card_ids=(expected_card,),
            )
            mapped = self._map_reconcile(
                reconciled,
                canonical_id=canonical_id,
                expected_contact=expected_contact,
                expected_card=expected_card,
                http_calls=http_calls,
            )
            if mapped is not None:
                return mapped
            attached = await identity.attach(
                provider=AMOCRM_IDENTITY_PROVIDER,
                connection_scope=DEFAULT_CONNECTION_SCOPE,
                entity_kind=IdentityEntityKind.AMOCRM_BUYER_CARD,
                external_id=expected_card,
                canonical_identity_id=canonical_id,
                confidence=IdentityLinkConfidence.CONFIRMED,
                source=AMOCRM_BUYER_CARD_BIND_SOURCE,
                create_canonical=False,
            )
            return self._map_attach(
                attached,
                canonical_id=canonical_id,
                expected_contact=expected_contact,
                expected_card=expected_card,
                http_calls=http_calls,
            )

    def _race_result(
        self,
        fresh: _GraphSnapshot | None,
        *,
        canonical_id: uuid.UUID,
        expected_contact: str,
        http_calls: tuple[str, ...],
    ) -> AmoCrmBuyerCardBindResult | None:
        if fresh is None or not fresh.active:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED,
                canonical_identity_id=canonical_id,
                contact_id=expected_contact,
                reason="CANONICAL_CONTEXT_CHANGED",
                http_calls=http_calls,
            )
        contacts = fresh.contact_ids
        if len(contacts) != 1 or contacts[0] != expected_contact:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED,
                canonical_identity_id=canonical_id,
                contact_id=expected_contact,
                reason="DURABLE_CONTACT_CHANGED",
                http_calls=http_calls,
            )
        return None

    def _map_reconcile(
        self,
        reconciled: ReconcileBuyerCardResult,
        *,
        canonical_id: uuid.UUID,
        expected_contact: str,
        expected_card: str,
        http_calls: tuple[str, ...],
    ) -> AmoCrmBuyerCardBindResult | None:
        outcome = reconciled.outcome
        if outcome is ReconcileBuyerCardOutcome.REUSED:
            if reconciled.buyer_card_external_id != expected_card:
                return AmoCrmBuyerCardBindResult(
                    outcome=AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED,
                    canonical_identity_id=canonical_id,
                    contact_id=expected_contact,
                    reason=reconciled.reason or "BUYER_CARD_AMBIGUOUS",
                    http_calls=http_calls,
                )
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.ALREADY_BOUND,
                canonical_identity_id=reconciled.canonical_identity_id or canonical_id,
                contact_id=expected_contact,
                buyer_card_id=expected_card,
                reason=reconciled.reason,
                http_calls=http_calls,
            )
        if outcome is ReconcileBuyerCardOutcome.MANUAL_REVIEW_REQUIRED:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED,
                canonical_identity_id=canonical_id,
                contact_id=expected_contact,
                reason=reconciled.reason,
                http_calls=http_calls,
            )
        if outcome is ReconcileBuyerCardOutcome.INVALID_INPUT:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.INVALID_INPUT,
                canonical_identity_id=canonical_id,
                contact_id=expected_contact,
                error_code="RECONCILE_INVALID_INPUT",
                http_calls=http_calls,
            )
        return None

    def _map_attach(
        self,
        attached: AttachIdentityLinkResult,
        *,
        canonical_id: uuid.UUID,
        expected_contact: str,
        expected_card: str,
        http_calls: tuple[str, ...],
    ) -> AmoCrmBuyerCardBindResult:
        outcome = attached.outcome
        if outcome is AttachIdentityLinkOutcome.LINKED:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.BOUND,
                canonical_identity_id=attached.canonical_identity_id or canonical_id,
                contact_id=expected_contact,
                buyer_card_id=expected_card,
                http_calls=http_calls,
            )
        if outcome is AttachIdentityLinkOutcome.ALREADY_LINKED:
            if attached.canonical_identity_id == canonical_id:
                return AmoCrmBuyerCardBindResult(
                    outcome=AmoCrmBuyerCardBindOutcome.ALREADY_BOUND,
                    canonical_identity_id=canonical_id,
                    contact_id=expected_contact,
                    buyer_card_id=expected_card,
                    reason=attached.reason,
                    http_calls=http_calls,
                )
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED,
                canonical_identity_id=canonical_id,
                contact_id=expected_contact,
                reason=attached.reason or "BUYER_CARD_BOUND_ELSEWHERE",
                http_calls=http_calls,
            )
        if outcome is AttachIdentityLinkOutcome.INVALID_INPUT:
            return AmoCrmBuyerCardBindResult(
                outcome=AmoCrmBuyerCardBindOutcome.INVALID_INPUT,
                canonical_identity_id=canonical_id,
                contact_id=expected_contact,
                error_code="ATTACH_INVALID_INPUT",
                http_calls=http_calls,
            )
        return AmoCrmBuyerCardBindResult(
            outcome=AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED,
            canonical_identity_id=canonical_id,
            contact_id=expected_contact,
            reason=attached.reason or "ATTACH_CONFLICT",
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
    ) -> ReconcileBuyerCardResult:
        if self._reconcile_override is not None:
            return await self._reconcile_override(
                canonical_identity_id=canonical_identity_id,
                candidate_buyer_card_ids=candidate_buyer_card_ids,
            )
        async with session_scope(self._session_factory) as session:
            return await IdentityResolutionService(session).reconcile_buyer_card(
                canonical_identity_id=canonical_identity_id,
                candidate_buyer_card_ids=candidate_buyer_card_ids,
            )

    async def _attach(
        self,
        *,
        canonical_identity_id: uuid.UUID,
        buyer_card_id: str,
    ) -> AttachIdentityLinkResult:
        if self._attach_override is not None:
            return await self._attach_override(
                provider=AMOCRM_IDENTITY_PROVIDER,
                connection_scope=DEFAULT_CONNECTION_SCOPE,
                entity_kind=IdentityEntityKind.AMOCRM_BUYER_CARD,
                external_id=buyer_card_id,
                canonical_identity_id=canonical_identity_id,
                confidence=IdentityLinkConfidence.CONFIRMED,
                source=AMOCRM_BUYER_CARD_BIND_SOURCE,
                create_canonical=False,
            )
        async with session_scope(self._session_factory) as session:
            return await IdentityResolutionService(session).attach(
                provider=AMOCRM_IDENTITY_PROVIDER,
                connection_scope=DEFAULT_CONNECTION_SCOPE,
                entity_kind=IdentityEntityKind.AMOCRM_BUYER_CARD,
                external_id=buyer_card_id,
                canonical_identity_id=canonical_identity_id,
                confidence=IdentityLinkConfidence.CONFIRMED,
                source=AMOCRM_BUYER_CARD_BIND_SOURCE,
                create_canonical=False,
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
            buyer_card_ids=_unique_ids_for_kind(
                links, IdentityEntityKind.AMOCRM_BUYER_CARD
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
