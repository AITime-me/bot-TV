from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import session_scope
from app.models.worker_heartbeat import REQUIRED_WORKER_LOOPS, WorkerHeartbeat
from app.repositories import worker_heartbeats as heartbeat_repo


@dataclass(frozen=True)
class WorkerHealthReport:
    healthy: bool
    checked_at: datetime
    missing_loops: tuple[str, ...]
    stale_loops: tuple[str, ...]
    failed_loops: tuple[str, ...]
    stuck_loops: tuple[str, ...]

    def public_view(self) -> dict[str, object]:
        return {
            "healthy": self.healthy,
            "missing_loops": list(self.missing_loops),
            "stale_loops": list(self.stale_loops),
            "failed_loops": list(self.failed_loops),
            "stuck_loops": list(self.stuck_loops),
        }


def assess_worker_health(
    rows: list[WorkerHeartbeat],
    *,
    checked_at: datetime,
    stale_after_seconds: int,
    tick_timeout_seconds: int,
) -> WorkerHealthReport:
    by_name = {row.loop_name: row for row in rows}
    missing: list[str] = []
    stale: list[str] = []
    failed: list[str] = []
    stuck: list[str] = []
    stale_before = checked_at - timedelta(seconds=stale_after_seconds)
    stuck_before = checked_at - timedelta(seconds=tick_timeout_seconds)

    for loop_name in REQUIRED_WORKER_LOOPS:
        row = by_name.get(loop_name)
        if row is None:
            missing.append(loop_name)
            continue
        if row.last_succeeded_at is None:
            missing.append(loop_name)
        elif row.last_succeeded_at < stale_before:
            stale.append(loop_name)
        if row.consecutive_failures > 0 or row.last_error_code is not None:
            failed.append(loop_name)
        if (
            row.last_succeeded_at is not None
            and row.last_tick_started_at is not None
            and row.last_tick_started_at > row.last_succeeded_at
            and row.last_tick_started_at < stuck_before
        ):
            stuck.append(loop_name)

    return WorkerHealthReport(
        healthy=not (missing or stale or failed or stuck),
        checked_at=checked_at,
        missing_loops=tuple(missing),
        stale_loops=tuple(stale),
        failed_loops=tuple(failed),
        stuck_loops=tuple(stuck),
    )


class WorkerHealthService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        stale_after_seconds: int,
        tick_timeout_seconds: int,
    ) -> None:
        self._session_factory = session_factory
        self._stale_after_seconds = stale_after_seconds
        self._tick_timeout_seconds = tick_timeout_seconds

    async def check(self) -> WorkerHealthReport:
        async with session_scope(self._session_factory) as session:
            checked_at = await heartbeat_repo.database_now(session)
            rows = await heartbeat_repo.list_required(session)
        return assess_worker_health(
            rows,
            checked_at=checked_at,
            stale_after_seconds=self._stale_after_seconds,
            tick_timeout_seconds=self._tick_timeout_seconds,
        )
