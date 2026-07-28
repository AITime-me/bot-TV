from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.clock import resolve_moment
from app.models.reply_plan import (
    BOT_RESPONSE_DELAY_MS,
    TERMINAL_REPLY_PLAN_STATUSES,
    ReplyPlan,
    ReplyPlanStatus,
    ReplyPlanType,
    reply_plan_transition_allowed,
)

DEFAULT_LEASE_SECONDS = 30
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_DELAY_SECONDS = 1


class ReplyPlanStateError(RuntimeError):
    """Raised when a ReplyPlan status transition is not allowed."""


class StaleReplyPlanLeaseError(RuntimeError):
    """Raised when a worker uses an expired or superseded ReplyPlan lease."""


@dataclass(frozen=True, repr=False)
class ReplyPlanClaim:
    plan_id: uuid.UUID
    conversation_id: uuid.UUID
    context_version: int
    plan_type: str
    status: str
    not_before: datetime
    bot_response_delay_ms: int
    attempt_count: int
    lease_owner: str
    lease_token: uuid.UUID
    lease_version: int
    lease_until: datetime
    correlation_id: uuid.UUID
    payload_json: dict[str, Any]

    def __repr__(self) -> str:
        return (
            f"ReplyPlanClaim(plan_id={self.plan_id!r}, "
            f"conversation_id={self.conversation_id!r}, "
            f"context_version={self.context_version!r}, status={self.status!r}, "
            f"lease_version={self.lease_version!r}, payload=<redacted>)"
        )


def _row_to_claim(row: ReplyPlan) -> ReplyPlanClaim:
    if row.lease_token is None or row.lease_owner is None or row.lease_until is None:
        raise RuntimeError("REPLY_PLAN_LEASE_INCOMPLETE")
    return ReplyPlanClaim(
        plan_id=row.id,
        conversation_id=row.conversation_id,
        context_version=row.context_version,
        plan_type=row.plan_type,
        status=row.status,
        not_before=row.not_before,
        bot_response_delay_ms=row.bot_response_delay_ms,
        attempt_count=row.attempt_count,
        lease_owner=row.lease_owner,
        lease_token=row.lease_token,
        lease_version=row.lease_version,
        lease_until=row.lease_until,
        correlation_id=row.correlation_id,
        payload_json=dict(row.payload_json),
    )


async def get_by_id(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
) -> ReplyPlan | None:
    return await session.get(ReplyPlan, plan_id)


async def create_client_reply_plan(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    context_version: int,
    correlation_id: uuid.UUID,
    payload_json: dict[str, Any],
    delay_ms: int = BOT_RESPONSE_DELAY_MS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: datetime | None = None,
) -> ReplyPlan:
    """Create PENDING CLIENT_REPLY plan with persisted not_before.

    ``created_at`` is pinned to the same PostgreSQL instant used for
    ``not_before`` so the persisted delay is exactly ``delay_ms`` regardless of
    application-host clock skew or round-trip latency.
    """
    if delay_ms < 0:
        raise ValueError("delay_ms must be nonnegative")
    moment = await resolve_moment(session, now)
    plan = ReplyPlan(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        context_version=context_version,
        plan_type=ReplyPlanType.CLIENT_REPLY.value,
        status=ReplyPlanStatus.PENDING.value,
        not_before=moment + timedelta(milliseconds=delay_ms),
        bot_response_delay_ms=delay_ms,
        payload_json=payload_json,
        cancel_reason=None,
        lease_owner=None,
        lease_token=None,
        lease_version=0,
        lease_until=None,
        attempt_count=0,
        max_attempts=max_attempts,
        correlation_id=correlation_id,
        created_at=moment,
        updated_at=moment,
    )
    session.add(plan)
    await session.flush()
    return plan


async def supersede_open_plans(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    reason: str = "NEW_CLIENT_MESSAGE",
) -> int:
    """Mark all non-terminal plans SUPERSEDED. Returns affected row count."""
    terminal = tuple(status.value for status in TERMINAL_REPLY_PLAN_STATUSES)
    stmt = (
        update(ReplyPlan)
        .where(
            ReplyPlan.conversation_id == conversation_id,
            ReplyPlan.status.not_in(terminal),
        )
        .values(
            status=ReplyPlanStatus.SUPERSEDED.value,
            cancel_reason=reason[:64],
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            updated_at=func.now(),
        )
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def cancel_open_plans_for_takeover(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    reason: str = "MANAGER_TAKEOVER",
) -> int:
    terminal = tuple(status.value for status in TERMINAL_REPLY_PLAN_STATUSES)
    stmt = (
        update(ReplyPlan)
        .where(
            ReplyPlan.conversation_id == conversation_id,
            ReplyPlan.status.not_in(terminal),
        )
        .values(
            status=ReplyPlanStatus.CANCELLED.value,
            cancel_reason=reason[:64],
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            updated_at=func.now(),
        )
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def mark_ready_due_plans(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """PENDING → READY when not_before has elapsed (no process sleep)."""
    moment = await resolve_moment(session, now)
    stmt = (
        update(ReplyPlan)
        .where(
            ReplyPlan.status == ReplyPlanStatus.PENDING.value,
            ReplyPlan.not_before <= moment,
        )
        .values(
            status=ReplyPlanStatus.READY.value,
            updated_at=moment,
        )
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def claim_next(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: datetime | None = None,
) -> ReplyPlanClaim | None:
    """Claim one due ReplyPlan with FOR UPDATE SKIP LOCKED + fencing."""
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    moment = await resolve_moment(session, now)
    await mark_ready_due_plans(session, now=moment)
    lease_until = moment + timedelta(seconds=lease_seconds)
    lease_token = uuid.uuid4()
    stmt = text(
        """
        WITH candidate AS (
            SELECT id
            FROM reply_plans
            WHERE attempt_count < :max_attempts
              AND not_before <= :now
              AND (
                    status = 'READY'
                 OR (status = 'FAILED'
                     AND (lease_until IS NULL OR lease_until < :now))
                 OR (status = 'PROCESSING'
                     AND lease_until IS NOT NULL
                     AND lease_until < :now)
              )
            ORDER BY not_before ASC, created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE reply_plans AS p
        SET
            status = 'PROCESSING',
            lease_owner = :worker_id,
            lease_token = CAST(:lease_token AS uuid),
            lease_version = p.lease_version + 1,
            lease_until = :lease_until,
            attempt_count = p.attempt_count + 1,
            cancel_reason = NULL,
            updated_at = :now
        FROM candidate
        WHERE p.id = candidate.id
        RETURNING p.id
        """
    )
    plan_id = await session.scalar(
        stmt,
        {
            "max_attempts": max_attempts,
            "now": moment,
            "worker_id": worker_id,
            "lease_token": str(lease_token),
            "lease_until": lease_until,
        },
    )
    if plan_id is None:
        return None
    plan = await get_by_id(session, plan_id=plan_id)
    if plan is None:
        raise RuntimeError("REPLY_PLAN_CLAIM_LOOKUP_FAILED")
    return _row_to_claim(plan)


async def complete_dispatched_with_lease(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
) -> ReplyPlan:
    if not reply_plan_transition_allowed(
        ReplyPlanStatus.PROCESSING,
        ReplyPlanStatus.DISPATCHED,
    ):
        raise ReplyPlanStateError("REPLY_PLAN_TRANSITION_DENIED")
    stmt = (
        update(ReplyPlan)
        .where(
            ReplyPlan.id == plan_id,
            ReplyPlan.status == ReplyPlanStatus.PROCESSING.value,
            ReplyPlan.lease_token == lease_token,
            ReplyPlan.lease_version == lease_version,
        )
        .values(
            status=ReplyPlanStatus.DISPATCHED.value,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            updated_at=func.now(),
        )
        .returning(ReplyPlan.id)
    )
    updated = await session.scalar(stmt)
    if updated is None:
        raise StaleReplyPlanLeaseError("REPLY_PLAN_STALE_LEASE")
    plan = await get_by_id(session, plan_id=plan_id)
    if plan is None:
        raise RuntimeError("REPLY_PLAN_LOOKUP_FAILED")
    return plan


async def fail_with_lease(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int,
    error_code: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    now: datetime | None = None,
) -> ReplyPlan:
    moment = await resolve_moment(session, now)
    plan = await get_by_id(session, plan_id=plan_id)
    if plan is None:
        raise RuntimeError("REPLY_PLAN_LOOKUP_FAILED")
    if (
        plan.status != ReplyPlanStatus.PROCESSING.value
        or plan.lease_token != lease_token
        or plan.lease_version != lease_version
    ):
        raise StaleReplyPlanLeaseError("REPLY_PLAN_STALE_LEASE")

    if plan.attempt_count >= max_attempts:
        target = ReplyPlanStatus.DEAD
        not_before = plan.not_before
    else:
        target = ReplyPlanStatus.FAILED
        not_before = moment + timedelta(seconds=retry_delay_seconds)

    if not reply_plan_transition_allowed(ReplyPlanStatus.PROCESSING, target):
        raise ReplyPlanStateError("REPLY_PLAN_TRANSITION_DENIED")

    stmt = (
        update(ReplyPlan)
        .where(
            ReplyPlan.id == plan_id,
            ReplyPlan.status == ReplyPlanStatus.PROCESSING.value,
            ReplyPlan.lease_token == lease_token,
            ReplyPlan.lease_version == lease_version,
        )
        .values(
            status=target.value,
            not_before=not_before,
            cancel_reason=error_code[:64],
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            updated_at=moment,
        )
        .returning(ReplyPlan.id)
    )
    updated = await session.scalar(stmt)
    if updated is None:
        raise StaleReplyPlanLeaseError("REPLY_PLAN_STALE_LEASE")
    refreshed = await get_by_id(session, plan_id=plan_id)
    if refreshed is None:
        raise RuntimeError("REPLY_PLAN_LOOKUP_FAILED")
    return refreshed
