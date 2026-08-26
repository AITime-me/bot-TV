"""PostgreSQL proofs for A2.2 booking-method analytics durable sync."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_analytics_fields import (
    AmoCrmAnalyticsApplyDecision,
    AmoCrmAnalyticsBookingMethodEnum,
    AmoCrmAnalyticsFieldId,
)
from app.core.booking_method_http import BookingMethodHttpError
from app.core.booking_method_remote import (
    BookingMethodContextDto,
    BookingMethodFeedCursor,
    BookingMethodFeedItem,
    BookingMethodFeedPage,
)
from app.core.booking_method_types import (
    FEED_CURSOR_ID,
    BookingMethodAnalyticsOutcome,
    BookingMethodCreatorKind,
    BookingMethodPendingState,
)
from app.db.clock import db_statement_now
from app.models.booking_method_analytics_pending import BookingMethodAnalyticsPending
from app.models.teya_request_feed_cursor import TeyaRequestFeedCursor
from app.repositories import booking_method_analytics_pendings as pending_repo
from app.repositories import teya_request_feed_cursors as feed_cursor_repo
from app.services.booking_method_analytics_worker import BookingMethodAnalyticsWorker
from app.services.teya_request_crm import TeyaCrmActionOutcome, TeyaCrmActionResult
from tests.pg_harness import truncate_foundation_tables

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
_PHONE = "+79001234567"


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


def _item(
    appointment_id: str,
    *,
    creator_kind: BookingMethodCreatorKind = BookingMethodCreatorKind.SELF_SERVICE,
    created_at: str = "2026-08-26T10:00:00.000Z",
) -> BookingMethodFeedItem:
    return BookingMethodFeedItem(
        appointment_id=appointment_id,
        creator_kind=creator_kind,
        created_at=created_at,
    )


@dataclass
class _FeedRemote:
    pages: list[BookingMethodFeedPage]
    contexts: dict[str, BookingMethodContextDto] = field(default_factory=dict)
    feed_error: str | None = None
    context_errors: dict[str, str] = field(default_factory=dict)
    default_context_error: str | None = None
    calls: list[BookingMethodFeedCursor | None] = field(default_factory=list)
    context_calls: list[str] = field(default_factory=list)

    def feed(
        self,
        *,
        limit: object = 20,
        cursor: BookingMethodFeedCursor | None = None,
    ) -> BookingMethodFeedPage:
        self.calls.append(cursor)
        if self.feed_error is not None:
            raise BookingMethodHttpError(self.feed_error)
        if not self.pages:
            return BookingMethodFeedPage(items=(), next_cursor=None)
        return self.pages.pop(0)

    def context(self, *, appointment_id: object) -> BookingMethodContextDto:
        aid = str(appointment_id)
        self.context_calls.append(aid)
        if aid in self.context_errors:
            raise BookingMethodHttpError(self.context_errors[aid])
        if self.default_context_error is not None and aid not in self.contexts:
            raise BookingMethodHttpError(self.default_context_error)
        if aid not in self.contexts:
            raise AssertionError(f"missing context for {aid}")
        return self.contexts[aid]


@dataclass
class _FakeCrm:
    discover: TeyaCrmActionResult
    apply: TeyaCrmActionResult
    discover_calls: list[str] = field(default_factory=list)
    apply_calls: list[dict] = field(default_factory=list)
    create_calls: list = field(default_factory=list)

    async def discover_existing_business_deal(self, *, phone_e164: str):
        self.discover_calls.append(phone_e164)
        return self.discover

    async def apply_lead_analytics_enum_if_empty(self, **kwargs):
        self.apply_calls.append(kwargs)
        return self.apply

    async def ensure_contact_and_deal(self, **_kwargs):
        self.create_calls.append(_kwargs)
        raise AssertionError("A2.2 must not create deals")


def _worker(
    session_factory,
    remote,
    crm,
) -> BookingMethodAnalyticsWorker:
    return BookingMethodAnalyticsWorker(
        session_factory,
        remote=remote,
        crm=crm,
        clock=lambda: _NOW,
    )


@pytest.mark.asyncio
async def test_pg_upsert_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aid = uuid.uuid4()
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            first = await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                appointment_id=aid,
                creator_kind=BookingMethodCreatorKind.SELF_SERVICE,
                now=now,
            )
            second = await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                appointment_id=aid,
                creator_kind=BookingMethodCreatorKind.MANAGER,
                now=now,
            )
            assert first.id == second.id
            assert first.creator_kind == "SELF_SERVICE"
            rows = (
                await session.scalars(select(BookingMethodAnalyticsPending))
            ).all()
            assert len(rows) == 1


@pytest.mark.asyncio
async def test_pg_cursor_admit_before_advance_replay(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = [str(uuid.uuid4()) for _ in range(2)]
    page = BookingMethodFeedPage(
        items=(
            _item(ids[0], created_at="2026-08-26T10:00:00.000Z"),
            _item(ids[1], created_at="2026-08-26T10:01:00.000Z"),
        ),
        next_cursor=BookingMethodFeedCursor(
            created_at="2026-08-26T10:01:00.000Z", id=ids[1]
        ),
    )
    remote = _FeedRemote([page, BookingMethodFeedPage(items=(), next_cursor=None)])
    worker = _worker(session_factory, remote, crm=None)
    n1 = await worker.ingest_feed()
    assert n1 == 2
    async with session_factory() as session:
        async with session.begin():
            created_at, cursor_id = await feed_cursor_repo.get_cursor(
                session, cursor_id=FEED_CURSOR_ID
            )
            assert created_at == "2026-08-26T10:01:00.000Z"
            assert cursor_id == ids[1]
            # Teya default cursor untouched.
            teya_at, teya_id = await feed_cursor_repo.get_cursor(session)
            assert teya_at is None and teya_id is None
    n2 = await worker.ingest_feed()
    assert n2 == 0
    assert remote.calls[1] is not None
    assert remote.calls[1].id == ids[1]


@pytest.mark.asyncio
async def test_pg_process_self_service_writes_851489(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _assert_kind_enum(
        session_factory,
        BookingMethodCreatorKind.SELF_SERVICE,
        int(AmoCrmAnalyticsBookingMethodEnum.SELF_SERVICE),
    )


@pytest.mark.asyncio
async def test_pg_process_manager_writes_851493(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _assert_kind_enum(
        session_factory,
        BookingMethodCreatorKind.MANAGER,
        int(AmoCrmAnalyticsBookingMethodEnum.MANAGER),
    )


@pytest.mark.asyncio
async def test_pg_process_master_writes_851495(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _assert_kind_enum(
        session_factory,
        BookingMethodCreatorKind.MASTER,
        int(AmoCrmAnalyticsBookingMethodEnum.MASTER),
    )


async def _assert_kind_enum(
    session_factory: async_sessionmaker[AsyncSession],
    kind: BookingMethodCreatorKind,
    enum_id: int,
) -> None:
    aid = uuid.uuid4()
    remote = _FeedRemote(
        [],
        contexts={
            str(aid): BookingMethodContextDto(
                appointment_id=str(aid),
                creator_kind=kind,
                phone_e164=_PHONE,
            )
        },
    )
    crm = _FakeCrm(
        discover=TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY,
            contact_id="11",
            deal_id="55",
        ),
        apply=TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY,
            deal_id="55",
            analytics_decision=AmoCrmAnalyticsApplyDecision.APPLIED.value,
            error_code="ANALYTICS_APPLIED",
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                appointment_id=aid,
                creator_kind=kind,
                now=now,
            )
    worker = _worker(session_factory, remote, crm)
    pending_id = await worker.claim_one()
    assert pending_id is not None
    result = await worker.process_one(pending_id)
    assert result.outcome is BookingMethodAnalyticsOutcome.TERMINAL
    assert result.pending_state is BookingMethodPendingState.DONE
    assert result.result_code == "ANALYTICS_APPLIED"
    assert len(crm.apply_calls) == 1
    assert crm.apply_calls[0]["enum_id"] == enum_id
    assert crm.apply_calls[0]["field_id"] == int(
        AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD
    )
    assert crm.create_calls == []


@pytest.mark.asyncio
async def test_pg_no_deal_retries_without_create(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aid = uuid.uuid4()
    remote = _FeedRemote(
        [],
        contexts={
            str(aid): BookingMethodContextDto(
                appointment_id=str(aid),
                creator_kind=BookingMethodCreatorKind.SELF_SERVICE,
                phone_e164=_PHONE,
            )
        },
    )
    crm = _FakeCrm(
        discover=TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.NONE, error_code="DEAL_NONE"
        ),
        apply=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
    )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                appointment_id=aid,
                creator_kind=BookingMethodCreatorKind.SELF_SERVICE,
                now=now,
            )
    worker = _worker(session_factory, remote, crm)
    pending_id = await worker.claim_one()
    result = await worker.process_one(pending_id)  # type: ignore[arg-type]
    assert result.outcome is BookingMethodAnalyticsOutcome.RETRY_SCHEDULED
    assert result.result_code == "DEAL_NONE"
    assert crm.apply_calls == []
    assert crm.create_calls == []
    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.scalars(
                    select(BookingMethodAnalyticsPending).where(
                        BookingMethodAnalyticsPending.appointment_id == aid
                    )
                )
            ).one()
            assert row.state == BookingMethodPendingState.RESOLVING.value
            assert row.next_retry_at is not None
            assert row.lease_token is None


@pytest.mark.asyncio
async def test_pg_later_deal_then_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aid = uuid.uuid4()
    remote = _FeedRemote(
        [],
        contexts={
            str(aid): BookingMethodContextDto(
                appointment_id=str(aid),
                creator_kind=BookingMethodCreatorKind.MANAGER,
                phone_e164=_PHONE,
            )
        },
    )
    crm = _FakeCrm(
        discover=TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.NONE, error_code="DEAL_NONE"
        ),
        apply=TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY,
            deal_id="77",
            analytics_decision=AmoCrmAnalyticsApplyDecision.APPLIED.value,
            error_code="ANALYTICS_APPLIED",
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                appointment_id=aid,
                creator_kind=BookingMethodCreatorKind.MANAGER,
                now=now,
            )
    worker = _worker(session_factory, remote, crm)
    first_id = await worker.claim_one()
    first = await worker.process_one(first_id)  # type: ignore[arg-type]
    assert first.outcome is BookingMethodAnalyticsOutcome.RETRY_SCHEDULED

    crm.discover = TeyaCrmActionResult(
        outcome=TeyaCrmActionOutcome.READY, contact_id="11", deal_id="77"
    )
    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.scalars(
                    select(BookingMethodAnalyticsPending).where(
                        BookingMethodAnalyticsPending.appointment_id == aid
                    )
                )
            ).one()
            row.next_retry_at = None
            await session.flush()
    second_id = await worker.claim_one()
    second = await worker.process_one(second_id)  # type: ignore[arg-type]
    assert second.pending_state is BookingMethodPendingState.DONE
    assert second.result_code == "ANALYTICS_APPLIED"
    assert crm.apply_calls[0]["enum_id"] == int(
        AmoCrmAnalyticsBookingMethodEnum.MANAGER
    )


@pytest.mark.asyncio
async def test_pg_ambiguous_manual(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aid = uuid.uuid4()
    remote = _FeedRemote(
        [],
        contexts={
            str(aid): BookingMethodContextDto(
                appointment_id=str(aid),
                creator_kind=BookingMethodCreatorKind.SELF_SERVICE,
                phone_e164=_PHONE,
            )
        },
    )
    crm = _FakeCrm(
        discover=TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
            contact_id="11",
            error_code="ACTIVE_DEAL_AMBIGUOUS",
        ),
        apply=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
    )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                appointment_id=aid,
                creator_kind=BookingMethodCreatorKind.SELF_SERVICE,
                now=now,
            )
    worker = _worker(session_factory, remote, crm)
    pending_id = await worker.claim_one()
    result = await worker.process_one(pending_id)  # type: ignore[arg-type]
    assert result.pending_state is BookingMethodPendingState.MANUAL_REVIEW
    assert result.result_code == "ACTIVE_DEAL_AMBIGUOUS"
    assert crm.apply_calls == []


@pytest.mark.asyncio
async def test_pg_technical_deal_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aid = uuid.uuid4()
    remote = _FeedRemote(
        [],
        contexts={
            str(aid): BookingMethodContextDto(
                appointment_id=str(aid),
                creator_kind=BookingMethodCreatorKind.SELF_SERVICE,
                phone_e164=_PHONE,
            )
        },
    )
    crm = _FakeCrm(
        discover=TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY, contact_id="11", deal_id="99"
        ),
        apply=TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.FAIL_CLOSED,
            deal_id="99",
            analytics_decision=AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW.value,
            error_code="ANALYTICS_TECHNICAL_DEAL_FORBIDDEN",
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                appointment_id=aid,
                creator_kind=BookingMethodCreatorKind.SELF_SERVICE,
                now=now,
            )
    worker = _worker(session_factory, remote, crm)
    pending_id = await worker.claim_one()
    result = await worker.process_one(pending_id)  # type: ignore[arg-type]
    assert result.pending_state is BookingMethodPendingState.MANUAL_REVIEW
    assert result.result_code == "ANALYTICS_TECHNICAL_DEAL_FORBIDDEN"


@pytest.mark.asyncio
async def test_pg_crm_down_retries(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aid = uuid.uuid4()
    remote = _FeedRemote(
        [],
        contexts={
            str(aid): BookingMethodContextDto(
                appointment_id=str(aid),
                creator_kind=BookingMethodCreatorKind.SELF_SERVICE,
                phone_e164=_PHONE,
            )
        },
    )
    crm = _FakeCrm(
        discover=TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.RETRY, error_code="IDENTITY_TRANSIENT"
        ),
        apply=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
    )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                appointment_id=aid,
                creator_kind=BookingMethodCreatorKind.SELF_SERVICE,
                now=now,
            )
    worker = _worker(session_factory, remote, crm)
    pending_id = await worker.claim_one()
    result = await worker.process_one(pending_id)  # type: ignore[arg-type]
    assert result.outcome is BookingMethodAnalyticsOutcome.RETRY_SCHEDULED
    assert result.result_code == "IDENTITY_TRANSIENT"


@pytest.mark.asyncio
async def test_pg_conflict_no_overwrite(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aid = uuid.uuid4()
    remote = _FeedRemote(
        [],
        contexts={
            str(aid): BookingMethodContextDto(
                appointment_id=str(aid),
                creator_kind=BookingMethodCreatorKind.SELF_SERVICE,
                phone_e164=_PHONE,
            )
        },
    )
    crm = _FakeCrm(
        discover=TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY, contact_id="11", deal_id="55"
        ),
        apply=TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY,
            deal_id="55",
            analytics_decision=AmoCrmAnalyticsApplyDecision.CONFLICT_NONEMPTY.value,
            error_code="ANALYTICS_CONFLICT_NONEMPTY",
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                appointment_id=aid,
                creator_kind=BookingMethodCreatorKind.SELF_SERVICE,
                now=now,
            )
    worker = _worker(session_factory, remote, crm)
    pending_id = await worker.claim_one()
    result = await worker.process_one(pending_id)  # type: ignore[arg-type]
    assert result.pending_state is BookingMethodPendingState.DONE
    assert result.result_code == "ANALYTICS_CONFLICT"


@pytest.mark.asyncio
async def test_pg_feed_404_safe(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remote = _FeedRemote([], feed_error="FEED_UNAVAILABLE")
    worker = _worker(session_factory, remote, crm=None)
    n = await worker.ingest_feed()
    assert n == 0
    async with session_factory() as session:
        async with session.begin():
            rows = (
                await session.scalars(select(BookingMethodAnalyticsPending))
            ).all()
            assert rows == []
            cursor = await session.get(TeyaRequestFeedCursor, FEED_CURSOR_ID)
            assert cursor is None


async def _seed_pending(
    session_factory: async_sessionmaker[AsyncSession],
    aid: uuid.UUID,
    *,
    kind: BookingMethodCreatorKind = BookingMethodCreatorKind.SELF_SERVICE,
    max_attempts: int = 8,
    attempt_count: int = 0,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            row = await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                appointment_id=aid,
                creator_kind=kind,
                now=now,
                max_attempts=max_attempts,
            )
            if attempt_count:
                row.attempt_count = attempt_count
                await session.flush()


@pytest.mark.asyncio
async def test_pg_context_rate_limited_retries(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aid = uuid.uuid4()
    remote = _FeedRemote([], context_errors={str(aid): "RATE_LIMITED"})
    crm = _FakeCrm(
        discover=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
        apply=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
    )
    await _seed_pending(session_factory, aid)
    worker = _worker(session_factory, remote, crm)
    pending_id = await worker.claim_one()
    result = await worker.process_one(pending_id)  # type: ignore[arg-type]
    assert result.outcome is BookingMethodAnalyticsOutcome.RETRY_SCHEDULED
    assert result.result_code == "RATE_LIMITED"
    assert result.pending_state is not BookingMethodPendingState.SKIPPED
    assert crm.apply_calls == []


@pytest.mark.asyncio
async def test_pg_context_5xx_and_transport_retry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for code in ("INTERNAL_ERROR", "TIMEOUT", "TRANSPORT_ERROR", "RESPONSE_INVALID"):
        aid = uuid.uuid4()
        remote = _FeedRemote([], context_errors={str(aid): code})
        crm = _FakeCrm(
            discover=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
            apply=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
        )
        pending_id = uuid.uuid4()
        async with session_factory() as session:
            async with session.begin():
                now = await db_statement_now(session)
                await pending_repo.upsert_discovered(
                    session,
                    row_id=pending_id,
                    appointment_id=aid,
                    creator_kind=BookingMethodCreatorKind.SELF_SERVICE,
                    now=now,
                )
        worker = _worker(session_factory, remote, crm)
        result = await worker.process_one(pending_id)
        assert result.outcome is BookingMethodAnalyticsOutcome.RETRY_SCHEDULED
        assert result.result_code == code
        async with session_factory() as session:
            async with session.begin():
                row = (
                    await session.scalars(
                        select(BookingMethodAnalyticsPending).where(
                            BookingMethodAnalyticsPending.id == pending_id
                        )
                    )
                ).one()
                assert row.state != BookingMethodPendingState.SKIPPED.value


@pytest.mark.asyncio
async def test_pg_context_auth_unavailable_retries(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aid = uuid.uuid4()
    remote = _FeedRemote([], context_errors={str(aid): "AUTH_UNAVAILABLE"})
    crm = _FakeCrm(
        discover=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
        apply=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
    )
    await _seed_pending(session_factory, aid)
    worker = _worker(session_factory, remote, crm)
    pending_id = await worker.claim_one()
    result = await worker.process_one(pending_id)  # type: ignore[arg-type]
    assert result.outcome is BookingMethodAnalyticsOutcome.RETRY_SCHEDULED
    assert result.result_code == "AUTH_UNAVAILABLE"


@pytest.mark.asyncio
async def test_pg_retry_exhaustion_manual_not_skipped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aid = uuid.uuid4()
    remote = _FeedRemote([], context_errors={str(aid): "RATE_LIMITED"})
    crm = _FakeCrm(
        discover=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
        apply=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
    )
    await _seed_pending(session_factory, aid, max_attempts=1)
    worker = _worker(session_factory, remote, crm)
    pending_id = await worker.claim_one()
    result = await worker.process_one(pending_id)  # type: ignore[arg-type]
    assert result.outcome is BookingMethodAnalyticsOutcome.TERMINAL
    assert result.pending_state is BookingMethodPendingState.MANUAL_REVIEW
    assert result.result_code == "MAX_ATTEMPTS_EXCEEDED"
    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.scalars(
                    select(BookingMethodAnalyticsPending).where(
                        BookingMethodAnalyticsPending.appointment_id == aid
                    )
                )
            ).one()
            assert row.state == BookingMethodPendingState.MANUAL_REVIEW.value
            assert row.state != BookingMethodPendingState.SKIPPED.value


@pytest.mark.asyncio
async def test_pg_crm_unbound_recoverable_then_succeeds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aid = uuid.uuid4()
    remote = _FeedRemote(
        [],
        contexts={
            str(aid): BookingMethodContextDto(
                appointment_id=str(aid),
                creator_kind=BookingMethodCreatorKind.SELF_SERVICE,
                phone_e164=_PHONE,
            )
        },
    )
    await _seed_pending(session_factory, aid)
    unbound = _worker(session_factory, remote, crm=None)
    assert await unbound.claim_one() is None
    denied = await unbound.process_one(uuid.uuid4())
    assert denied.outcome is BookingMethodAnalyticsOutcome.CLAIM_DENIED
    assert denied.result_code == "CRM_UNBOUND"
    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.scalars(
                    select(BookingMethodAnalyticsPending).where(
                        BookingMethodAnalyticsPending.appointment_id == aid
                    )
                )
            ).one()
            assert row.state == BookingMethodPendingState.DISCOVERED.value
            assert row.attempt_count == 0

    crm = _FakeCrm(
        discover=TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY, contact_id="11", deal_id="55"
        ),
        apply=TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY,
            deal_id="55",
            analytics_decision=AmoCrmAnalyticsApplyDecision.APPLIED.value,
            error_code="ANALYTICS_APPLIED",
        ),
    )
    bound = _worker(session_factory, remote, crm)
    pending_id = await bound.claim_one()
    assert pending_id is not None
    result = await bound.process_one(pending_id)
    assert result.pending_state is BookingMethodPendingState.DONE
    assert result.result_code == "ANALYTICS_APPLIED"
    assert len(crm.apply_calls) == 1
    assert crm.create_calls == []


@pytest.mark.asyncio
async def test_pg_context_not_found_skipped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aid = uuid.uuid4()
    remote = _FeedRemote([], context_errors={str(aid): "NOT_FOUND"})
    crm = _FakeCrm(
        discover=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
        apply=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
    )
    await _seed_pending(session_factory, aid)
    worker = _worker(session_factory, remote, crm)
    pending_id = await worker.claim_one()
    result = await worker.process_one(pending_id)  # type: ignore[arg-type]
    assert result.outcome is BookingMethodAnalyticsOutcome.TERMINAL
    assert result.pending_state is BookingMethodPendingState.SKIPPED
    assert result.result_code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_pg_context_unavailable_retries(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aid = uuid.uuid4()
    remote = _FeedRemote([], context_errors={str(aid): "CONTEXT_UNAVAILABLE"})
    crm = _FakeCrm(
        discover=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
        apply=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
    )
    await _seed_pending(session_factory, aid)
    worker = _worker(session_factory, remote, crm)
    pending_id = await worker.claim_one()
    result = await worker.process_one(pending_id)  # type: ignore[arg-type]
    assert result.outcome is BookingMethodAnalyticsOutcome.RETRY_SCHEDULED
    assert result.result_code == "CONTEXT_UNAVAILABLE"
