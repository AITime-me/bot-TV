from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.core.booking_eligibility_factory import build_booking_eligibility_client
from app.core.booking_eligibility_http import BookingEligibilityHttpClient
from app.core.outbound_policy import (
    OutboundAction,
    is_automatic_outbound_allowed,
)
from app.db.session import create_engine, create_session_factory
from app.services.booking_eligibility_flow import BookingEligibilityFlowService
from app.services.worker_health import WorkerHealthService

_BOOKING_ELIGIBILITY_CLIENT_UNSET: Final[object] = object()
_BOOKING_ELIGIBILITY_FLOW_UNSET: Final[object] = object()


def create_app(
    settings: Settings | None = None,
    *,
    worker_health_service: WorkerHealthService | None = None,
    booking_eligibility_client: BookingEligibilityHttpClient
    | None
    | object = _BOOKING_ELIGIBILITY_CLIENT_UNSET,
    booking_eligibility_flow: BookingEligibilityFlowService
    | None
    | object = _BOOKING_ELIGIBILITY_FLOW_UNSET,
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

    if booking_eligibility_client is _BOOKING_ELIGIBILITY_CLIENT_UNSET:
        resolved_eligibility_client = build_booking_eligibility_client(
            loaded_settings
        )
    else:
        resolved_eligibility_client = booking_eligibility_client

    if booking_eligibility_flow is _BOOKING_ELIGIBILITY_FLOW_UNSET:
        resolved_eligibility_flow = BookingEligibilityFlowService(
            resolved_eligibility_client  # type: ignore[arg-type]
        )
    else:
        resolved_eligibility_flow = booking_eligibility_flow

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()

    application = FastAPI(lifespan=lifespan)
    application.state.booking_eligibility_client = resolved_eligibility_client
    application.state.booking_eligibility_flow = resolved_eligibility_flow

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
