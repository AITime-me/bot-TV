from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.clock import db_statement_now
from app.models.amocrm_mirror import AmoCrmMirrorJob, AmoCrmMirrorJobType
from app.models.conversation import (
    Conversation,
    ConversationStatus,
    HandoffState,
)
from app.models.manager_message import ManagerMessage, ManagerMessageStatus
from app.models.outbox import DeliveryStatus, DestinationType, OutboxMessage
from app.models.reply_plan import ReplyPlan, ReplyPlanStatus
from app.schemas.inbound import SyntheticInboundEvent
from app.schemas.manager_message import SyntheticManagerMessageEvent
from app.services.dialog_context import DialogContextService
from app.services.handoff_expiry import HandoffExpiryWorker
from app.services.inbound import InboundService
from app.services.manager_messages import (
    SyntheticManagerMessageService,
    apply_manager_message_in_session,
)
from app.services.reply_outbound import ReplyPlanWorker
from app.services.synthetic_outbound import SyntheticOutboundRequest
from tests.pg_harness import truncate_foundation_tables


@pytest_asyncio.fixture(autouse=True)
async def manager_handoff_row_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    yield
    await truncate_foundation_tables(session_factory)


def _client(message_id: str, text: str) -> SyntheticInboundEvent:
    return SyntheticInboundEvent(
        external_conversation_id="handoff-conv",
        external_message_id=message_id,
        text=text,
    )


def _manager(
    message_id: str,
    sequence: int | None,
    text: str,
) -> SyntheticManagerMessageEvent:
    return SyntheticManagerMessageEvent(
        external_conversation_id="handoff-conv",
        external_message_id=message_id,
        provider_sequence=sequence,
        text=text,
    )


async def _conversation(
    session: AsyncSession,
    conversation_id,
) -> Conversation:
    row = await session.get(Conversation, conversation_id)
    assert row is not None
    return row


async def test_manager_client_context_and_deferred_plan_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        async with session.begin():
            first = await InboundService(session).accept(
                _client("client-1", "Нужна запись на пятницу")
            )
            conversation_id = first.conversation.id
            initial_plan_id = first.reply_plan.id if first.reply_plan else None
            manager = await apply_manager_message_in_session(
                session,
                event=_manager(
                    "manager-20",
                    20,
                    "Записала Вас на пятницу в 15:00",
                ),
            )
            assert manager.status == ManagerMessageStatus.APPLIED.value
            assert manager.entered_from_bot is True
            assert manager.cancelled_plans == 1
            assert manager.context_version == 2
            assert manager.manager_epoch == 1
            assert manager.event_seq_hwm == 2

            initial_plan = await session.get(ReplyPlan, initial_plan_id)
            assert initial_plan is not None
            assert initial_plan.status == ReplyPlanStatus.CANCELLED.value

            context = await DialogContextService(session).load(
                conversation_id=conversation_id
            )
            assert [
                (message.conversation_event_seq, message.author, message.text)
                for message in context.messages
            ] == [
                (1, "client", "Нужна запись на пятницу"),
                (2, "manager", "Записала Вас на пятницу в 15:00"),
            ]

    async with session_factory() as session:
        async with session.begin():
            paused = await InboundService(session).accept(
                _client("client-2", "А можно на час позже?")
            )
            assert paused.conversation.handoff_state == HandoffState.HUMAN_PAUSE.value
            assert paused.reply_plan_created is True
            assert paused.reply_plan is not None
            assert paused.reply_plan.manager_epoch == 1
            assert paused.reply_plan.event_seq_hwm == 3
            assert (
                paused.reply_plan.not_before
                == paused.conversation.handoff_deadline_at
            )
            first_pause_deadline = paused.conversation.handoff_deadline_at
            first_deferred_id = paused.reply_plan.id

    async with session_factory() as session:
        async with session.begin():
            updated = await InboundService(session).accept(
                _client("client-3", "Лучше в 17:00")
            )
            assert updated.conversation.handoff_state == HandoffState.HUMAN_PAUSE.value
            assert updated.conversation.handoff_deadline_at == first_pause_deadline
            assert updated.reply_plan is not None
            assert updated.reply_plan.id != first_deferred_id
            assert updated.reply_plan.not_before == first_pause_deadline
            old = await session.get(ReplyPlan, first_deferred_id)
            assert old is not None
            assert old.status == ReplyPlanStatus.SUPERSEDED.value

            context = await DialogContextService(session).load(
                conversation_id=conversation_id
            )
            assert [
                message.conversation_event_seq for message in context.messages
            ] == [1, 2, 3, 4]
            assert [message.author for message in context.messages] == [
                "client",
                "manager",
                "client",
                "client",
            ]


async def test_stale_and_duplicate_manager_events_have_no_fsm_side_effects(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager_service = SyntheticManagerMessageService(session_factory)
    current = await manager_service.apply(
        _manager("manager-new", 20, "Новое сообщение менеджера")
    )
    duplicate = await manager_service.apply(
        _manager("manager-new", 20, "Новое сообщение менеджера")
    )
    assert duplicate.duplicate is True
    assert duplicate.manager_message_id == current.manager_message_id
    assert duplicate.context_version == current.context_version
    assert duplicate.manager_epoch == current.manager_epoch
    assert duplicate.event_seq_hwm == current.event_seq_hwm

    async with session_factory() as session:
        async with session.begin():
            client = await InboundService(session).accept(
                _client("client-after-manager", "Подождите, пожалуйста")
            )
            deferred_id = client.reply_plan.id if client.reply_plan else None
            deadline = client.conversation.handoff_deadline_at
            state_before = (
                client.conversation.handoff_state,
                client.conversation.context_version,
                client.conversation.manager_epoch,
                client.conversation.current_event_seq,
                client.conversation.active_reply_plan_id,
            )

    stale = await manager_service.apply(
        _manager("manager-old", 19, "Запоздалое старое сообщение")
    )
    assert stale.status == ManagerMessageStatus.STALE.value
    assert stale.fsm_changed is False
    assert (
        stale.context_version,
        stale.manager_epoch,
        stale.event_seq_hwm,
    ) == (state_before[1], state_before[2], state_before[3])

    async with session_factory() as session:
        async with session.begin():
            conversation = await _conversation(session, stale.conversation_id)
            assert (
                conversation.handoff_state,
                conversation.context_version,
                conversation.manager_epoch,
                conversation.current_event_seq,
                conversation.active_reply_plan_id,
            ) == state_before
            assert conversation.handoff_deadline_at == deadline
            deferred = await session.get(ReplyPlan, deferred_id)
            assert deferred is not None
            assert deferred.status == ReplyPlanStatus.PENDING.value
            stale_row = await session.get(ManagerMessage, stale.manager_message_id)
            assert stale_row is not None
            assert stale_row.conversation_event_seq is None

            context = await DialogContextService(session).load(
                conversation_id=conversation.id
            )
            assert "Запоздалое старое сообщение" not in [
                message.text for message in context.messages
            ]


async def test_reverse_delivery_equal_sequence_and_missing_order_are_safe(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = SyntheticManagerMessageService(session_factory)
    newer = await service.apply(_manager("manager-m2", 200, "M2"))
    older = await service.apply(_manager("manager-m1", 100, "M1"))
    equal = await service.apply(_manager("manager-equal", 200, "same sequence"))
    quarantined = await service.apply(
        _manager("manager-no-order", None, "missing order")
    )

    assert newer.status == ManagerMessageStatus.APPLIED.value
    assert older.status == ManagerMessageStatus.STALE.value
    assert equal.status == ManagerMessageStatus.STALE.value
    assert quarantined.status == ManagerMessageStatus.QUARANTINED.value
    assert newer.event_seq_hwm == 1
    assert older.event_seq_hwm == 1
    assert equal.event_seq_hwm == 1
    assert quarantined.event_seq_hwm == 1
    assert newer.manager_epoch == 1
    assert older.manager_epoch == 1
    assert equal.manager_epoch == 1
    assert quarantined.manager_epoch == 1

    async with session_factory() as session:
        async with session.begin():
            context = await DialogContextService(session).load(
                conversation_id=newer.conversation_id
            )
            assert [(message.author, message.text) for message in context.messages] == [
                ("manager", "M2")
            ]
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AmoCrmMirrorJob)
                    .where(
                        AmoCrmMirrorJob.job_type
                        == AmoCrmMirrorJobType.MANAGER_TAKEOVER.value
                    )
                )
                == 1
            )


async def test_new_manager_message_ends_pause_and_cancels_deferred_plan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = SyntheticManagerMessageService(session_factory)
    first = await service.apply(_manager("manager-1", 1, "Сейчас уточню"))
    async with session_factory() as session:
        async with session.begin():
            paused = await InboundService(session).accept(
                _client("client-paused", "Жду")
            )
            assert paused.reply_plan is not None
            deferred_id = paused.reply_plan.id

    resumed = await service.apply(_manager("manager-2", 2, "Уточнила"))
    assert resumed.entered_from_bot is False
    assert resumed.cancelled_plans == 1
    assert resumed.manager_epoch == first.manager_epoch + 1

    async with session_factory() as session:
        async with session.begin():
            conversation = await _conversation(session, resumed.conversation_id)
            assert conversation.handoff_state == HandoffState.HUMAN_ACTIVE.value
            assert conversation.human_pause_anchor_at is None
            assert conversation.active_reply_plan_id is None
            deferred = await session.get(ReplyPlan, deferred_id)
            assert deferred is not None
            assert deferred.status == ReplyPlanStatus.CANCELLED.value


async def test_second_applied_manager_in_human_active_extends_deadline(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = SyntheticManagerMessageService(session_factory)
    first = await service.apply(_manager("manager-seq-1", 1, "Первый ответ"))
    assert first.status == ManagerMessageStatus.APPLIED.value
    assert first.fsm_changed is True

    async with session_factory() as session:
        after_first = await _conversation(session, first.conversation_id)
        assert after_first.handoff_state == HandoffState.HUMAN_ACTIVE.value
        first_deadline = after_first.handoff_deadline_at
        first_epoch = after_first.manager_epoch
        first_context = after_first.context_version
        first_hwm = after_first.manager_sequence_hwm
        assert first_deadline is not None
        assert first_hwm == 1

    second = await service.apply(_manager("manager-seq-2", 2, "Второй ответ"))
    assert second.status == ManagerMessageStatus.APPLIED.value
    assert second.fsm_changed is True
    assert second.manager_epoch == first_epoch + 1
    assert second.context_version == first_context + 1
    assert second.manager_sequence_hwm == 2
    assert second.entered_from_bot is False
    assert second.cancelled_plans == 0
    assert second.cancelled_outbound == 0

    async with session_factory() as session:
        after_second = await _conversation(session, second.conversation_id)
        assert after_second.handoff_state == HandoffState.HUMAN_ACTIVE.value
        assert after_second.manager_epoch == first_epoch + 1
        assert after_second.context_version == first_context + 1
        assert after_second.manager_sequence_hwm == 2
        assert after_second.handoff_deadline_at is not None
        assert after_second.handoff_deadline_at >= first_deadline
        moment = await db_statement_now(session)
        assert (
            after_second.handoff_deadline_at - moment
            <= timedelta(seconds=15 * 60)
        )
        assert (
            after_second.handoff_deadline_at - moment
            >= timedelta(seconds=10 * 60 - 1)
        )


async def test_closed_conversation_manager_event_is_audit_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        async with session.begin():
            inbound = await InboundService(session).accept(
                _client("client-before-close", "Сообщение до закрытия")
            )
            conversation_id = inbound.conversation.id
            closed_state = (
                inbound.conversation.handoff_state,
                inbound.conversation.context_version,
                inbound.conversation.manager_epoch,
                inbound.conversation.handoff_deadline_at,
                inbound.conversation.current_event_seq,
            )
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(
                    status=ConversationStatus.CLOSED.value,
                    ownership="BOT",
                    handoff_state=HandoffState.BOT_ACTIVE.value,
                    handoff_deadline_at=None,
                    human_pause_anchor_at=None,
                    manager_takeover_at=None,
                    active_reply_plan_id=None,
                )
            )

    service = SyntheticManagerMessageService(session_factory)
    closed_event = await service.apply(
        _manager("manager-after-close", 1, "После закрытия")
    )
    assert closed_event.duplicate is False
    assert closed_event.fsm_changed is False
    assert closed_event.status == ManagerMessageStatus.QUARANTINED.value
    assert closed_event.context_version == closed_state[1]
    assert closed_event.manager_epoch == closed_state[2]
    assert closed_event.cancelled_plans == 0
    assert closed_event.cancelled_outbound == 0

    async with session_factory() as session:
        async with session.begin():
            conversation = await _conversation(session, closed_event.conversation_id)
            assert conversation.status == ConversationStatus.CLOSED.value
            assert (
                conversation.handoff_state,
                conversation.context_version,
                conversation.manager_epoch,
                conversation.handoff_deadline_at,
                conversation.current_event_seq,
            ) == closed_state
            message = await session.get(
                ManagerMessage,
                closed_event.manager_message_id,
            )
            assert message is not None
            assert message.status == ManagerMessageStatus.QUARANTINED.value
            assert message.classification_reason == "CONVERSATION_CLOSED"
            assert message.conversation_event_seq is None
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(OutboxMessage)
                    .where(
                        OutboxMessage.destination_type
                        == DestinationType.SYNTHETIC_OUTBOUND.value
                    )
                )
                == 0
            )


_MANAGER_CONTEXT_SECRET = "ManagerContextSecretToken42"


def _assert_no_manager_text_in_value(value: object, secret: str) -> None:
    if isinstance(value, str):
        assert secret not in value
        return
    if isinstance(value, dict):
        for nested in value.values():
            _assert_no_manager_text_in_value(nested, secret)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _assert_no_manager_text_in_value(nested, secret)


async def test_handoff_resume_keeps_dialog_context_out_of_transport_payload(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager_service = SyntheticManagerMessageService(session_factory)
    manager = await manager_service.apply(
        _manager("manager-context", 1, _MANAGER_CONTEXT_SECRET)
    )
    conversation_id = manager.conversation_id

    async with session_factory() as session:
        async with session.begin():
            paused = await InboundService(session).accept(
                _client("client-context", "Клиентский вопрос")
            )
            assert paused.reply_plan is not None
            plan_id = paused.reply_plan.id

    async with session_factory() as session:
        async with session.begin():
            due = await db_statement_now(session) - timedelta(seconds=1)
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(handoff_deadline_at=due)
            )
            await session.execute(
                update(ReplyPlan)
                .where(ReplyPlan.id == plan_id)
                .values(not_before=due)
            )

    assert await HandoffExpiryWorker(session_factory).expire_one() is not None

    plan_worker = ReplyPlanWorker(session_factory, worker_id="context-after-expiry")
    claim = await plan_worker.claim_one()
    assert claim is not None
    dispatched = await plan_worker.dispatch_claimed(claim)

    async with session_factory() as session:
        context = await DialogContextService(session).load(
            conversation_id=conversation_id
        )
        assert any(
            message.author == "manager" and message.text == _MANAGER_CONTEXT_SECRET
            for message in context.messages
        )
        plan = await session.get(ReplyPlan, plan_id)
        assert plan is not None
        outbound = await session.get(OutboxMessage, dispatched.outbound_id)
        assert outbound is not None
        plan_payload = dict(plan.payload_json)
        outbound_payload = dict(outbound.payload_json)
        outbound_payload_json = json.dumps(outbound.payload_json)

    _assert_no_manager_text_in_value(plan_payload, _MANAGER_CONTEXT_SECRET)
    _assert_no_manager_text_in_value(outbound_payload, _MANAGER_CONTEXT_SECRET)
    _assert_no_manager_text_in_value(
        outbound_payload_json,
        _MANAGER_CONTEXT_SECRET,
    )
    request = SyntheticOutboundRequest(
        outbound_id=str(dispatched.outbound_id),
        conversation_id=str(conversation_id),
        reply_plan_id=str(plan_id),
        context_version=outbound_payload.get("context_version"),
        correlation_id=None,
        _payload_schema=str(outbound_payload.get("schema", "unknown")),
    )
    assert _MANAGER_CONTEXT_SECRET not in repr(request)
