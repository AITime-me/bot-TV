"""Repository for acquisition_source_analytics_pendings. No commit; caller owns UoW."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.acquisition_source_types import (
    DEFAULT_MAX_ATTEMPTS,
    PURPOSE,
    TERMINAL_ACQUISITION_SOURCE_STATES,
    AcquisitionSourceOwnerKind,
    AcquisitionSourcePendingState,
)
from app.models.acquisition_source_analytics_pending import (
    AcquisitionSourceAnalyticsPending,
)

_TERMINAL_SQL = ", ".join(
    f"'{s.value}'" for s in TERMINAL_ACQUISITION_SOURCE_STATES
)


async def get_by_id(
    session: AsyncSession, *, pending_id: uuid.UUID
) -> AcquisitionSourceAnalyticsPending | None:
    return await session.get(AcquisitionSourceAnalyticsPending, pending_id)


async def get_by_evidence_id(
    session: AsyncSession,
    *,
    evidence_id: uuid.UUID,
    purpose: str = PURPOSE,
) -> AcquisitionSourceAnalyticsPending | None:
    stmt = select(AcquisitionSourceAnalyticsPending).where(
        AcquisitionSourceAnalyticsPending.evidence_id == evidence_id,
        AcquisitionSourceAnalyticsPending.purpose == purpose,
    )
    return await session.scalar(stmt)


async def upsert_discovered(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
    evidence_id: uuid.UUID,
    owner_kind: AcquisitionSourceOwnerKind | str,
    owner_id: uuid.UUID,
    source_key: str,
    now: datetime,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    purpose: str = PURPOSE,
) -> AcquisitionSourceAnalyticsPending:
    """Insert DISCOVERED pending if evidence_id+purpose absent; else existing."""

    if isinstance(owner_kind, AcquisitionSourceOwnerKind):
        kind_value = owner_kind.value
    else:
        kind_value = AcquisitionSourceOwnerKind(str(owner_kind)).value

    existing = await get_by_evidence_id(
        session, evidence_id=evidence_id, purpose=purpose
    )
    if existing is not None:
        return existing
    row = AcquisitionSourceAnalyticsPending(
        id=row_id,
        evidence_id=evidence_id,
        purpose=purpose,
        owner_kind=kind_value,
        owner_id=owner_id,
        source_key=source_key,
        state=AcquisitionSourcePendingState.DISCOVERED.value,
        attempt_count=0,
        max_attempts=max_attempts,
        lease_token=None,
        lease_expires_at=None,
        next_retry_at=None,
        amocrm_contact_id=None,
        amocrm_deal_id=None,
        result_code=None,
        result_outcome=None,
        manual_review_reason=None,
        created_at=now,
        updated_at=now,
    )
    async with session.begin_nested():
        session.add(row)
        await session.flush()
    return row


async def lock_next_claimable_id(
    session: AsyncSession, *, now: datetime
) -> uuid.UUID | None:
    stmt = text(
        f"""
        SELECT id
        FROM acquisition_source_analytics_pendings
        WHERE attempt_count < max_attempts
          AND state NOT IN ({_TERMINAL_SQL})
          AND (next_retry_at IS NULL OR next_retry_at <= :now)
          AND (
                lease_expires_at IS NULL
             OR lease_expires_at <= :now
          )
        ORDER BY created_at ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 1
        """
    )
    value = await session.scalar(stmt, {"now": now})
    if value is None:
        return None
    if type(value) is uuid.UUID:
        return value
    return uuid.UUID(str(value))


async def claim_lease(
    session: AsyncSession,
    *,
    row: AcquisitionSourceAnalyticsPending,
    lease_token: uuid.UUID,
    lease_expires_at: datetime,
    now: datetime,
) -> bool:
    terminal = [s.value for s in TERMINAL_ACQUISITION_SOURCE_STATES]
    stmt = (
        update(AcquisitionSourceAnalyticsPending)
        .where(
            AcquisitionSourceAnalyticsPending.id == row.id,
            AcquisitionSourceAnalyticsPending.state.notin_(terminal),
            AcquisitionSourceAnalyticsPending.attempt_count
            < AcquisitionSourceAnalyticsPending.max_attempts,
            (
                (AcquisitionSourceAnalyticsPending.lease_expires_at.is_(None))
                | (AcquisitionSourceAnalyticsPending.lease_expires_at <= now)
            ),
            (
                (AcquisitionSourceAnalyticsPending.next_retry_at.is_(None))
                | (AcquisitionSourceAnalyticsPending.next_retry_at <= now)
            ),
        )
        .values(
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            attempt_count=AcquisitionSourceAnalyticsPending.attempt_count + 1,
            next_retry_at=None,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    return bool(result.rowcount and result.rowcount == 1)


async def release_lease(
    session: AsyncSession,
    *,
    row: AcquisitionSourceAnalyticsPending,
    lease_token: uuid.UUID,
    now: datetime,
    next_retry_at: datetime | None = None,
    result_code: str | None = None,
) -> bool:
    stmt = (
        update(AcquisitionSourceAnalyticsPending)
        .where(
            AcquisitionSourceAnalyticsPending.id == row.id,
            AcquisitionSourceAnalyticsPending.lease_token == lease_token,
        )
        .values(
            lease_token=None,
            lease_expires_at=None,
            next_retry_at=next_retry_at,
            result_code=result_code,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    return bool(result.rowcount and result.rowcount == 1)


async def advance_state(
    session: AsyncSession,
    *,
    row: AcquisitionSourceAnalyticsPending,
    lease_token: uuid.UUID,
    state: AcquisitionSourcePendingState,
    now: datetime,
    result_code: str | None = None,
    result_outcome: str | None = None,
    manual_review_reason: str | None = None,
    amocrm_contact_id: str | None = None,
    amocrm_deal_id: str | None = None,
    clear_lease: bool = False,
    next_retry_at: datetime | None = None,
) -> bool:
    values: dict[str, object] = {
        "state": state.value,
        "updated_at": now,
    }
    if result_code is not None:
        values["result_code"] = result_code
    if result_outcome is not None:
        values["result_outcome"] = result_outcome
    if manual_review_reason is not None:
        values["manual_review_reason"] = manual_review_reason
    if amocrm_contact_id is not None:
        values["amocrm_contact_id"] = amocrm_contact_id
    if amocrm_deal_id is not None:
        values["amocrm_deal_id"] = amocrm_deal_id
    if clear_lease or state in TERMINAL_ACQUISITION_SOURCE_STATES:
        values["lease_token"] = None
        values["lease_expires_at"] = None
        values["next_retry_at"] = None
    if next_retry_at is not None and state not in TERMINAL_ACQUISITION_SOURCE_STATES:
        values["next_retry_at"] = next_retry_at

    stmt = (
        update(AcquisitionSourceAnalyticsPending)
        .where(
            AcquisitionSourceAnalyticsPending.id == row.id,
            AcquisitionSourceAnalyticsPending.lease_token == lease_token,
        )
        .values(**values)
    )
    result = await session.execute(stmt)
    await session.flush()
    return bool(result.rowcount and result.rowcount == 1)


async def expire_exhausted_to_terminal(
    session: AsyncSession, *, now: datetime
) -> int:
    stmt = (
        update(AcquisitionSourceAnalyticsPending)
        .where(
            AcquisitionSourceAnalyticsPending.attempt_count
            >= AcquisitionSourceAnalyticsPending.max_attempts,
            AcquisitionSourceAnalyticsPending.state.notin_(
                [s.value for s in TERMINAL_ACQUISITION_SOURCE_STATES]
            ),
        )
        .values(
            state=AcquisitionSourcePendingState.MANUAL_REVIEW.value,
            result_code="MAX_ATTEMPTS_EXCEEDED",
            result_outcome=AcquisitionSourcePendingState.MANUAL_REVIEW.value,
            manual_review_reason="MAX_ATTEMPTS_EXCEEDED",
            lease_token=None,
            lease_expires_at=None,
            next_retry_at=None,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    return int(result.rowcount or 0)
