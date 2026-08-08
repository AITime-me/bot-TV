"""Repository for master_channel_bindings. No commit; caller owns UoW."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.master_channel_binding import (
    MasterBindingChannel,
    MasterBindingStatus,
    MasterChannelBindingRecord,
)
from app.models.master_channel_binding import MasterChannelBinding


def _db_uuid(value: object) -> uuid.UUID:
    if type(value) is uuid.UUID:
        return value
    return uuid.UUID(str(value))


def _to_record(row: MasterChannelBinding) -> MasterChannelBindingRecord:
    return MasterChannelBindingRecord(
        binding_id=_db_uuid(row.id),
        channel=MasterBindingChannel(row.channel),
        connection_scope=row.connection_scope,
        external_account_id=row.external_account_id,
        master_id=row.master_id,
        status=MasterBindingStatus(row.status),
        bound_at=row.bound_at,
        revoked_at=row.revoked_at,
    )


async def list_active_by_identity(
    session: AsyncSession,
    *,
    channel: str,
    connection_scope: str,
    external_account_id: str,
) -> list[MasterChannelBinding]:
    """Load ACTIVE rows for identity (should be 0 or 1 under the unique index)."""

    stmt = select(MasterChannelBinding).where(
        MasterChannelBinding.channel == channel,
        MasterChannelBinding.connection_scope == connection_scope,
        MasterChannelBinding.external_account_id == external_account_id,
        MasterChannelBinding.status == MasterBindingStatus.ACTIVE.value,
    )
    result = await session.scalars(stmt)
    return list(result.all())


async def lock_active_by_identity(
    session: AsyncSession,
    *,
    channel: str,
    connection_scope: str,
    external_account_id: str,
) -> MasterChannelBinding | None:
    """SELECT ... FOR UPDATE the ACTIVE binding for this identity, if any."""

    stmt = (
        select(MasterChannelBinding)
        .where(
            MasterChannelBinding.channel == channel,
            MasterChannelBinding.connection_scope == connection_scope,
            MasterChannelBinding.external_account_id == external_account_id,
            MasterChannelBinding.status == MasterBindingStatus.ACTIVE.value,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await session.scalars(stmt)
    rows = list(result.all())
    if not rows:
        return None
    # Partial unique index should guarantee ≤1; service counts for AMBIGUOUS/CONFLICT.
    return rows[0]


async def count_active_by_identity(
    session: AsyncSession,
    *,
    channel: str,
    connection_scope: str,
    external_account_id: str,
) -> int:
    stmt = select(func.count()).select_from(MasterChannelBinding).where(
        MasterChannelBinding.channel == channel,
        MasterChannelBinding.connection_scope == connection_scope,
        MasterChannelBinding.external_account_id == external_account_id,
        MasterChannelBinding.status == MasterBindingStatus.ACTIVE.value,
    )
    value = await session.scalar(stmt)
    return int(value or 0)


async def insert_active_binding(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
    channel: str,
    connection_scope: str,
    external_account_id: str,
    master_id: str,
) -> MasterChannelBinding:
    """Insert ACTIVE row. Caller handles IntegrityError on unique race."""

    now = func.statement_timestamp()
    row = MasterChannelBinding(
        id=row_id,
        channel=channel,
        connection_scope=connection_scope,
        external_account_id=external_account_id,
        master_id=master_id,
        status=MasterBindingStatus.ACTIVE.value,
        bound_at=now,
        revoked_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def mark_revoked(
    session: AsyncSession,
    *,
    binding_id: uuid.UUID,
) -> MasterChannelBinding | None:
    """Transition ACTIVE → REVOKED. Returns updated row or None if not ACTIVE."""

    stmt = (
        update(MasterChannelBinding)
        .where(
            MasterChannelBinding.id == binding_id,
            MasterChannelBinding.status == MasterBindingStatus.ACTIVE.value,
        )
        .values(
            status=MasterBindingStatus.REVOKED.value,
            revoked_at=func.statement_timestamp(),
            updated_at=func.statement_timestamp(),
        )
        .returning(MasterChannelBinding)
    )
    row = await session.scalar(stmt)
    if row is None:
        return None
    await session.flush()
    return row


def as_record(row: MasterChannelBinding) -> MasterChannelBindingRecord:
    return _to_record(row)
