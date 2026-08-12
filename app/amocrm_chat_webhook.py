"""amoCRM Chat manager webhook (AMO-01A).

Verify HMAC-SHA1 → durable ingress commit → ACK.
Handler does not apply manager FSM and does not perform outbound HTTP.

Validation errors are handled in-route (no global exception handler) so this
module cannot overwrite the closed-test RequestValidationError handler.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Header, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_chat_config import (
    AMOCRM_CHAT_SIGNATURE_HEADER,
    AmoCrmChatConfig,
)
from app.core.amocrm_chat_signature import verify_amocrm_chat_signature
from app.schemas.amocrm_manager_ingress import AmoCrmChatWebhookPayload
from app.services.amocrm_manager_ingress import (
    AmoCrmManagerIngressAdapter,
    IngressIdempotencyConflict,
)
from app.services.ingress import IngressPersistError

AMOCRM_CHAT_WEBHOOK_PATH = "/webhooks/amocrm/chat"


def build_amocrm_chat_router(
    *,
    config: AmoCrmChatConfig,
    session_factory: async_sessionmaker[AsyncSession],
) -> APIRouter:
    config.require_runtime()
    adapter = AmoCrmManagerIngressAdapter(session_factory)
    router = APIRouter(tags=["amocrm-chat"])

    @router.post(AMOCRM_CHAT_WEBHOOK_PATH)
    async def amocrm_chat_webhook(
        request: Request,
        x_signature: Annotated[
            str | None, Header(alias=AMOCRM_CHAT_SIGNATURE_HEADER)
        ] = None,
    ) -> PlainTextResponse:
        raw = await request.body()
        if not verify_amocrm_chat_signature(
            raw_body=raw,
            provided_signature=x_signature,
            config=config,
        ):
            return PlainTextResponse(
                content="unauthorized",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            payload_obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return PlainTextResponse(
                content="validation_error",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        try:
            payload = AmoCrmChatWebhookPayload.model_validate(payload_obj)
        except ValidationError:
            return PlainTextResponse(
                content="validation_error",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        try:
            await adapter.accept(payload.to_ingress_event())
        except IngressIdempotencyConflict:
            return PlainTextResponse(
                content="conflict",
                status_code=status.HTTP_409_CONFLICT,
            )
        except IngressPersistError:
            return PlainTextResponse(
                content="unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return PlainTextResponse(content="ok", status_code=status.HTTP_200_OK)

    return router
