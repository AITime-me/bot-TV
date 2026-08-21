"""Self-booking CREATE execution worker (SELF-BOOKING-COMMAND-03L).

Discovers claimable READY/expired-EXECUTING pendings, then runs
SelfBookingCreateExecutionService.execute (claim → fence → PII → CREATE).

Never mints idempotency keys. Never invokes CREATE from inbound.
No reply-plan / CONFIRM schema / PII admission contract changes.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.self_booking_create_types import SelfBookingCreateExecutionResult
from app.db.clock import db_statement_now
from app.db.session import session_scope
from app.repositories import self_booking_create_pendings as pending_repo
from app.services.booking_flow import BookingFlowService
from app.services.client_ref_resolution import ClientRefResolverService
from app.services.self_booking_create_execution import (
    SelfBookingCreateExecutionService,
    SelfBookingPiiStore,
)
from app.services.self_booking_create_pending import SelfBookingCreatePendingService

logger = logging.getLogger(__name__)

_ALLOWED_LOG_CODES: frozenset[str] = frozenset(
    {
        "SELF_BOOKING_EXEC_WORKER_SKIPPED_NO_PII_STORE",
        "SELF_BOOKING_EXEC_WORKER_CLAIMED",
        "SELF_BOOKING_EXEC_WORKER_EMPTY",
    }
)


def _log(event: str) -> None:
    if type(event) is not str or event not in _ALLOWED_LOG_CODES:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return


class SelfBookingCreateExecutionWorker:
    """Drain self-booking create pendings via existing execution service."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        booking_flow: BookingFlowService,
        pii_store: SelfBookingPiiStore | None,
    ) -> None:
        self._session_factory = session_factory
        self._booking_flow = booking_flow
        self._pii_store = pii_store

    async def claim_one(self) -> uuid.UUID | None:
        """Return one claimable pending id, or None when idle / PII unavailable."""

        if self._pii_store is None:
            _log("SELF_BOOKING_EXEC_WORKER_SKIPPED_NO_PII_STORE")
            return None
        async with session_scope(self._session_factory) as session:
            now = await db_statement_now(session)
            pending_id = await pending_repo.lock_next_claimable_id(
                session, now=now
            )
            if pending_id is None:
                _log("SELF_BOOKING_EXEC_WORKER_EMPTY")
                return None
            _log("SELF_BOOKING_EXEC_WORKER_CLAIMED")
            return pending_id

    async def process_one(
        self, pending_id: uuid.UUID
    ) -> SelfBookingCreateExecutionResult:
        """Run claim → fence → CREATE for a previously discovered pending id."""

        if self._pii_store is None:
            raise RuntimeError("SELF_BOOKING_EXEC_PII_STORE_REQUIRED") from None
        async with session_scope(self._session_factory) as session:
            pending_service = SelfBookingCreatePendingService(session)
            execution = SelfBookingCreateExecutionService(
                session,
                pending_service=pending_service,
                booking_flow=self._booking_flow,
                client_ref_resolver=ClientRefResolverService(session),
                pii_store=self._pii_store,
            )
            return await execution.execute(pending_id=pending_id)
