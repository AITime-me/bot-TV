from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys

from app.config import Settings
from app.db.session import create_engine, create_session_factory
from app.db.worker_lock import worker_singleton_lock
from app.services.worker_runtime import (
    WorkerHeartbeatStore,
    WorkerRuntime,
    build_default_loop_specs,
)


def _worker_id() -> str:
    configured = os.environ.get("WORKER_INSTANCE_ID")
    if configured:
        return configured[:128]
    return f"{socket.gethostname()}:{os.getpid()}"[:128]


async def run_worker(settings: Settings) -> None:
    settings.validate_worker_runtime()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    worker_id = _worker_id()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        async with worker_singleton_lock(engine):
            runtime = WorkerRuntime(
                settings=settings,
                worker_id=worker_id,
                heartbeat_store=WorkerHeartbeatStore(session_factory),
                loops=build_default_loop_specs(
                    settings=settings,
                    session_factory=session_factory,
                    worker_id=worker_id,
                ),
            )
            await runtime.run(stop_event)
    finally:
        await engine.dispose()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_worker(Settings.from_env()))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Never print exception text: driver errors may embed credentials.
        print(
            f"worker stopped error_code={type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
