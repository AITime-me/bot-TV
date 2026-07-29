from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.clock import db_statement_now
from app.models.conversation import (
    Conversation,
    ConversationOwnership,
    ConversationStatus,
    HandoffState,
)
from app.models.reply_plan import ReplyPlan, ReplyPlanStatus
from app.repositories import conversations as conversation_repo
from app.schemas.inbound import SyntheticInboundEvent
from app.schemas.manager_message import SyntheticManagerMessageEvent
from app.services.handoff_expiry import (
    HandoffExpiryInvariantError,
    HandoffExpiryTransition,
    HandoffExpiryWorker,
)
from app.services.inbound import InboundService
from app.services.manager_messages import SyntheticManagerMessageService
from app.services.reply_outbound import ReplyPlanWorker
from tests.pg_harness import truncate_foundation_tables


@pytest_asyncio.fixture(autouse=True)
async def handoff_expiry_row_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


def _manager(
    message_id: str = "manager-1",
    *,
    provider_sequence: int = 1,
) -> SyntheticManagerMessageEvent:
    return SyntheticManagerMessageEvent(
        external_conversation_id="expiry-conv",
        external_message_id=message_id,
        provider_sequence=provider_sequence,
        text="Ответ менеджера",
    )


def _client(message_id: str = "client-1") -> SyntheticInboundEvent:
    return SyntheticInboundEvent(
        external_conversation_id="expiry-conv",
        external_message_id=message_id,
        text="Ответ клиента",
    )


async def _make_due(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    with_client_pause: bool,
) -> tuple[object, object | None]:
    manager = await SyntheticManagerMessageService(session_factory).apply(_manager())
    plan_id = None
    if with_client_pause:
        async with session_factory() as session:
            async with session.begin():
                paused = await InboundService(session).accept(_client())
                assert paused.reply_plan is not None
                plan_id = paused.reply_plan.id

    async with session_factory() as session:
        async with session.begin():
            due = await db_statement_now(session) - timedelta(seconds=1)
            await session.execute(
                update(Conversation)
                .where(Conversation.id == manager.conversation_id)
                .values(handoff_deadline_at=due)
            )
            if plan_id is not None:
                await session.execute(
                    update(ReplyPlan)
                    .where(ReplyPlan.id == plan_id)
                    .values(not_before=due)
                )
    return manager.conversation_id, plan_id


async def _load_conversation(
    session_factory: async_sessionmaker[AsyncSession],
    conversation_id: object,
) -> Conversation:
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        return conversation


async def test_due_human_active_returns_to_bot_without_reply(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id, _ = await _make_due(
        session_factory,
        with_client_pause=False,
    )

    result = await HandoffExpiryWorker(session_factory).expire_one()

    assert result is not None
    assert result.transition is HandoffExpiryTransition.HUMAN_ACTIVE_TO_BOT
    assert result.active_reply_plan_id is None
    conversation = await _load_conversation(session_factory, conversation_id)
    assert conversation.status == ConversationStatus.OPEN.value
    assert conversation.ownership == ConversationOwnership.BOT.value
    assert conversation.handoff_state == HandoffState.BOT_ACTIVE.value
    assert conversation.handoff_deadline_at is None
    assert conversation.human_pause_anchor_at is None
    assert conversation.manager_takeover_at is None
    assert conversation.active_reply_plan_id is None
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ReplyPlan)) == 0


async def test_due_human_pause_returns_to_bot_and_preserves_exactly_one_plan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id, plan_id = await _make_due(
        session_factory,
        with_client_pause=True,
    )

    result = await HandoffExpiryWorker(session_factory).expire_one()

    assert result is not None
    assert result.transition is HandoffExpiryTransition.HUMAN_PAUSE_TO_BOT
    assert result.active_reply_plan_id == plan_id
    conversation = await _load_conversation(session_factory, conversation_id)
    assert conversation.handoff_state == HandoffState.BOT_ACTIVE.value
    assert conversation.active_reply_plan_id == plan_id

    reply_worker = ReplyPlanWorker(session_factory, worker_id="after-expiry")
    claim = await reply_worker.claim_one()
    assert claim is not None
    assert claim.plan_id == plan_id
    assert await reply_worker.claim_one() is None


async def test_not_due_handoff_is_not_returned_early(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = await SyntheticManagerMessageService(session_factory).apply(_manager())

    assert await HandoffExpiryWorker(session_factory).expire_one() is None

    conversation = await _load_conversation(
        session_factory,
        manager.conversation_id,
    )
    assert conversation.handoff_state == HandoffState.HUMAN_ACTIVE.value


async def test_configured_ten_minute_window_applies_to_manager_and_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager_service = SyntheticManagerMessageService(
        session_factory,
        handoff_pause_seconds=600,
    )
    manager = await manager_service.apply(_manager())
    conversation = await _load_conversation(
        session_factory,
        manager.conversation_id,
    )
    assert conversation.manager_takeover_at is not None
    assert conversation.handoff_deadline_at is not None
    assert (
        conversation.handoff_deadline_at - conversation.manager_takeover_at
        == timedelta(seconds=600)
    )

    async with session_factory() as session:
        async with session.begin():
            paused = await InboundService(
                session,
                handoff_pause_seconds=600,
            ).accept(_client())
            assert paused.conversation.human_pause_anchor_at is not None
            assert paused.conversation.handoff_deadline_at is not None
            assert (
                paused.conversation.handoff_deadline_at
                - paused.conversation.human_pause_anchor_at
                == timedelta(seconds=600)
            )


async def test_two_workers_cannot_expire_the_same_dialog_twice(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id, _ = await _make_due(
        session_factory,
        with_client_pause=False,
    )
    worker_a = HandoffExpiryWorker(session_factory)
    worker_b = HandoffExpiryWorker(session_factory)

    results = await asyncio.gather(worker_a.expire_one(), worker_b.expire_one())

    assert sum(result is not None for result in results) == 1
    conversation = await _load_conversation(session_factory, conversation_id)
    assert conversation.handoff_state == HandoffState.BOT_ACTIVE.value


async def test_manager_message_waiting_on_expiry_lock_wins_final_state(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id, plan_id = await _make_due(
        session_factory,
        with_client_pause=True,
    )
    expiry_holds_lock = asyncio.Event()
    allow_expiry_commit = asyncio.Event()
    original_transition = conversation_repo.return_due_handoff_to_bot

    async def _paused_transition(*args, **kwargs):  # type: ignore[no-untyped-def]
        expiry_holds_lock.set()
        await allow_expiry_commit.wait()
        return await original_transition(*args, **kwargs)

    monkeypatch.setattr(
        "app.services.handoff_expiry.conversation_repo."
        "return_due_handoff_to_bot",
        _paused_transition,
    )

    expiry_task = asyncio.create_task(
        HandoffExpiryWorker(session_factory).expire_one()
    )
    await asyncio.wait_for(expiry_holds_lock.wait(), timeout=2)
    manager_task = asyncio.create_task(
        SyntheticManagerMessageService(session_factory).apply(
            _manager("manager-2", provider_sequence=2)
        )
    )
    await asyncio.sleep(0)
    assert not manager_task.done()

    allow_expiry_commit.set()
    expiry_result, manager_result = await asyncio.gather(
        expiry_task,
        manager_task,
    )

    assert expiry_result is not None
    assert manager_result.manager_epoch == 2
    conversation = await _load_conversation(session_factory, conversation_id)
    assert conversation.handoff_state == HandoffState.HUMAN_ACTIVE.value
    assert conversation.ownership == ConversationOwnership.MANAGER.value
    assert conversation.active_reply_plan_id is None
    async with session_factory() as session:
        plan = await session.get(ReplyPlan, plan_id)
        assert plan is not None
        assert plan.status == ReplyPlanStatus.CANCELLED.value


async def test_crash_before_commit_leaves_due_row_for_restarted_worker(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id, _ = await _make_due(
        session_factory,
        with_client_pause=False,
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        async with session_factory() as session:
            async with session.begin():
                claimed = await conversation_repo.claim_next_due_handoff(session)
                assert claimed is not None
                raise RuntimeError("simulated crash")

    result = await HandoffExpiryWorker(session_factory).expire_one()
    assert result is not None
    assert result.conversation_id == conversation_id


async def test_restart_after_expiry_commit_does_not_duplicate_deferred_plan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, plan_id = await _make_due(session_factory, with_client_pause=True)

    first_process = HandoffExpiryWorker(session_factory)
    assert await first_process.expire_one() is not None
    restarted_process = HandoffExpiryWorker(session_factory)
    assert await restarted_process.expire_one() is None

    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReplyPlan)
                .where(ReplyPlan.id == plan_id)
            )
            == 1
        )


async def test_invalid_paused_dialog_fails_closed_and_remains_recoverable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id, _ = await _make_due(
        session_factory,
        with_client_pause=True,
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(active_reply_plan_id=None)
            )

    with pytest.raises(
        HandoffExpiryInvariantError,
        match="HANDOFF_DEFERRED_PLAN_MISSING",
    ):
        await HandoffExpiryWorker(session_factory).expire_one()

    conversation = await _load_conversation(session_factory, conversation_id)
    assert conversation.handoff_state == HandoffState.HUMAN_PAUSE.value
    assert conversation.ownership == ConversationOwnership.MANAGER.value
