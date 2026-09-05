from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, AsyncSession

from app.amocrm_chat_webhook import build_amocrm_chat_router
from app.amocrm_native_outgoing_capture_webhook import (
    build_amocrm_native_outgoing_capture_router,
)
from app.channels.vk_client_config import VkClientCallbackConfig, VkClientConfigError
from app.channels.vk_client_http import build_vk_client_router
from app.channels.vk_master_config import VkMasterAdapterConfig, VkMasterConfigError
from app.channels.vk_master_http import NullVkMasterSender, VkMasterHttpSender
from app.teya_ops_router import build_teya_ops_router, load_teya_ops_config
from app.closed_test_router import (
    build_closed_test_router,
    install_closed_test_validation_handler,
)
from app.config import Settings
from app.core.amocrm_chat_config import AmoCrmChatConfig, AmoCrmChatConfigError
from app.core.amocrm_native_outgoing_capture_config import (
    AmoCrmNativeOutgoingCaptureConfig,
    AmoCrmNativeOutgoingCaptureConfigError,
)
from app.core.booking_eligibility_factory import (
    build_booking_flow_from_settings,
    build_booking_s2s_clients,
    build_master_command_client,
    rebind_availability_client_to_runtime_settings,
    rebind_booking_flow_to_runtime_settings,
    rebind_eligibility_client_to_runtime_settings,
)
from app.core.booking_eligibility_http import BookingEligibilityHttpClient
from app.core.closed_test_config import ClosedTestConfig, ClosedTestConfigError
from app.core.ephemeral_pii_types import EphemeralPiiError
from app.core.outbound_policy import (
    OutboundAction,
    is_automatic_outbound_allowed,
)
from app.db.session import create_engine, create_session_factory
from app.services.booking_eligibility_flow import BookingEligibilityFlowService
from app.services.booking_flow import BookingFlowService
from app.services.ephemeral_pii_store import build_ephemeral_pii_store_from_env
from app.services.vk_master_adapter import VkMasterAdapterService
from app.services.worker_health import WorkerHealthService

_BOOKING_ELIGIBILITY_CLIENT_UNSET: Final[object] = object()
_BOOKING_ELIGIBILITY_FLOW_UNSET: Final[object] = object()
_BOOKING_FLOW_UNSET: Final[object] = object()

logger = logging.getLogger(__name__)
_VK_WIRING_LOG: Final[frozenset[str]] = frozenset(
    {
        "VK_MASTER_PII_UNAVAILABLE",
    }
)


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
    booking_flow: BookingFlowService | None | object = _BOOKING_FLOW_UNSET,
) -> FastAPI:
    """Compose the API app.

    Lower booking dependencies (HTTP clients, eligibility flow) are built
    locally for ``BookingFlowService`` only. ``application.state`` exposes
    ``booking_flow`` alone — the prepared application boundary for a future
    channel-wiring gate. Raw eligibility/availability clients/flows are not
    published on ``app.state``.
    """

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

    if booking_flow is _BOOKING_FLOW_UNSET:
        if (
            booking_eligibility_client is _BOOKING_ELIGIBILITY_CLIENT_UNSET
            and booking_eligibility_flow is _BOOKING_ELIGIBILITY_FLOW_UNSET
        ):
            resolved_booking_flow = build_booking_flow_from_settings(loaded_settings)
        else:
            if booking_eligibility_client is _BOOKING_ELIGIBILITY_CLIENT_UNSET:
                clients = build_booking_s2s_clients(loaded_settings)
                resolved_eligibility_client = clients.eligibility
                resolved_availability_client = clients.availability
                resolved_booking_create_client = clients.booking_create
            else:
                # Runtime Settings win over any injected live HTTP policy.
                resolved_eligibility_client = (
                    rebind_eligibility_client_to_runtime_settings(
                        loaded_settings,
                        booking_eligibility_client,
                    )
                )
                resolved_availability_client = None
                resolved_booking_create_client = None

            if booking_eligibility_flow is _BOOKING_ELIGIBILITY_FLOW_UNSET:
                resolved_eligibility_flow = BookingEligibilityFlowService(
                    resolved_eligibility_client  # type: ignore[arg-type]
                )
            else:
                # Rebind live HTTP client inside an injected eligibility flow.
                if isinstance(
                    booking_eligibility_flow, BookingEligibilityFlowService
                ):
                    bound_client = rebind_eligibility_client_to_runtime_settings(
                        loaded_settings,
                        booking_eligibility_flow._client,  # noqa: SLF001
                    )
                    if bound_client is booking_eligibility_flow._client:  # noqa: SLF001
                        resolved_eligibility_flow = booking_eligibility_flow
                    else:
                        resolved_eligibility_flow = BookingEligibilityFlowService(
                            bound_client  # type: ignore[arg-type]
                        )
                else:
                    resolved_eligibility_flow = booking_eligibility_flow

            resolved_availability_client = (
                rebind_availability_client_to_runtime_settings(
                    loaded_settings,
                    resolved_availability_client,
                )
            )
            resolved_booking_flow = BookingFlowService(
                resolved_eligibility_flow,  # type: ignore[arg-type]
                resolved_availability_client,  # type: ignore[arg-type]
                resolved_booking_create_client,  # type: ignore[arg-type]
            )
    elif booking_flow is None:
        # Never publish None: fail-closed consumer with unset eligibility flow.
        resolved_booking_flow = BookingFlowService(None)
    else:
        resolved_booking_flow = rebind_booking_flow_to_runtime_settings(
            loaded_settings,
            booking_flow,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()

    application = FastAPI(lifespan=lifespan)
    application.state.booking_flow = resolved_booking_flow

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

    # CURSOR-29: VK master Callback — registered only with complete callback
    # config + database. Business execution remains default-off / safety-gated.
    _register_vk_master_route(
        application,
        settings=loaded_settings,
        engine=engine,
    )

    # VK CLIENT shadow observer Callback — separate from master; default-off.
    _register_vk_client_route(
        application,
        settings=loaded_settings,
        engine=engine,
    )

    # BOT-CLOSED-TEST-01A: synthetic closed-test HTTP surface (default-off).
    _register_closed_test_routes(
        application,
        settings=loaded_settings,
        engine=engine,
    )

    # Teya reliability ops snapshot (default-off; token-gated).
    _register_teya_ops_routes(
        application,
        engine=engine,
    )

    # AMO-01A: amoCRM Chat manager webhook → durable ingress (default-off).
    _register_amocrm_chat_routes(
        application,
        settings=loaded_settings,
        engine=engine,
    )

    # Native CRM Platform outgoing_message CAPTURE-ONLY (default-off).
    _register_amocrm_native_outgoing_capture_routes(
        application,
        settings=loaded_settings,
        engine=engine,
    )

    return application


def _register_teya_ops_routes(
    application: FastAPI,
    *,
    engine: AsyncEngine | None,
) -> None:
    config = load_teya_ops_config()
    if config is None or engine is None:
        return
    session_factory = create_session_factory(engine)
    application.include_router(
        build_teya_ops_router(config=config, session_factory=session_factory)
    )


def _register_closed_test_routes(
    application: FastAPI,
    *,
    settings: Settings,
    engine: AsyncEngine | None,
) -> None:
    """Register `/internal/closed-test` only when fully configured.

    Enabled + incomplete/invalid config fails closed (raises). Disabled → no
    routes. Never registers a half-active surface.
    """

    config = ClosedTestConfig.from_env()
    if not config.enabled:
        return
    if engine is None or settings.database_url is None:
        raise ClosedTestConfigError("CLOSED_TEST_DATABASE_REQUIRED") from None

    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    application.include_router(
        build_closed_test_router(config=config, session_factory=session_factory)
    )
    install_closed_test_validation_handler(application)


def _register_amocrm_chat_routes(
    application: FastAPI,
    *,
    settings: Settings,
    engine: AsyncEngine | None,
) -> None:
    """Register `/webhooks/amocrm/chat/{scope_id}` only when fully configured.

    Enabled + incomplete/invalid config fails closed (raises). Disabled → no
    routes. Never registers a half-active surface. No outbound amoCRM HTTP.
    """

    config = AmoCrmChatConfig.from_env()
    if not config.enabled:
        return
    if engine is None or settings.database_url is None:
        raise AmoCrmChatConfigError("AMOCRM_CHAT_DATABASE_REQUIRED") from None

    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    application.include_router(
        build_amocrm_chat_router(config=config, session_factory=session_factory)
    )


def _register_amocrm_native_outgoing_capture_routes(
    application: FastAPI,
    *,
    settings: Settings,
    engine: AsyncEngine | None,
) -> None:
    """Register native outgoing CAPTURE webhook only when fully configured.

    Enabled + incomplete/invalid config fails closed (raises). Disabled → no
    routes. Path token is ephemeral URL secrecy only — not Chat HMAC.
    """

    config = AmoCrmNativeOutgoingCaptureConfig.from_env()
    if not config.enabled:
        return
    if engine is None or settings.database_url is None:
        raise AmoCrmNativeOutgoingCaptureConfigError(
            "AMOCRM_NATIVE_OUTGOING_CAPTURE_DATABASE_REQUIRED"
        ) from None

    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    application.include_router(
        build_amocrm_native_outgoing_capture_router(
            config=config,
            session_factory=session_factory,
        )
    )


def _register_vk_client_route(
    application: FastAPI,
    *,
    settings: Settings,
    engine: AsyncEngine | None,
) -> None:
    """Register `/webhooks/vk/client` only when enabled and fully configured.

    Disabled → no route. Enabled + incomplete/invalid config fails closed (raises).
    Secrets stay on the API surface only; worker does not need callback config.
    """

    config = VkClientCallbackConfig.from_env()
    if not config.enabled:
        return
    if engine is None or settings.database_url is None:
        raise VkClientConfigError("VK_CLIENT_DATABASE_REQUIRED") from None

    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    application.include_router(
        build_vk_client_router(config=config, session_factory=session_factory)
    )


def _register_vk_master_route(
    application: FastAPI,
    *,
    settings: Settings,
    engine: AsyncEngine | None,
) -> None:
    try:
        vk_config = VkMasterAdapterConfig.from_env()
    except VkMasterConfigError:
        return
    if not vk_config.callback_config_complete():
        return

    if engine is None:
        # Callback config only: confirmation handshake; no business execution.
        from app.channels.vk_master_webhook import parse_vk_master_callback
        from app.channels.vk_master_types import VkMasterWebhookKind

        @application.post("/webhooks/vk/master")
        async def vk_master_webhook_confirmation_only(request: Request) -> Response:
            raw = await request.body()
            parsed = parse_vk_master_callback(raw, config=vk_config)
            if parsed.kind is VkMasterWebhookKind.CONFIRMATION:
                assert parsed.confirmation_response is not None
                return PlainTextResponse(content=parsed.confirmation_response)
            return PlainTextResponse(content="ok")

        return

    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    master_client = build_master_command_client(settings)
    if vk_config.runtime_config_complete():
        sender: NullVkMasterSender | VkMasterHttpSender = VkMasterHttpSender(vk_config)
    else:
        sender = NullVkMasterSender()
    adapter = build_vk_master_adapter_service(
        session_factory,
        settings=settings,
        vk_config=vk_config,
        master_client=master_client,
        sender=sender,
    )

    @application.post("/webhooks/vk/master")
    async def vk_master_webhook(request: Request) -> Response:
        raw = await request.body()
        result = await adapter.handle_callback(raw)
        return PlainTextResponse(content=result.body, status_code=result.status_code)


def build_vk_master_adapter_service(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    vk_config: VkMasterAdapterConfig,
    master_client: object,
    sender: NullVkMasterSender | VkMasterHttpSender,
    environ: Mapping[str, str] | None = None,
) -> VkMasterAdapterService:
    """Production VK adapter wiring (injects real EphemeralPiiStore when configured).

    Invalid/partial ``EPHEMERAL_PII_*`` must not abort API startup: catch at this
    boundary, degrade to ``pii_store=None`` (CREATE_BOOKING unavailable), keep
    confirmation + non-PII commands. Canonical factory stays strict.
    """

    try:
        pii_store = build_ephemeral_pii_store_from_env(
            session_factory, environ=environ
        )
    except EphemeralPiiError:
        # Constant code only — never log exception / key material / env values.
        _log_vk_wiring("VK_MASTER_PII_UNAVAILABLE")
        pii_store = None
    return VkMasterAdapterService(
        session_factory,
        settings=settings,
        config=vk_config,
        master_client=master_client,  # type: ignore[arg-type]
        pii_store=pii_store,
        sender=sender,
    )


def _log_vk_wiring(event: str) -> None:
    if event not in _VK_WIRING_LOG:
        return
    try:
        logger.warning("%s", event)
    except Exception:
        return


app = create_app()
