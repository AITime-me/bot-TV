"""Repository for self_booking_active_offers. No commit; caller owns UoW."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.self_booking_active_offer import SelfBookingActiveOffer


async def get_by_conversation(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
) -> SelfBookingActiveOffer | None:
    return await session.get(SelfBookingActiveOffer, conversation_id)


async def upsert_if_newer_or_same_outbound(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    source_outbound_id: uuid.UUID,
    source_context_version: int,
    source_manager_epoch: int,
    source_event_seq_hwm: int,
    offered_slots: list[dict[str, Any]],
    now: datetime,
) -> str:
    """Insert or replace when newer fence / same outbound. Returns action tag.

    Returns:
      ``activated`` — first row for conversation
      ``replaced`` — overwritten by strictly newer fence
      ``replayed`` — same ``source_outbound_id`` already active
      ``ignored_stale`` — existing row is newer than candidate
    """

    existing = await get_by_conversation(session, conversation_id=conversation_id)
    if existing is not None and existing.source_outbound_id == source_outbound_id:
        return "replayed"

    stmt = (
        insert(SelfBookingActiveOffer)
        .values(
            conversation_id=conversation_id,
            source_outbound_id=source_outbound_id,
            source_context_version=source_context_version,
            source_manager_epoch=source_manager_epoch,
            source_event_seq_hwm=source_event_seq_hwm,
            offered_slots=offered_slots,
            activated_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[SelfBookingActiveOffer.conversation_id],
            set_={
                "source_outbound_id": source_outbound_id,
                "source_context_version": source_context_version,
                "source_manager_epoch": source_manager_epoch,
                "source_event_seq_hwm": source_event_seq_hwm,
                "offered_slots": offered_slots,
                "activated_at": now,
                "updated_at": now,
            },
            where=text(
                "("
                "self_booking_active_offers.source_manager_epoch, "
                "self_booking_active_offers.source_context_version, "
                "self_booking_active_offers.source_event_seq_hwm"
                ") < ("
                "EXCLUDED.source_manager_epoch, "
                "EXCLUDED.source_context_version, "
                "EXCLUDED.source_event_seq_hwm"
                ")"
            ),
        )
        .returning(SelfBookingActiveOffer.source_outbound_id)
    )
    # SAVEPOINT so unique conflicts on source_outbound_id do not abort UoW.
    async with session.begin_nested():
        result = await session.execute(stmt)
        returned = result.scalar_one_or_none()

    if returned is None:
        return "ignored_stale"
    if existing is None:
        return "activated"
    return "replaced"


async def delete_by_conversation(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
) -> bool:
    stmt = delete(SelfBookingActiveOffer).where(
        SelfBookingActiveOffer.conversation_id == conversation_id
    )
    result = await session.execute(stmt)
    await session.flush()
    return bool(result.rowcount and result.rowcount == 1)
