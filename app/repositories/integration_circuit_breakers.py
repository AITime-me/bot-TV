"""Repository for integration circuit breakers."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.amocrm_circuit_breaker import (
    AMOCRM_BUSINESS_WRITES_BREAKER_KEY,
    CircuitBreakerPolicy,
    CircuitBreakerSnapshot,
    CircuitBreakerState,
    ProbeClaimOutcome,
    ProbeClaimResult,
)
from app.models.integration_circuit_breaker import IntegrationCircuitBreaker


async def get_or_create(
    session: AsyncSession,
    *,
    key: str = AMOCRM_BUSINESS_WRITES_BREAKER_KEY,
    now: datetime,
) -> CircuitBreakerSnapshot:
    row = await session.get(IntegrationCircuitBreaker, key)
    if row is None:
        stmt = insert(IntegrationCircuitBreaker).values(
            key=key,
            state=CircuitBreakerState.CLOSED.value,
            failure_count=0,
            opened_at=None,
            half_open_successes=0,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=["key"])
        await session.execute(stmt)
        row = await session.get(IntegrationCircuitBreaker, key)
        assert row is not None
    return _snap(row)


async def get(
    session: AsyncSession,
    *,
    key: str = AMOCRM_BUSINESS_WRITES_BREAKER_KEY,
) -> CircuitBreakerSnapshot | None:
    """Read-only lookup; never inserts."""

    row = await session.get(IntegrationCircuitBreaker, key)
    if row is None:
        return None
    return _snap(row)


async def record_success(
    session: AsyncSession,
    *,
    key: str = AMOCRM_BUSINESS_WRITES_BREAKER_KEY,
    now: datetime,
    policy: CircuitBreakerPolicy,
) -> CircuitBreakerSnapshot:
    row = await _ensure(session, key=key, now=now)
    state = CircuitBreakerState(row.state)
    if state is CircuitBreakerState.HALF_OPEN:
        row.half_open_successes += 1
        if row.half_open_successes >= policy.half_open_successes:
            row.state = CircuitBreakerState.CLOSED.value
            row.failure_count = 0
            row.opened_at = None
            row.half_open_successes = 0
    else:
        row.state = CircuitBreakerState.CLOSED.value
        row.failure_count = 0
        row.opened_at = None
        row.half_open_successes = 0
    row.updated_at = now
    return _snap(row)


async def record_failure(
    session: AsyncSession,
    *,
    key: str = AMOCRM_BUSINESS_WRITES_BREAKER_KEY,
    now: datetime,
    policy: CircuitBreakerPolicy,
) -> CircuitBreakerSnapshot:
    row = await _ensure(session, key=key, now=now)
    state = CircuitBreakerState(row.state)
    if state is CircuitBreakerState.HALF_OPEN:
        row.state = CircuitBreakerState.OPEN.value
        row.failure_count = policy.failure_threshold
        row.opened_at = now
        row.half_open_successes = 0
    else:
        row.failure_count += 1
        if row.failure_count >= policy.failure_threshold:
            row.state = CircuitBreakerState.OPEN.value
            row.opened_at = now
            row.half_open_successes = 0
    row.updated_at = now
    return _snap(row)


async def try_claim_probe(
    session: AsyncSession,
    *,
    key: str = AMOCRM_BUSINESS_WRITES_BREAKER_KEY,
    now: datetime,
    policy: CircuitBreakerPolicy,
) -> ProbeClaimResult:
    """Atomically grant at most one HALF_OPEN probe across workers.

    Uses ``opened_at`` as probe-lease start while state is HALF_OPEN.
    Crash recovery: after ``probe_lease_seconds`` another worker may reclaim.
    CLOSED always allows writes without taking the probe lease.
    """

    await get_or_create(session, key=key, now=now)
    cooldown_deadline = now - timedelta(seconds=policy.cooldown_seconds)
    probe_expiry = now - timedelta(seconds=policy.probe_lease_seconds)

    claimed = await session.execute(
        text(
            "UPDATE integration_circuit_breakers SET "
            "state = 'HALF_OPEN', "
            "opened_at = :now, "
            "half_open_successes = 0, "
            "updated_at = :now "
            "WHERE key = :key AND ("
            "  (state = 'OPEN' AND opened_at IS NOT NULL "
            "   AND opened_at <= :cooldown_deadline)"
            "  OR (state = 'HALF_OPEN' AND opened_at IS NOT NULL "
            "   AND opened_at <= :probe_expiry)"
            ") "
            "RETURNING key, state, failure_count, opened_at, "
            "half_open_successes, updated_at"
        ),
        {
            "key": key,
            "now": now,
            "cooldown_deadline": cooldown_deadline,
            "probe_expiry": probe_expiry,
        },
    )
    row = claimed.mappings().first()
    if row is not None:
        # Raw UPDATE bypasses the identity map; force reload for callers.
        session.expire_all()
        current = await session.get(IntegrationCircuitBreaker, key)
        assert current is not None
        return ProbeClaimResult(
            outcome=ProbeClaimOutcome.ALLOWED, snapshot=_snap(current)
        )

    session.expire_all()
    current = await session.get(IntegrationCircuitBreaker, key)
    assert current is not None
    snap = _snap(current)
    if snap.state is CircuitBreakerState.CLOSED:
        return ProbeClaimResult(
            outcome=ProbeClaimOutcome.ALLOWED, snapshot=snap
        )
    if snap.state is CircuitBreakerState.OPEN:
        return ProbeClaimResult(
            outcome=ProbeClaimOutcome.DENIED_OPEN, snapshot=snap
        )
    return ProbeClaimResult(
        outcome=ProbeClaimOutcome.DENIED_PROBE_BUSY, snapshot=snap
    )


async def maybe_half_open(
    session: AsyncSession,
    *,
    key: str = AMOCRM_BUSINESS_WRITES_BREAKER_KEY,
    now: datetime,
    policy: CircuitBreakerPolicy,
) -> CircuitBreakerSnapshot:
    """Compatibility wrapper; prefer try_claim_probe for single-probe semantics."""

    result = await try_claim_probe(session, key=key, now=now, policy=policy)
    return result.snapshot


async def _ensure(
    session: AsyncSession, *, key: str, now: datetime
) -> IntegrationCircuitBreaker:
    snap = await get_or_create(session, key=key, now=now)
    row = await session.get(IntegrationCircuitBreaker, snap.key)
    assert row is not None
    return row


def _snap(row: IntegrationCircuitBreaker) -> CircuitBreakerSnapshot:
    return CircuitBreakerSnapshot(
        key=row.key,
        state=CircuitBreakerState(row.state),
        failure_count=row.failure_count,
        opened_at=row.opened_at,
        half_open_successes=row.half_open_successes,
        updated_at=row.updated_at,
    )
