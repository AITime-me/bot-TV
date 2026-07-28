from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.outbound_policy import OutboundAction, is_automatic_outbound_allowed
from app.db.clock import db_now
from app.db.session import session_scope
from app.models.amocrm_mirror import (
    FORBIDDEN_MIRROR_PAYLOAD_KEYS,
    MIRROR_PAYLOAD_SCHEMA,
    AmoCrmMirrorJob,
    AmoCrmMirrorJobType,
    AmoCrmMirrorSkipReason,
    AmoCrmMirrorStatus,
    AmoCrmMirrorSubjectKind,
    client_message_mirror_key,
    manager_takeover_mirror_key,
    outbound_delivered_mirror_key,
    reply_plan_state_mirror_key,
)
from app.models.conversation import Conversation
from app.models.inbox import InboxMessage
from app.models.outbox import DeliveryStatus, OutboxMessage
from app.models.reply_plan import ReplyPlan, ReplyPlanStatus
from app.repositories import amocrm_mirror as mirror_repo
from app.repositories import conversations as conversation_repo
from app.repositories.amocrm_mirror import (
    AmoCrmMirrorClaim,
    StaleAmoCrmMirrorLeaseError,
)
from app.schemas.inbound import SyntheticInboundEvent
from app.services.amocrm_adapter import (
    AmoCrmMirrorOutcome,
    NoopAmoCrmMirrorAdapter,
)
from app.services.amocrm_mirror import (
    AmoCrmMirrorRejected,
    AmoCrmMirrorWorker,
    enqueue_manager_takeover,
)
from app.services.inbound import InboundService
from app.services.outbound_arbiter import OutboundArbiter
from app.services.reply_outbound import OutboundWorker, ReplyPlanWorker
from app.services.takeover import ManagerTakeoverService
from tests.pg_harness import truncate_foundation_tables


def _inbound(
    event_id: str,
    conv: str = "mirror-conv",
    text: str = "synth-text",
) -> SyntheticInboundEvent:
    return SyntheticInboundEvent(
        external_conversation_id=conv,
        external_message_id=event_id,
        text=text,
    )


@pytest_asyncio.fixture(autouse=True)
async def mirror_row_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


async def _all_jobs(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[AmoCrmMirrorJob]:
    async with session_scope(session_factory) as session:
        rows = await session.scalars(
            select(AmoCrmMirrorJob).order_by(AmoCrmMirrorJob.created_at)
        )
        return list(rows)


async def _job_of_type(
    session_factory: async_sessionmaker[AsyncSession],
    job_type: AmoCrmMirrorJobType,
) -> AmoCrmMirrorJob:
    jobs = [
        job
        for job in await _all_jobs(session_factory)
        if job.job_type == job_type.value
    ]
    assert len(jobs) == 1, f"expected exactly one {job_type.value}, got {len(jobs)}"
    return jobs[0]


async def _job_count(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_scope(session_factory) as session:
        count = await session.scalar(select(func.count()).select_from(AmoCrmMirrorJob))
        return int(count or 0)


async def _claim_job_of_type(
    worker: AmoCrmMirrorWorker,
    job_type: AmoCrmMirrorJobType,
    *,
    now: datetime | None = None,
) -> AmoCrmMirrorClaim:
    """Drain the queue in insertion order until the wanted job is claimed."""
    for _ in range(10):
        claim = await worker.claim_one(now=now)
        assert claim is not None, f"no claimable job left before {job_type.value}"
        if claim.job_type == job_type.value:
            return claim
        await worker.process_claimed(claim, now=now)
    raise AssertionError("queue drain limit exceeded")


async def _dispatch_first_plan(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    conv: str = "mirror-conv",
    event_id: str = "mirror-msg-1",
) -> tuple[Conversation, ReplyPlan, datetime]:
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound(event_id, conv=conv))
        assert result.reply_plan is not None
        conversation = result.conversation
        plan = result.reply_plan
        due = plan.not_before + timedelta(seconds=1)

    plan_worker = ReplyPlanWorker(session_factory, worker_id="mirror-plan")
    claim = await plan_worker.claim_one(now=due)
    assert claim is not None
    dispatched = await plan_worker.dispatch_claimed(claim)
    assert dispatched.plan_status == ReplyPlanStatus.DISPATCHED.value
    return conversation, plan, due


@pytest.mark.asyncio
async def test_inbound_enqueues_client_message_meta_exactly_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event = _inbound("mirror-msg-once")
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(event)
        inbox_id = result.inbox.id
        conversation_id = result.conversation.id

    job = await _job_of_type(session_factory, AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META)
    assert job.status == AmoCrmMirrorStatus.PENDING.value
    assert job.subject_kind == AmoCrmMirrorSubjectKind.INBOX_MESSAGE.value
    assert job.subject_id == inbox_id
    assert job.conversation_id == conversation_id
    assert job.context_version == 1
    assert job.mirror_key == client_message_mirror_key(inbox_id)
    assert job.attempt_count == 0
    assert job.lease_token is None
    assert job.lease_version == 0
    assert job.next_attempt_at is None
    assert job.payload_json["schema"] == MIRROR_PAYLOAD_SCHEMA

    # A duplicate delivery bumps nothing and enqueues nothing.
    async with session_scope(session_factory) as session:
        duplicate = await InboundService(session).accept(event)
        assert duplicate.duplicate is True
    assert await _job_count(session_factory) == 1


@pytest.mark.asyncio
async def test_enqueue_rolls_back_with_the_domain_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The job exists if and only if the domain change commits."""
    with pytest.raises(RuntimeError, match="forced_rollback"):
        async with session_scope(session_factory) as session:
            await InboundService(session).accept(_inbound("mirror-msg-rollback"))
            raise RuntimeError("forced_rollback")

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Conversation)) == 0
        assert await session.scalar(select(func.count()).select_from(InboxMessage)) == 0
        assert await session.scalar(
            select(func.count()).select_from(AmoCrmMirrorJob)
        ) == 0


@pytest.mark.asyncio
async def test_takeover_enqueues_one_job_without_context_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound("mirror-msg-takeover"))
        conversation_id = result.conversation.id

    first = await ManagerTakeoverService(session_factory).apply(conversation_id)
    assert first.changed is True
    job = await _job_of_type(session_factory, AmoCrmMirrorJobType.MANAGER_TAKEOVER)
    assert job.subject_kind == AmoCrmMirrorSubjectKind.CONVERSATION.value
    assert job.subject_id == conversation_id
    assert job.context_version is None
    assert job.mirror_key == manager_takeover_mirror_key(conversation_id)
    assert "context_version" not in job.payload_json

    # Idempotent takeover: no second job.
    repeated = await ManagerTakeoverService(session_factory).apply(conversation_id)
    assert repeated.changed is False
    await _job_of_type(session_factory, AmoCrmMirrorJobType.MANAGER_TAKEOVER)


@pytest.mark.asyncio
async def test_dispatch_and_admission_enqueue_bot_action_jobs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation, plan, due = await _dispatch_first_plan(session_factory)

    plan_job = await _job_of_type(
        session_factory,
        AmoCrmMirrorJobType.REPLY_PLAN_STATE_CHANGED,
    )
    assert plan_job.subject_kind == AmoCrmMirrorSubjectKind.REPLY_PLAN.value
    assert plan_job.subject_id == plan.id
    assert plan_job.context_version == 1
    assert plan_job.mirror_key == reply_plan_state_mirror_key(
        plan.id,
        ReplyPlanStatus.DISPATCHED.value,
    )
    assert plan_job.payload_json["subject_status"] == ReplyPlanStatus.DISPATCHED.value

    arbiter = OutboundArbiter(session_factory)
    out_worker = OutboundWorker(
        session_factory,
        worker_id="mirror-out",
        arbiter=arbiter,
    )
    out_claim = await out_worker.claim_one(now=due)
    assert out_claim is not None
    admitted = await out_worker.process_claimed(out_claim, now=due)
    assert admitted.admitted is True

    outbound_job = await _job_of_type(
        session_factory,
        AmoCrmMirrorJobType.OUTBOUND_DELIVERED_META,
    )
    assert outbound_job.subject_kind == AmoCrmMirrorSubjectKind.OUTBOX_MESSAGE.value
    assert outbound_job.subject_id == out_claim.outbound_id
    assert outbound_job.mirror_key == outbound_delivered_mirror_key(
        out_claim.outbound_id
    )
    assert outbound_job.payload_json["subject_status"] == DeliveryStatus.DELIVERED.value

    # Three distinct events, one job each, all still PENDING.
    jobs = await _all_jobs(session_factory)
    assert [job.job_type for job in jobs] == [
        AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META.value,
        AmoCrmMirrorJobType.REPLY_PLAN_STATE_CHANGED.value,
        AmoCrmMirrorJobType.OUTBOUND_DELIVERED_META.value,
    ]
    assert {job.status for job in jobs} == {AmoCrmMirrorStatus.PENDING.value}
    assert conversation.id == jobs[0].conversation_id


@pytest.mark.asyncio
async def test_claim_issues_fresh_fencing_and_completes_mirrored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        await InboundService(session).accept(_inbound("mirror-msg-claim"))
        before_claim = await db_now(session)

    worker = AmoCrmMirrorWorker(session_factory, worker_id="mirror-w")
    claim = await worker.claim_one()
    assert claim is not None
    assert claim.status == AmoCrmMirrorStatus.PROCESSING.value
    assert claim.attempt_count == 1
    assert claim.lease_version == 1
    assert claim.lease_owner == "mirror-w"
    # The lease window is measured on the PostgreSQL timeline, not the host's.
    assert claim.lease_until > before_claim

    result = await worker.process_claimed(claim)
    assert result.mirrored is True
    assert result.status == AmoCrmMirrorStatus.MIRRORED.value
    assert result.skip_reason is None
    assert len(worker.adapter.calls) == 1
    assert worker.adapter.calls[0].job_id == str(claim.job_id)

    async with session_scope(session_factory) as session:
        job = await mirror_repo.get_by_id(session, job_id=claim.job_id)
        assert job is not None
        assert job.status == AmoCrmMirrorStatus.MIRRORED.value
        assert job.lease_token is None
        assert job.lease_owner is None
        assert job.lease_until is None
        assert job.error_code is None
        assert job.skip_reason is None

    # Terminal jobs are never claimable again, even later on the PG timeline.
    async with session_scope(session_factory) as session:
        later = await db_now(session) + timedelta(hours=1)
    assert await worker.claim_one(now=later) is None


@pytest.mark.asyncio
async def test_second_worker_cannot_claim_a_leased_job(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        await InboundService(session).accept(_inbound("mirror-msg-lease"))

    worker_a = AmoCrmMirrorWorker(session_factory, worker_id="mirror-a")
    worker_b = AmoCrmMirrorWorker(session_factory, worker_id="mirror-b")
    claim_a = await worker_a.claim_one()
    assert claim_a is not None
    assert await worker_b.claim_one() is None
    assert worker_b.adapter.calls == []


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_stale_completion_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        await InboundService(session).accept(_inbound("mirror-msg-fencing"))

    worker_a = AmoCrmMirrorWorker(session_factory, worker_id="mirror-a")
    worker_b = AmoCrmMirrorWorker(session_factory, worker_id="mirror-b")
    claim_a = await worker_a.claim_one()
    assert claim_a is not None

    async with session_scope(session_factory) as session:
        expired = await db_now(session) - timedelta(seconds=5)
        await session.execute(
            update(AmoCrmMirrorJob)
            .where(AmoCrmMirrorJob.id == claim_a.job_id)
            .values(lease_until=expired)
        )

    claim_b = await worker_b.claim_one()
    assert claim_b is not None
    assert claim_b.job_id == claim_a.job_id
    assert claim_b.lease_version == claim_a.lease_version + 1
    assert claim_b.lease_token != claim_a.lease_token
    assert claim_b.attempt_count == claim_a.attempt_count + 1

    with pytest.raises(StaleAmoCrmMirrorLeaseError):
        await worker_a.process_claimed(claim_a)
    # Fencing must reject the superseded lease before any sink call.
    assert worker_a.adapter.calls == []
    async with session_scope(session_factory) as session:
        with pytest.raises(StaleAmoCrmMirrorLeaseError):
            await mirror_repo.complete_with_lease(
                session,
                job_id=claim_a.job_id,
                lease_token=claim_a.lease_token,
                lease_version=claim_a.lease_version,
            )

    result = await worker_b.process_claimed(claim_b)
    assert result.status == AmoCrmMirrorStatus.MIRRORED.value
    assert worker_a.adapter.calls == []
    assert len(worker_b.adapter.calls) == 1


@pytest.mark.asyncio
async def test_expired_final_mirror_lease_recovers_to_dead_without_adapter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        await InboundService(session).accept(_inbound("mirror-msg-final-lease"))
        job = await session.scalar(select(AmoCrmMirrorJob))
        assert job is not None
        job.max_attempts = 1
        await session.flush()

    crashed_worker = AmoCrmMirrorWorker(
        session_factory,
        worker_id="mirror-crashed",
    )
    stale_claim = await crashed_worker.claim_one()
    assert stale_claim is not None
    assert stale_claim.attempt_count == stale_claim.max_attempts == 1

    async with session_scope(session_factory) as session:
        expired = await db_now(session) - timedelta(seconds=5)
        await session.execute(
            update(AmoCrmMirrorJob)
            .where(AmoCrmMirrorJob.id == stale_claim.job_id)
            .values(lease_until=expired)
        )

    recovery_worker = AmoCrmMirrorWorker(
        session_factory,
        worker_id="mirror-recovery",
    )
    assert await recovery_worker.claim_one() is None
    assert crashed_worker.adapter.calls == []
    assert recovery_worker.adapter.calls == []

    async with session_scope(session_factory) as session:
        job = await mirror_repo.get_by_id(session, job_id=stale_claim.job_id)
        assert job is not None
        assert job.status == AmoCrmMirrorStatus.DEAD.value
        assert job.attempt_count == job.max_attempts == 1
        assert job.lease_owner is None
        assert job.lease_token is None
        assert job.lease_until is None
        assert job.lease_version == stale_claim.lease_version
        assert job.error_code == "LEASE_ATTEMPTS_EXHAUSTED"
        assert job.skip_reason is None

    with pytest.raises(StaleAmoCrmMirrorLeaseError):
        async with session_scope(session_factory) as session:
            await mirror_repo.complete_with_lease(
                session,
                job_id=stale_claim.job_id,
                lease_token=stale_claim.lease_token,
                lease_version=stale_claim.lease_version,
            )
    with pytest.raises(StaleAmoCrmMirrorLeaseError):
        async with session_scope(session_factory) as session:
            await mirror_repo.fail_with_lease(
                session,
                job_id=stale_claim.job_id,
                lease_token=stale_claim.lease_token,
                lease_version=stale_claim.lease_version,
                error_code="STALE_WORKER",
            )

    async with session_scope(session_factory) as session:
        later = await db_now(session) + timedelta(hours=1)
    assert await recovery_worker.claim_one(now=later) is None
    assert crashed_worker.adapter.calls == []
    assert recovery_worker.adapter.calls == []


@pytest.mark.asyncio
async def test_bot_action_job_is_skipped_after_manager_takeover(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation, _plan, _due = await _dispatch_first_plan(session_factory)
    await ManagerTakeoverService(session_factory).apply(conversation.id)

    worker = AmoCrmMirrorWorker(session_factory, worker_id="mirror-skip-takeover")
    claim = await _claim_job_of_type(
        worker,
        AmoCrmMirrorJobType.REPLY_PLAN_STATE_CHANGED,
    )
    calls_before = len(worker.adapter.calls)
    result = await worker.process_claimed(claim)

    assert result.status == AmoCrmMirrorStatus.SKIPPED.value
    assert result.mirrored is False
    assert result.skip_reason == AmoCrmMirrorSkipReason.MANAGER_TAKEOVER.value
    assert len(worker.adapter.calls) == calls_before

    async with session_scope(session_factory) as session:
        job = await mirror_repo.get_by_id(session, job_id=claim.job_id)
        assert job is not None
        assert job.status == AmoCrmMirrorStatus.SKIPPED.value
        assert job.skip_reason == AmoCrmMirrorSkipReason.MANAGER_TAKEOVER.value
        assert job.error_code is None

    # The takeover fact itself is still mirrored.
    takeover_claim = await _claim_job_of_type(
        worker,
        AmoCrmMirrorJobType.MANAGER_TAKEOVER,
    )
    takeover_result = await worker.process_claimed(takeover_claim)
    assert takeover_result.status == AmoCrmMirrorStatus.MIRRORED.value


@pytest.mark.asyncio
async def test_bot_action_job_is_skipped_on_stale_context(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _dispatch_first_plan(session_factory)
    async with session_scope(session_factory) as session:
        second = await InboundService(session).accept(_inbound("mirror-msg-2"))
        assert second.context_version == 2

    worker = AmoCrmMirrorWorker(session_factory, worker_id="mirror-skip-context")
    claim = await _claim_job_of_type(
        worker,
        AmoCrmMirrorJobType.REPLY_PLAN_STATE_CHANGED,
    )
    assert claim.context_version == 1
    result = await worker.process_claimed(claim)
    assert result.status == AmoCrmMirrorStatus.SKIPPED.value
    assert result.skip_reason == AmoCrmMirrorSkipReason.STALE_CONTEXT.value


@pytest.mark.asyncio
async def test_client_message_job_survives_takeover_and_newer_context(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Domain facts are never dropped: the CRM must still see the message."""
    async with session_scope(session_factory) as session:
        first = await InboundService(session).accept(_inbound("mirror-msg-fact"))
        conversation_id = first.conversation.id
    async with session_scope(session_factory) as session:
        await InboundService(session).accept(_inbound("mirror-msg-fact-2"))
    await ManagerTakeoverService(session_factory).apply(conversation_id)

    worker = AmoCrmMirrorWorker(session_factory, worker_id="mirror-fact")
    claim = await worker.claim_one()
    assert claim is not None
    assert claim.job_type == AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META.value
    assert claim.context_version == 1
    result = await worker.process_claimed(claim)
    assert result.status == AmoCrmMirrorStatus.MIRRORED.value


@pytest.mark.asyncio
async def test_missing_subject_row_is_skipped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound("mirror-msg-subject"))
        inbox_id = result.inbox.id

    async with session_scope(session_factory) as session:
        inbox = await session.get(InboxMessage, inbox_id)
        assert inbox is not None
        await session.delete(inbox)

    worker = AmoCrmMirrorWorker(session_factory, worker_id="mirror-subject")
    claim = await worker.claim_one()
    assert claim is not None
    outcome = await worker.process_claimed(claim)
    assert outcome.status == AmoCrmMirrorStatus.SKIPPED.value
    assert outcome.skip_reason == AmoCrmMirrorSkipReason.SUBJECT_STATE_CHANGED.value
    assert worker.adapter.calls == []


@pytest.mark.asyncio
async def test_adapter_failure_retries_on_pg_clock_then_dies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        await InboundService(session).accept(_inbound("mirror-msg-retry"))
        job = await session.scalar(select(AmoCrmMirrorJob))
        assert job is not None
        job.max_attempts = 2
        await session.flush()

    worker = AmoCrmMirrorWorker(
        session_factory,
        worker_id="mirror-retry",
        adapter=NoopAmoCrmMirrorAdapter(
            forced_outcome=AmoCrmMirrorOutcome.TRANSIENT_ERROR
        ),
        retry_delay_seconds=1,
    )

    first = await worker.claim_one()
    assert first is not None
    with pytest.raises(AmoCrmMirrorRejected):
        await worker.process_claimed(first)

    async with session_scope(session_factory) as session:
        job = await mirror_repo.get_by_id(session, job_id=first.job_id)
        assert job is not None
        assert job.status == AmoCrmMirrorStatus.FAILED.value
        assert job.attempt_count == 1
        assert job.error_code == "AMOCRM_MIRROR_TRANSIENT"
        assert job.next_attempt_at is not None
        retry_at = job.next_attempt_at

    # Not claimable before next_attempt_at on the PostgreSQL timeline.
    assert await worker.claim_one(now=retry_at - timedelta(milliseconds=1)) is None

    second = await worker.claim_one(now=retry_at)
    assert second is not None
    assert second.attempt_count == 2
    assert second.lease_version == first.lease_version + 1
    with pytest.raises(AmoCrmMirrorRejected):
        await worker.process_claimed(second)

    async with session_scope(session_factory) as session:
        job = await mirror_repo.get_by_id(session, job_id=first.job_id)
        assert job is not None
        assert job.status == AmoCrmMirrorStatus.DEAD.value
        assert job.attempt_count == 2
        assert job.next_attempt_at is None

    # DEAD is terminal: no automatic resurrection.
    async with session_scope(session_factory) as session:
        later = await db_now(session) + timedelta(days=1)
    assert await worker.claim_one(now=later) is None

    statuses = [job.status for job in await _all_jobs(session_factory)]
    assert AmoCrmMirrorStatus.MIRRORED.value not in statuses


@pytest.mark.asyncio
async def test_concurrent_enqueue_of_one_event_creates_one_job(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound("mirror-msg-conc"))
        conversation_id = result.conversation.id

    async def _enqueue_once() -> tuple[object, bool]:
        async with session_scope(session_factory) as session:
            job, created = await enqueue_manager_takeover(
                session,
                conversation_id=conversation_id,
                correlation_id=uuid4(),
            )
            return job.id, created

    first, second = await asyncio.gather(_enqueue_once(), _enqueue_once())
    assert first[0] == second[0]
    assert [first[1], second[1]].count(True) == 1
    await _job_of_type(session_factory, AmoCrmMirrorJobType.MANAGER_TAKEOVER)


@pytest.mark.asyncio
async def test_mirror_worker_and_inbound_do_not_deadlock(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror worker holds the dialog lock while inbound waits for it.

    Deterministic barriers, no sleep: the worker takes Conversation FOR UPDATE
    first, so a concurrent client message queues behind it instead of closing a
    lock cycle.
    """
    async with session_scope(session_factory) as session:
        await InboundService(session).accept(_inbound("mirror-msg-race"))

    worker = AmoCrmMirrorWorker(session_factory, worker_id="mirror-race")
    claim = await worker.claim_one()
    assert claim is not None
    assert claim.job_type == AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META.value

    original_for_update = conversation_repo.get_by_id_for_update
    original_lock = conversation_repo.lock_for_update
    worker_holds_conversation = asyncio.Event()
    allow_worker_past_lock = asyncio.Event()
    inbound_reached_lock = asyncio.Event()

    async def gated_for_update(session, *, conversation_id):  # type: ignore[no-untyped-def]
        conversation = await original_for_update(
            session,
            conversation_id=conversation_id,
        )
        worker_holds_conversation.set()
        await allow_worker_past_lock.wait()
        return conversation

    async def gated_lock(session, *, conversation_id):  # type: ignore[no-untyped-def]
        inbound_reached_lock.set()
        return await original_lock(session, conversation_id=conversation_id)

    monkeypatch.setattr(
        "app.services.amocrm_mirror.conversation_repo.get_by_id_for_update",
        gated_for_update,
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.lock_for_update",
        gated_lock,
    )

    process_task = asyncio.create_task(worker.process_claimed(claim))
    await asyncio.wait_for(worker_holds_conversation.wait(), timeout=10)

    async def _accept_second() -> int:
        async with session_scope(session_factory) as session:
            accepted = await InboundService(session).accept(
                _inbound("mirror-msg-race-2")
            )
            return accepted.context_version

    inbound_task = asyncio.create_task(_accept_second())
    await asyncio.wait_for(inbound_reached_lock.wait(), timeout=10)
    assert not inbound_task.done()

    allow_worker_past_lock.set()
    mirrored, context_version = await asyncio.wait_for(
        asyncio.gather(process_task, inbound_task),
        timeout=15,
    )
    assert mirrored.status == AmoCrmMirrorStatus.MIRRORED.value
    assert context_version == 2
    assert await _job_count(session_factory) == 2


@pytest.mark.asyncio
async def test_mirror_never_sends_or_delivers_anything(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        await InboundService(session).accept(_inbound("mirror-msg-fail-closed"))
        outbox_before = await session.scalar(
            select(func.count()).select_from(OutboxMessage)
        )

    worker = AmoCrmMirrorWorker(session_factory, worker_id="mirror-fail-closed")
    claim = await worker.claim_one()
    assert claim is not None
    result = await worker.process_claimed(claim)
    assert result.status == AmoCrmMirrorStatus.MIRRORED.value

    async with session_scope(session_factory) as session:
        outbox_after = await session.scalar(
            select(func.count()).select_from(OutboxMessage)
        )
        statuses = set(await session.scalars(select(OutboxMessage.delivery_status)))
    assert outbox_after == outbox_before
    assert statuses == {DeliveryStatus.PENDING.value}
    assert is_automatic_outbound_allowed(Settings(), OutboundAction.SEND_MESSAGE) is False


@pytest.mark.asyncio
async def test_persisted_payload_and_key_contain_no_client_data(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    secret = "СЕКРЕТ КЛИЕНТА +79990001122"
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(
            _inbound("mirror-msg-pii", text=secret)
        )
        inbox_id = result.inbox.id
        # The source row genuinely holds client text — the mirror must not.
        assert secret in json.dumps(result.inbox.payload_json, ensure_ascii=False)

    job = await _job_of_type(
        session_factory,
        AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META,
    )
    rendered = json.dumps(job.payload_json, ensure_ascii=False)
    assert secret not in rendered
    assert "+7999" not in rendered
    assert not set(job.payload_json) & FORBIDDEN_MIRROR_PAYLOAD_KEYS
    assert "mirror-msg-pii" not in job.mirror_key
    assert "mirror-conv" not in job.mirror_key
    assert job.mirror_key == client_message_mirror_key(inbox_id)
    assert "payload=<redacted>" in repr(job)
    assert secret not in repr(job)


@pytest.mark.asyncio
async def test_db_rejects_duplicate_mirror_key_and_unknown_values(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound("mirror-msg-db"))
        conversation_id = result.conversation.id
        inbox_id = result.inbox.id

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                session.add(
                    AmoCrmMirrorJob(
                        job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META.value,
                        subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE.value,
                        subject_id=inbox_id,
                        conversation_id=conversation_id,
                        context_version=1,
                        mirror_key=client_message_mirror_key(inbox_id),
                        payload_json={"schema": MIRROR_PAYLOAD_SCHEMA},
                        correlation_id=uuid4(),
                    )
                )
                await session.flush()

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                session.add(
                    AmoCrmMirrorJob(
                        job_type="AMOCRM_LEAD_CREATED",
                        subject_kind=AmoCrmMirrorSubjectKind.CONVERSATION.value,
                        subject_id=conversation_id,
                        conversation_id=conversation_id,
                        mirror_key="unknown-job-type",
                        payload_json={"schema": MIRROR_PAYLOAD_SCHEMA},
                        correlation_id=uuid4(),
                    )
                )
                await session.flush()

    assert await _job_count(session_factory) == 1


@pytest.mark.asyncio
async def test_conversation_delete_cascades_mirror_jobs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        result = await InboundService(session).accept(_inbound("mirror-msg-cascade"))
        conversation_id = result.conversation.id
    assert await _job_count(session_factory) == 1

    async with session_scope(session_factory) as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(active_reply_plan_id=None)
        )
        await session.execute(
            delete(Conversation).where(Conversation.id == conversation_id)
        )
    assert await _job_count(session_factory) == 0
