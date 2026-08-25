"""Teya BookingRequest orchestrator worker loop.

Discovers claimable pendings and runs TeyaRequestOrchestratorService.
Never mixes into ReplyPlan/inbound. Never sends client outbound messages.
"""

from __future__ import annotations

import logging
import uuid
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.booking_request_http import BookingRequestHttpClient
from app.core.teya_request_types import (
    TeyaRequestOrchestratorOutcome,
    TeyaRequestOrchestratorResult,
)
from app.db.clock import db_statement_now
from app.db.session import session_scope
from app.repositories import teya_request_pendings as pending_repo
from app.services.teya_request_crm import TeyaRequestCrmService
from app.services.teya_request_orchestrator import TeyaRequestOrchestratorService
from app.services.teya_request_pending import TeyaRequestPendingService

logger = logging.getLogger(__name__)

_ALLOWED_LOG_CODES: frozenset[str] = frozenset(
    {
        "TEYA_ORCH_WORKER_EMPTY",
        "TEYA_ORCH_WORKER_CLAIMED",
        "TEYA_ORCH_WORKER_FEED",
    }
)


def _log(event: str) -> None:
    if type(event) is not str or event not in _ALLOWED_LOG_CODES:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return


class TeyaRequestOrchestratorWorker:
    """Drain teya_request_pendings via orchestrator state machine."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        remote: BookingRequestHttpClient | None,
        crm: TeyaRequestCrmService | None = None,
        orchestrator_factory: Callable[..., TeyaRequestOrchestratorService]
        | None = None,
        feed_limit: int = 20,
    ) -> None:
        self._session_factory = session_factory
        self._remote = remote
        self._crm = crm
        self._orchestrator_factory = orchestrator_factory
        self._feed_limit = feed_limit

    async def ingest_feed(self) -> int:
        """Pull NEW BookingRequests from online-zapis and upsert pendings."""

        if self._remote is None:
            return 0
        page = self._remote.feed(limit=self._feed_limit)
        count = 0
        async with session_scope(self._session_factory) as session:
            pending = TeyaRequestPendingService(session)
            for item in page.items:
                await pending.upsert_discovered(request_id=item.request_id)
                count += 1
        if count:
            _log("TEYA_ORCH_WORKER_FEED")
        return count

    async def claim_one(self) -> uuid.UUID | None:
        async with session_scope(self._session_factory) as session:
            now = await db_statement_now(session)
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
            pending_service = TeyaRequestPendingService(session)
            claimed = await pending_service.claim_by_id(pending_id=pending_id)
            if claimed is None:
                return TeyaRequestOrchestratorResult(
                    outcome=TeyaRequestOrchestratorOutcome.CLAIM_DENIED,
                    pending_id=pending_id,
                    result_code="CLAIM_DENIED",
                )
            if self._orchestrator_factory is not None:
                orch = self._orchestrator_factory(
                    session,
                    pending_service=pending_service,
                    remote=self._remote,
                    crm=self._crm,
                )
            else:
                orch = TeyaRequestOrchestratorService(
                    session,
                    pending_service=pending_service,
                    remote=self._remote,
                    crm=self._crm,
                )
            return await orch.process_claimed(claimed)
