"""A2.3b2 acquisition-source analytics worker (poll feed → discover deal → apply enum).

Never creates deals. No phone in durable pending.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.acquisition_source_http import AcquisitionSourceHttpError
from app.core.acquisition_source_remote import AcquisitionSourceFeedCursor
from app.core.acquisition_source_types import (
    DEFAULT_MAX_ATTEMPTS,
    EXECUTION_LEASE_SECONDS,
    FEED_CURSOR_ID,
    AcquisitionSourceAnalyticsOutcome,
    AcquisitionSourceAnalyticsResult,
    AcquisitionSourceOwnerKind,
    AcquisitionSourcePendingState,
    enum_id_for_source_key,
)
from app.core.amocrm_analytics_fields import (
    AmoCrmAnalyticsApplyDecision,
    AmoCrmAnalyticsFieldId,
)
from app.core.teya_request_retry import (
    compute_next_retry_delay_seconds,
    is_retryable_crm_error,
    load_teya_retry_policy,
)
from app.db.clock import db_statement_now
from app.db.session import session_scope
from app.models.acquisition_source_analytics_pending import (
    AcquisitionSourceAnalyticsPending,
)
from app.repositories import acquisition_source_analytics_pendings as pending_repo
from app.repositories import teya_request_feed_cursors as feed_cursor_repo
from app.services.teya_request_crm import (
    TeyaCrmActionOutcome,
    TeyaCrmActionResult,
    TeyaRequestCrmService,
)

logger = logging.getLogger(__name__)

_ALLOWED_LOG_CODES: frozenset[str] = frozenset(
    {
        "ACQUISITION_SOURCE_WORKER_FEED",
        "ACQUISITION_SOURCE_WORKER_CLAIMED",
        "ACQUISITION_SOURCE_WORKER_EMPTY",
        "ACQUISITION_SOURCE_WORKER_CURSOR",
        "ACQUISITION_SOURCE_FEED_UNAVAILABLE",
        "ACQUISITION_SOURCE_CRM_UNBOUND",
    }
)

_RETRYABLE_CONTEXT_CODES: frozenset[str] = frozenset(
    {
        "RATE_LIMITED",
        "TIMEOUT",
        "TRANSPORT_ERROR",
        "INTERNAL_ERROR",
        "RESPONSE_TOO_LARGE",
        "RESPONSE_INVALID",
        "AUTH_UNAVAILABLE",
        "UNAUTHORIZED",
        "CONTEXT_UNAVAILABLE",
        "FEED_UNAVAILABLE",
        "REMOTE_REJECTED",
        "CRM_UNBOUND",
    }
)

_PERMANENT_SKIP_CODES: frozenset[str] = frozenset(
    {
        "NOT_FOUND",
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
        cursor: AcquisitionSourceFeedCursor | None = None,
    ): ...

    def context(
        self,
        *,
        evidence_id: object,
        owner_kind: object,
        owner_id: object,
    ): ...


class AcquisitionSourceAnalyticsWorker:
    """Ingest acquisition-source feed and apply Source Primary if empty."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        remote: _FeedRemote | None,
        crm: TeyaRequestCrmService | None = None,
        feed_limit: int = 20,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._remote = remote
        self._crm = crm
        self._feed_limit = feed_limit
        self._clock = clock
        self._retry_policy = load_teya_retry_policy()

    async def ingest_feed(self) -> int:
        if self._remote is None:
            return 0
        count = 0
        async with session_scope(self._session_factory) as session:
            await pending_repo.expire_exhausted_to_terminal(
                session, now=await db_statement_now(session)
            )
            created_at, cursor_id = await feed_cursor_repo.get_cursor(
                session, cursor_id=FEED_CURSOR_ID
            )
            cursor = None
            if created_at and cursor_id:
                cursor = AcquisitionSourceFeedCursor(
                    feed_order=created_at, evidence_id=cursor_id
                )
            try:
                page = self._remote.feed(limit=self._feed_limit, cursor=cursor)
            except AcquisitionSourceHttpError as exc:
                if exc.code == "FEED_UNAVAILABLE":
                    _log("ACQUISITION_SOURCE_FEED_UNAVAILABLE")
                    return 0
                raise
            last_item = None
            for item in page.items:
                eid = uuid.UUID(item.evidence_id)
                oid = uuid.UUID(item.owner_id)
                now = await db_statement_now(session)
                try:
                    await pending_repo.upsert_discovered(
                        session,
                        row_id=uuid.uuid4(),
                        evidence_id=eid,
                        owner_kind=item.owner_kind,
                        owner_id=oid,
                        source_key=item.source_key,
                        now=now,
                        max_attempts=DEFAULT_MAX_ATTEMPTS,
                    )
                except IntegrityError:
                    raced = await pending_repo.get_by_evidence_id(
                        session, evidence_id=eid
                    )
                    if raced is None:
                        raise
                last_item = item
                count += 1
            now = await db_statement_now(session)
            if page.next_cursor is not None:
                await feed_cursor_repo.save_cursor(
                    session,
                    created_at=page.next_cursor.feed_order,
                    cursor_id=page.next_cursor.evidence_id,
                    now=now,
                    feed_cursor_id=FEED_CURSOR_ID,
                )
                _log("ACQUISITION_SOURCE_WORKER_CURSOR")
            elif last_item is not None:
                await feed_cursor_repo.save_cursor(
                    session,
                    created_at=last_item.feed_order,
                    cursor_id=last_item.evidence_id,
                    now=now,
                    feed_cursor_id=FEED_CURSOR_ID,
                )
                _log("ACQUISITION_SOURCE_WORKER_CURSOR")
        if count:
            _log("ACQUISITION_SOURCE_WORKER_FEED")
        return count

    async def claim_one(self) -> uuid.UUID | None:
        if self._remote is None or self._crm is None:
            _log("ACQUISITION_SOURCE_CRM_UNBOUND")
            return None
        async with session_scope(self._session_factory) as session:
            now = await db_statement_now(session)
            await pending_repo.expire_exhausted_to_terminal(session, now=now)
            pending_id = await pending_repo.lock_next_claimable_id(
                session, now=now
            )
            if pending_id is None:
                _log("ACQUISITION_SOURCE_WORKER_EMPTY")
                return None
            _log("ACQUISITION_SOURCE_WORKER_CLAIMED")
            return pending_id

    async def process_one(
        self, pending_id: uuid.UUID
    ) -> AcquisitionSourceAnalyticsResult:
        if self._remote is None:
            return AcquisitionSourceAnalyticsResult(
                outcome=AcquisitionSourceAnalyticsOutcome.CLAIM_DENIED,
                pending_id=pending_id,
                result_code="REMOTE_UNBOUND",
            )
        if self._crm is None:
            return AcquisitionSourceAnalyticsResult(
                outcome=AcquisitionSourceAnalyticsOutcome.CLAIM_DENIED,
                pending_id=pending_id,
                result_code="CRM_UNBOUND",
            )
        async with session_scope(self._session_factory) as session:
            now = (
                self._clock()
                if self._clock is not None
                else await db_statement_now(session)
            )
            row = await pending_repo.get_by_id(session, pending_id=pending_id)
            if row is None:
                return AcquisitionSourceAnalyticsResult(
                    outcome=AcquisitionSourceAnalyticsOutcome.CLAIM_DENIED,
                    pending_id=pending_id,
                    result_code="CLAIM_DENIED",
                )
            lease_token = uuid.uuid4()
            lease_expires = now + timedelta(seconds=EXECUTION_LEASE_SECONDS)
            claimed = await pending_repo.claim_lease(
                session,
                row=row,
                lease_token=lease_token,
                lease_expires_at=lease_expires,
                now=now,
            )
            if not claimed:
                return AcquisitionSourceAnalyticsResult(
                    outcome=AcquisitionSourceAnalyticsOutcome.CLAIM_DENIED,
                    pending_id=pending_id,
                    result_code="CLAIM_DENIED",
                )
            await session.refresh(row)
            return await self._process_claimed(session, row, lease_token)

    def _context_matches_pending(
        self,
        row: AcquisitionSourceAnalyticsPending,
        ctx: object,
    ) -> bool:
        evidence_id = getattr(ctx, "evidence_id", None)
        owner_kind = getattr(ctx, "owner_kind", None)
        owner_id = getattr(ctx, "owner_id", None)
        source_key = getattr(ctx, "source_key", None)
        if evidence_id != str(row.evidence_id):
            return False
        if owner_kind is None or owner_kind.value != row.owner_kind:
            return False
        if owner_id != str(row.owner_id):
            return False
        if source_key != row.source_key:
            return False
        return True

    async def _process_claimed(
        self,
        session: AsyncSession,
        row: AcquisitionSourceAnalyticsPending,
        lease_token: uuid.UUID,
    ) -> AcquisitionSourceAnalyticsResult:
        now = (
            self._clock()
            if self._clock is not None
            else await db_statement_now(session)
        )
        if self._crm is None:
            return await self._retry(
                session,
                row,
                lease_token,
                now=now,
                result_code="CRM_UNBOUND",
            )

        try:
            ctx = self._remote.context(  # type: ignore[union-attr]
                evidence_id=str(row.evidence_id),
                owner_kind=row.owner_kind,
                owner_id=str(row.owner_id),
            )
        except AcquisitionSourceHttpError as exc:
            return await self._map_context_http_error(
                session, row, lease_token, now=now, code=exc.code
            )

        if not self._context_matches_pending(row, ctx):
            return await self._terminal(
                session,
                row,
                lease_token,
                now=now,
                state=AcquisitionSourcePendingState.MANUAL_REVIEW,
                result_code="CONTEXT_MISMATCH",
                manual_review_reason="CONTEXT_MISMATCH",
            )

        await pending_repo.advance_state(
            session,
            row=row,
            lease_token=lease_token,
            state=AcquisitionSourcePendingState.RESOLVING,
            now=now,
        )

        discovery = await self._crm.discover_existing_business_deal(
            phone_e164=ctx.phone_e164
        )
        if discovery.outcome is TeyaCrmActionOutcome.NONE or discovery.error_code in {
            "DEAL_NONE",
            "CONTACT_NONE",
        }:
            return await self._retry(
                session,
                row,
                lease_token,
                now=now,
                result_code=discovery.error_code or "DEAL_NONE",
            )
        if discovery.outcome is TeyaCrmActionOutcome.RETRY or is_retryable_crm_error(
            discovery.error_code
        ):
            return await self._retry(
                session,
                row,
                lease_token,
                now=now,
                result_code=discovery.error_code or "DEAL_TRANSIENT",
            )
        if (
            discovery.outcome is TeyaCrmActionOutcome.MANUAL_REVIEW
            or discovery.error_code == "ACTIVE_DEAL_AMBIGUOUS"
        ):
            return await self._terminal(
                session,
                row,
                lease_token,
                now=now,
                state=AcquisitionSourcePendingState.MANUAL_REVIEW,
                result_code=discovery.error_code or "ACTIVE_DEAL_AMBIGUOUS",
                contact_id=discovery.contact_id,
                deal_id=discovery.deal_id,
                manual_review_reason=discovery.error_code
                or "ACTIVE_DEAL_AMBIGUOUS",
            )
        if discovery.outcome is not TeyaCrmActionOutcome.READY or not discovery.deal_id:
            return await self._terminal(
                session,
                row,
                lease_token,
                now=now,
                state=AcquisitionSourcePendingState.MANUAL_REVIEW,
                result_code=discovery.error_code or "DEAL_DISCOVERY_FAILED",
                contact_id=discovery.contact_id,
                deal_id=discovery.deal_id,
                manual_review_reason=discovery.error_code
                or "DEAL_DISCOVERY_FAILED",
            )

        await pending_repo.advance_state(
            session,
            row=row,
            lease_token=lease_token,
            state=AcquisitionSourcePendingState.APPLYING,
            now=now,
            amocrm_contact_id=discovery.contact_id,
            amocrm_deal_id=discovery.deal_id,
        )

        try:
            enum_id = enum_id_for_source_key(ctx.source_key)
        except ValueError:
            return await self._terminal(
                session,
                row,
                lease_token,
                now=now,
                state=AcquisitionSourcePendingState.MANUAL_REVIEW,
                result_code="SOURCE_KEY_INVALID",
                manual_review_reason="SOURCE_KEY_INVALID",
            )

        applied = await self._crm.apply_lead_analytics_enum_if_empty(
            deal_id=discovery.deal_id,
            field_id=int(AmoCrmAnalyticsFieldId.SOURCE_PRIMARY),
            enum_id=enum_id,
        )
        return await self._map_analytics_result(
            session,
            row,
            lease_token,
            now=now,
            applied=applied,
            contact_id=discovery.contact_id,
            deal_id=discovery.deal_id,
        )

    async def _map_analytics_result(
        self,
        session: AsyncSession,
        row: AcquisitionSourceAnalyticsPending,
        lease_token: uuid.UUID,
        *,
        now: datetime,
        applied: TeyaCrmActionResult,
        contact_id: str | None,
        deal_id: str | None,
    ) -> AcquisitionSourceAnalyticsResult:
        decision = applied.analytics_decision
        if applied.outcome is TeyaCrmActionOutcome.RETRY or decision in {
            AmoCrmAnalyticsApplyDecision.TRANSIENT_RETRY.value,
        }:
            return await self._retry(
                session,
                row,
                lease_token,
                now=now,
                result_code=applied.error_code or "AMOCRM_ANALYTICS_PATCH_TRANSIENT",
            )
        if (
            applied.outcome is TeyaCrmActionOutcome.FAIL_CLOSED
            or applied.error_code == "ANALYTICS_TECHNICAL_DEAL_FORBIDDEN"
            or applied.error_code == "AMOCRM_ANALYTICS_TECHNICAL_DEAL_FORBIDDEN"
        ):
            return await self._terminal(
                session,
                row,
                lease_token,
                now=now,
                state=AcquisitionSourcePendingState.MANUAL_REVIEW,
                result_code=applied.error_code or "ANALYTICS_TECHNICAL_DEAL_FORBIDDEN",
                contact_id=contact_id,
                deal_id=deal_id,
                manual_review_reason=applied.error_code
                or "ANALYTICS_TECHNICAL_DEAL_FORBIDDEN",
            )
        if (
            applied.outcome is TeyaCrmActionOutcome.MANUAL_REVIEW
            or decision == AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW.value
        ):
            return await self._terminal(
                session,
                row,
                lease_token,
                now=now,
                state=AcquisitionSourcePendingState.MANUAL_REVIEW,
                result_code=applied.error_code or "AMOCRM_ANALYTICS_MANUAL",
                contact_id=contact_id,
                deal_id=deal_id,
                manual_review_reason=applied.error_code
                or "AMOCRM_ANALYTICS_MANUAL",
            )
        if decision == AmoCrmAnalyticsApplyDecision.CONFLICT_NONEMPTY.value:
            return await self._terminal(
                session,
                row,
                lease_token,
                now=now,
                state=AcquisitionSourcePendingState.DONE,
                result_code="ANALYTICS_CONFLICT",
                contact_id=contact_id,
                deal_id=deal_id,
                result_outcome="CONFLICT",
            )
        if decision in {
            AmoCrmAnalyticsApplyDecision.APPLIED.value,
            AmoCrmAnalyticsApplyDecision.ALREADY_SAME.value,
            None,
        } or applied.outcome is TeyaCrmActionOutcome.READY:
            code = "ANALYTICS_APPLIED"
            if decision == AmoCrmAnalyticsApplyDecision.ALREADY_SAME.value:
                code = "ANALYTICS_ALREADY_SAME"
            elif decision == AmoCrmAnalyticsApplyDecision.SKIPPED_NO_EVIDENCE.value:
                code = applied.error_code or "ANALYTICS_SKIPPED_NO_EVIDENCE"
            return await self._terminal(
                session,
                row,
                lease_token,
                now=now,
                state=AcquisitionSourcePendingState.DONE,
                result_code=code,
                contact_id=contact_id,
                deal_id=deal_id,
            )
        return await self._terminal(
            session,
            row,
            lease_token,
            now=now,
            state=AcquisitionSourcePendingState.MANUAL_REVIEW,
            result_code=applied.error_code or "ANALYTICS_UNEXPECTED",
            contact_id=contact_id,
            deal_id=deal_id,
            manual_review_reason=applied.error_code or "ANALYTICS_UNEXPECTED",
        )

    async def _map_context_http_error(
        self,
        session: AsyncSession,
        row: AcquisitionSourceAnalyticsPending,
        lease_token: uuid.UUID,
        *,
        now: datetime,
        code: str,
    ) -> AcquisitionSourceAnalyticsResult:
        if code in _PERMANENT_SKIP_CODES:
            return await self._terminal(
                session,
                row,
                lease_token,
                now=now,
                state=AcquisitionSourcePendingState.SKIPPED,
                result_code=code,
            )
        if code in _RETRYABLE_CONTEXT_CODES:
            return await self._retry(
                session,
                row,
                lease_token,
                now=now,
                result_code=code,
            )
        return await self._retry(
            session,
            row,
            lease_token,
            now=now,
            result_code=code or "REMOTE_REJECTED",
        )

    async def _retry(
        self,
        session: AsyncSession,
        row: AcquisitionSourceAnalyticsPending,
        lease_token: uuid.UUID,
        *,
        now: datetime,
        result_code: str,
    ) -> AcquisitionSourceAnalyticsResult:
        if row.attempt_count >= row.max_attempts:
            return await self._terminal(
                session,
                row,
                lease_token,
                now=now,
                state=AcquisitionSourcePendingState.MANUAL_REVIEW,
                result_code="MAX_ATTEMPTS_EXCEEDED",
                manual_review_reason=result_code or "MAX_ATTEMPTS_EXCEEDED",
            )
        delay = compute_next_retry_delay_seconds(
            attempt_count=row.attempt_count,
            policy=self._retry_policy,
        )
        next_retry = now + timedelta(seconds=delay)
        await pending_repo.release_lease(
            session,
            row=row,
            lease_token=lease_token,
            now=now,
            next_retry_at=next_retry,
            result_code=result_code,
        )
        return AcquisitionSourceAnalyticsResult(
            outcome=AcquisitionSourceAnalyticsOutcome.RETRY_SCHEDULED,
            pending_id=row.id,
            pending_state=AcquisitionSourcePendingState(row.state)
            if row.state
            in {s.value for s in AcquisitionSourcePendingState}
            else AcquisitionSourcePendingState.DISCOVERED,
            result_code=result_code,
        )

    async def _terminal(
        self,
        session: AsyncSession,
        row: AcquisitionSourceAnalyticsPending,
        lease_token: uuid.UUID,
        *,
        now: datetime,
        state: AcquisitionSourcePendingState,
        result_code: str,
        contact_id: str | None = None,
        deal_id: str | None = None,
        result_outcome: str | None = None,
        manual_review_reason: str | None = None,
    ) -> AcquisitionSourceAnalyticsResult:
        await pending_repo.advance_state(
            session,
            row=row,
            lease_token=lease_token,
            state=state,
            now=now,
            result_code=result_code,
            result_outcome=result_outcome or state.value,
            manual_review_reason=manual_review_reason,
            amocrm_contact_id=contact_id,
            amocrm_deal_id=deal_id,
            clear_lease=True,
        )
        return AcquisitionSourceAnalyticsResult(
            outcome=AcquisitionSourceAnalyticsOutcome.TERMINAL,
            pending_id=row.id,
            pending_state=state,
            result_code=result_code,
        )
