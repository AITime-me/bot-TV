from fastapi import FastAPI

from app.config import Settings
from app.core.outbound_policy import (
    OutboundAction,
    is_automatic_outbound_allowed,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    loaded_settings = settings if settings is not None else Settings.from_env()
    application = FastAPI()

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready")
    def health_ready() -> dict[str, str | bool]:
        outbound_enabled = is_automatic_outbound_allowed(
            loaded_settings,
            OutboundAction.SEND_MESSAGE,
        )
        return {
            "status": "ready",
            "bot_mode": loaded_settings.bot_mode.value,
            "emergency_lock": loaded_settings.emergency_lock,
            "outbound_enabled": outbound_enabled,
        }

    return application


app = create_app()
