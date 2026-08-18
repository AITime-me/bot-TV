"""PostgreSQL behavioral and concurrency tests for CURSOR-30 identity resolution."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.identity_resolution import (
    DEFAULT_CONNECTION_SCOPE,
    EMAIL_PROVIDER,
    PHONE_PROVIDER,
    REASON_DEAL_TECH_ROLE_CONFLICT,
    REASON_EMAIL_ONLY_SECONDARY,
    AttachIdentityLinkOutcome,
    IdentityEntityKind,
    IdentityLinkConfidence,
    IdentityLinkStatus,
    IdentityResolveSignals,
    InspectIdentityOutcome,
    ReconcileBuyerCardOutcome,
    ResolveIdentityOutcome,
    RevokeIdentityLinkOutcome,
)
from app.db.session import session_scope
from app.models.canonical_identity import CanonicalIdentity, ExternalIdentityLink
from app.services.identity_resolution import IdentityResolutionService
from tests.pg_harness import truncate_foundation_tables

_PHONE = "+79001234567"
_PHONE_ALT = "+79007654321"
_EMAIL = "client@example.com"
_EMAIL_ALT = "other@example.com"
_ACCOUNT = "vk-user-42"
_BUYER_CARD = "buyer-card-100"
_TECH_DEAL = "tech-deal-200"


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


async def _active_link_count(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    async with session_factory() as session:
        value = await session.scalar(
            select(func.count()).select_from(ExternalIdentityLink).where(
                ExternalIdentityLink.status == "ACTIVE"
            )
        )
        return int(value or 0)


@pytest.mark.asyncio
async def test_migration_creates_identity_tables_and_partial_unique(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        assert await session.scalar(
            text("SELECT to_regclass('public.canonical_identities') IS NOT NULL")
        )
        assert await session.scalar(
            text(
                "SELECT to_regclass('public.external_identity_links') IS NOT NULL"
            )
        )
        partial = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'uq_external_identity_links_active_key'"
            )
        )
        assert partial is not None
        assert "UNIQUE" in partial.upper()
        assert "ACTIVE" in partial


@pytest.mark.asyncio
async def test_exact_existing_link_resolution(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        created = await svc.attach(
            provider="vk",
            entity_kind=IdentityEntityKind.CHANNEL_ACCOUNT,
            external_id=_ACCOUNT,
            connection_scope="vk-group-1",
            create_canonical=True,
        )
        assert created.outcome is AttachIdentityLinkOutcome.CREATED
        resolved = await svc.resolve(
            IdentityResolveSignals(
                channel_provider="vk",
                channel_connection_scope="vk-group-1",
                channel_external_account_id=_ACCOUNT,
            )
        )
        assert resolved.outcome is ResolveIdentityOutcome.RESOLVED
        assert resolved.canonical_identity_id == created.canonical_identity_id
        assert resolved.reason == "exact_channel_link"
        assert resolved.confidence is IdentityLinkConfidence.CONFIRMED


@pytest.mark.asyncio
async def test_channel_scope_isolation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        a = await svc.attach(
            provider="vk",
            entity_kind=IdentityEntityKind.CHANNEL_ACCOUNT,
            external_id=_ACCOUNT,
            connection_scope="scope-a",
            create_canonical=True,
        )
        b = await svc.attach(
            provider="vk",
            entity_kind=IdentityEntityKind.CHANNEL_ACCOUNT,
            external_id=_ACCOUNT,
            connection_scope="scope-b",
            create_canonical=True,
        )
        assert a.outcome is AttachIdentityLinkOutcome.CREATED
        assert b.outcome is AttachIdentityLinkOutcome.CREATED
        assert a.canonical_identity_id != b.canonical_identity_id
        ra = await svc.resolve(
            IdentityResolveSignals(
                channel_provider="vk",
                channel_connection_scope="scope-a",
                channel_external_account_id=_ACCOUNT,
            )
        )
        rb = await svc.resolve(
            IdentityResolveSignals(
                channel_provider="vk",
                channel_connection_scope="scope-b",
                channel_external_account_id=_ACCOUNT,
            )
        )
        assert ra.canonical_identity_id == a.canonical_identity_id
        assert rb.canonical_identity_id == b.canonical_identity_id


@pytest.mark.asyncio
async def test_normalized_phone_match(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        created = await svc.attach(
            provider=PHONE_PROVIDER,
            entity_kind=IdentityEntityKind.PHONE,
            external_id="8 (900) 123-45-67",
            create_canonical=True,
        )
        assert created.outcome is AttachIdentityLinkOutcome.CREATED
        resolved = await svc.resolve(IdentityResolveSignals(phone=_PHONE))
        assert resolved.outcome is ResolveIdentityOutcome.RESOLVED
        assert resolved.canonical_identity_id == created.canonical_identity_id
        assert resolved.reason == "normalized_phone"


@pytest.mark.asyncio
async def test_email_only_is_not_resolved(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        created = await svc.attach(
            provider=EMAIL_PROVIDER,
            entity_kind=IdentityEntityKind.EMAIL,
            external_id=_EMAIL,
            create_canonical=True,
        )
        assert created.outcome is AttachIdentityLinkOutcome.CREATED
        email_only = await svc.resolve(IdentityResolveSignals(email=_EMAIL))
        assert email_only.outcome is ResolveIdentityOutcome.NOT_FOUND
        assert email_only.reason == REASON_EMAIL_ONLY_SECONDARY
        assert email_only.canonical_identity_id is None


@pytest.mark.asyncio
async def test_email_corroborates_primary_phone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        created = await svc.attach(
            provider=PHONE_PROVIDER,
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE,
            create_canonical=True,
        )
        await svc.attach(
            provider=EMAIL_PROVIDER,
            entity_kind=IdentityEntityKind.EMAIL,
            external_id=_EMAIL,
            canonical_identity_id=created.canonical_identity_id,
        )
        resolved = await svc.resolve(
            IdentityResolveSignals(phone=_PHONE, email=_EMAIL)
        )
        assert resolved.outcome is ResolveIdentityOutcome.RESOLVED
        assert resolved.canonical_identity_id == created.canonical_identity_id
        assert resolved.reason == "normalized_phone"
        assert resolved.confidence is IdentityLinkConfidence.CONFIRMED


@pytest.mark.asyncio
async def test_email_conflicts_with_primary_manual_review(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        phone_id = await svc.attach(
            provider=PHONE_PROVIDER,
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE,
            create_canonical=True,
        )
        email_other = await svc.attach(
            provider=EMAIL_PROVIDER,
            entity_kind=IdentityEntityKind.EMAIL,
            external_id=_EMAIL_ALT,
            create_canonical=True,
        )
        assert phone_id.canonical_identity_id != email_other.canonical_identity_id
        conflict = await svc.resolve(
            IdentityResolveSignals(phone=_PHONE, email=_EMAIL_ALT)
        )
        assert conflict.outcome is ResolveIdentityOutcome.MANUAL_REVIEW_REQUIRED


@pytest.mark.asyncio
async def test_zero_candidates_not_found(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        empty = await svc.resolve(IdentityResolveSignals())
        assert empty.outcome is ResolveIdentityOutcome.NOT_FOUND
        assert empty.canonical_identity_id is None


@pytest.mark.asyncio
async def test_multiple_phone_candidates_manual_review(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two ACTIVE phone links with same digits under different scopes → >1 canonical."""

    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        first = await svc.attach(
            provider=PHONE_PROVIDER,
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE,
            connection_scope="import-a",
            create_canonical=True,
        )
        second = await svc.attach(
            provider=PHONE_PROVIDER,
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE,
            connection_scope="import-b",
            create_canonical=True,
        )
        assert first.canonical_identity_id != second.canonical_identity_id
        resolved = await svc.resolve(IdentityResolveSignals(phone=_PHONE))
        assert resolved.outcome is ResolveIdentityOutcome.MANUAL_REVIEW_REQUIRED
        assert resolved.reason == "ambiguous_canonical_candidates"


@pytest.mark.asyncio
async def test_confirmed_link_and_revoked_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        created = await svc.attach(
            provider="online_zapis",
            entity_kind=IdentityEntityKind.ONLINE_ZAPIS_CLIENT,
            external_id="client-uuid-1",
            create_canonical=True,
        )
        revoked = await svc.revoke(
            provider="online_zapis",
            entity_kind=IdentityEntityKind.ONLINE_ZAPIS_CLIENT,
            external_id="client-uuid-1",
        )
        assert revoked.outcome is RevokeIdentityLinkOutcome.REVOKED
        assert revoked.link is not None
        assert revoked.link.status is IdentityLinkStatus.REVOKED

        missing = await svc.resolve(
            IdentityResolveSignals(
                confirmed_links=(
                    (
                        "online_zapis",
                        DEFAULT_CONNECTION_SCOPE,
                        IdentityEntityKind.ONLINE_ZAPIS_CLIENT,
                        "client-uuid-1",
                    ),
                )
            )
        )
        assert missing.outcome is ResolveIdentityOutcome.NOT_FOUND

        # Re-attach after revoke creates a new ACTIVE link (history preserved).
        again = await svc.attach(
            provider="online_zapis",
            entity_kind=IdentityEntityKind.ONLINE_ZAPIS_CLIENT,
            external_id="client-uuid-1",
            canonical_identity_id=created.canonical_identity_id,
        )
        assert again.outcome is AttachIdentityLinkOutcome.LINKED
        found = await svc.resolve(
            IdentityResolveSignals(
                confirmed_links=(
                    (
                        "online_zapis",
                        DEFAULT_CONNECTION_SCOPE,
                        IdentityEntityKind.ONLINE_ZAPIS_CLIENT,
                        "client-uuid-1",
                    ),
                )
            )
        )
        assert found.outcome is ResolveIdentityOutcome.RESOLVED
        assert found.canonical_identity_id == created.canonical_identity_id


@pytest.mark.asyncio
async def test_conflicting_durable_links_manual_review(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        a = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_CONTACT,
            external_id="contact-a",
            create_canonical=True,
        )
        b = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_CONTACT,
            external_id="contact-b",
            create_canonical=True,
        )
        assert a.canonical_identity_id != b.canonical_identity_id
        resolved = await svc.resolve(
            IdentityResolveSignals(
                confirmed_links=(
                    (
                        "amocrm",
                        DEFAULT_CONNECTION_SCOPE,
                        IdentityEntityKind.AMOCRM_CONTACT,
                        "contact-a",
                    ),
                    (
                        "amocrm",
                        DEFAULT_CONNECTION_SCOPE,
                        IdentityEntityKind.AMOCRM_CONTACT,
                        "contact-b",
                    ),
                )
            )
        )
        assert resolved.outcome is ResolveIdentityOutcome.MANUAL_REVIEW_REQUIRED


@pytest.mark.asyncio
async def test_buyer_card_reuse_and_customer_lead_namespace_overlap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        created = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_BUYER_CARD,
            external_id=_BUYER_CARD,
            create_canonical=True,
        )
        await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_TECHNICAL_DEAL,
            external_id=_TECH_DEAL,
            canonical_identity_id=created.canonical_identity_id,
        )
        reused = await svc.reconcile_buyer_card(
            canonical_identity_id=created.canonical_identity_id,
        )
        assert reused.outcome is ReconcileBuyerCardOutcome.REUSED
        assert reused.buyer_card_external_id == _BUYER_CARD

        mismatched = await svc.reconcile_buyer_card(
            canonical_identity_id=created.canonical_identity_id,
            candidate_buyer_card_ids=(_TECH_DEAL,),
            candidate_technical_deal_ids=(_TECH_DEAL,),
        )
        assert mismatched.outcome is ReconcileBuyerCardOutcome.MANUAL_REVIEW_REQUIRED
        assert mismatched.reason == "ambiguous_buyer_cards"

        overlap = "123"
        tech = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_TECHNICAL_DEAL,
            external_id=overlap,
            canonical_identity_id=created.canonical_identity_id,
        )
        buyer = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_BUYER_CARD,
            external_id=overlap,
            canonical_identity_id=created.canonical_identity_id,
        )
        assert tech.outcome is AttachIdentityLinkOutcome.LINKED
        assert buyer.outcome is AttachIdentityLinkOutcome.LINKED


@pytest.mark.asyncio
async def test_ambiguous_buyer_cards_manual_review(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        created = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_BUYER_CARD,
            external_id=_BUYER_CARD,
            connection_scope="scope-a",
            create_canonical=True,
        )
        second = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_BUYER_CARD,
            external_id="buyer-card-999",
            connection_scope="scope-b",
            canonical_identity_id=created.canonical_identity_id,
        )
        assert second.outcome is AttachIdentityLinkOutcome.LINKED
        result = await svc.reconcile_buyer_card(
            canonical_identity_id=created.canonical_identity_id,
        )
        assert result.outcome is ReconcileBuyerCardOutcome.MANUAL_REVIEW_REQUIRED
        assert result.reason == "ambiguous_buyer_cards"


@pytest.mark.asyncio
async def test_duplicate_attach_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        first = await svc.attach(
            provider="vk",
            entity_kind=IdentityEntityKind.CHANNEL_ACCOUNT,
            external_id=_ACCOUNT,
            create_canonical=True,
        )
        second = await svc.attach(
            provider="vk",
            entity_kind=IdentityEntityKind.CHANNEL_ACCOUNT,
            external_id=_ACCOUNT,
            create_canonical=True,
        )
        assert first.outcome is AttachIdentityLinkOutcome.CREATED
        assert second.outcome is AttachIdentityLinkOutcome.ALREADY_LINKED
        assert first.canonical_identity_id == second.canonical_identity_id
        count = await session.scalar(
            select(func.count()).select_from(ExternalIdentityLink).where(
                ExternalIdentityLink.status == "ACTIVE"
            )
        )
        assert int(count or 0) == 1


@pytest.mark.asyncio
async def test_inspect_graph_includes_revoked_history(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        created = await svc.attach(
            provider="max",
            entity_kind=IdentityEntityKind.CHANNEL_ACCOUNT,
            external_id="max-1",
            create_canonical=True,
        )
        await svc.revoke(
            provider="max",
            entity_kind=IdentityEntityKind.CHANNEL_ACCOUNT,
            external_id="max-1",
        )
        inspected = await svc.inspect(
            canonical_identity_id=created.canonical_identity_id
        )
        assert inspected.outcome is InspectIdentityOutcome.FOUND
        assert inspected.graph is not None
        assert len(inspected.graph.links) == 1
        assert inspected.graph.links[0].status is IdentityLinkStatus.REVOKED


@pytest.mark.asyncio
async def test_concurrent_same_link_attach(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def _attempt() -> AttachIdentityLinkOutcome:
        async with session_scope(session_factory) as session:
            svc = IdentityResolutionService(session)
            result = await svc.attach(
                provider="vk",
                entity_kind=IdentityEntityKind.CHANNEL_ACCOUNT,
                external_id=_ACCOUNT,
                connection_scope="race-scope",
                create_canonical=True,
            )
            assert result.outcome is not AttachIdentityLinkOutcome.INVALID_INPUT
            return result.outcome

    first, second = await asyncio.gather(_attempt(), _attempt())
    assert {first, second} <= {
        AttachIdentityLinkOutcome.CREATED,
        AttachIdentityLinkOutcome.ALREADY_LINKED,
    }
    assert AttachIdentityLinkOutcome.CREATED in {first, second}
    assert await _active_link_count(session_factory) == 1
    async with session_factory() as session:
        identities = int(
            await session.scalar(select(func.count()).select_from(CanonicalIdentity))
            or 0
        )
        assert identities == 1


@pytest.mark.asyncio
async def test_concurrent_conflicting_attach_to_different_canonicals(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        left = await svc.attach(
            provider=PHONE_PROVIDER,
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE,
            create_canonical=True,
        )
        right = await svc.attach(
            provider=PHONE_PROVIDER,
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE_ALT,
            create_canonical=True,
        )
        assert left.canonical_identity_id != right.canonical_identity_id

    async def _attach_vk(canonical: uuid.UUID) -> AttachIdentityLinkOutcome:
        async with session_scope(session_factory) as session:
            svc = IdentityResolutionService(session)
            result = await svc.attach(
                provider="vk",
                entity_kind=IdentityEntityKind.CHANNEL_ACCOUNT,
                external_id=_ACCOUNT,
                canonical_identity_id=canonical,
            )
            return result.outcome

    first, second = await asyncio.gather(
        _attach_vk(left.canonical_identity_id),  # type: ignore[arg-type]
        _attach_vk(right.canonical_identity_id),  # type: ignore[arg-type]
    )
    assert AttachIdentityLinkOutcome.LINKED in {first, second}
    assert AttachIdentityLinkOutcome.CONFLICT in {first, second}
    # Exactly one ACTIVE vk link for this account.
    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(ExternalIdentityLink).where(
                        ExternalIdentityLink.provider == "vk",
                        ExternalIdentityLink.external_id == _ACCOUNT,
                        ExternalIdentityLink.status == "ACTIVE",
                    )
                )
            ).all()
        )
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_race_creating_resolving_canonical_identity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def _create_and_resolve() -> uuid.UUID | None:
        async with session_scope(session_factory) as session:
            svc = IdentityResolutionService(session)
            attached = await svc.attach(
                provider=PHONE_PROVIDER,
                entity_kind=IdentityEntityKind.PHONE,
                external_id=_PHONE,
                create_canonical=True,
            )
            resolved = await svc.resolve(IdentityResolveSignals(phone=_PHONE))
            if resolved.outcome is ResolveIdentityOutcome.RESOLVED:
                return resolved.canonical_identity_id
            if attached.outcome in (
                AttachIdentityLinkOutcome.CREATED,
                AttachIdentityLinkOutcome.ALREADY_LINKED,
            ):
                return attached.canonical_identity_id
            return None

    left, right = await asyncio.gather(_create_and_resolve(), _create_and_resolve())
    assert left is not None and right is not None
    assert left == right
    assert await _active_link_count(session_factory) == 1


@pytest.mark.asyncio
async def test_db_rejects_two_active_same_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        created = await svc.attach(
            provider="vk",
            entity_kind=IdentityEntityKind.CHANNEL_ACCOUNT,
            external_id=_ACCOUNT,
            create_canonical=True,
        )
        assert created.outcome is AttachIdentityLinkOutcome.CREATED
        cid = created.canonical_identity_id
        assert cid is not None

    with pytest.raises((IntegrityError, Exception)):
        async with session_scope(session_factory) as session:
            now = func.statement_timestamp()
            session.add(
                ExternalIdentityLink(
                    id=uuid.uuid4(),
                    canonical_identity_id=cid,
                    provider="vk",
                    connection_scope=DEFAULT_CONNECTION_SCOPE,
                    entity_kind=IdentityEntityKind.CHANNEL_ACCOUNT.value,
                    external_id=_ACCOUNT,
                    status="ACTIVE",
                    confidence="CONFIRMED",
                    source="SYSTEM",
                    linked_at=now,
                    revoked_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()


@pytest.mark.asyncio
async def test_no_pii_in_service_logs(
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    with caplog.at_level(logging.INFO, logger="app.services.identity_resolution"):
        async with session_scope(session_factory) as session:
            svc = IdentityResolutionService(session)
            await svc.attach(
                provider=PHONE_PROVIDER,
                entity_kind=IdentityEntityKind.PHONE,
                external_id=_PHONE,
                create_canonical=True,
            )
            await svc.resolve(IdentityResolveSignals(phone=_PHONE, email=_EMAIL))
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert _PHONE not in blob
    assert _EMAIL not in blob
    assert "7900" not in blob
    assert "IDENTITY_" in blob


@pytest.mark.asyncio
async def test_tech_then_buyer_same_id_allowed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    shared_id = "amo-namespace-shared-1"
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        tech = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_TECHNICAL_DEAL,
            external_id=shared_id,
            create_canonical=True,
        )
        assert tech.outcome is AttachIdentityLinkOutcome.CREATED
        buyer = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_BUYER_CARD,
            external_id=shared_id,
            create_canonical=True,
        )
        assert buyer.outcome is AttachIdentityLinkOutcome.CREATED
        assert buyer.canonical_identity_id != tech.canonical_identity_id


@pytest.mark.asyncio
async def test_buyer_then_tech_same_id_allowed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    shared_id = "amo-namespace-shared-2"
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        buyer = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_BUYER_CARD,
            external_id=shared_id,
            create_canonical=True,
        )
        assert buyer.outcome is AttachIdentityLinkOutcome.CREATED
        tech = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_TECHNICAL_DEAL,
            external_id=shared_id,
            create_canonical=True,
        )
        assert tech.outcome is AttachIdentityLinkOutcome.CREATED
        reused = await svc.reconcile_buyer_card(
            canonical_identity_id=buyer.canonical_identity_id,
        )
        assert reused.outcome is ReconcileBuyerCardOutcome.REUSED
        assert reused.buyer_card_external_id == shared_id


@pytest.mark.asyncio
async def test_concurrent_customer_and_technical_same_id_both_active(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    shared_id = "amo-namespace-race-1"

    async def _attach(kind: IdentityEntityKind) -> AttachIdentityLinkOutcome:
        async with session_scope(session_factory) as session:
            svc = IdentityResolutionService(session)
            result = await svc.attach(
                provider="amocrm",
                entity_kind=kind,
                external_id=shared_id,
                create_canonical=True,
            )
            assert result.outcome is not AttachIdentityLinkOutcome.INVALID_INPUT
            return result.outcome

    first, second = await asyncio.gather(
        _attach(IdentityEntityKind.AMOCRM_BUYER_CARD),
        _attach(IdentityEntityKind.AMOCRM_TECHNICAL_DEAL),
    )
    assert first is AttachIdentityLinkOutcome.CREATED
    assert second is AttachIdentityLinkOutcome.CREATED
    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(ExternalIdentityLink).where(
                        ExternalIdentityLink.provider == "amocrm",
                        ExternalIdentityLink.external_id == shared_id,
                        ExternalIdentityLink.status == "ACTIVE",
                    )
                )
            ).all()
        )
        assert len(rows) == 2
        assert {row.entity_kind for row in rows} == {
            IdentityEntityKind.AMOCRM_BUYER_CARD.value,
            IdentityEntityKind.AMOCRM_TECHNICAL_DEAL.value,
        }


@pytest.mark.asyncio
async def test_concurrent_deal_and_technical_same_lead_id_conflict(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    lead_id = "amo-lead-role-race-1"

    async def _attach(kind: IdentityEntityKind) -> AttachIdentityLinkOutcome:
        async with session_scope(session_factory) as session:
            svc = IdentityResolutionService(session)
            result = await svc.attach(
                provider="amocrm",
                entity_kind=kind,
                external_id=lead_id,
                create_canonical=True,
            )
            assert result.outcome is not AttachIdentityLinkOutcome.INVALID_INPUT
            return result.outcome

    first, second = await asyncio.gather(
        _attach(IdentityEntityKind.AMOCRM_DEAL),
        _attach(IdentityEntityKind.AMOCRM_TECHNICAL_DEAL),
    )
    assert AttachIdentityLinkOutcome.CREATED in {first, second}
    assert AttachIdentityLinkOutcome.CONFLICT in {first, second}
    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(ExternalIdentityLink).where(
                        ExternalIdentityLink.provider == "amocrm",
                        ExternalIdentityLink.external_id == lead_id,
                        ExternalIdentityLink.status == "ACTIVE",
                    )
                )
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].entity_kind in {
            IdentityEntityKind.AMOCRM_DEAL.value,
            IdentityEntityKind.AMOCRM_TECHNICAL_DEAL.value,
        }


@pytest.mark.asyncio
async def test_db_customer_and_technical_same_id_allowed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    shared_id = "amo-namespace-db-ok"
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        created = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_BUYER_CARD,
            external_id=shared_id,
            create_canonical=True,
        )
        assert created.outcome is AttachIdentityLinkOutcome.CREATED
        other = uuid.uuid4()
        now = func.statement_timestamp()
        session.add(
            CanonicalIdentity(
                id=other,
                status="ACTIVE",
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            ExternalIdentityLink(
                id=uuid.uuid4(),
                canonical_identity_id=other,
                provider="amocrm",
                connection_scope=DEFAULT_CONNECTION_SCOPE,
                entity_kind=IdentityEntityKind.AMOCRM_TECHNICAL_DEAL.value,
                external_id=shared_id,
                status="ACTIVE",
                confidence="CONFIRMED",
                source="SYSTEM",
                linked_at=now,
                revoked_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()


@pytest.mark.asyncio
async def test_db_amocrm_deal_role_partial_unique_enforced(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    lead_id = "amo-lead-db-constraint"
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        created = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_DEAL,
            external_id=lead_id,
            create_canonical=True,
        )
        assert created.outcome is AttachIdentityLinkOutcome.CREATED

    with pytest.raises((IntegrityError, Exception)):
        async with session_scope(session_factory) as session:
            now = func.statement_timestamp()
            other = uuid.uuid4()
            session.add(
                CanonicalIdentity(
                    id=other,
                    status="ACTIVE",
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add(
                ExternalIdentityLink(
                    id=uuid.uuid4(),
                    canonical_identity_id=other,
                    provider="amocrm",
                    connection_scope=DEFAULT_CONNECTION_SCOPE,
                    entity_kind=IdentityEntityKind.AMOCRM_TECHNICAL_DEAL.value,
                    external_id=lead_id,
                    status="ACTIVE",
                    confidence="CONFIRMED",
                    source="SYSTEM",
                    linked_at=now,
                    revoked_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()


@pytest.mark.asyncio
async def test_deal_then_technical_attach_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    lead_id = "amo-lead-shared-role"
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        deal = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_DEAL,
            external_id=lead_id,
            create_canonical=True,
        )
        assert deal.outcome is AttachIdentityLinkOutcome.CREATED
        tech = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_TECHNICAL_DEAL,
            external_id=lead_id,
            create_canonical=True,
        )
        assert tech.outcome is AttachIdentityLinkOutcome.CONFLICT
        assert tech.reason == REASON_DEAL_TECH_ROLE_CONFLICT


@pytest.mark.asyncio
async def test_customer_and_business_lead_same_id_allowed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    shared_id = "123"
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        customer = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_BUYER_CARD,
            external_id=shared_id,
            create_canonical=True,
        )
        deal = await svc.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_DEAL,
            external_id=shared_id,
            create_canonical=True,
        )
        assert customer.outcome is AttachIdentityLinkOutcome.CREATED
        assert deal.outcome is AttachIdentityLinkOutcome.CREATED


@pytest.mark.asyncio
async def test_archived_canonical_not_resolved_or_reconciled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = IdentityResolutionService(session)
        created = await svc.attach(
            provider=PHONE_PROVIDER,
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE,
            create_canonical=True,
        )
        assert created.outcome is AttachIdentityLinkOutcome.CREATED
        cid = created.canonical_identity_id
        assert cid is not None
        from app.models.canonical_identity import CanonicalIdentity

        row = await session.get(CanonicalIdentity, cid)
        assert row is not None
        row.status = "ARCHIVED"
        await session.flush()

        resolved = await svc.resolve(IdentityResolveSignals(phone=_PHONE))
        assert resolved.outcome is ResolveIdentityOutcome.NOT_FOUND
        assert resolved.canonical_identity_id is None

        reconciled = await svc.reconcile_buyer_card(canonical_identity_id=cid)
        assert reconciled.outcome is ReconcileBuyerCardOutcome.NOT_FOUND
        assert reconciled.reason == "canonical_not_active"


@pytest.mark.asyncio
async def test_integrity_error_savepoint_keeps_outer_uow_usable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """IntegrityError inside attach savepoint must not abort the outer transaction."""

    async def _race_then_follow(follow_account: str) -> tuple[str, str]:
        async with session_scope(session_factory) as session:
            svc = IdentityResolutionService(session)
            raced = await svc.attach(
                provider="vk",
                entity_kind=IdentityEntityKind.CHANNEL_ACCOUNT,
                external_id=_ACCOUNT,
                connection_scope="uow-proof-race",
                create_canonical=True,
            )
            assert raced.outcome in {
                AttachIdentityLinkOutcome.CREATED,
                AttachIdentityLinkOutcome.ALREADY_LINKED,
            }
            # Same outer UoW must still accept a distinct durable write.
            follow = await svc.attach(
                provider="vk",
                entity_kind=IdentityEntityKind.CHANNEL_ACCOUNT,
                external_id=follow_account,
                connection_scope="uow-proof-race",
                create_canonical=True,
            )
            assert follow.outcome is AttachIdentityLinkOutcome.CREATED
            assert follow.canonical_identity_id is not None
            return raced.outcome.value, follow.outcome.value

    left, right = await asyncio.gather(
        _race_then_follow("vk-uow-follow-a"),
        _race_then_follow("vk-uow-follow-b"),
    )
    assert {left[0], right[0]} == {
        AttachIdentityLinkOutcome.CREATED.value,
        AttachIdentityLinkOutcome.ALREADY_LINKED.value,
    }
    assert left[1] == AttachIdentityLinkOutcome.CREATED.value
    assert right[1] == AttachIdentityLinkOutcome.CREATED.value
    assert await _active_link_count(session_factory) == 3
