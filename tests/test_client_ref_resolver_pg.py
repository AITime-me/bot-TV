from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.client_ref_resolution import (
    ClientRefResolutionOutcome,
)
from app.core.identity_resolution import CanonicalIdentityStatus
from app.db.session import session_scope
from app.models.canonical_identity import CanonicalIdentity, ExternalIdentityLink
from app.models.conversation import Channel, Conversation
from app.repositories import conversations as conversation_repo
from app.services.client_ref_resolution import ClientRefResolverService
from tests.pg_harness import truncate_foundation_tables


@pytest_asyncio.fixture(autouse=True)
async def client_ref_row_cleanup(
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
    canonical_status: CanonicalIdentityStatus | None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    """Create synthetic conversation; optionally attach a canonical identity."""
    async with session_scope(session_factory) as session:
        conversation, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.SYNTHETIC,
            external_conversation_id=f"clientref-{uuid.uuid4().hex[:12]}",
        )

        canonical_id: uuid.UUID | None = None
        if canonical_status is not None:
            canonical_id = uuid.uuid4()
            now = func.statement_timestamp()
            session.add(
                CanonicalIdentity(
                    id=canonical_id,
                    status=canonical_status.value,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            conversation.canonical_identity_id = canonical_id
            await session.flush()

        return conversation.id, canonical_id


@pytest.mark.asyncio
async def test_found_active_canonical_is_deterministic_client_ref(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id, canonical_id = await _seed_conversation(
        session_factory, canonical_status=CanonicalIdentityStatus.ACTIVE
    )
    assert canonical_id is not None

    async with session_scope(session_factory) as session:
        svc = ClientRefResolverService(session)
        first = await svc.resolve_for_conversation(
            conversation_id=conversation_id
        )
        second = await svc.resolve_for_conversation(
            conversation_id=conversation_id
        )
        assert first.outcome is ClientRefResolutionOutcome.FOUND
        assert second.outcome is ClientRefResolutionOutcome.FOUND
        assert first.client_ref == str(canonical_id)
        assert second.client_ref == str(canonical_id)
        assert first.client_ref == second.client_ref


@pytest.mark.asyncio
async def test_two_different_canonicals_have_two_client_refs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    a_conv, a_cid = await _seed_conversation(
        session_factory, canonical_status=CanonicalIdentityStatus.ACTIVE
    )
    b_conv, b_cid = await _seed_conversation(
        session_factory, canonical_status=CanonicalIdentityStatus.ACTIVE
    )
    assert a_cid is not None and b_cid is not None
    assert a_cid != b_cid

    async with session_scope(session_factory) as session:
        svc = ClientRefResolverService(session)
        ra = await svc.resolve_for_conversation(conversation_id=a_conv)
        rb = await svc.resolve_for_conversation(conversation_id=b_conv)
        assert ra.outcome is ClientRefResolutionOutcome.FOUND
        assert rb.outcome is ClientRefResolutionOutcome.FOUND
        assert ra.client_ref == str(a_cid)
        assert rb.client_ref == str(b_cid)
        assert ra.client_ref != rb.client_ref


@pytest.mark.asyncio
async def test_conversation_without_canonical_is_not_found(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id, canonical_id = await _seed_conversation(
        session_factory, canonical_status=None
    )
    assert canonical_id is None

    async with session_scope(session_factory) as session:
        svc = ClientRefResolverService(session)
        result = await svc.resolve_for_conversation(
            conversation_id=conversation_id
        )
        assert result.outcome is ClientRefResolutionOutcome.NOT_FOUND


@pytest.mark.asyncio
async def test_archived_canonical_is_fail_closed_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id, archived_cid = await _seed_conversation(
        session_factory, canonical_status=CanonicalIdentityStatus.ARCHIVED
    )
    assert archived_cid is not None

    async with session_scope(session_factory) as session:
        svc = ClientRefResolverService(session)
        result = await svc.resolve_for_conversation(
            conversation_id=conversation_id
        )
        assert result.outcome is ClientRefResolutionOutcome.REFUSED
        assert result.reason_code == "CANONICAL_NOT_ACTIVE"
        assert result.client_ref is None


@pytest.mark.asyncio
async def test_invalid_conversation_id_is_invalid_input(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = ClientRefResolverService(session)
        result = await svc.resolve_for_conversation(
            conversation_id="not-a-uuid"
        )
        assert result.outcome is ClientRefResolutionOutcome.INVALID_INPUT
        assert result.error_code == "CONVERSATION_ID_INVALID"


@pytest.mark.asyncio
async def test_dangling_canonical_id_is_fail_closed_if_possible(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id, _active_cid = await _seed_conversation(
        session_factory, canonical_status=CanonicalIdentityStatus.ACTIVE
    )

    dangling = uuid.uuid4()
    # Postgres FK constraints may prevent creation of dangling references
    # unless triggers/constraints are bypassed. If we cannot create this
    # inconsistent graph, the test is skipped.
    async with session_scope(session_factory) as session:
        try:
            await session.execute(
                text("SET session_replication_role = replica")
            )
            await session.execute(
                text(
                    "UPDATE public.conversations "
                    "SET canonical_identity_id = :dangling "
                    "WHERE id = :cid"
                ),
                {"dangling": str(dangling), "cid": str(conversation_id)},
            )
            await session.execute(
                text("SET session_replication_role = origin")
            )
            await session.flush()
        except Exception:
            pytest.skip("Cannot bypass FK constraints to create dangling canonical")

        svc = ClientRefResolverService(session)
        result = await svc.resolve_for_conversation(
            conversation_id=conversation_id
        )
        assert result.outcome is ClientRefResolutionOutcome.REFUSED
        assert result.reason_code == "CANONICAL_MISSING"


@pytest.mark.asyncio
async def test_resolver_is_read_only_no_writes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id, canonical_id = await _seed_conversation(
        session_factory, canonical_status=CanonicalIdentityStatus.ACTIVE
    )
    assert canonical_id is not None

    async with session_scope(session_factory) as session:
        svc = ClientRefResolverService(session)

        before_conversations = await session.scalar(
            select(func.count()).select_from(
                # Only rows; resolver must not insert/update.
                Conversation
            )
        )
        before_canonical = await session.scalar(
            select(func.count()).select_from(CanonicalIdentity)
        )
        before_links = await session.scalar(
            select(func.count()).select_from(ExternalIdentityLink)
        )

        result = await svc.resolve_for_conversation(
            conversation_id=conversation_id
        )
        assert result.outcome is ClientRefResolutionOutcome.FOUND

        after_conversations = await session.scalar(
            select(func.count()).select_from(Conversation)
        )
        after_canonical = await session.scalar(
            select(func.count()).select_from(CanonicalIdentity)
        )
        after_links = await session.scalar(
            select(func.count()).select_from(ExternalIdentityLink)
        )

        assert before_conversations == after_conversations
        assert before_canonical == after_canonical
        assert before_links == after_links

