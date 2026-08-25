"""Teya request pending foundation — upsert / claim / lease only.

Never calls BookingRequest book HTTP, CRM writes, or OutboundArbiter.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.teya_request_types import (
    DEFAULT_MAX_ATTEMPTS,
    EXECUTION_LEASE_SECONDS,
    TeyaRequestPendingState,
)
from app.db.clock import db_statement_now
from app.models.teya_request_pending import TeyaRequestPending
from app.repositories import teya_request_pendings as pending_repo

logger = logging.getLogger(__name__)

_ALLOWED_LOG_CODES: frozenset[str] = frozenset(
    {
        "TEYA_REQUEST_UPSERTED",
        "TEYA_REQUEST_DUPLICATE",
        "TEYA_REQUEST_CLAIMED",
        "TEYA_REQUEST_CLAIM_DENIED",
    }
)


def _log(event: str) -> None:
    if type(event) is not str or event not in _ALLOWED_LOG_CODES:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return


def _as_uuid(value: object) -> uuid.UUID:
    if type(value) is uuid.UUID:
        return value
    if isinstance(value, uuid.UUID):
        return uuid.UUID(str(value))
    return uuid.UUID(str(value))


class TeyaRequestPendingService:
    """Durable foundation boundary for BookingRequest workflow rows."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._clock = clock

    async def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        return await db_statement_now(self._session)

    async def upsert_discovered(
        self,
        *,
        request_id: object,
        max_attempts: object = DEFAULT_MAX_ATTEMPTS,
    ) -> TeyaRequestPending:
        rid = _as_uuid(request_id)
        attempts = (
            max_attempts
            if type(max_attempts) is int and not isinstance(max_attempts, bool)
            else DEFAULT_MAX_ATTEMPTS
        )
        if attempts < 1:
            attempts = DEFAULT_MAX_ATTEMPTS
        now = await self._now()
        try:
            row = await pending_repo.upsert_discovered(
                self._session,
                row_id=uuid.uuid4(),
                request_id=rid,
                now=now,
                max_attempts=attempts,
            )
        except IntegrityError:
            raced = await pending_repo.get_by_request_id(
                self._session, request_id=rid
            )
            if raced is None:
                raise
            _log("TEYA_REQUEST_DUPLICATE")
            return raced
        if row.request_id == rid and row.state == TeyaRequestPendingState.DISCOVERED.value:
            _log("TEYA_REQUEST_UPSERTED")
        else:
            _log("TEYA_REQUEST_DUPLICATE")
        return row

    async def claim_one(
        self, *, lease_token: object | None = None
    ) -> TeyaRequestPending | None:
        """Claim next claimable pending (SKIP LOCKED). Returns refreshed row."""

        try:
            token = (
                uuid.UUID(str(lease_token))
                if lease_token is not None
                else uuid.uuid4()
            )
        except (ValueError, TypeError, AttributeError):
            _log("TEYA_REQUEST_CLAIM_DENIED")
            return None

        now = await self._now()
        pending_id = await pending_repo.lock_next_claimable_id(
            self._session, now=now
        )
        if pending_id is None:
            _log("TEYA_REQUEST_CLAIM_DENIED")
            return None
        row = await pending_repo.get_by_id(self._session, pending_id=pending_id)
        if row is None:
            _log("TEYA_REQUEST_CLAIM_DENIED")
            return None
        ok = await pending_repo.claim_lease(
            self._session,
            row=row,
            lease_token=token,
            lease_expires_at=now + timedelta(seconds=EXECUTION_LEASE_SECONDS),
            now=now,
        )
        if not ok:
            _log("TEYA_REQUEST_CLAIM_DENIED")
            return None
        refreshed = await pending_repo.get_by_id(
            self._session, pending_id=pending_id
        )
        if refreshed is None:
            _log("TEYA_REQUEST_CLAIM_DENIED")
            return None
        _log("TEYA_REQUEST_CLAIMED")
        return refreshed

    async def claim_by_id(
        self,
        *,
        pending_id: object,
        lease_token: object | None = None,
    ) -> TeyaRequestPending | None:
        try:
            pid = _as_uuid(pending_id)
            token = (
                uuid.UUID(str(lease_token))
                if lease_token is not None
                else uuid.uuid4()
            )
        except (ValueError, TypeError, AttributeError):
            _log("TEYA_REQUEST_CLAIM_DENIED")
            return None
        row = await pending_repo.get_by_id(self._session, pending_id=pid)
        if row is None:
            _log("TEYA_REQUEST_CLAIM_DENIED")
            return None
        now = await self._now()
        ok = await pending_repo.claim_lease(
            self._session,
            row=row,
            lease_token=token,
            lease_expires_at=now + timedelta(seconds=EXECUTION_LEASE_SECONDS),
            now=now,
        )
        if not ok:
            _log("TEYA_REQUEST_CLAIM_DENIED")
            return None
        refreshed = await pending_repo.get_by_id(self._session, pending_id=pid)
        if refreshed is None:
            _log("TEYA_REQUEST_CLAIM_DENIED")
            return None
        _log("TEYA_REQUEST_CLAIMED")
        return refreshed
