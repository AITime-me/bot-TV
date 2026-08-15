"""AMO-PROD-ENABLEMENT-OPS-01 PostgreSQL: Chat binding seed idempotency/conflict."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import session_scope
from app.models.amocrm_chat_binding import AmocrmChatBinding, AmocrmChatBindingStatus
from app.repositories import amocrm_chat_bindings as binding_repo
from app.schemas.inbound import SyntheticInboundEvent
from app.services.amocrm_chat_binding_ops import (
    AmoCrmChatBindingOpsOutcome,
    seed_active_chat_binding,
)
from app.services.inbound import InboundService
from tests.pg_harness import truncate_foundation_tables


@pytest_asyncio.fixture(autouse=True)
async def cleanup(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


async def _seed_conversation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    external_conversation_id: str,
):
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=external_conversation_id,
                external_message_id=f"msg-{uuid4().hex[:10]}",
                text="seed",
            )
        )
        return accepted.conversation


@pytest.mark.asyncio
async def test_seed_binding_create_and_identical_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(
        session_factory, external_conversation_id="bind-ops-1"
    )
    chat_id = "amo-chat-ops-1"
    integ = "integ-ops-1"

    first = await seed_active_chat_binding(
        session_factory,
        conversation_id=conversation.id,
        amocrm_chat_id=chat_id,
        integration_conversation_id=integ,
    )
    assert first.outcome is AmoCrmChatBindingOpsOutcome.SEEDED
    assert first.created is True

    second = await seed_active_chat_binding(
        session_factory,
        conversation_id=conversation.id,
        amocrm_chat_id=chat_id,
        integration_conversation_id=integ,
    )
    assert second.outcome is AmoCrmChatBindingOpsOutcome.ALREADY_PRESENT
    assert second.created is False

    async with session_factory() as session:
        async with session.begin():
            count = await session.scalar(
                select(func.count()).select_from(AmocrmChatBinding)
            )
            assert count == 1
            row = (
                await session.scalars(
                    select(AmocrmChatBinding).where(
                        AmocrmChatBinding.status
                        == AmocrmChatBindingStatus.ACTIVE.value
                    )
                )
            ).one()
            assert row.conversation_id == conversation.id
            assert row.amocrm_chat_id == chat_id
            assert row.integration_conversation_id == integ


@pytest.mark.asyncio
async def test_seed_binding_chat_conflict_fail_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await _seed_conversation(
        session_factory, external_conversation_id="bind-ops-conflict-a"
    )
    second = await _seed_conversation(
        session_factory, external_conversation_id="bind-ops-conflict-b"
    )
    chat_id = "amo-chat-shared"
    await seed_active_chat_binding(
        session_factory,
        conversation_id=first.id,
        amocrm_chat_id=chat_id,
        integration_conversation_id="integ-a",
    )
    refused = await seed_active_chat_binding(
        session_factory,
        conversation_id=second.id,
        amocrm_chat_id=chat_id,
        integration_conversation_id="integ-b",
    )
    assert refused.outcome is AmoCrmChatBindingOpsOutcome.REFUSED
    assert refused.error_code == "BINDING_CHAT_CONFLICT"

    async with session_factory() as session:
        async with session.begin():
            count = await session.scalar(
                select(func.count()).select_from(AmocrmChatBinding)
            )
            assert count == 1


@pytest.mark.asyncio
async def test_seed_binding_integ_repoint_fail_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(
        session_factory, external_conversation_id="bind-ops-integ"
    )
    await seed_active_chat_binding(
        session_factory,
        conversation_id=conversation.id,
        amocrm_chat_id="amo-chat-integ",
        integration_conversation_id="integ-original",
    )
    refused = await seed_active_chat_binding(
        session_factory,
        conversation_id=conversation.id,
        amocrm_chat_id="amo-chat-integ",
        integration_conversation_id="integ-repoint",
    )
    assert refused.outcome is AmoCrmChatBindingOpsOutcome.REFUSED
    assert refused.error_code == "BINDING_INTEGRATION_CONVERSATION_CONFLICT"

    async with session_scope(session_factory) as session:
        row = await binding_repo.get_active_by_amocrm_chat_id(
            session, amocrm_chat_id="amo-chat-integ"
        )
        assert row is not None
        assert row.integration_conversation_id == "integ-original"


@pytest.mark.asyncio
async def test_seed_binding_conversation_conflict_fail_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(
        session_factory, external_conversation_id="bind-ops-conv-conflict"
    )
    chat_a = "amo-chat-conv-a"
    integ_a = "integ-conv-a"
    first = await seed_active_chat_binding(
        session_factory,
        conversation_id=conversation.id,
        amocrm_chat_id=chat_a,
        integration_conversation_id=integ_a,
    )
    assert first.outcome is AmoCrmChatBindingOpsOutcome.SEEDED

    refused = await seed_active_chat_binding(
        session_factory,
        conversation_id=conversation.id,
        amocrm_chat_id="amo-chat-conv-b",
        integration_conversation_id="integ-conv-b",
    )
    assert refused.outcome is AmoCrmChatBindingOpsOutcome.REFUSED
    assert refused.error_code == "BINDING_CONVERSATION_CONFLICT"

    async with session_factory() as session:
        async with session.begin():
            rows = (
                await session.scalars(select(AmocrmChatBinding))
            ).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.conversation_id == conversation.id
            assert row.amocrm_chat_id == chat_a
            assert row.integration_conversation_id == integ_a
            assert row.status == AmocrmChatBindingStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_seed_binding_null_integ_fill_updated_then_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(
        session_factory, external_conversation_id="bind-ops-null-integ"
    )
    chat_id = "amo-chat-null-integ"
    integ = "integ-filled-once"

    async with session_scope(session_factory) as session:
        row, created = await binding_repo.insert_active_if_absent(
            session,
            conversation_id=conversation.id,
            amocrm_chat_id=chat_id,
            integration_conversation_id=None,
        )
        assert created is True
        assert row.integration_conversation_id is None
        binding_id = row.id

    updated = await seed_active_chat_binding(
        session_factory,
        conversation_id=conversation.id,
        amocrm_chat_id=chat_id,
        integration_conversation_id=integ,
    )
    assert updated.outcome is AmoCrmChatBindingOpsOutcome.UPDATED
    assert updated.created is False

    again = await seed_active_chat_binding(
        session_factory,
        conversation_id=conversation.id,
        amocrm_chat_id=chat_id,
        integration_conversation_id=integ,
    )
    assert again.outcome is AmoCrmChatBindingOpsOutcome.ALREADY_PRESENT
    assert again.created is False

    async with session_factory() as session:
        async with session.begin():
            count = await session.scalar(
                select(func.count()).select_from(AmocrmChatBinding)
            )
            assert count == 1
            row = await session.get(AmocrmChatBinding, binding_id)
            assert row is not None
            assert row.conversation_id == conversation.id
            assert row.amocrm_chat_id == chat_id
            assert row.integration_conversation_id == integ
            assert row.status == AmocrmChatBindingStatus.ACTIVE.value
