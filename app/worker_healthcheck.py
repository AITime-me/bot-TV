from __future__ import annotations

import asyncio

from app.config import Settings
from app.db.session import create_engine, create_session_factory
from app.services.worker_health import WorkerHealthService


async def _check(settings: Settings) -> bool:
    settings.validate_worker_runtime()
    engine = create_engine(settings)
    try:
        report = await WorkerHealthService(
            create_session_factory(engine),
            stale_after_seconds=settings.worker_heartbeat_stale_seconds,
            tick_timeout_seconds=settings.worker_tick_timeout_seconds,
        ).check()
        return report.healthy
    finally:
        await engine.dispose()


def main() -> int:
    try:
        healthy = asyncio.run(_check(Settings.from_env()))
    except Exception:
        return 1
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
