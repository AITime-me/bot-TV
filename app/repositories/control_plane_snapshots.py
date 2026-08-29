"""Repository for control_plane_snapshots. No commit; caller owns UoW."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.control_plane_types import ControlPlaneSnapshotKind
from app.models.control_plane_snapshot import ControlPlaneSnapshot

# Fixed advisory-lock key for serialized control-plane refresh cycles.
CONTROL_PLANE_REFRESH_LOCK_KEY = 0xB07C07C0_04


async def try_acquire_refresh_lock(session: AsyncSession) -> bool:
    locked = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"),
        {"key": CONTROL_PLANE_REFRESH_LOCK_KEY},
    )
    return locked is True


async def get_by_kind(
    session: AsyncSession, *, kind: ControlPlaneSnapshotKind | str
) -> ControlPlaneSnapshot | None:
    kind_value = (
        kind.value if isinstance(kind, ControlPlaneSnapshotKind) else str(kind)
    )
    return await session.get(ControlPlaneSnapshot, kind_value)


async def list_all(session: AsyncSession) -> list[ControlPlaneSnapshot]:
    result = await session.scalars(select(ControlPlaneSnapshot))
    return list(result.all())


async def upsert_verified(
    session: AsyncSession,
    *,
    kind: ControlPlaneSnapshotKind | str,
    schema_version: int,
    publication_id: str,
    version: int,
    checksum: str,
    payload: dict[str, Any],
    published_at: datetime,
    verified_at: datetime,
    fetched_at: datetime,
) -> ControlPlaneSnapshot:
    kind_value = (
        kind.value if isinstance(kind, ControlPlaneSnapshotKind) else str(kind)
    )
    stmt = (
        insert(ControlPlaneSnapshot)
        .values(
            kind=kind_value,
            schema_version=schema_version,
            publication_id=publication_id,
            version=version,
            checksum=checksum,
            payload=payload,
            published_at=published_at,
            verified_at=verified_at,
            fetched_at=fetched_at,
            updated_at=fetched_at,
            usable=True,
            last_error_code=None,
        )
        .on_conflict_do_update(
            index_elements=[ControlPlaneSnapshot.kind],
            set_={
                "schema_version": schema_version,
                "publication_id": publication_id,
                "version": version,
                "checksum": checksum,
                "payload": payload,
                "published_at": published_at,
                "verified_at": verified_at,
                "fetched_at": fetched_at,
                "updated_at": fetched_at,
                "usable": True,
                "last_error_code": None,
            },
        )
        .returning(ControlPlaneSnapshot)
    )
    row = await session.scalar(stmt)
    if row is None:
        raise RuntimeError("CONTROL_PLANE_UPSERT_FAILED")
    await session.flush()
    return row


async def mark_unusable(
    session: AsyncSession,
    *,
    kind: ControlPlaneSnapshotKind | str,
    error_code: str,
    fetched_at: datetime,
) -> ControlPlaneSnapshot | None:
    kind_value = (
        kind.value if isinstance(kind, ControlPlaneSnapshotKind) else str(kind)
    )
    stmt = (
        update(ControlPlaneSnapshot)
        .where(ControlPlaneSnapshot.kind == kind_value)
        .values(
            usable=False,
            last_error_code=error_code,
            fetched_at=fetched_at,
            updated_at=fetched_at,
        )
        .returning(ControlPlaneSnapshot)
    )
    row = await session.scalar(stmt)
    await session.flush()
    return row


async def touch_fetched_at(
    session: AsyncSession,
    *,
    kind: ControlPlaneSnapshotKind | str,
    fetched_at: datetime,
) -> None:
    kind_value = (
        kind.value if isinstance(kind, ControlPlaneSnapshotKind) else str(kind)
    )
    await session.execute(
        update(ControlPlaneSnapshot)
        .where(ControlPlaneSnapshot.kind == kind_value)
        .values(fetched_at=fetched_at, updated_at=fetched_at)
    )
    await session.flush()
