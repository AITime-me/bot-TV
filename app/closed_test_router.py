"""Closed-test HTTP router (BOT-CLOSED-TEST-01A / SELF-BOOKING-COMMAND-03I).

Registered only when ClosedTestConfig is fully enabled and DATABASE_URL exists.
Never delivers to real channels; never calls SyntheticOutboundAdapter.deliver.
PII admission is a separate pre-durability path — not ingress/Inbox/outbox.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.closed_test_config import (
    CLOSED_TEST_TOKEN_HEADER,
    ClosedTestConfig,
)
from app.core.ephemeral_pii_types import EphemeralPiiError
from app.core.pii_admission_mac_keys import EnvPiiAdmissionMacKeyProvider
from app.core.pii_admission_mac_types import PiiAdmissionMacError
from app.core.self_booking_pii_admission_types import PiiAdmissionError
from app.schemas.closed_test import (
    ClosedTestEventAck,
    ClosedTestEventCreate,
    ClosedTestEventStatus,
    ClosedTestPiiAdmissionAck,
    ClosedTestPiiAdmissionCreate,
)
from app.services.closed_test import (
    ClosedTestConversationNotFound,
    ClosedTestIdempotencyConflict,
    ClosedTestService,
)
from app.services.ephemeral_pii_store import build_ephemeral_pii_store_from_env
from app.services.ingress import IngressPersistError
from app.services.self_booking_pii_admission import SelfBookingPiiAdmissionService

CLOSED_TEST_PREFIX = "/internal/closed-test"


def install_closed_test_validation_handler(application: FastAPI) -> None:
    """Replace RequestValidationError detail for closed-test paths only.

    Prevents FastAPI default 422 bodies from echoing ``input`` / raw text.
    Other routes keep the stock validation handler.
    """

    @application.exception_handler(RequestValidationError)
    async def closed_test_safe_validation(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        path = request.url.path
        if path == CLOSED_TEST_PREFIX or path.startswith(CLOSED_TEST_PREFIX + "/"):
            return JSONResponse(
                status_code=422,
                content={"detail": "VALIDATION_ERROR"},
            )
        return await request_validation_exception_handler(request, exc)


def _build_pii_admission_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> SelfBookingPiiAdmissionService | None:
    """Compose admission deps from env. Incomplete wiring → None (503 at call)."""

    try:
        pii_store = build_ephemeral_pii_store_from_env(session_factory)
    except EphemeralPiiError:
        return None
    if pii_store is None:
        return None
    try:
        mac_provider = EnvPiiAdmissionMacKeyProvider()
        mac_provider.get_active_key()
    except PiiAdmissionMacError:
        return None
    return SelfBookingPiiAdmissionService(
        session_factory=session_factory,
        pii_store=pii_store,
        mac_key_provider=mac_provider,
    )


def build_closed_test_router(
    *,
    config: ClosedTestConfig,
    session_factory: async_sessionmaker[AsyncSession],
) -> APIRouter:
    config.require_runtime()
    service = ClosedTestService(session_factory)
    pii_admission = _build_pii_admission_service(session_factory)
    router = APIRouter(prefix=CLOSED_TEST_PREFIX, tags=["closed-test"])

    def _authorize(
        x_bot_closed_test_token: Annotated[
            str | None, Header(alias=CLOSED_TEST_TOKEN_HEADER)
        ] = None,
    ) -> None:
        if not config.verify_token(x_bot_closed_test_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="UNAUTHORIZED",
            )

    @router.post(
        "/events",
        response_model=ClosedTestEventAck,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(_authorize)],
    )
    async def post_event(body: ClosedTestEventCreate) -> ClosedTestEventAck:
        try:
            return await service.accept_event(body)
        except ClosedTestIdempotencyConflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="IDEMPOTENCY_CONFLICT",
            ) from None
        except IngressPersistError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="INGRESS_UNAVAILABLE",
            ) from None

    @router.get(
        "/events/{event_id}",
        response_model=ClosedTestEventStatus,
        dependencies=[Depends(_authorize)],
    )
    async def get_event(event_id: uuid.UUID) -> ClosedTestEventStatus:
        projection = await service.get_event_status(event_id)
        if projection is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="NOT_FOUND",
            )
        return projection

    @router.post(
        "/pii-admissions",
        response_model=ClosedTestPiiAdmissionAck,
        status_code=status.HTTP_200_OK,
        dependencies=[Depends(_authorize)],
    )
    async def post_pii_admission(
        body: ClosedTestPiiAdmissionCreate,
    ) -> ClosedTestPiiAdmissionAck:
        if pii_admission is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PII_ADMISSION_UNAVAILABLE",
            )
        try:
            return await service.admit_pii(body, admission=pii_admission)
        except ClosedTestConversationNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="CONVERSATION_NOT_FOUND",
            ) from None
        except PiiAdmissionError as exc:
            if exc.code == "PII_ADMISSION_CONFLICT":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="PII_ADMISSION_CONFLICT",
                ) from None
            if exc.code == "PII_ADMISSION_EXPIRED":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="REFRESH_REQUIRED",
                ) from None
            if exc.code == "PII_ADMISSION_INPUT_INVALID":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="PII_ADMISSION_INPUT_INVALID",
                ) from None
            if exc.code == "PII_ADMISSION_CONFIG_INVALID":
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="PII_ADMISSION_UNAVAILABLE",
                ) from None
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PII_ADMISSION_STORE_FAILED",
            ) from None

    return router
