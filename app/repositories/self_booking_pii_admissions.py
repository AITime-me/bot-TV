"""Repository for self_booking_pii_admissions. No commit; caller owns UoW."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.self_booking_pii_admission import SelfBookingPiiAdmission


async def get_by_request(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    request_id: str,
) -> SelfBookingPiiAdmission | None:
    stmt = select(SelfBookingPiiAdmission).where(
        SelfBookingPiiAdmission.conversation_id == conversation_id,
        SelfBookingPiiAdmission.request_id == request_id,
    )
    return await session.scalar(stmt)


async def insert_if_absent(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
    conversation_id: uuid.UUID,
    request_id: str,
    phone_ref_token: str,
    name_ref_token: str,
    content_mac: bytes,
    mac_key_id: str,
) -> SelfBookingPiiAdmission | None:
    """Insert admission map row. Returns row when inserted, else None on conflict."""

    stmt = (
        insert(SelfBookingPiiAdmission)
        .values(
            id=row_id,
            conversation_id=conversation_id,
            request_id=request_id,
            phone_ref_token=phone_ref_token,
            name_ref_token=name_ref_token,
            content_mac=content_mac,
            mac_key_id=mac_key_id,
            created_at=func.statement_timestamp(),
        )
        .on_conflict_do_nothing(
            index_elements=[
                SelfBookingPiiAdmission.conversation_id,
                SelfBookingPiiAdmission.request_id,
            ],
        )
        .returning(SelfBookingPiiAdmission)
    )
    return await session.scalar(stmt)
