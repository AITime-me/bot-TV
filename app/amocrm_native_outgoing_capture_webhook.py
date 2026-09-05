"""Native amoCRM CRM Platform outgoing_message CAPTURE webhook.

Path-token URL secrecy only (no Chat HMAC / X-Signature). CAPTURE-ONLY:
validate → sanitize → insert. No FSM, no VK, no Chat bindings/projections.

Do not log request URL/path (uvicorn access log may already contain path_token;
application code must not add path/token logging).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_native_outgoing_capture_config import (
    AmoCrmNativeOutgoingCaptureConfig,
)
from app.db.session import session_scope
from app.repositories import amocrm_native_outgoing_captures as capture_repo
from app.schemas.amocrm_native_outgoing_capture import (
    extract_outgoing_message_adds,
    parse_native_outgoing_form_body,
)

AMOCRM_NATIVE_OUTGOING_CAPTURE_PATH = (
    "/webhooks/amocrm/native-outgoing/{path_token}"
)
_REQUEST_ID_HEADER = "X-Amocrm-Requestid"
_REQUEST_ID_MAX = 128


def amocrm_native_outgoing_capture_path(path_token: str) -> str:
    """Concrete POST path for a configured ephemeral path token."""

    return f"/webhooks/amocrm/native-outgoing/{path_token}"


def _safe_request_id(value: str | None) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value:
        return None
    stripped = value.strip()
    if not stripped or len(stripped) > _REQUEST_ID_MAX:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in stripped):
        return None
    if any(ch.isspace() for ch in stripped):
        return None
    return stripped


def build_amocrm_native_outgoing_capture_router(
    *,
    config: AmoCrmNativeOutgoingCaptureConfig,
    session_factory: async_sessionmaker[AsyncSession],
) -> APIRouter:
    config.require_runtime()
    router = APIRouter(tags=["amocrm-native-outgoing-capture"])

    @router.post(AMOCRM_NATIVE_OUTGOING_CAPTURE_PATH)
    async def amocrm_native_outgoing_capture_webhook(
        request: Request,
        path_token: str,
        x_amocrm_requestid: Annotated[
            str | None, Header(alias=_REQUEST_ID_HEADER)
        ] = None,
    ) -> PlainTextResponse:
        if not config.matches_path_token(path_token):
            return PlainTextResponse(
                content="unauthorized",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        raw = await request.body()
        form = parse_native_outgoing_form_body(raw)
        candidates = extract_outgoing_message_adds(form)
        request_id = _safe_request_id(x_amocrm_requestid)

        for candidate in candidates:
            if not config.matches_allowlist(
                talk_id=candidate.talk_id,
                chat_id=candidate.chat_id,
                contact_id=candidate.contact_id,
                origin=candidate.origin,
                source_id=candidate.source_id,
            ):
                continue
            async with session_scope(session_factory) as session:
                await capture_repo.insert_capture_if_absent(
                    session,
                    candidate=candidate,
                    request_id=request_id,
                )

        return PlainTextResponse(content="ok", status_code=status.HTTP_200_OK)

    return router
