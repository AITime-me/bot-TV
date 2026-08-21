"""Repository for self_booking_create_pendings. No commit; caller owns UoW."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.self_booking_create_types import (
    ACTIVE_SELF_BOOKING_CREATE_STATES,
    SelfBookingCreatePendingState,
)
from app.models.self_booking_create_pending import SelfBookingCreatePending


async def get_by_confirm(
    session: AsyncSession,
    *,
    channel: str,
    confirm_external_message_id: str,
) -> SelfBookingCreatePending | None:
    stmt = select(SelfBookingCreatePending).where(
        SelfBookingCreatePending.channel == channel,
        SelfBookingCreatePending.confirm_external_message_id
        == confirm_external_message_id,
    )
    return await session.scalar(stmt)


async def get_by_id(
    session: AsyncSession,
    *,
    pending_id: uuid.UUID,
) -> SelfBookingCreatePending | None:
    return await session.get(SelfBookingCreatePending, pending_id)


async def lock_active_by_conversation(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
) -> SelfBookingCreatePending | None:
    active = [s.value for s in ACTIVE_SELF_BOOKING_CREATE_STATES]
    stmt = (
        select(SelfBookingCreatePending)
        .where(
            SelfBookingCreatePending.conversation_id == conversation_id,
            SelfBookingCreatePending.state.in_(active),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await session.scalars(stmt)
    rows = list(result.all())
    if not rows:
        return None
    return rows[0]


async def insert_pending(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
    conversation_id: uuid.UUID,
    channel: str,
    confirm_external_message_id: str,
    state: SelfBookingCreatePendingState,
    command_version: int,
    attempt_count: int,
    max_attempts: int,
    idempotency_key: str,
    slot_id: str,
    starts_at: str,
    fence_context_version: int,
    fence_manager_epoch: int,
    fence_event_seq_hwm: int,
    phone_ref_token: str,
    name_ref_token: str,
    now: datetime,
) -> SelfBookingCreatePending:
    row = SelfBookingCreatePending(
        id=row_id,
        conversation_id=conversation_id,
        channel=channel,
        confirm_external_message_id=confirm_external_message_id,
        state=state.value,
        command_version=command_version,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        idempotency_key=idempotency_key,
        slot_id=slot_id,
        starts_at=starts_at,
        fence_context_version=fence_context_version,
        fence_manager_epoch=fence_manager_epoch,
        fence_event_seq_hwm=fence_event_seq_hwm,
        personal_data_consent=True,
        offer_acknowledgement=True,
        phone_ref_token=phone_ref_token,
        name_ref_token=name_ref_token,
        execution_lease_token=None,
        execution_lease_expires_at=None,
        result_code=None,
        result_outcome=None,
        created_at=now,
        updated_at=now,
    )
    # SAVEPOINT so unique/partial-index conflicts do not abort the caller UoW.
    async with session.begin_nested():
        session.add(row)
        await session.flush()
    return row


async def claim_for_execution(
    session: AsyncSession,
    *,
    row: SelfBookingCreatePending,
    lease_token: uuid.UUID,
    lease_expires_at: datetime,
    expected_version: int,
    now: datetime,
) -> bool:
    """CAS: READY + matching version + under max attempts → EXECUTING."""

    stmt = (
        update(SelfBookingCreatePending)
        .where(
            SelfBookingCreatePending.id == row.id,
            SelfBookingCreatePending.state
            == SelfBookingCreatePendingState.READY.value,
            SelfBookingCreatePending.command_version == expected_version,
            SelfBookingCreatePending.attempt_count
            < SelfBookingCreatePending.max_attempts,
        )
        .values(
            state=SelfBookingCreatePendingState.EXECUTING.value,
            execution_lease_token=lease_token,
            execution_lease_expires_at=lease_expires_at,
            attempt_count=SelfBookingCreatePending.attempt_count + 1,
            result_code=None,
            result_outcome=None,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    return bool(result.rowcount and result.rowcount == 1)


async def reclaim_expired_execution(
    session: AsyncSession,
    *,
    row: SelfBookingCreatePending,
    lease_token: uuid.UUID,
    lease_expires_at: datetime,
    expected_version: int,
    now: datetime,
) -> bool:
    """CAS: EXECUTING with expired lease → new EXECUTING lease (same key)."""

    stmt = (
        update(SelfBookingCreatePending)
        .where(
            SelfBookingCreatePending.id == row.id,
            SelfBookingCreatePending.state
            == SelfBookingCreatePendingState.EXECUTING.value,
            SelfBookingCreatePending.command_version == expected_version,
            SelfBookingCreatePending.execution_lease_expires_at.is_not(None),
            SelfBookingCreatePending.execution_lease_expires_at <= now,
            SelfBookingCreatePending.attempt_count
            < SelfBookingCreatePending.max_attempts,
        )
        .values(
            execution_lease_token=lease_token,
            execution_lease_expires_at=lease_expires_at,
            attempt_count=SelfBookingCreatePending.attempt_count + 1,
            result_code=None,
            result_outcome=None,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    return bool(result.rowcount and result.rowcount == 1)


async def release_to_ready(
    session: AsyncSession,
    *,
    row: SelfBookingCreatePending,
    lease_token: uuid.UUID,
    result_code: str | None,
    now: datetime,
) -> bool:
    """Retryable path: EXECUTING + matching lease → READY (same idempotency)."""

    stmt = (
        update(SelfBookingCreatePending)
        .where(
            SelfBookingCreatePending.id == row.id,
            SelfBookingCreatePending.state
            == SelfBookingCreatePendingState.EXECUTING.value,
            SelfBookingCreatePending.execution_lease_token == lease_token,
        )
        .values(
            state=SelfBookingCreatePendingState.READY.value,
            execution_lease_token=None,
            execution_lease_expires_at=None,
            result_code=result_code,
            result_outcome=None,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    return bool(result.rowcount and result.rowcount == 1)


async def mark_terminal(
    session: AsyncSession,
    *,
    row: SelfBookingCreatePending,
    state: SelfBookingCreatePendingState,
    result_code: str | None,
    result_outcome: str | None,
    now: datetime,
    lease_token: uuid.UUID | None = None,
) -> bool:
    """Mark terminal. When lease_token set, require EXECUTING + matching lease."""

    if state not in {
        SelfBookingCreatePendingState.SUCCEEDED,
        SelfBookingCreatePendingState.FAILED,
        SelfBookingCreatePendingState.CANCELLED,
        SelfBookingCreatePendingState.EXPIRED,
    }:
        raise ValueError("SELF_BOOKING_TERMINAL_STATE_INVALID") from None

    where = [
        SelfBookingCreatePending.id == row.id,
        SelfBookingCreatePending.state.in_(
            [
                SelfBookingCreatePendingState.READY.value,
                SelfBookingCreatePendingState.EXECUTING.value,
            ]
        ),
    ]
    if lease_token is not None:
        where = [
            SelfBookingCreatePending.id == row.id,
            SelfBookingCreatePending.state
            == SelfBookingCreatePendingState.EXECUTING.value,
            SelfBookingCreatePending.execution_lease_token == lease_token,
        ]

    stmt = (
        update(SelfBookingCreatePending)
        .where(*where)
        .values(
            state=state.value,
            result_code=result_code,
            result_outcome=result_outcome,
            execution_lease_token=None,
            execution_lease_expires_at=None,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    return bool(result.rowcount and result.rowcount == 1)


async def expire_exhausted_attempts(
    session: AsyncSession,
    *,
    row: SelfBookingCreatePending,
    now: datetime,
) -> bool:
    """READY with attempt_count >= max_attempts → EXPIRED."""

    stmt = (
        update(SelfBookingCreatePending)
        .where(
            SelfBookingCreatePending.id == row.id,
            SelfBookingCreatePending.state
            == SelfBookingCreatePendingState.READY.value,
            SelfBookingCreatePending.attempt_count
            >= SelfBookingCreatePending.max_attempts,
        )
        .values(
            state=SelfBookingCreatePendingState.EXPIRED.value,
            result_code="MAX_ATTEMPTS_EXCEEDED",
            result_outcome=SelfBookingCreatePendingState.EXPIRED.value,
            execution_lease_token=None,
            execution_lease_expires_at=None,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    return bool(result.rowcount and result.rowcount == 1)


async def lock_next_claimable_id(
    session: AsyncSession,
    *,
    now: datetime,
) -> uuid.UUID | None:
    """Pick one claimable pending id with FOR UPDATE SKIP LOCKED.

    Does not change state — caller must run claim_for_execution / execute.
    READY under max attempts, or EXECUTING with expired lease under max.
    """

    stmt = text(
        """
        SELECT id
        FROM self_booking_create_pendings
        WHERE attempt_count < max_attempts
          AND (
                state = 'READY'
             OR (
                    state = 'EXECUTING'
                AND execution_lease_expires_at IS NOT NULL
                AND execution_lease_expires_at <= :now
             )
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
