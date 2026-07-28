from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import session_scope
from app.models.conversation import Conversation
from app.repositories import conversations as conversation_repo
from app.repositories import reply_plans as reply_plan_repo


@dataclass(frozen=True)
class ManagerTakeoverResult:
    conversation_id: uuid.UUID
    changed: bool
    cancelled_plans: int
    ownership: str
    status: str


class ManagerTakeoverService:
    """Synthetic manager-takeover contract. No amoCRM/panel/channel events."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def apply(
        self,
        conversation_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> ManagerTakeoverResult:
        async with session_scope(self._session_factory) as session:
            conversation, changed = await conversation_repo.apply_manager_takeover(
                session,
                conversation_id=conversation_id,
                now=now,
            )
            cancelled = await reply_plan_repo.cancel_open_plans_for_takeover(
                session,
                conversation_id=conversation_id,
            )
            return ManagerTakeoverResult(
                conversation_id=conversation.id,
                changed=changed,
                cancelled_plans=cancelled,
                ownership=conversation.ownership,
                status=conversation.status,
            )


async def apply_manager_takeover_in_session(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    now: datetime | None = None,
) -> tuple[Conversation, int, bool]:
    """Session-scoped takeover for tests/transactions already open."""
    conversation, changed = await conversation_repo.apply_manager_takeover(
        session,
        conversation_id=conversation_id,
        now=now,
    )
    cancelled = await reply_plan_repo.cancel_open_plans_for_takeover(
        session,
        conversation_id=conversation_id,
    )
    return conversation, cancelled, changed
