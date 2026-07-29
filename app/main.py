from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.core.outbound_policy import (
    OutboundAction,
    is_automatic_outbound_allowed,
)
from app.db.session import create_engine, create_session_factory
from app.services.worker_health import WorkerHealthService


def create_app(
    settings: Settings | None = None,
    *,
    worker_health_service: WorkerHealthService | None = None,
) -> FastAPI:
    loaded_settings = settings if settings is not None else Settings.from_env()
    engine: AsyncEngine | None = None
    health_service = worker_health_service
    if health_service is None and loaded_settings.database_url is not None:
        engine = create_engine(loaded_settings)
        health_service = WorkerHealthService(
            create_session_factory(engine),
            stale_after_seconds=loaded_settings.worker_heartbeat_stale_seconds,
            tick_timeout_seconds=loaded_settings.worker_tick_timeout_seconds,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()

    application = FastAPI(lifespan=lifespan)

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready")
    async def health_ready() -> JSONResponse:
        outbound_enabled = is_automatic_outbound_allowed(
            loaded_settings,
            OutboundAction.SEND_MESSAGE,
        )
        payload: dict[str, object] = {
            "status": "ready",
            "bot_mode": loaded_settings.bot_mode.value,
            "emergency_lock": loaded_settings.emergency_lock,
            "outbound_enabled": outbound_enabled,
        }
        if health_service is None:
            return JSONResponse(status_code=200, content=payload)

        payload["database_configured"] = True
        try:
            report = await health_service.check()
        except Exception:
            payload["status"] = "not_ready"
            payload["worker_health"] = {
                "healthy": False,
                "reason": "database_or_health_probe_unavailable",
            }
            return JSONResponse(status_code=503, content=payload)

        payload["worker_health"] = report.public_view()
        if not report.healthy:
            payload["status"] = "not_ready"
            return JSONResponse(status_code=503, content=payload)
        return JSONResponse(status_code=200, content=payload)

    return application


app = create_app()
