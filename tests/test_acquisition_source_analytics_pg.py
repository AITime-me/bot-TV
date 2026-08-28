"""PostgreSQL proofs for A2.3b2 acquisition-source analytics durable sync."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.acquisition_source_http import AcquisitionSourceHttpError
from app.core.acquisition_source_remote import (
    AcquisitionSourceContextDto,
    AcquisitionSourceFeedCursor,
    AcquisitionSourceFeedItem,
    AcquisitionSourceFeedPage,
)
from app.core.acquisition_source_types import (
    FEED_CURSOR_ID,
    AcquisitionSourceAnalyticsOutcome,
    AcquisitionSourceOwnerKind,
    AcquisitionSourcePendingState,
)
from app.core.amocrm_analytics_fields import (
    AmoCrmAnalyticsApplyDecision,
    AmoCrmAnalyticsFieldId,
    AmoCrmAnalyticsSourcePrimaryEnum,
)
from app.core.booking_method_types import FEED_CURSOR_ID as BOOKING_METHOD_CURSOR
from app.db.clock import db_statement_now
from app.models.acquisition_source_analytics_pending import (
    AcquisitionSourceAnalyticsPending,
)
from app.repositories import acquisition_source_analytics_pendings as pending_repo
from app.repositories import teya_request_feed_cursors as feed_cursor_repo
from app.services.acquisition_source_analytics_worker import (
    AcquisitionSourceAnalyticsWorker,
)
from app.services.teya_request_crm import TeyaCrmActionOutcome, TeyaCrmActionResult
from tests.pg_harness import truncate_foundation_tables

_NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
_PHONE = "+79001234567"
_EID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_OID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


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
    evidence_id: str = _EID,
    *,
    owner_kind: AcquisitionSourceOwnerKind = AcquisitionSourceOwnerKind.APPOINTMENT,
    owner_id: str = _OID,
    source_key: str = "VK_ADS",
    consumed_at: str = "2026-08-28T10:00:00.000Z",
    feed_order: str = "1",
) -> AcquisitionSourceFeedItem:
    return AcquisitionSourceFeedItem(
        evidence_id=evidence_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        source_key=source_key,
        consumed_at=consumed_at,
        feed_order=feed_order,
    )


@dataclass
class _FeedRemote:
    pages: list[AcquisitionSourceFeedPage]
    contexts: dict[str, AcquisitionSourceContextDto] = field(default_factory=dict)
    context_errors: dict[str, str] = field(default_factory=dict)
    calls: list[AcquisitionSourceFeedCursor | None] = field(default_factory=list)

    def feed(
        self,
        *,
        limit: object = 20,
        cursor: AcquisitionSourceFeedCursor | None = None,
    ) -> AcquisitionSourceFeedPage:
        self.calls.append(cursor)
        if not self.pages:
            return AcquisitionSourceFeedPage(items=(), next_cursor=None)
        return self.pages.pop(0)

    def context(
        self,
        *,
        evidence_id: object,
        owner_kind: object,
        owner_id: object,
    ) -> AcquisitionSourceContextDto:
        eid = str(evidence_id)
        if eid in self.context_errors:
            raise AcquisitionSourceHttpError(self.context_errors[eid])
        if eid not in self.contexts:
            raise AcquisitionSourceHttpError("NOT_FOUND")
        return self.contexts[eid]


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
        raise AssertionError("A2.3b2 must not create deals")


def _worker(session_factory, remote, crm) -> AcquisitionSourceAnalyticsWorker:
    return AcquisitionSourceAnalyticsWorker(
        session_factory,
        remote=remote,
        crm=crm,
        clock=lambda: _NOW,
    )


@pytest.mark.asyncio
async def test_pg_upsert_idempotent_by_evidence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    eid = uuid.UUID(_EID)
    oid = uuid.UUID(_OID)
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            first = await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                evidence_id=eid,
                owner_kind=AcquisitionSourceOwnerKind.APPOINTMENT,
                owner_id=oid,
                source_key="VK_ADS",
                now=now,
            )
            second = await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                evidence_id=eid,
                owner_kind=AcquisitionSourceOwnerKind.BOOKING_REQUEST,
                owner_id=oid,
                source_key="YANDEX",
                now=now,
            )
            assert first.id == second.id
            assert first.source_key == "VK_ADS"
            rows = (
                await session.scalars(select(AcquisitionSourceAnalyticsPending))
            ).all()
            assert len(rows) == 1


@pytest.mark.asyncio
async def test_pg_cursor_admit_before_advance_isolated(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    page = AcquisitionSourceFeedPage(
        items=(
            _item(ids[0], consumed_at="2026-08-28T10:00:00.000Z", feed_order="10"),
            _item(ids[1], consumed_at="2026-08-28T10:01:00.000Z", feed_order="11"),
        ),
        next_cursor=AcquisitionSourceFeedCursor(
            feed_order="11", evidence_id=ids[1]
        ),
    )
    remote = _FeedRemote([page, AcquisitionSourceFeedPage(items=(), next_cursor=None)])
    worker = _worker(session_factory, remote, crm=None)
    assert await worker.ingest_feed() == 2
    async with session_factory() as session:
        async with session.begin():
            created_at, cursor_id = await feed_cursor_repo.get_cursor(
                session, cursor_id=FEED_CURSOR_ID
            )
            assert created_at == "2026-08-28T10:01:00.000Z"
            assert cursor_id == ids[1]
            bm_at, bm_id = await feed_cursor_repo.get_cursor(
                session, cursor_id=BOOKING_METHOD_CURSOR
            )
            assert bm_at is None and bm_id is None


@pytest.mark.asyncio
async def test_pg_applies_source_primary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    eid = uuid.UUID(_EID)
    oid = uuid.UUID(_OID)
    remote = _FeedRemote(
        [],
        contexts={
            _EID: AcquisitionSourceContextDto(
                evidence_id=_EID,
                owner_kind=AcquisitionSourceOwnerKind.APPOINTMENT,
                owner_id=_OID,
                source_key="YANDEX",
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
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                evidence_id=eid,
                owner_kind=AcquisitionSourceOwnerKind.APPOINTMENT,
                owner_id=oid,
                source_key="YANDEX",
                now=now,
            )
    worker = _worker(session_factory, remote, crm)
    pending_id = await worker.claim_one()
    assert pending_id is not None
    result = await worker.process_one(pending_id)
    assert result.outcome is AcquisitionSourceAnalyticsOutcome.TERMINAL
    assert result.pending_state is AcquisitionSourcePendingState.DONE
    assert crm.apply_calls[0]["field_id"] == int(AmoCrmAnalyticsFieldId.SOURCE_PRIMARY)
    assert crm.apply_calls[0]["enum_id"] == int(AmoCrmAnalyticsSourcePrimaryEnum.YANDEX)
    assert crm.create_calls == []


@pytest.mark.asyncio
async def test_pg_context_mismatch_manual_review(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    eid = uuid.UUID(_EID)
    oid = uuid.UUID(_OID)
    remote = _FeedRemote(
        [],
        contexts={
            _EID: AcquisitionSourceContextDto(
                evidence_id=_EID,
                owner_kind=AcquisitionSourceOwnerKind.APPOINTMENT,
                owner_id=_OID,
                source_key="VK_CONTENT",
                phone_e164=_PHONE,
            )
        },
    )
    crm = _FakeCrm(
        discover=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY, deal_id="55"),
        apply=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
    )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                evidence_id=eid,
                owner_kind=AcquisitionSourceOwnerKind.APPOINTMENT,
                owner_id=oid,
                source_key="VK_ADS",
                now=now,
            )
    worker = _worker(session_factory, remote, crm)
    pending_id = await worker.claim_one()
    assert pending_id is not None
    result = await worker.process_one(pending_id)
    assert result.pending_state is AcquisitionSourcePendingState.MANUAL_REVIEW
    assert result.result_code == "CONTEXT_MISMATCH"
    assert crm.apply_calls == []


@pytest.mark.asyncio
async def test_pg_not_found_skipped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    eid = uuid.UUID(_EID)
    oid = uuid.UUID(_OID)
    remote = _FeedRemote([], context_errors={_EID: "NOT_FOUND"})
    crm = _FakeCrm(
        discover=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY, deal_id="55"),
        apply=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
    )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                evidence_id=eid,
                owner_kind=AcquisitionSourceOwnerKind.APPOINTMENT,
                owner_id=oid,
                source_key="TWO_GIS",
                now=now,
            )
    worker = _worker(session_factory, remote, crm)
    pending_id = await worker.claim_one()
    assert pending_id is not None
    result = await worker.process_one(pending_id)
    assert result.pending_state is AcquisitionSourcePendingState.SKIPPED
    assert result.result_code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_pg_no_deal_retries_without_create(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    eid = uuid.UUID(_EID)
    oid = uuid.UUID(_OID)
    remote = _FeedRemote(
        [],
        contexts={
            _EID: AcquisitionSourceContextDto(
                evidence_id=_EID,
                owner_kind=AcquisitionSourceOwnerKind.APPOINTMENT,
                owner_id=_OID,
                source_key="VK_ADS",
                phone_e164=_PHONE,
            )
        },
    )
    crm = _FakeCrm(
        discover=TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.NONE, error_code="CONTACT_NONE"
        ),
        apply=TeyaCrmActionResult(outcome=TeyaCrmActionOutcome.READY),
    )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                evidence_id=eid,
                owner_kind=AcquisitionSourceOwnerKind.APPOINTMENT,
                owner_id=oid,
                source_key="VK_ADS",
                now=now,
            )
    worker = _worker(session_factory, remote, crm)
    pending_id = await worker.claim_one()
    assert pending_id is not None
    result = await worker.process_one(pending_id)
    assert result.outcome is AcquisitionSourceAnalyticsOutcome.RETRY_SCHEDULED
    assert crm.create_calls == []
    assert crm.apply_calls == []


@pytest.mark.asyncio
async def test_pg_conflict_preserves_existing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    eid = uuid.UUID(_EID)
    oid = uuid.UUID(_OID)
    remote = _FeedRemote(
        [],
        contexts={
            _EID: AcquisitionSourceContextDto(
                evidence_id=_EID,
                owner_kind=AcquisitionSourceOwnerKind.APPOINTMENT,
                owner_id=_OID,
                source_key="VK_ADS",
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
            analytics_decision=AmoCrmAnalyticsApplyDecision.CONFLICT_NONEMPTY.value,
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            now = await db_statement_now(session)
            await pending_repo.upsert_discovered(
                session,
                row_id=uuid.uuid4(),
                evidence_id=eid,
                owner_kind=AcquisitionSourceOwnerKind.APPOINTMENT,
                owner_id=oid,
                source_key="VK_ADS",
                now=now,
            )
    worker = _worker(session_factory, remote, crm)
    pending_id = await worker.claim_one()
    assert pending_id is not None
    result = await worker.process_one(pending_id)
    assert result.pending_state is AcquisitionSourcePendingState.DONE
    assert result.result_code == "ANALYTICS_CONFLICT"
