"""Repository for QA-only yandex_shadow_drafts. No commit; caller owns UoW.

Never logs generated_text or JSON bodies. First-write-wins per inbox message.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.yandex_shadow_draft import YandexShadowDraft


async def get_by_inbox_message_id(
    session: AsyncSession,
    *,
    inbox_message_id: uuid.UUID,
) -> YandexShadowDraft | None:
    stmt = select(YandexShadowDraft).where(
        YandexShadowDraft.inbox_message_id == inbox_message_id,
    )
    return await session.scalar(stmt)


async def get_latest_for_conversation(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
) -> YandexShadowDraft | None:
    stmt = (
        select(YandexShadowDraft)
        .where(YandexShadowDraft.conversation_id == conversation_id)
        .order_by(YandexShadowDraft.created_at.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def insert_if_absent(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
    inbox_message_id: uuid.UUID,
    conversation_id: uuid.UUID,
    disposition: str,
    reason_code: str,
    handoff_required: bool,
    generated_text: str | None,
    provenance_json: dict[str, Any],
    generation_metadata_json: dict[str, Any],
) -> YandexShadowDraft | None:
    """Insert shadow draft. Returns row when inserted, else None on conflict."""

    stmt = (
        insert(YandexShadowDraft)
        .values(
            id=row_id,
            inbox_message_id=inbox_message_id,
            conversation_id=conversation_id,
            disposition=disposition,
            reason_code=reason_code,
            handoff_required=handoff_required,
            generated_text=generated_text,
            provenance_json=provenance_json,
            generation_metadata_json=generation_metadata_json,
        )
        .on_conflict_do_nothing(
            constraint="uq_yandex_shadow_drafts_inbox_message_id",
        )
        .returning(YandexShadowDraft)
    )
    return await session.scalar(stmt)
