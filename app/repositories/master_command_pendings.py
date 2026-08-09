"""Repository for master_command_pendings. No commit; caller owns UoW."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.master_command_types import (
    ACTIVE_PENDING_STATES,
    MasterCommandKind,
    MasterCommandPendingState,
    MasterCommandSafePayload,
)
from app.models.master_command_pending import MasterCommandPending


def _db_uuid(value: object) -> uuid.UUID:
    if type(value) is uuid.UUID:
        return value
    return uuid.UUID(str(value))


async def get_by_inbound(
    session: AsyncSession,
    *,
    channel: str,
    connection_scope: str,
    external_account_id: str,
    inbound_message_id: str,
) -> MasterCommandPending | None:
    stmt = select(MasterCommandPending).where(
        MasterCommandPending.channel == channel,
        MasterCommandPending.connection_scope == connection_scope,
        MasterCommandPending.external_account_id == external_account_id,
        MasterCommandPending.inbound_message_id == inbound_message_id,
    )
    return await session.scalar(stmt)


async def lock_active_by_identity(
    session: AsyncSession,
    *,
    channel: str,
    connection_scope: str,
    external_account_id: str,
) -> MasterCommandPending | None:
    active = [s.value for s in ACTIVE_PENDING_STATES]
    stmt = (
        select(MasterCommandPending)
        .where(
            MasterCommandPending.channel == channel,
            MasterCommandPending.connection_scope == connection_scope,
            MasterCommandPending.external_account_id == external_account_id,
            MasterCommandPending.state.in_(active),
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
    channel: str,
    connection_scope: str,
    external_account_id: str,
    master_id: str,
    inbound_message_id: str,
    command_kind: MasterCommandKind,
    state: MasterCommandPendingState,
    command_version: int,
    idempotency_key: str | None,
    safe_payload: MasterCommandSafePayload,
    phone_ref_token: str | None,
    name_ref_token: str | None,
    pii_conversation_id: uuid.UUID | None,
    confirmation_expires_at: datetime | None,
    now: datetime,
    result_code: str | None = None,
    result_outcome: str | None = None,
) -> MasterCommandPending:
    row = MasterCommandPending(
        id=row_id,
        channel=channel,
        connection_scope=connection_scope,
        external_account_id=external_account_id,
        master_id=master_id,
        inbound_message_id=inbound_message_id,
        command_kind=command_kind.value,
        state=state.value,
        command_version=command_version,
        idempotency_key=idempotency_key,
        safe_payload=safe_payload.to_json_dict(),
        phone_ref_token=phone_ref_token,
        name_ref_token=name_ref_token,
        pii_conversation_id=pii_conversation_id,
        confirmation_expires_at=confirmation_expires_at,
        execution_lease_token=None,
        execution_lease_expires_at=None,
        result_code=result_code,
        result_outcome=result_outcome,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def mark_terminal(
    session: AsyncSession,
    *,
    row: MasterCommandPending,
    state: MasterCommandPendingState,
    result_code: str | None,
    result_outcome: str | None,
    now: datetime,
) -> None:
    row.state = state.value
    row.result_code = result_code
    row.result_outcome = result_outcome
    row.execution_lease_token = None
    row.execution_lease_expires_at = None
    row.updated_at = now
    await session.flush()


async def update_clarification(
    session: AsyncSession,
    *,
    row: MasterCommandPending,
    safe_payload: MasterCommandSafePayload,
    phone_ref_token: str | None,
    name_ref_token: str | None,
    state: MasterCommandPendingState,
    confirmation_expires_at: datetime | None,
    idempotency_key: str | None,
    now: datetime,
) -> None:
    row.safe_payload = safe_payload.to_json_dict()
    if phone_ref_token is not None:
        row.phone_ref_token = phone_ref_token
    if name_ref_token is not None:
        row.name_ref_token = name_ref_token
    row.state = state.value
    row.confirmation_expires_at = confirmation_expires_at
    if idempotency_key is not None:
        row.idempotency_key = idempotency_key
    row.updated_at = now
    await session.flush()


async def claim_for_execution(
    session: AsyncSession,
    *,
    row: MasterCommandPending,
    lease_token: uuid.UUID,
    lease_expires_at: datetime,
    expected_version: int,
    now: datetime,
) -> bool:
    """CAS: AWAITING_CONFIRMATION + matching version → EXECUTING."""

    stmt = (
        update(MasterCommandPending)
        .where(
            MasterCommandPending.id == row.id,
            MasterCommandPending.state
            == MasterCommandPendingState.AWAITING_CONFIRMATION.value,
            MasterCommandPending.command_version == expected_version,
            MasterCommandPending.confirmation_expires_at.is_not(None),
            MasterCommandPending.confirmation_expires_at > now,
        )
        .values(
            state=MasterCommandPendingState.EXECUTING.value,
            execution_lease_token=lease_token,
            execution_lease_expires_at=lease_expires_at,
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
    row: MasterCommandPending,
    lease_token: uuid.UUID,
    lease_expires_at: datetime,
    expected_version: int,
    now: datetime,
) -> bool:
    """CAS: EXECUTING with expired lease → new EXECUTING lease (same version/key)."""

    stmt = (
        update(MasterCommandPending)
        .where(
            MasterCommandPending.id == row.id,
            MasterCommandPending.state == MasterCommandPendingState.EXECUTING.value,
            MasterCommandPending.command_version == expected_version,
            MasterCommandPending.execution_lease_expires_at.is_not(None),
            MasterCommandPending.execution_lease_expires_at <= now,
        )
        .values(
            execution_lease_token=lease_token,
            execution_lease_expires_at=lease_expires_at,
            result_code=None,
            result_outcome=None,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    return bool(result.rowcount and result.rowcount == 1)


async def recover_expired_execution_to_confirmation(
    session: AsyncSession,
    *,
    row: MasterCommandPending,
    confirmation_expires_at: datetime,
    now: datetime,
) -> bool:
    """CAS: expired EXECUTING → AWAITING_CONFIRMATION (PII refs + idempotency kept)."""

    stmt = (
        update(MasterCommandPending)
        .where(
            MasterCommandPending.id == row.id,
            MasterCommandPending.state == MasterCommandPendingState.EXECUTING.value,
            MasterCommandPending.execution_lease_expires_at.is_not(None),
            MasterCommandPending.execution_lease_expires_at <= now,
        )
        .values(
            state=MasterCommandPendingState.AWAITING_CONFIRMATION.value,
            execution_lease_token=None,
            execution_lease_expires_at=None,
            confirmation_expires_at=confirmation_expires_at,
            result_code="LEASE_EXPIRED",
            result_outcome="UNAVAILABLE",
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    return bool(result.rowcount and result.rowcount == 1)


async def release_execution_to_confirmation(
    session: AsyncSession,
    *,
    row: MasterCommandPending,
    lease_token: uuid.UUID,
    confirmation_expires_at: datetime,
    now: datetime,
    result_code: str = "TIMEOUT",
) -> bool:
    """Retryable remote outcome: EXECUTING + lease → AWAITING_CONFIRMATION.

    Keeps the same command_version, idempotency_key, and PII ref tokens.
    """

    stmt = (
        update(MasterCommandPending)
        .where(
            MasterCommandPending.id == row.id,
            MasterCommandPending.state == MasterCommandPendingState.EXECUTING.value,
            MasterCommandPending.execution_lease_token == lease_token,
        )
        .values(
            state=MasterCommandPendingState.AWAITING_CONFIRMATION.value,
            execution_lease_token=None,
            execution_lease_expires_at=None,
            confirmation_expires_at=confirmation_expires_at,
            updated_at=now,
            result_code=result_code,
            result_outcome="UNAVAILABLE",
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    return bool(result.rowcount and result.rowcount == 1)


async def complete_execution(
    session: AsyncSession,
    *,
    row: MasterCommandPending,
    lease_token: uuid.UUID,
    state: MasterCommandPendingState,
    result_code: str | None,
    result_outcome: str | None,
    now: datetime,
) -> bool:
    stmt = (
        update(MasterCommandPending)
        .where(
            MasterCommandPending.id == row.id,
            MasterCommandPending.state == MasterCommandPendingState.EXECUTING.value,
            MasterCommandPending.execution_lease_token == lease_token,
        )
        .values(
            state=state.value,
            result_code=result_code,
            result_outcome=result_outcome,
            execution_lease_token=None,
            execution_lease_expires_at=None,
            phone_ref_token=None,
            name_ref_token=None,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    return bool(result.rowcount and result.rowcount == 1)


async def count_active_by_identity(
    session: AsyncSession,
    *,
    channel: str,
    connection_scope: str,
    external_account_id: str,
) -> int:
    active = [s.value for s in ACTIVE_PENDING_STATES]
    stmt = select(func.count()).select_from(MasterCommandPending).where(
        MasterCommandPending.channel == channel,
        MasterCommandPending.connection_scope == connection_scope,
        MasterCommandPending.external_account_id == external_account_id,
        MasterCommandPending.state.in_(active),
    )
    value = await session.scalar(stmt)
    return int(value or 0)
