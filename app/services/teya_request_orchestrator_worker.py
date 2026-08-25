"""Teya BookingRequest ingest worker with durable feed cursor."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.booking_request_remote import BookingRequestFeedCursor
from app.core.teya_request_types import (
    TeyaRequestOrchestratorOutcome,
    TeyaRequestOrchestratorResult,
)
from app.db.clock import db_statement_now
from app.db.session import session_scope
from app.repositories import teya_request_feed_cursors as feed_cursor_repo
from app.repositories import teya_request_pendings as pending_repo
from app.services.teya_request_crm import TeyaRequestCrmService
from app.services.teya_request_orchestrator import TeyaRequestOrchestratorService
from app.services.teya_request_pending import TeyaRequestPendingService

logger = logging.getLogger(__name__)

_ALLOWED_LOG_CODES: frozenset[str] = frozenset(
    {
        "TEYA_ORCH_WORKER_FEED",
        "TEYA_ORCH_WORKER_CLAIMED",
        "TEYA_ORCH_WORKER_EMPTY",
        "TEYA_ORCH_WORKER_CURSOR",
    }
)


def _log(event: str) -> None:
    if type(event) is not str or event not in _ALLOWED_LOG_CODES:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return


class _FeedRemote(Protocol):
    def feed(
        self,
        *,
        limit: object = 20,
        cursor: BookingRequestFeedCursor | None = None,
    ): ...

    def get(self, *, request_id: object): ...

    def appointments_lookup(self, *, phone: object = None, client_id: object = None): ...

    def book(
        self,
        *,
        request_id: object,
        starts_at: object,
        idempotency_key: object,
        service_id: object = None,
    ): ...


class TeyaRequestOrchestratorWorker:
    """Drain teya_request_pendings via orchestrator state machine."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        remote: _FeedRemote | None,
        crm: TeyaRequestCrmService | None = None,
        feed_limit: int = 20,
        orchestrator_factory: Callable[..., TeyaRequestOrchestratorService]
        | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._remote = remote
        self._crm = crm
        self._orchestrator_factory = orchestrator_factory
        self._feed_limit = feed_limit

    async def ingest_feed(self) -> int:
        """Pull NEW BookingRequests using durable cursor; upsert pendings first."""

        if self._remote is None:
            return 0
        count = 0
        async with session_scope(self._session_factory) as session:
            await pending_repo.expire_exhausted_to_manual_review(
                session, now=await db_statement_now(session)
            )
            created_at, cursor_id = await feed_cursor_repo.get_cursor(session)
            cursor = None
            if created_at and cursor_id:
                cursor = BookingRequestFeedCursor(
                    created_at=created_at, id=cursor_id
                )
            page = self._remote.feed(limit=self._feed_limit, cursor=cursor)
            pending = TeyaRequestPendingService(session)
            last_item = None
            for item in page.items:
                await pending.upsert_discovered(request_id=item.request_id)
                last_item = item
                count += 1
            # Advance cursor only after durable upserts of this page.
            now = await db_statement_now(session)
            if page.next_cursor is not None:
                await feed_cursor_repo.save_cursor(
                    session,
                    created_at=page.next_cursor.created_at,
                    cursor_id=page.next_cursor.id,
                    now=now,
                )
                _log("TEYA_ORCH_WORKER_CURSOR")
            elif last_item is not None and last_item.created_at:
                await feed_cursor_repo.save_cursor(
                    session,
                    created_at=last_item.created_at,
                    cursor_id=str(last_item.request_id),
                    now=now,
                )
                _log("TEYA_ORCH_WORKER_CURSOR")
        if count:
            _log("TEYA_ORCH_WORKER_FEED")
        return count

    async def claim_one(self) -> uuid.UUID | None:
        async with session_scope(self._session_factory) as session:
            now = await db_statement_now(session)
            await pending_repo.expire_exhausted_to_manual_review(
                session, now=now
            )
            pending_id = await pending_repo.lock_next_claimable_id(
                session, now=now
            )
            if pending_id is None:
                _log("TEYA_ORCH_WORKER_EMPTY")
                return None
            _log("TEYA_ORCH_WORKER_CLAIMED")
            return pending_id

    async def process_one(
        self, pending_id: uuid.UUID
    ) -> TeyaRequestOrchestratorResult:
        if self._remote is None:
            return TeyaRequestOrchestratorResult(
                outcome=TeyaRequestOrchestratorOutcome.CLAIM_DENIED,
                pending_id=pending_id,
                result_code="REMOTE_UNBOUND",
            )
        async with session_scope(self._session_factory) as session:
            pending = TeyaRequestPendingService(session)
            row = await pending.claim_by_id(pending_id=pending_id)
            if row is None:
                return TeyaRequestOrchestratorResult(
                    outcome=TeyaRequestOrchestratorOutcome.CLAIM_DENIED,
                    pending_id=pending_id,
                    result_code="CLAIM_DENIED",
                )
            if self._orchestrator_factory is not None:
                orch = self._orchestrator_factory(
                    session,
                    pending_service=pending,
                    remote=self._remote,
                    crm=self._crm,
                )
            else:
                orch = TeyaRequestOrchestratorService(
                    session,
                    pending_service=pending,
                    remote=self._remote,
                    crm=self._crm,
                )
            return await orch.process_claimed(row)
