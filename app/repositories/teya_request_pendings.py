"""Repository for teya_request_pendings. No commit; caller owns UoW."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.teya_request_types import (
    DEFAULT_MAX_ATTEMPTS,
    TERMINAL_TEYA_REQUEST_STATES,
    TeyaRequestPendingState,
)
from app.models.teya_request_pending import TeyaRequestPending

_TERMINAL_SQL = ", ".join(
    f"'{s.value}'" for s in TERMINAL_TEYA_REQUEST_STATES
)


async def get_by_id(
    session: AsyncSession, *, pending_id: uuid.UUID
) -> TeyaRequestPending | None:
    return await session.get(TeyaRequestPending, pending_id)


async def get_by_request_id(
    session: AsyncSession, *, request_id: uuid.UUID
) -> TeyaRequestPending | None:
    stmt = select(TeyaRequestPending).where(
        TeyaRequestPending.request_id == request_id
    )
    return await session.scalar(stmt)


async def upsert_discovered(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
    request_id: uuid.UUID,
    now: datetime,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> TeyaRequestPending:
    """Insert DISCOVERED pending if request_id absent; return existing otherwise."""

    existing = await get_by_request_id(session, request_id=request_id)
    if existing is not None:
        return existing
    row = TeyaRequestPending(
        id=row_id,
        request_id=request_id,
        state=TeyaRequestPendingState.DISCOVERED.value,
        attempt_count=0,
        max_attempts=max_attempts,
        lease_token=None,
        lease_expires_at=None,
        next_retry_at=None,
        result_code=None,
        result_outcome=None,
        contact_route_outcome=None,
        amocrm_contact_id=None,
        amocrm_deal_id=None,
        amocrm_task_id=None,
        structured_note=None,
        selected_starts_at=None,
        book_idempotency_key=None,
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
    """Pick one claimable pending id with FOR UPDATE SKIP LOCKED."""

    stmt = text(
        f"""
        SELECT id
        FROM teya_request_pendings
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
    row: TeyaRequestPending,
    lease_token: uuid.UUID,
    lease_expires_at: datetime,
    now: datetime,
) -> bool:
    """CAS: non-terminal + free/expired lease → take lease, bump attempt."""

    terminal = [s.value for s in TERMINAL_TEYA_REQUEST_STATES]
    stmt = (
        update(TeyaRequestPending)
        .where(
            TeyaRequestPending.id == row.id,
            TeyaRequestPending.state.notin_(terminal),
            TeyaRequestPending.attempt_count < TeyaRequestPending.max_attempts,
            (
                (TeyaRequestPending.lease_expires_at.is_(None))
                | (TeyaRequestPending.lease_expires_at <= now)
            ),
            (
                (TeyaRequestPending.next_retry_at.is_(None))
                | (TeyaRequestPending.next_retry_at <= now)
            ),
        )
        .values(
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            attempt_count=TeyaRequestPending.attempt_count + 1,
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
    row: TeyaRequestPending,
    lease_token: uuid.UUID,
    now: datetime,
    next_retry_at: datetime | None = None,
    result_code: str | None = None,
) -> bool:
    stmt = (
        update(TeyaRequestPending)
        .where(
            TeyaRequestPending.id == row.id,
            TeyaRequestPending.lease_token == lease_token,
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
    row: TeyaRequestPending,
    lease_token: uuid.UUID,
    state: TeyaRequestPendingState,
    now: datetime,
    result_code: str | None = None,
    result_outcome: str | None = None,
    contact_route_outcome: str | None = None,
    amocrm_contact_id: str | None = None,
    amocrm_deal_id: str | None = None,
    amocrm_task_id: str | None = None,
    structured_note: str | None = None,
    selected_starts_at: str | None = None,
    book_idempotency_key: str | None = None,
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
    if contact_route_outcome is not None:
        values["contact_route_outcome"] = contact_route_outcome
    if amocrm_contact_id is not None:
        values["amocrm_contact_id"] = amocrm_contact_id
    if amocrm_deal_id is not None:
        values["amocrm_deal_id"] = amocrm_deal_id
    if amocrm_task_id is not None:
        values["amocrm_task_id"] = amocrm_task_id
    if structured_note is not None:
        values["structured_note"] = structured_note
    if selected_starts_at is not None:
        values["selected_starts_at"] = selected_starts_at
    if book_idempotency_key is not None:
        values["book_idempotency_key"] = book_idempotency_key
    if clear_lease or state in TERMINAL_TEYA_REQUEST_STATES:
        values["lease_token"] = None
        values["lease_expires_at"] = None
    if next_retry_at is not None:
        values["next_retry_at"] = next_retry_at

    stmt = (
        update(TeyaRequestPending)
        .where(
            TeyaRequestPending.id == row.id,
            TeyaRequestPending.lease_token == lease_token,
        )
        .values(**values)
    )
    result = await session.execute(stmt)
    await session.flush()
    return bool(result.rowcount and result.rowcount == 1)
