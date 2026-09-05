"""Idempotent insert for native amoCRM outgoing CAPTURE rows."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.amocrm_native_outgoing_capture import AmocrmNativeOutgoingCapture
from app.schemas.amocrm_native_outgoing_capture import NativeOutgoingCaptureCandidate

__all__ = ("insert_capture_if_absent",)


async def insert_capture_if_absent(
    session: AsyncSession,
    *,
    candidate: NativeOutgoingCaptureCandidate,
    request_id: str | None,
    received_at: datetime | None = None,
) -> tuple[AmocrmNativeOutgoingCapture, bool]:
    """Insert one sanitized capture row. Returns (row, created)."""

    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "amocrm_message_id": candidate.amocrm_message_id,
        "talk_id": candidate.talk_id,
        "chat_id": candidate.chat_id,
        "contact_id": candidate.contact_id,
        "origin": candidate.origin,
        "source_id": candidate.source_id,
        "author_id": candidate.author_id,
        "author_type": candidate.author_type,
        "author_user_id": candidate.author_user_id,
        "recipient_id": candidate.recipient_id,
        "recipient_type": candidate.recipient_type,
        "type": candidate.outgoing_type,
        "message_type": candidate.message_type,
        "provider_created_at": candidate.provider_created_at,
        "account_id": candidate.account_id,
        "request_id": request_id,
    }
    if received_at is not None:
        values["received_at"] = received_at

    stmt = (
        insert(AmocrmNativeOutgoingCapture)
        .values(**values)
        .on_conflict_do_nothing(
            constraint="uq_amocrm_native_outgoing_captures_message_id"
        )
        .returning(AmocrmNativeOutgoingCapture.id)
    )
    inserted_id = await session.scalar(stmt)
    if inserted_id is not None:
        row = await session.get(AmocrmNativeOutgoingCapture, inserted_id)
        assert row is not None
        return row, True

    existing = await session.scalar(
        select(AmocrmNativeOutgoingCapture).where(
            AmocrmNativeOutgoingCapture.amocrm_message_id
            == candidate.amocrm_message_id
        )
    )
    if existing is None:
        raise RuntimeError("NATIVE_OUTGOING_CAPTURE_INSERT_RACE")
    return existing, False
