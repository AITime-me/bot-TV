"""Closed-test HTTP router (BOT-CLOSED-TEST-01A).

Registered only when ClosedTestConfig is fully enabled and DATABASE_URL exists.
Never delivers to real channels; never calls SyntheticOutboundAdapter.deliver.
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
from app.schemas.closed_test import (
    ClosedTestEventAck,
    ClosedTestEventCreate,
    ClosedTestEventStatus,
)
from app.services.closed_test import (
    ClosedTestIdempotencyConflict,
    ClosedTestService,
)
from app.services.ingress import IngressPersistError

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


def build_closed_test_router(
    *,
    config: ClosedTestConfig,
    session_factory: async_sessionmaker[AsyncSession],
) -> APIRouter:
    config.require_runtime()
    service = ClosedTestService(session_factory)
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

    return router
