"""PostgreSQL tests for IR-1 conversation↔canonical identity glue."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.identity_glue import (
    ApproveIdentityReviewOutcome,
    ConversationIdentityGlueOutcome,
    IdentityReviewCaseStatus,
    IdentityReviewReasonCode,
)
from app.core.identity_resolution import (
    REASON_EMAIL_ONLY_SECONDARY,
    AttachIdentityLinkOutcome,
    IdentityEntityKind,
    IdentityResolveSignals,
    ReconcileBuyerCardOutcome,
)
from app.db.session import session_scope
from app.identity_glue_ops import format_inspect_case_line
from app.models.canonical_identity import CanonicalIdentity
from app.models.conversation import Channel
from app.models.identity_review_case import IdentityReviewCase
from app.repositories import conversations as conversation_repo
from app.repositories import identity_glue as glue_repo
from app.services.identity_glue import ConversationIdentityGlueService
from app.services.identity_resolution import IdentityResolutionService
from app.services.identity_glue_ops import (
    IdentityGlueOpsOutcome,
    inspect_open_identity_reviews,
)
from tests.foundation_test_db import SecretDatabaseUrl, run_alembic_command_async
from tests.pg_harness import truncate_foundation_tables

_PHONE = "+79001234567"
_PHONE_ALT = "+79007654321"
_EMAIL = "client@example.com"
_ACCOUNT = "glue-channel-1"


@pytest_asyncio.fixture(autouse=True)
async def glue_row_cleanup(
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


async def _seed_conversation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    external_id: str | None = None,
):
    async with session_scope(session_factory) as session:
        conversation, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.SYNTHETIC,
            external_conversation_id=external_id or f"glue-{uuid4().hex[:12]}",
        )
        return conversation.id


@pytest.mark.asyncio
async def test_migration_creates_glue_objects(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        assert await session.scalar(
            text("SELECT to_regclass('public.identity_review_cases') IS NOT NULL")
        )
        col = await session.scalar(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'conversations' "
                "AND column_name = 'canonical_identity_id'"
            )
        )
        assert col == 1
        partial = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = "
                "'uq_identity_review_cases_open_conversation_reason'"
            )
        )
        assert partial is not None
        assert "UNIQUE" in partial.upper()
        assert "OPEN" in partial


@pytest.mark.asyncio
async def test_resolved_phone_attaches_canonical(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = await _seed_conversation(session_factory)
    async with session_scope(session_factory) as session:
        identity = IdentityResolutionService(session)
        created = await identity.attach(
            provider="phone",
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE,
            create_canonical=True,
        )
        assert created.outcome is AttachIdentityLinkOutcome.CREATED
        glue = ConversationIdentityGlueService(session)
        result = await glue.resolve_for_conversation(
            conversation_id=conversation_id,
            signals=IdentityResolveSignals(phone=_PHONE),
        )
        assert result.outcome is ConversationIdentityGlueOutcome.ATTACHED
        assert result.canonical_identity_id == created.canonical_identity_id
        conversation = await conversation_repo.get_by_id_for_update(
            session,
            conversation_id=conversation_id,
        )
        assert conversation is not None
        assert conversation.canonical_identity_id == created.canonical_identity_id

        again = await glue.resolve_for_conversation(
            conversation_id=conversation_id,
            signals=IdentityResolveSignals(phone=_PHONE),
        )
        assert again.outcome is ConversationIdentityGlueOutcome.ALREADY_ATTACHED


@pytest.mark.asyncio
async def test_resolved_channel_attaches_canonical(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = await _seed_conversation(session_factory)
    async with session_scope(session_factory) as session:
        identity = IdentityResolutionService(session)
        created = await identity.attach(
            provider="vk",
            entity_kind=IdentityEntityKind.CHANNEL_ACCOUNT,
            external_id=_ACCOUNT,
            connection_scope="vk-group-1",
            create_canonical=True,
        )
        glue = ConversationIdentityGlueService(session)
        result = await glue.resolve_for_conversation(
            conversation_id=conversation_id,
            signals=IdentityResolveSignals(
                channel_provider="vk",
                channel_connection_scope="vk-group-1",
                channel_external_account_id=_ACCOUNT,
            ),
        )
        assert result.outcome is ConversationIdentityGlueOutcome.ATTACHED
        assert result.canonical_identity_id == created.canonical_identity_id


@pytest.mark.asyncio
async def test_email_only_does_not_attach(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = await _seed_conversation(session_factory)
    async with session_scope(session_factory) as session:
        identity = IdentityResolutionService(session)
        await identity.attach(
            provider="email",
            entity_kind=IdentityEntityKind.EMAIL,
            external_id=_EMAIL,
            create_canonical=True,
        )
        glue = ConversationIdentityGlueService(session)
        result = await glue.resolve_for_conversation(
            conversation_id=conversation_id,
            signals=IdentityResolveSignals(email=_EMAIL),
        )
        assert result.outcome is ConversationIdentityGlueOutcome.NOT_FOUND
        assert result.error_code == REASON_EMAIL_ONLY_SECONDARY
        conversation = await conversation_repo.get_by_id_for_update(
            session,
            conversation_id=conversation_id,
        )
        assert conversation is not None
        assert conversation.canonical_identity_id is None
        open_count = await session.scalar(
            select(func.count()).select_from(IdentityReviewCase).where(
                IdentityReviewCase.status == IdentityReviewCaseStatus.OPEN.value
            )
        )
        assert int(open_count or 0) == 0


@pytest.mark.asyncio
async def test_ambiguity_creates_one_idempotent_open_review(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = await _seed_conversation(session_factory)
    async with session_scope(session_factory) as session:
        identity = IdentityResolutionService(session)
        a = await identity.attach(
            provider="phone",
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE,
            create_canonical=True,
        )
        b = await identity.attach(
            provider="phone",
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE_ALT,
            create_canonical=True,
        )
        assert a.canonical_identity_id != b.canonical_identity_id
        glue = ConversationIdentityGlueService(session)
        signals = IdentityResolveSignals(
            confirmed_links=(
                (
                    "phone",
                    "default",
                    IdentityEntityKind.PHONE,
                    _PHONE,
                ),
                (
                    "phone",
                    "default",
                    IdentityEntityKind.PHONE,
                    _PHONE_ALT,
                ),
            )
        )
        first = await glue.resolve_for_conversation(
            conversation_id=conversation_id,
            signals=signals,
        )
        assert first.outcome is ConversationIdentityGlueOutcome.REVIEW_OPENED
        assert first.reason_code == IdentityReviewReasonCode.AMBIGUOUS_RESOLVE.value
        second = await glue.resolve_for_conversation(
            conversation_id=conversation_id,
            signals=signals,
        )
        assert second.outcome is ConversationIdentityGlueOutcome.REVIEW_EXISTS
        assert second.review_case_id == first.review_case_id
        open_count = await session.scalar(
            select(func.count()).select_from(IdentityReviewCase).where(
                IdentityReviewCase.conversation_id == conversation_id,
                IdentityReviewCase.status == IdentityReviewCaseStatus.OPEN.value,
                IdentityReviewCase.reason_code
                == IdentityReviewReasonCode.AMBIGUOUS_RESOLVE.value,
            )
        )
        assert int(open_count or 0) == 1
        conversation = await conversation_repo.get_by_id_for_update(
            session,
            conversation_id=conversation_id,
        )
        assert conversation is not None
        assert conversation.canonical_identity_id is None


@pytest.mark.asyncio
async def test_conflicting_canonical_rebind_does_not_overwrite(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = await _seed_conversation(session_factory)
    async with session_scope(session_factory) as session:
        identity = IdentityResolutionService(session)
        first = await identity.attach(
            provider="phone",
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE,
            create_canonical=True,
        )
        second = await identity.attach(
            provider="phone",
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE_ALT,
            create_canonical=True,
        )
        glue = ConversationIdentityGlueService(session)
        attached = await glue.resolve_for_conversation(
            conversation_id=conversation_id,
            signals=IdentityResolveSignals(phone=_PHONE),
        )
        assert attached.outcome is ConversationIdentityGlueOutcome.ATTACHED
        conflict = await glue.resolve_for_conversation(
            conversation_id=conversation_id,
            signals=IdentityResolveSignals(phone=_PHONE_ALT),
        )
        assert conflict.outcome is ConversationIdentityGlueOutcome.REVIEW_OPENED
        assert (
            conflict.reason_code
            == IdentityReviewReasonCode.CONFLICTING_CANONICAL.value
        )
        assert conflict.canonical_identity_id == second.canonical_identity_id
        conversation = await conversation_repo.get_by_id_for_update(
            session,
            conversation_id=conversation_id,
        )
        assert conversation is not None
        assert conversation.canonical_identity_id == first.canonical_identity_id


@pytest.mark.asyncio
async def test_manual_approve_attaches_and_resolves_atomically(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = await _seed_conversation(session_factory)
    async with session_scope(session_factory) as session:
        identity = IdentityResolutionService(session)
        a = await identity.attach(
            provider="phone",
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE,
            create_canonical=True,
        )
        b = await identity.attach(
            provider="phone",
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE_ALT,
            create_canonical=True,
        )
        glue = ConversationIdentityGlueService(session)
        review = await glue.resolve_for_conversation(
            conversation_id=conversation_id,
            signals=IdentityResolveSignals(
                confirmed_links=(
                    (
                        "phone",
                        "default",
                        IdentityEntityKind.PHONE,
                        _PHONE,
                    ),
                    (
                        "phone",
                        "default",
                        IdentityEntityKind.PHONE,
                        _PHONE_ALT,
                    ),
                )
            ),
        )
        assert review.outcome is ConversationIdentityGlueOutcome.REVIEW_OPENED
        assert review.review_case_id is not None
        approved = await glue.approve_review(
            review_case_id=review.review_case_id,
            canonical_identity_id=a.canonical_identity_id,
        )
        assert approved.outcome is ApproveIdentityReviewOutcome.APPROVED
        conversation = await conversation_repo.get_by_id_for_update(
            session,
            conversation_id=conversation_id,
        )
        assert conversation is not None
        assert conversation.canonical_identity_id == a.canonical_identity_id
        case = await session.get(IdentityReviewCase, review.review_case_id)
        assert case is not None
        assert case.status == IdentityReviewCaseStatus.RESOLVED.value
        assert case.resolved_canonical_identity_id == a.canonical_identity_id
        assert case.resolved_at is not None
        assert b.canonical_identity_id != a.canonical_identity_id


@pytest.mark.asyncio
async def test_archived_canonical_cannot_attach(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = await _seed_conversation(session_factory)
    async with session_scope(session_factory) as session:
        identity = IdentityResolutionService(session)
        created = await identity.attach(
            provider="phone",
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE,
            create_canonical=True,
        )
        assert created.canonical_identity_id is not None
        row = await session.get(CanonicalIdentity, created.canonical_identity_id)
        assert row is not None
        row.status = "ARCHIVED"
        await session.flush()
        glue = ConversationIdentityGlueService(session)
        result = await glue.resolve_for_conversation(
            conversation_id=conversation_id,
            signals=IdentityResolveSignals(phone=_PHONE),
        )
        # Archived identities are ignored by resolve → NOT_FOUND, or if somehow
        # resolved, glue opens CANONICAL_NOT_ACTIVE. Either way no attach.
        conversation = await conversation_repo.get_by_id_for_update(
            session,
            conversation_id=conversation_id,
        )
        assert conversation is not None
        assert conversation.canonical_identity_id is None
        assert result.outcome in {
            ConversationIdentityGlueOutcome.NOT_FOUND,
            ConversationIdentityGlueOutcome.REVIEW_OPENED,
            ConversationIdentityGlueOutcome.REVIEW_EXISTS,
        }


@pytest.mark.asyncio
async def test_technical_deal_is_not_auto_bound_as_buyer_card(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        identity = IdentityResolutionService(session)
        created = await identity.attach(
            provider="phone",
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE,
            create_canonical=True,
        )
        assert created.canonical_identity_id is not None
        await identity.attach(
            provider="amocrm",
            entity_kind=IdentityEntityKind.AMOCRM_TECHNICAL_DEAL,
            external_id="tech-deal-9",
            canonical_identity_id=created.canonical_identity_id,
        )
        result = await identity.reconcile_buyer_card(
            canonical_identity_id=created.canonical_identity_id,
            candidate_buyer_card_ids=("tech-deal-9",),
            candidate_technical_deal_ids=("tech-deal-9",),
        )
        assert result.outcome is ReconcileBuyerCardOutcome.NOT_FOUND
        assert result.reason == "buyer_card_not_linked"
        assert result.buyer_card_external_id is None


@pytest.mark.asyncio
async def test_review_rows_store_no_pii_columns(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = await _seed_conversation(session_factory)
    async with session_scope(session_factory) as session:
        identity = IdentityResolutionService(session)
        await identity.attach(
            provider="phone",
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE,
            create_canonical=True,
        )
        await identity.attach(
            provider="phone",
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE_ALT,
            create_canonical=True,
        )
        glue = ConversationIdentityGlueService(session)
        await glue.resolve_for_conversation(
            conversation_id=conversation_id,
            signals=IdentityResolveSignals(
                confirmed_links=(
                    (
                        "phone",
                        "default",
                        IdentityEntityKind.PHONE,
                        _PHONE,
                    ),
                    (
                        "phone",
                        "default",
                        IdentityEntityKind.PHONE,
                        _PHONE_ALT,
                    ),
                )
            ),
        )
        rows = (
            await session.scalars(select(IdentityReviewCase))
        ).all()
        assert rows
        for row in rows:
            dumped = repr(row)
            assert _PHONE not in dumped
            assert _EMAIL not in dumped
            assert _PHONE_ALT not in dumped


@pytest.mark.asyncio
async def test_inspect_ops_returns_all_open_cases_with_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = await _seed_conversation(session_factory)
    async with session_scope(session_factory) as session:
        identity = IdentityResolutionService(session)
        first = await identity.attach(
            provider="phone",
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE,
            create_canonical=True,
        )
        await identity.attach(
            provider="phone",
            entity_kind=IdentityEntityKind.PHONE,
            external_id=_PHONE_ALT,
            create_canonical=True,
        )
        glue = ConversationIdentityGlueService(session)
        await glue.resolve_for_conversation(
            conversation_id=conversation_id,
            signals=IdentityResolveSignals(
                confirmed_links=(
                    ("phone", "default", IdentityEntityKind.PHONE, _PHONE),
                    ("phone", "default", IdentityEntityKind.PHONE, _PHONE_ALT),
                )
            ),
        )
        conversation = await conversation_repo.get_by_id_for_update(
            session,
            conversation_id=conversation_id,
        )
        assert conversation is not None
        assert first.canonical_identity_id is not None
        await glue_repo.set_conversation_canonical_identity(
            session,
            conversation=conversation,
            canonical_identity_id=first.canonical_identity_id,
        )
        await glue.resolve_for_conversation(
            conversation_id=conversation_id,
            signals=IdentityResolveSignals(phone=_PHONE_ALT),
        )

    inspected = await inspect_open_identity_reviews(
        session_factory,
        conversation_id=conversation_id,
    )
    assert inspected.outcome is IdentityGlueOpsOutcome.INSPECTED
    assert inspected.open_review_count == len(inspected.cases)
    assert inspected.open_review_count >= 2
    reason_codes = {case.reason_code for case in inspected.cases}
    assert "AMBIGUOUS_RESOLVE" in reason_codes
    assert "CONFLICTING_CANONICAL" in reason_codes
    assert len({case.id for case in inspected.cases}) == len(inspected.cases)
    for case in inspected.cases:
        assert case.conversation_id == conversation_id
        line = format_inspect_case_line(case)
        assert str(case.id) in line
        assert str(case.conversation_id) in line
        assert case.reason_code in line
        assert _PHONE not in line
        assert _EMAIL not in line


@pytest.mark.asyncio
async def test_glue_migration_downgrade_upgrade(
    pg_database_url: SecretDatabaseUrl,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from pathlib import Path

    alembic_ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    await run_alembic_command_async(
        alembic_ini=alembic_ini,
        command_name="downgrade",
        revision="20260813_25_amo_deal_reserve",
        database_url=pg_database_url,
    )
    async with session_factory() as session:
        assert not await session.scalar(
            text("SELECT to_regclass('public.identity_review_cases') IS NOT NULL")
        )
        col = await session.scalar(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'conversations' "
                "AND column_name = 'canonical_identity_id'"
            )
        )
        assert col is None
    await run_alembic_command_async(
        alembic_ini=alembic_ini,
        command_name="upgrade",
        revision="head",
        database_url=pg_database_url,
    )
    async with session_factory() as session:
        assert await session.scalar(
            text("SELECT to_regclass('public.identity_review_cases') IS NOT NULL")
        )
