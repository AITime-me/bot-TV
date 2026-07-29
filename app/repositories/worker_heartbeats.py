from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.clock import db_statement_now
from app.models.worker_heartbeat import REQUIRED_WORKER_LOOPS, WorkerHeartbeat


class StaleWorkerGenerationError(RuntimeError):
    """A previous worker generation attempted to update current health."""


async def register_generation(
    session: AsyncSession,
    *,
    generation_id: uuid.UUID,
    worker_id: str,
) -> None:
    if not worker_id or len(worker_id) > 128:
        raise ValueError("worker_id must contain 1..128 characters")
    moment = await db_statement_now(session)
    for loop_name in REQUIRED_WORKER_LOOPS:
        stmt = (
            insert(WorkerHeartbeat)
            .values(
                loop_name=loop_name,
                generation_id=generation_id,
                worker_id=worker_id,
                started_at=moment,
                last_tick_started_at=None,
                last_succeeded_at=None,
                last_failed_at=None,
                consecutive_failures=0,
                last_error_code=None,
                updated_at=moment,
            )
            .on_conflict_do_update(
                index_elements=[WorkerHeartbeat.loop_name],
                set_={
                    "generation_id": generation_id,
                    "worker_id": worker_id,
                    "started_at": moment,
                    "last_tick_started_at": None,
                    "last_succeeded_at": None,
                    "last_failed_at": None,
                    "consecutive_failures": 0,
                    "last_error_code": None,
                    "updated_at": moment,
                },
            )
        )
        await session.execute(stmt)


async def record_tick_started(
    session: AsyncSession,
    *,
    loop_name: str,
    generation_id: uuid.UUID,
) -> None:
    moment = await db_statement_now(session)
    result = await session.execute(
        update(WorkerHeartbeat)
        .where(
            WorkerHeartbeat.loop_name == loop_name,
            WorkerHeartbeat.generation_id == generation_id,
        )
        .values(last_tick_started_at=moment, updated_at=moment)
    )
    if result.rowcount != 1:
        raise StaleWorkerGenerationError("WORKER_GENERATION_REPLACED")


async def record_tick_succeeded(
    session: AsyncSession,
    *,
    loop_name: str,
    generation_id: uuid.UUID,
) -> None:
    moment = await db_statement_now(session)
    result = await session.execute(
        update(WorkerHeartbeat)
        .where(
            WorkerHeartbeat.loop_name == loop_name,
            WorkerHeartbeat.generation_id == generation_id,
        )
        .values(
            last_succeeded_at=moment,
            consecutive_failures=0,
            last_error_code=None,
            updated_at=moment,
        )
    )
    if result.rowcount != 1:
        raise StaleWorkerGenerationError("WORKER_GENERATION_REPLACED")


async def record_tick_failed(
    session: AsyncSession,
    *,
    loop_name: str,
    generation_id: uuid.UUID,
    error_code: str,
) -> int:
    moment = await db_statement_now(session)
    row = await session.scalar(
        update(WorkerHeartbeat)
        .where(
            WorkerHeartbeat.loop_name == loop_name,
            WorkerHeartbeat.generation_id == generation_id,
        )
        .values(
            last_failed_at=moment,
            consecutive_failures=WorkerHeartbeat.consecutive_failures + 1,
            last_error_code=error_code[:64],
            updated_at=moment,
        )
        .returning(WorkerHeartbeat.consecutive_failures)
    )
    if row is None:
        raise StaleWorkerGenerationError("WORKER_GENERATION_REPLACED")
    return int(row)


async def list_required(
    session: AsyncSession,
) -> list[WorkerHeartbeat]:
    rows = await session.scalars(
        select(WorkerHeartbeat).where(
            WorkerHeartbeat.loop_name.in_(REQUIRED_WORKER_LOOPS)
        )
    )
    return list(rows.all())


async def database_now(session: AsyncSession) -> datetime:
    return await db_statement_now(session)
