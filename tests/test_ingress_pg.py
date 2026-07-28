from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.outbound_policy import OutboundAction, is_automatic_outbound_allowed
from app.config import Settings
from app.db.session import session_scope
from app.models.inbox import InboxMessage
from app.models.ingress import IngressEvent, IngressStatus
from app.models.outbox import OutboxMessage
from app.repositories import ingress as ingress_repo
from app.repositories.ingress import StaleIngressLeaseError
from app.schemas.ingress import SyntheticIngressEvent
from app.services.ingress import (
    IngressPersistError,
    IngressWorker,
    SyntheticIngressAdapter,
)
from tests.pg_harness import truncate_foundation_tables


def _event(
    *,
    external_event_id: str,
    external_conversation_id: str = "synth-conv-ingress",
    text: str = "ingress-fixture-text",
) -> SyntheticIngressEvent:
    return SyntheticIngressEvent(
        external_event_id=external_event_id,
        external_conversation_id=external_conversation_id,
        text=text,
    )


@pytest_asyncio.fixture(autouse=True)
async def ingress_row_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


@pytest.mark.asyncio
async def test_first_ingress_persist(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = SyntheticIngressAdapter(session_factory)
    ack = await adapter.accept(_event(external_event_id="evt-first"))
    assert ack.accepted is True
    assert ack.duplicate is False
    assert ack.status == IngressStatus.RECEIVED.value

    async with session_factory() as session:
        async with session.begin():
            row = await session.get(IngressEvent, ack.event_id)
            assert row is not None
            assert row.attempt_count == 0
            assert row.lease_token is None


@pytest.mark.asyncio
async def test_duplicate_delivery_returns_same_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = SyntheticIngressAdapter(session_factory)
    event = _event(external_event_id="evt-dup")
    first = await adapter.accept(event)
    second = await adapter.accept(event)
    assert second.accepted is True
    assert second.duplicate is True
    assert second.event_id == first.event_id
    assert second.correlation_id == first.correlation_id

    async with session_factory() as session:
        async with session.begin():
            count = await session.scalar(select(func.count()).select_from(IngressEvent))
            assert count == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_insert_is_safe(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = SyntheticIngressAdapter(session_factory)
    event = _event(external_event_id="evt-race")
    first, second = await asyncio.gather(adapter.accept(event), adapter.accept(event))
    assert {first.event_id, second.event_id} == {first.event_id}
    assert first.event_id == second.event_id
    assert sorted([first.duplicate, second.duplicate]) == [False, True]

    async with session_factory() as session:
        async with session.begin():
            assert (
                await session.scalar(select(func.count()).select_from(IngressEvent))
                == 1
            )


@pytest.mark.asyncio
async def test_distinct_events_same_provider(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = SyntheticIngressAdapter(session_factory)
    a = await adapter.accept(_event(external_event_id="evt-a"))
    b = await adapter.accept(_event(external_event_id="evt-b"))
    assert a.event_id != b.event_id
    async with session_factory() as session:
        async with session.begin():
            assert (
                await session.scalar(select(func.count()).select_from(IngressEvent))
                == 2
            )


@pytest.mark.asyncio
async def test_db_rejects_invalid_ingress_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = SyntheticIngressAdapter(session_factory)
    ack = await adapter.accept(_event(external_event_id="evt-bad-status"))
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE ingress_events SET status = 'SENT' "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(ack.event_id)},
                )


@pytest.mark.asyncio
async def test_claim_lease_and_exclusive_processing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = SyntheticIngressAdapter(session_factory)
    await adapter.accept(_event(external_event_id="evt-lease"))

    worker_a = IngressWorker(session_factory, worker_id="worker-a", lease_seconds=30)
    worker_b = IngressWorker(session_factory, worker_id="worker-b", lease_seconds=30)
    claim_a = await worker_a.claim_one()
    claim_b = await worker_b.claim_one()
    assert claim_a is not None
    assert claim_b is None
    assert claim_a.lease_owner == "worker-a"
    assert claim_a.status == IngressStatus.PROCESSING.value
    assert claim_a.attempt_count == 1


@pytest.mark.asyncio
async def test_reclaim_after_lease_expiry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = SyntheticIngressAdapter(session_factory)
    await adapter.accept(_event(external_event_id="evt-expire"))
    worker_a = IngressWorker(session_factory, worker_id="worker-a", lease_seconds=30)
    claim_a = await worker_a.claim_one()
    assert claim_a is not None

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(IngressEvent)
                .where(IngressEvent.id == claim_a.event_id)
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=5))
            )

    worker_b = IngressWorker(session_factory, worker_id="worker-b", lease_seconds=30)
    claim_b = await worker_b.claim_one()
    assert claim_b is not None
    assert claim_b.event_id == claim_a.event_id
    assert claim_b.lease_owner == "worker-b"
    assert claim_b.lease_version == claim_a.lease_version + 1
    assert claim_b.lease_token != claim_a.lease_token
    assert claim_b.attempt_count == 2


@pytest.mark.asyncio
async def test_stale_worker_cannot_complete(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = SyntheticIngressAdapter(session_factory)
    await adapter.accept(_event(external_event_id="evt-stale"))
    worker_a = IngressWorker(session_factory, worker_id="worker-a", lease_seconds=30)
    claim_a = await worker_a.claim_one()
    assert claim_a is not None

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(IngressEvent)
                .where(IngressEvent.id == claim_a.event_id)
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=5))
            )

    worker_b = IngressWorker(session_factory, worker_id="worker-b", lease_seconds=30)
    claim_b = await worker_b.claim_one()
    assert claim_b is not None

    with pytest.raises(StaleIngressLeaseError):
        async with session_scope(session_factory) as session:
            await ingress_repo.complete_with_lease(
                session,
                event_id=claim_a.event_id,
                lease_token=claim_a.lease_token,
                lease_version=claim_a.lease_version,
            )

    async with session_scope(session_factory) as session:
        event = await ingress_repo.complete_with_lease(
            session,
            event_id=claim_b.event_id,
            lease_token=claim_b.lease_token,
            lease_version=claim_b.lease_version,
        )
        assert event.status == IngressStatus.PROCESSED.value


@pytest.mark.asyncio
async def test_retry_after_failed_then_dead_on_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = SyntheticIngressAdapter(session_factory, max_attempts=2)
    await adapter.accept(_event(external_event_id="evt-retry-dead"))
    worker = IngressWorker(
        session_factory,
        worker_id="worker-retry",
        lease_seconds=30,
        retry_delay_seconds=0,
    )

    first = await worker.claim_one()
    assert first is not None
    failed = await worker.fail_claimed(first, error_code="forced_fail")
    assert failed.status == IngressStatus.FAILED.value
    assert failed.attempt_count == 1
    assert failed.next_attempt_at is not None

    second = await worker.claim_one()
    assert second is not None
    assert second.attempt_count == 2
    dead = await worker.fail_claimed(second, error_code="forced_fail_again")
    assert dead.status == IngressStatus.DEAD.value

    assert await worker.claim_one() is None


@pytest.mark.asyncio
async def test_expired_final_ingress_lease_recovers_to_dead_without_processing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = SyntheticIngressAdapter(session_factory, max_attempts=1)
    await adapter.accept(_event(external_event_id="evt-final-lease"))

    crashed_worker = IngressWorker(session_factory, worker_id="worker-crashed")
    stale_claim = await crashed_worker.claim_one()
    assert stale_claim is not None
    assert stale_claim.attempt_count == stale_claim.max_attempts == 1

    async with session_scope(session_factory) as session:
        await session.execute(
            update(IngressEvent)
            .where(IngressEvent.id == stale_claim.event_id)
            .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=5))
        )

    recovery_worker = IngressWorker(session_factory, worker_id="worker-recovery")
    assert await recovery_worker.claim_one() is None

    async with session_scope(session_factory) as session:
        row = await session.get(IngressEvent, stale_claim.event_id)
        assert row is not None
        assert row.status == IngressStatus.DEAD.value
        assert row.attempt_count == row.max_attempts == 1
        assert row.lease_owner is None
        assert row.lease_token is None
        assert row.lease_until is None
        assert row.lease_version == stale_claim.lease_version
        assert row.error_code == "LEASE_ATTEMPTS_EXHAUSTED"
        assert await session.scalar(select(func.count()).select_from(InboxMessage)) == 0
        assert await session.scalar(select(func.count()).select_from(OutboxMessage)) == 0

    with pytest.raises(StaleIngressLeaseError):
        async with session_scope(session_factory) as session:
            await ingress_repo.complete_with_lease(
                session,
                event_id=stale_claim.event_id,
                lease_token=stale_claim.lease_token,
                lease_version=stale_claim.lease_version,
            )
    with pytest.raises(StaleIngressLeaseError):
        async with session_scope(session_factory) as session:
            await ingress_repo.fail_with_lease(
                session,
                event_id=stale_claim.event_id,
                lease_token=stale_claim.lease_token,
                lease_version=stale_claim.lease_version,
                error_code="STALE_WORKER",
            )

    assert await recovery_worker.claim_one() is None


@pytest.mark.asyncio
async def test_crash_after_commit_before_processing_is_recoverable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = SyntheticIngressAdapter(session_factory)
    ack = await adapter.accept(_event(external_event_id="evt-crash"))
    # Simulate process crash: ACK already issued, no worker ran yet.
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(IngressEvent, ack.event_id)
            assert row is not None
            assert row.status == IngressStatus.RECEIVED.value

    worker = IngressWorker(session_factory, worker_id="worker-recover")
    claim = await worker.claim_one()
    assert claim is not None
    result = await worker.process_claimed(claim)
    assert result.status == IngressStatus.PROCESSED.value
    assert result.inbox_id is not None
    assert result.outbox_id is not None

    async with session_factory() as session:
        async with session.begin():
            assert (
                await session.scalar(select(func.count()).select_from(InboxMessage))
                == 1
            )
            assert (
                await session.scalar(select(func.count()).select_from(OutboxMessage))
                == 1
            )


@pytest.mark.asyncio
async def test_no_ack_when_postgres_write_fails(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SyntheticIngressAdapter(session_factory)

    async def _boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated_pg_failure")

    monkeypatch.setattr(ingress_repo, "insert_if_absent", _boom)
    with pytest.raises(IngressPersistError) as exc:
        await adapter.accept(_event(external_event_id="evt-no-ack"))
    message = str(exc.value)
    assert "INGRESS_PERSIST_FAILED" in message
    assert "://" not in message
    assert "password" not in message.lower()

    async with session_factory() as session:
        async with session.begin():
            assert (
                await session.scalar(select(func.count()).select_from(IngressEvent))
                == 0
            )


@pytest.mark.asyncio
async def test_ingress_secrets_and_text_stay_out_of_repr(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    secret_text = f"private-client-text-{uuid4()}"
    adapter = SyntheticIngressAdapter(session_factory)
    ack = await adapter.accept(
        _event(external_event_id="evt-redact", text=secret_text)
    )
    worker = IngressWorker(session_factory, worker_id="worker-redact")
    claim = await worker.claim_one()
    assert claim is not None
    assert secret_text not in repr(claim)
    assert secret_text not in repr(ack)
    assert "envelope=<redacted>" in repr(claim)

    async with session_factory() as session:
        async with session.begin():
            row = await session.get(IngressEvent, ack.event_id)
            assert row is not None
            assert secret_text not in repr(row)
            assert secret_text in row.envelope_json["text"]


@pytest.mark.asyncio
async def test_ingress_processing_has_no_outbound_side_effects(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = SyntheticIngressAdapter(session_factory)
    await adapter.accept(_event(external_event_id="evt-no-out"))
    worker = IngressWorker(session_factory, worker_id="worker-no-out")
    claim = await worker.claim_one()
    assert claim is not None
    await worker.process_claimed(claim)
    assert (
        is_automatic_outbound_allowed(Settings(), OutboundAction.SEND_MESSAGE) is False
    )
