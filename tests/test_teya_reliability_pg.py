"""PostgreSQL proofs for Teya reliability R1 (feed cursor, MANUAL_REVIEW, breaker)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_circuit_breaker import (
    CircuitBreakerPolicy,
    CircuitBreakerState,
)
from app.core.booking_request_remote import (
    BookingRequestFeedCursor,
    BookingRequestFeedPage,
    BotBookingRequestDto,
)
from app.core.teya_request_types import TeyaRequestPendingState
from app.db.clock import db_statement_now
from app.models.integration_circuit_breaker import IntegrationCircuitBreaker
from app.models.teya_request_feed_cursor import TeyaRequestFeedCursor
from app.models.teya_request_pending import TeyaRequestPending
from app.repositories import integration_circuit_breakers as breaker_repo
from app.repositories import teya_request_feed_cursors as feed_cursor_repo
from app.repositories import teya_request_pendings as pending_repo
from app.services.teya_request_orchestrator_worker import (
    TeyaRequestOrchestratorWorker,
)
from app.services.teya_request_pending import TeyaRequestPendingService
from app.services.teya_request_reconciliation_worker import (
    TeyaRequestReconciliationWorker,
)
from tests.pg_harness import truncate_foundation_tables

_NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


def _dto(
    request_id: str,
    *,
    created_at: str,
    status: str = "NEW",
    appointment_id: str | None = None,
) -> BotBookingRequestDto:
    return BotBookingRequestDto(
        request_id=request_id,
        status=status,
        request_type="MANAGER_REQUEST",
        created_at=created_at,
        appointment_id=appointment_id,
    )


class _FeedRemote:
    def __init__(self, pages: list[BookingRequestFeedPage]) -> None:
        self.pages = list(pages)
        self.calls: list[BookingRequestFeedCursor | None] = []
        self.gets: list[str] = []
        self._by_id: dict[str, BotBookingRequestDto] = {}
        for page in pages:
            for item in page.items:
                self._by_id[item.request_id] = item

    def feed(
        self,
        *,
        limit: object = 20,
        cursor: BookingRequestFeedCursor | None = None,
    ) -> BookingRequestFeedPage:
        self.calls.append(cursor)
        if not self.pages:
            return BookingRequestFeedPage(items=(), next_cursor=None)
        return self.pages.pop(0)

    def get(self, *, request_id: object) -> BotBookingRequestDto:
        rid = str(request_id)
        self.gets.append(rid)
        return self._by_id[rid]

    def appointments_lookup(self, **_kwargs: object):
        raise AssertionError("unused")

    def book(self, **_kwargs: object):
        raise AssertionError("unused")


@pytest.mark.asyncio
async def test_pg_max_attempts_to_manual_review(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rid = uuid.uuid4()
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                request_id=rid,
                now=now,
                max_attempts=2,
            )
            row = (
                await session.scalars(
                    select(TeyaRequestPending).where(
                        TeyaRequestPending.request_id == rid
                    )
                )
            ).one()
            row.attempt_count = 2
            row.state = TeyaRequestPendingState.IDENTITY.value
            await session.flush()
            n = await pending_repo.expire_exhausted_to_manual_review(
                session, now=now
            )
            assert n == 1
            await session.refresh(row)
            assert row.state == TeyaRequestPendingState.MANUAL_REVIEW.value
            assert row.manual_review_reason == "MAX_ATTEMPTS_EXCEEDED"
            assert row.next_retry_at is None
            assert row.lease_token is None


@pytest.mark.asyncio
async def test_pg_manual_review_remains_durable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rid = uuid.uuid4()
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session, row_id=uuid.uuid4(), request_id=rid, now=now
            )
            row = (
                await session.scalars(
                    select(TeyaRequestPending).where(
                        TeyaRequestPending.request_id == rid
                    )
                )
            ).one()
            await pending_repo.mark_manual_review(
                session, row=row, now=now, reason="IDENTITY_AMBIGUOUS"
            )
    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.scalars(
                    select(TeyaRequestPending).where(
                        TeyaRequestPending.request_id == rid
                    )
                )
            ).one()
            assert row.state == TeyaRequestPendingState.MANUAL_REVIEW.value
            claimable = await pending_repo.lock_next_claimable_id(
                session, now=await db_statement_now(session)
            )
            assert claimable is None


@pytest.mark.asyncio
async def test_pg_feed_multipage_no_starvation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = [str(uuid.uuid4()) for _ in range(5)]
    page1_items = tuple(
        _dto(ids[i], created_at=f"2026-08-25T10:0{i}:00.000Z")
        for i in range(3)
    )
    page2_items = tuple(
        _dto(ids[i], created_at=f"2026-08-25T10:0{i}:00.000Z")
        for i in range(3, 5)
    )
    remote = _FeedRemote(
        [
            BookingRequestFeedPage(
                items=page1_items,
                next_cursor=BookingRequestFeedCursor(
                    created_at=page1_items[-1].created_at or "",
                    id=page1_items[-1].request_id,
                ),
            ),
            BookingRequestFeedPage(items=page2_items, next_cursor=None),
        ]
    )
    worker = TeyaRequestOrchestratorWorker(
        session_factory, remote=remote, feed_limit=3
    )
    assert await worker.ingest_feed() == 3
    assert await worker.ingest_feed() == 2
    async with session_factory() as session:
        async with session.begin():
            rows = (
                await session.scalars(select(TeyaRequestPending))
            ).all()
            assert {str(r.request_id) for r in rows} == set(ids)
            created_at, cursor_id = await feed_cursor_repo.get_cursor(session)
            assert created_at == page2_items[-1].created_at
            assert cursor_id == page2_items[-1].request_id


@pytest.mark.asyncio
async def test_pg_feed_cursor_crash_replay_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rid = str(uuid.uuid4())
    item = _dto(rid, created_at="2026-08-25T10:00:00.000Z")
    page = BookingRequestFeedPage(
        items=(item,),
        next_cursor=BookingRequestFeedCursor(
            created_at=item.created_at or "", id=item.request_id
        ),
    )
    remote = _FeedRemote([page, page])
    worker = TeyaRequestOrchestratorWorker(
        session_factory, remote=remote, feed_limit=20
    )
    assert await worker.ingest_feed() == 1
    # Replay same page content (simulates crash before cursor / duplicate page).
    remote.pages = [
        BookingRequestFeedPage(
            items=(item,),
            next_cursor=BookingRequestFeedCursor(
                created_at=item.created_at or "", id=item.request_id
            ),
        )
    ]
    assert await worker.ingest_feed() == 1
    async with session_factory() as session:
        async with session.begin():
            count = len((await session.scalars(select(TeyaRequestPending))).all())
            assert count == 1


@pytest.mark.asyncio
async def test_pg_next_retry_retained_and_resume(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rid = uuid.uuid4()
    lease = uuid.uuid4()
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session, row_id=uuid.uuid4(), request_id=rid, now=now
            )
            row = (
                await session.scalars(
                    select(TeyaRequestPending).where(
                        TeyaRequestPending.request_id == rid
                    )
                )
            ).one()
            future = now + timedelta(hours=1)
            ok = await pending_repo.claim_lease(
                session,
                row=row,
                lease_token=lease,
                lease_expires_at=now + timedelta(seconds=30),
                now=now,
            )
            assert ok
            await session.refresh(row)
            await pending_repo.release_lease(
                session,
                row=row,
                lease_token=lease,
                now=now,
                next_retry_at=future,
                result_code="TIMEOUT",
            )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            assert (
                await pending_repo.lock_next_claimable_id(session, now=now)
                is None
            )
            row = (
                await session.scalars(
                    select(TeyaRequestPending).where(
                        TeyaRequestPending.request_id == rid
                    )
                )
            ).one()
            assert row.next_retry_at is not None
            row.next_retry_at = now - timedelta(seconds=1)
            await session.flush()
            claimed = await pending_repo.lock_next_claimable_id(
                session, now=now
            )
            assert claimed == row.id


@pytest.mark.asyncio
async def test_pg_lease_reclaim_after_expiry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rid = uuid.uuid4()
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session, row_id=uuid.uuid4(), request_id=rid, now=now
            )
            row = (
                await session.scalars(
                    select(TeyaRequestPending).where(
                        TeyaRequestPending.request_id == rid
                    )
                )
            ).one()
            await pending_repo.claim_lease(
                session,
                row=row,
                lease_token=uuid.uuid4(),
                lease_expires_at=now + timedelta(seconds=30),
                now=now,
            )
            await session.refresh(row)
            row.lease_expires_at = now - timedelta(seconds=1)
            await session.flush()
            claimed = await pending_repo.lock_next_claimable_id(
                session, now=now
            )
            assert claimed == row.id


@pytest.mark.asyncio
async def test_pg_reconciler_repairs_closed_booking(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rid = str(uuid.uuid4())
    apt = str(uuid.uuid4())
    dto = _dto(
        rid,
        created_at="2026-08-25T10:00:00.000Z",
        status="CLOSED",
        appointment_id=apt,
    )

    class _Remote:
        def get(self, *, request_id: object) -> BotBookingRequestDto:
            return dto

        def appointments_lookup(self, **_kwargs: object):
            raise AssertionError("unused")

    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                request_id=uuid.UUID(rid),
                now=now,
            )
            row = (
                await session.scalars(
                    select(TeyaRequestPending).where(
                        TeyaRequestPending.request_id == uuid.UUID(rid)
                    )
                )
            ).one()
            row.state = TeyaRequestPendingState.VERIFYING.value
            await session.flush()

    worker = TeyaRequestReconciliationWorker(
        session_factory, remote=_Remote()
    )
    assert await worker.tick() == 1
    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.scalars(
                    select(TeyaRequestPending).where(
                        TeyaRequestPending.request_id == uuid.UUID(rid)
                    )
                )
            ).one()
            assert row.state == TeyaRequestPendingState.DONE.value
            assert row.result_code == "BOOKED_ANALYTICS_SKIPPED"


@pytest.mark.asyncio
async def test_pg_reconciler_ambiguous_manual(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.core.booking_request_remote import (
        AppointmentsLookupOutcome,
        BookingRequestAppointmentsLookupResult,
    )

    rid = str(uuid.uuid4())
    dto = BotBookingRequestDto(
        request_id=rid,
        status="NEW",
        request_type="GAME_REQUEST",
        created_at="2026-08-25T10:00:00.000Z",
        phone_e164="+79001234567",
    )

    class _Remote:
        def get(self, *, request_id: object) -> BotBookingRequestDto:
            return dto

        def appointments_lookup(self, **_kwargs: object):
            return BookingRequestAppointmentsLookupResult(
                outcome=AppointmentsLookupOutcome.AMBIGUOUS
            )

    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                request_id=uuid.UUID(rid),
                now=now,
            )
            row = (
                await session.scalars(
                    select(TeyaRequestPending).where(
                        TeyaRequestPending.request_id == uuid.UUID(rid)
                    )
                )
            ).one()
            row.state = TeyaRequestPendingState.RECONCILIATION_REQUIRED.value
            await session.flush()

    worker = TeyaRequestReconciliationWorker(
        session_factory, remote=_Remote()
    )
    assert await worker.tick() == 1
    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.scalars(
                    select(TeyaRequestPending).where(
                        TeyaRequestPending.request_id == uuid.UUID(rid)
                    )
                )
            ).one()
            assert row.state == TeyaRequestPendingState.MANUAL_REVIEW.value
            assert row.manual_review_reason == "APPOINTMENTS_AMBIGUOUS"


@pytest.mark.asyncio
async def test_pg_breaker_survives_restart(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    policy = CircuitBreakerPolicy(failure_threshold=2, cooldown_seconds=60.0)
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await breaker_repo.record_failure(
                session, now=now, policy=policy
            )
            await breaker_repo.record_failure(
                session, now=now, policy=policy
            )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            snap = await breaker_repo.get_or_create(session, now=now)
            assert snap.state is CircuitBreakerState.OPEN
            row = await session.get(
                IntegrationCircuitBreaker, "amocrm_business_writes"
            )
            assert row is not None
            assert row.state == CircuitBreakerState.OPEN.value


@pytest.mark.asyncio
async def test_pg_half_open_single_probe_atomic(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    import asyncio

    from app.core.amocrm_circuit_breaker import ProbeClaimOutcome

    policy = CircuitBreakerPolicy(
        failure_threshold=1,
        cooldown_seconds=1.0,
        probe_lease_seconds=30.0,
    )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await breaker_repo.record_failure(
                session, now=now, policy=policy
            )
            # Move opened_at into the past so cooldown is elapsed.
            row = await session.get(
                IntegrationCircuitBreaker, "amocrm_business_writes"
            )
            assert row is not None
            row.opened_at = now - timedelta(seconds=5)
            await session.flush()

    results: list[object] = []

    async def _claim() -> None:
        async with session_factory() as session:
            async with session.begin():
                now = await db_statement_now(session)
                claim = await breaker_repo.try_claim_probe(
                    session, now=now, policy=policy
                )
                results.append(claim.outcome)

    await asyncio.gather(_claim(), _claim())
    assert results.count(ProbeClaimOutcome.ALLOWED) == 1
    assert results.count(ProbeClaimOutcome.DENIED_PROBE_BUSY) == 1


@pytest.mark.asyncio
async def test_pg_probe_lease_expiry_allows_reclaim(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.core.amocrm_circuit_breaker import ProbeClaimOutcome

    policy = CircuitBreakerPolicy(
        failure_threshold=1,
        cooldown_seconds=1.0,
        probe_lease_seconds=1.0,
    )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await breaker_repo.record_failure(
                session, now=now, policy=policy
            )
            row = await session.get(
                IntegrationCircuitBreaker, "amocrm_business_writes"
            )
            assert row is not None
            row.opened_at = now - timedelta(seconds=10)
            await session.flush()
            first = await breaker_repo.try_claim_probe(
                session, now=now, policy=policy
            )
            assert first.outcome is ProbeClaimOutcome.ALLOWED
            # Simulate crash: leave HALF_OPEN with expired lease.
            row = await session.get(
                IntegrationCircuitBreaker, "amocrm_business_writes"
            )
            assert row is not None
            row.opened_at = now - timedelta(seconds=5)
            await session.flush()
            second = await breaker_repo.try_claim_probe(
                session, now=now, policy=policy
            )
            assert second.outcome is ProbeClaimOutcome.ALLOWED


@pytest.mark.asyncio
async def test_pg_probe_success_closes_failure_reopens(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.core.amocrm_circuit_breaker import ProbeClaimOutcome

    policy = CircuitBreakerPolicy(
        failure_threshold=1,
        cooldown_seconds=1.0,
        probe_lease_seconds=30.0,
    )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await breaker_repo.record_failure(
                session, now=now, policy=policy
            )
            row = await session.get(
                IntegrationCircuitBreaker, "amocrm_business_writes"
            )
            assert row is not None
            row.opened_at = now - timedelta(seconds=5)
            await session.flush()
            claim = await breaker_repo.try_claim_probe(
                session, now=now, policy=policy
            )
            assert claim.outcome is ProbeClaimOutcome.ALLOWED
            snap = await breaker_repo.record_success(
                session, now=now, policy=policy
            )
            assert snap.state is CircuitBreakerState.CLOSED

            now2 = await db_statement_now(session)
            await breaker_repo.record_failure(
                session, now=now2, policy=policy
            )
            row = await session.get(
                IntegrationCircuitBreaker, "amocrm_business_writes"
            )
            assert row is not None
            row.opened_at = now2 - timedelta(seconds=5)
            await session.flush()
            claim2 = await breaker_repo.try_claim_probe(
                session, now=now2, policy=policy
            )
            assert claim2.outcome is ProbeClaimOutcome.ALLOWED
            snap2 = await breaker_repo.record_failure(
                session, now=now2, policy=policy
            )
            assert snap2.state is CircuitBreakerState.OPEN


@pytest.mark.asyncio
async def test_pg_reconciler_recovers_crm_when_not_claimable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.core.amocrm_identity_lookup import (
        AmoCrmIdentityLookupOutcome,
        AmoCrmIdentityLookupResult,
    )
    from app.core.amocrm_deal_discovery import (
        AmoCrmDealDiscoveryOutcome,
        AmoCrmDealDiscoveryResult,
    )
    from app.services.teya_request_crm import (
        TeyaCrmActionOutcome,
        TeyaCrmActionResult,
        TeyaRequestCrmService,
    )

    rid = str(uuid.uuid4())
    dto = _dto(rid, created_at="2026-08-25T10:00:00.000Z")
    dto = BotBookingRequestDto(
        request_id=rid,
        status="NEW",
        request_type="MANAGER_REQUEST",
        created_at="2026-08-25T10:00:00.000Z",
        phone_e164="+79001234567",
    )

    class _Remote:
        def get(self, *, request_id: object) -> BotBookingRequestDto:
            return dto

        def appointments_lookup(self, **_kwargs: object):
            raise AssertionError("unused")

    class _Crm:
        writes = 0

        async def reconcile_readonly(self, **_kwargs: object):
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.READY,
                contact_id="101",
                deal_id="201",
                note_id="9",
                task_id="77",
            )

    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                request_id=uuid.UUID(rid),
                now=now,
                max_attempts=2,
            )
            row = (
                await session.scalars(
                    select(TeyaRequestPending).where(
                        TeyaRequestPending.request_id == uuid.UUID(rid)
                    )
                )
            ).one()
            row.state = TeyaRequestPendingState.IDENTITY.value
            row.attempt_count = 2
            await session.flush()

    worker = TeyaRequestReconciliationWorker(
        session_factory, remote=_Remote(), crm=_Crm()  # type: ignore[arg-type]
    )
    assert await worker.tick() >= 1
    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.scalars(
                    select(TeyaRequestPending).where(
                        TeyaRequestPending.request_id == uuid.UUID(rid)
                    )
                )
            ).one()
            assert row.amocrm_contact_id == "101"
            assert row.amocrm_deal_id == "201"
            assert row.amocrm_task_id == "77"
            assert row.state == TeyaRequestPendingState.RECONCILED.value
            # Not claimable by attempt count before recovery; now recovered.
            assert row.next_retry_at is None


@pytest.mark.asyncio
async def test_pg_exhausted_partial_stays_manual_no_oscillation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.services.teya_request_crm import (
        TeyaCrmActionOutcome,
        TeyaCrmActionResult,
    )

    rid = str(uuid.uuid4())
    dto = BotBookingRequestDto(
        request_id=rid,
        status="NEW",
        request_type="MANAGER_REQUEST",
        created_at="2026-08-25T10:00:00.000Z",
        phone_e164="+79001234567",
    )

    class _Remote:
        def get(self, *, request_id: object) -> BotBookingRequestDto:
            return dto

        def appointments_lookup(self, **_kwargs: object):
            raise AssertionError("unused")

    class _Crm:
        writes = 0

        async def reconcile_readonly(self, **_kwargs: object):
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.READY,
                contact_id="101",
                deal_id="201",
                note_id=None,
                task_id=None,
            )

    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                request_id=uuid.UUID(rid),
                now=now,
                max_attempts=2,
            )
            row = (
                await session.scalars(
                    select(TeyaRequestPending).where(
                        TeyaRequestPending.request_id == uuid.UUID(rid)
                    )
                )
            ).one()
            row.state = TeyaRequestPendingState.MANUAL_REVIEW.value
            row.attempt_count = 2
            row.result_code = "MAX_ATTEMPTS_EXCEEDED"
            row.result_outcome = TeyaRequestPendingState.MANUAL_REVIEW.value
            row.manual_review_reason = "MAX_ATTEMPTS_EXCEEDED"
            await session.flush()

    worker = TeyaRequestReconciliationWorker(
        session_factory, remote=_Remote(), crm=_Crm()  # type: ignore[arg-type]
    )
    assert await worker.tick() >= 1

    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.scalars(
                    select(TeyaRequestPending).where(
                        TeyaRequestPending.request_id == uuid.UUID(rid)
                    )
                )
            ).one()
            assert row.state == TeyaRequestPendingState.MANUAL_REVIEW.value
            assert row.amocrm_contact_id == "101"
            assert row.amocrm_deal_id == "201"
            assert row.amocrm_task_id is None
            assert row.manual_review_reason == "RECON_CRM_PARTIAL"
            assert row.attempt_count == 2
            # expire must not flip state (already MANUAL).
            n = await pending_repo.expire_exhausted_to_manual_review(
                session, now=await db_statement_now(session)
            )
            assert n == 0
            row2 = (
                await session.scalars(
                    select(TeyaRequestPending).where(
                        TeyaRequestPending.request_id == uuid.UUID(rid)
                    )
                )
            ).one()
            assert row2.state == TeyaRequestPendingState.MANUAL_REVIEW.value
            # Still not claimable.
            claimable = await pending_repo.lock_next_claimable_id(
                session, now=await db_statement_now(session)
            )
            assert claimable is None


@pytest.mark.asyncio
async def test_pg_ops_snapshot_does_not_insert_breaker(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ops path uses breaker get (not get_or_create); no row inserted."""

    from sqlalchemy import func

    from app.models.integration_circuit_breaker import IntegrationCircuitBreaker

    async with session_factory() as session:
        async with session.begin():
            before = await session.scalar(
                select(func.count()).select_from(IntegrationCircuitBreaker)
            )
            assert before == 0
            snap = await breaker_repo.get(session)
            assert snap is None
            # Mirror ops semantic default without INSERT.
            assert snap is None
            after = await session.scalar(
                select(func.count()).select_from(IntegrationCircuitBreaker)
            )
            assert after == 0
            # Contrast: get_or_create would insert.
            now = await db_statement_now(session)
            created = await breaker_repo.get_or_create(session, now=now)
            assert created.state is CircuitBreakerState.CLOSED
            inserted = await session.scalar(
                select(func.count()).select_from(IntegrationCircuitBreaker)
            )
            assert inserted == 1
