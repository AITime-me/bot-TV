"""IR-5 manual Buyer Card bind PostgreSQL coverage."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_buyer_card_bind import (
    AMOCRM_BUYER_CARD_BIND_SOURCE,
    AmoCrmBuyerCardBindOutcome,
)
from app.core.amocrm_buyer_card_discovery import (
    AmoCrmBuyerCardDiscoveryOutcome,
    AmoCrmBuyerCardDiscoveryResult,
)
from app.core.amocrm_identity_lookup import (
    AmoCrmIdentityLookupOutcome,
    AmoCrmIdentityLookupResult,
)
from app.core.identity_resolution import (
    DEFAULT_CONNECTION_SCOPE,
    AttachIdentityLinkOutcome,
    IdentityEntityKind,
    IdentityLinkConfidence,
    IdentityLinkStatus,
    InspectIdentityOutcome,
)
from app.db.session import session_scope
from app.models.canonical_identity import CanonicalIdentity
from app.services.amocrm_buyer_card_bind import AmoCrmBuyerCardBindService
from app.services.identity_resolution import IdentityResolutionService
from tests.pg_harness import truncate_foundation_tables

_CONTACT = "42"
_CARD = "7"
_OTHER_CARD = "8"
_OTHER_CONTACT = "99"


class _FakeLookup:
    def __init__(self, contact_id: str = _CONTACT) -> None:
        self.contact_id = contact_id
        self.by_id_calls = 0

    async def lookup_contact_by_id(self, *, contact_id: object) -> AmoCrmIdentityLookupResult:
        self.by_id_calls += 1
        return AmoCrmIdentityLookupResult(
            outcome=AmoCrmIdentityLookupOutcome.FOUND,
            contact_id=self.contact_id,
            http_calls=("GET_CONTACT_BY_ID",),
        )


class _FakeDiscovery:
    def __init__(self, eligible: tuple[str, ...] = (_CARD,)) -> None:
        self.eligible = eligible
        self.calls: list[tuple[object, object]] = []

    async def discover_buyer_card_candidates(
        self,
        *,
        contact_id: object,
        known_technical_deal_ids: object = (),
    ) -> AmoCrmBuyerCardDiscoveryResult:
        self.calls.append((contact_id, known_technical_deal_ids))
        outcome = (
            AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE
            if len(self.eligible) == 1
            else AmoCrmBuyerCardDiscoveryOutcome.AMBIGUOUS
        )
        return AmoCrmBuyerCardDiscoveryResult(
            outcome=outcome,
            contact_id=str(contact_id),
            eligible_lead_ids=self.eligible,
            known_technical_deal_ids=tuple(known_technical_deal_ids),  # type: ignore[arg-type]
            http_calls=("GET_CONTACT_WITH_LEADS",),
        )


@pytest_asyncio.fixture(autouse=True)
async def identity_row_cleanup(
    request: pytest.FixtureRequest,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    if request.node.get_closest_marker("no_foundation_row_cleanup"):
        yield
        return
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


async def _seed_contact(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    contact_id: str = _CONTACT,
) -> object:
    async with session_scope(session_factory) as session:
        created = await IdentityResolutionService(session).attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_CONTACT,
            external_id=contact_id,
            connection_scope=DEFAULT_CONNECTION_SCOPE,
            create_canonical=True,
        )
        assert created.outcome is AttachIdentityLinkOutcome.CREATED
        assert created.canonical_identity_id is not None
        return created.canonical_identity_id


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    lookup: object | None = None,
    discovery: object | None = None,
) -> AmoCrmBuyerCardBindService:
    return AmoCrmBuyerCardBindService(
        session_factory=session_factory,
        lookup=lookup or _FakeLookup(),  # type: ignore[arg-type]
        discovery=discovery or _FakeDiscovery(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_happy_bind(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cid = await _seed_contact(session_factory)
    result = await _service(session_factory).bind_buyer_card(
        canonical_identity_id=cid,
        contact_id=_CONTACT,
        buyer_card_id=_CARD,
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.BOUND
    assert result.buyer_card_id == _CARD
    async with session_scope(session_factory) as session:
        inspected = await IdentityResolutionService(session).inspect(
            canonical_identity_id=cid
        )
        assert inspected.outcome is InspectIdentityOutcome.FOUND
        assert inspected.graph is not None
        cards = [
            link
            for link in inspected.graph.links
            if link.entity_kind is IdentityEntityKind.AMOCRM_BUYER_CARD
            and link.status is IdentityLinkStatus.ACTIVE
        ]
        assert len(cards) == 1
        assert cards[0].external_id == _CARD
        assert cards[0].source == AMOCRM_BUYER_CARD_BIND_SOURCE
        assert cards[0].confidence is IdentityLinkConfidence.CONFIRMED


@pytest.mark.asyncio
async def test_repeated_idempotent_bind(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cid = await _seed_contact(session_factory)
    first = await _service(session_factory).bind_buyer_card(
        canonical_identity_id=cid,
        contact_id=_CONTACT,
        buyer_card_id=_CARD,
    )
    second = await _service(session_factory).bind_buyer_card(
        canonical_identity_id=cid,
        contact_id=_CONTACT,
        buyer_card_id=_CARD,
    )
    assert first.outcome is AmoCrmBuyerCardBindOutcome.BOUND
    assert second.outcome is AmoCrmBuyerCardBindOutcome.ALREADY_BOUND


@pytest.mark.asyncio
async def test_existing_different_buyer_card_manual(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cid = await _seed_contact(session_factory)
    async with session_scope(session_factory) as session:
        attached = await IdentityResolutionService(session).attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_BUYER_CARD,
            external_id=_OTHER_CARD,
            canonical_identity_id=cid,
        )
        assert attached.outcome is AttachIdentityLinkOutcome.LINKED
    result = await _service(session_factory).bind_buyer_card(
        canonical_identity_id=cid,
        contact_id=_CONTACT,
        buyer_card_id=_CARD,
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "ambiguous_buyer_cards"


@pytest.mark.asyncio
async def test_technical_role_conflict(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cid = await _seed_contact(session_factory)
    async with session_scope(session_factory) as session:
        tech = await IdentityResolutionService(session).attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_TECHNICAL_DEAL,
            external_id=_CARD,
            canonical_identity_id=cid,
        )
        assert tech.outcome is AttachIdentityLinkOutcome.LINKED
    result = await _service(session_factory).bind_buyer_card(
        canonical_identity_id=cid,
        contact_id=_CONTACT,
        buyer_card_id=_CARD,
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "technical_deal_is_not_buyer_card"


@pytest.mark.asyncio
async def test_conflicting_canonical_attach_race(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cid = await _seed_contact(session_factory)
    async with session_scope(session_factory) as session:
        other = await IdentityResolutionService(session).attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_BUYER_CARD,
            external_id=_CARD,
            create_canonical=True,
        )
        assert other.outcome is AttachIdentityLinkOutcome.CREATED
        assert other.canonical_identity_id != cid
    result = await _service(session_factory).bind_buyer_card(
        canonical_identity_id=cid,
        contact_id=_CONTACT,
        buyer_card_id=_CARD,
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED


@pytest.mark.asyncio
async def test_concurrent_same_bind(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cid = await _seed_contact(session_factory)

    async def _once() -> AmoCrmBuyerCardBindOutcome:
        result = await _service(session_factory).bind_buyer_card(
            canonical_identity_id=cid,
            contact_id=_CONTACT,
            buyer_card_id=_CARD,
        )
        return result.outcome

    first, second = await asyncio.gather(_once(), _once())
    assert {first, second} == {
        AmoCrmBuyerCardBindOutcome.BOUND,
        AmoCrmBuyerCardBindOutcome.ALREADY_BOUND,
    }


@pytest.mark.asyncio
async def test_concurrent_different_buyer_cards_one_bound(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cid = await _seed_contact(session_factory)

    async def _bind(card: str) -> AmoCrmBuyerCardBindOutcome:
        result = await _service(
            session_factory,
            discovery=_FakeDiscovery(eligible=(card,)),
        ).bind_buyer_card(
            canonical_identity_id=cid,
            contact_id=_CONTACT,
            buyer_card_id=card,
        )
        return result.outcome

    first, second = await asyncio.gather(_bind(_CARD), _bind(_OTHER_CARD))
    assert {first, second} == {
        AmoCrmBuyerCardBindOutcome.BOUND,
        AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED,
    }
    async with session_scope(session_factory) as session:
        inspected = await IdentityResolutionService(session).inspect(
            canonical_identity_id=cid
        )
        assert inspected.graph is not None
        cards = [
            link
            for link in inspected.graph.links
            if link.entity_kind is IdentityEntityKind.AMOCRM_BUYER_CARD
            and link.status is IdentityLinkStatus.ACTIVE
        ]
        assert len(cards) == 1


@pytest.mark.asyncio
async def test_canonical_archived_during_http(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cid = await _seed_contact(session_factory)

    class _ArchiveLookup(_FakeLookup):
        async def lookup_contact_by_id(
            self, *, contact_id: object
        ) -> AmoCrmIdentityLookupResult:
            async with session_scope(session_factory) as session:
                row = await session.get(CanonicalIdentity, cid)
                assert row is not None
                row.status = "ARCHIVED"
            return await super().lookup_contact_by_id(contact_id=contact_id)

    result = await _service(session_factory, lookup=_ArchiveLookup()).bind_buyer_card(
        canonical_identity_id=cid,
        contact_id=_CONTACT,
        buyer_card_id=_CARD,
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "CANONICAL_CONTEXT_CHANGED"


@pytest.mark.asyncio
async def test_durable_contact_changed_during_http(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cid = await _seed_contact(session_factory)

    class _SwapLookup(_FakeLookup):
        async def lookup_contact_by_id(
            self, *, contact_id: object
        ) -> AmoCrmIdentityLookupResult:
            async with session_scope(session_factory) as session:
                svc = IdentityResolutionService(session)
                revoked = await svc.revoke(
                    provider="amocrm",
                    entity_kind=IdentityEntityKind.AMOCRM_CONTACT,
                    external_id=_CONTACT,
                    connection_scope=DEFAULT_CONNECTION_SCOPE,
                )
                assert revoked.outcome.value == "REVOKED"
                attached = await svc.attach(
                    provider="amocrm",
                    entity_kind=IdentityEntityKind.AMOCRM_CONTACT,
                    external_id=_OTHER_CONTACT,
                    connection_scope=DEFAULT_CONNECTION_SCOPE,
                    canonical_identity_id=cid,
                )
                assert attached.outcome is AttachIdentityLinkOutcome.LINKED
            return await super().lookup_contact_by_id(contact_id=contact_id)

    result = await _service(session_factory, lookup=_SwapLookup()).bind_buyer_card(
        canonical_identity_id=cid,
        contact_id=_CONTACT,
        buyer_card_id=_CARD,
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "DURABLE_CONTACT_CHANGED"
