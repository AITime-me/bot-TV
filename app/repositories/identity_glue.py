"""Persistence for IR-1 identity review cases + conversation canonical attach."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity_glue import (
    IdentityReviewCaseRecord,
    IdentityReviewCaseStatus,
    require_identity_review_reason_code,
)
from app.db.clock import db_statement_now
from app.models.conversation import Conversation
from app.models.identity_review_case import IdentityReviewCase

__all__ = (
    "as_review_record",
    "get_open_review",
    "get_review_by_id_for_update",
    "insert_open_review_idempotent",
    "list_open_reviews",
    "mark_review_resolved",
    "set_conversation_canonical_identity",
)


def as_review_record(row: IdentityReviewCase) -> IdentityReviewCaseRecord:
    return IdentityReviewCaseRecord(
        id=uuid.UUID(str(row.id)),
        conversation_id=uuid.UUID(str(row.conversation_id)),
        reason_code=str(row.reason_code),
        status=str(row.status),
        proposed_canonical_identity_id=(
            uuid.UUID(str(row.proposed_canonical_identity_id))
            if row.proposed_canonical_identity_id is not None
            else None
        ),
        resolved_canonical_identity_id=(
            uuid.UUID(str(row.resolved_canonical_identity_id))
            if row.resolved_canonical_identity_id is not None
            else None
        ),
    )


async def get_open_review(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    reason_code: object,
) -> IdentityReviewCase | None:
    code = require_identity_review_reason_code(reason_code)
    stmt = select(IdentityReviewCase).where(
        IdentityReviewCase.conversation_id == conversation_id,
        IdentityReviewCase.reason_code == code,
        IdentityReviewCase.status == IdentityReviewCaseStatus.OPEN.value,
    )
    return await session.scalar(stmt)


async def get_review_by_id_for_update(
    session: AsyncSession,
    *,
    review_case_id: uuid.UUID,
) -> IdentityReviewCase | None:
    stmt = (
        select(IdentityReviewCase)
        .where(IdentityReviewCase.id == review_case_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def list_open_reviews(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID | None = None,
) -> list[IdentityReviewCase]:
    stmt = select(IdentityReviewCase).where(
        IdentityReviewCase.status == IdentityReviewCaseStatus.OPEN.value
    )
    if conversation_id is not None:
        stmt = stmt.where(IdentityReviewCase.conversation_id == conversation_id)
    stmt = stmt.order_by(
        IdentityReviewCase.created_at.asc(),
        IdentityReviewCase.id.asc(),
    )
    result = await session.scalars(stmt)
    return list(result.all())


async def insert_open_review_idempotent(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    reason_code: object,
    proposed_canonical_identity_id: uuid.UUID | None = None,
) -> tuple[IdentityReviewCase, bool]:
    """Insert OPEN review; on unique race re-read without aborting the UoW."""

    code = require_identity_review_reason_code(reason_code)
    existing = await get_open_review(
        session,
        conversation_id=conversation_id,
        reason_code=code,
    )
    if existing is not None:
        return existing, False

    now = await db_statement_now(session)
    row = IdentityReviewCase(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        reason_code=code,
        status=IdentityReviewCaseStatus.OPEN.value,
        proposed_canonical_identity_id=proposed_canonical_identity_id,
        resolved_canonical_identity_id=None,
        created_at=now,
        resolved_at=None,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
        return row, True
    except IntegrityError:
        raced = await get_open_review(
            session,
            conversation_id=conversation_id,
            reason_code=code,
        )
        if raced is None:
            raise
        return raced, False


async def mark_review_resolved(
    session: AsyncSession,
    *,
    row: IdentityReviewCase,
    resolved_canonical_identity_id: uuid.UUID,
    now: datetime | None = None,
) -> IdentityReviewCase:
    resolved_at = now if now is not None else await db_statement_now(session)
    row.status = IdentityReviewCaseStatus.RESOLVED.value
    row.resolved_canonical_identity_id = resolved_canonical_identity_id
    row.resolved_at = resolved_at
    await session.flush()
    return row


async def set_conversation_canonical_identity(
    session: AsyncSession,
    *,
    conversation: Conversation,
    canonical_identity_id: uuid.UUID,
) -> Conversation:
    conversation.canonical_identity_id = canonical_identity_id
    await session.flush()
    return conversation
