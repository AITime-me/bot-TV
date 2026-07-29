from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.clock import db_statement_now
from app.db.session import session_scope
from app.models.conversation import Conversation, HandoffState
from app.models.reply_plan import ReplyPlan, ReplyPlanStatus, ReplyPlanType
from app.repositories import conversations as conversation_repo
from app.repositories import outbound as outbound_repo
from app.repositories import reply_plans as reply_plan_repo


class HandoffExpiryTransition(str, enum.Enum):
    HUMAN_ACTIVE_TO_BOT = "HUMAN_ACTIVE_TO_BOT"
    HUMAN_PAUSE_TO_BOT = "HUMAN_PAUSE_TO_BOT"
    QUARANTINED = "QUARANTINED"


class HandoffExpiryInvariantError(RuntimeError):
    """Fail closed when a paused dialog lacks its current deferred plan."""


@dataclass(frozen=True)
class HandoffExpiryResult:
    conversation_id: uuid.UUID
    transition: HandoffExpiryTransition
    active_reply_plan_id: uuid.UUID | None
    cancelled_plans: int
    cancelled_outbound: int
    quarantine_reason: str | None = None


class HandoffExpiryWorker:
    """Atomically returns due manager handoffs to BOT_ACTIVE.

    The due Conversation row is locked with ``FOR UPDATE SKIP LOCKED``. The
    worker has no in-memory lease or deadline: PostgreSQL is the clock and the
    durable row is the recovery checkpoint across process restarts.

    Poisoned HUMAN_PAUSE/unsupported rows are quarantined in the same
    transaction and never auto-returned to BOT_ACTIVE. Expected invariant
    failures are not raised to WorkerRuntime.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def expire_one(self) -> HandoffExpiryResult | None:
        async with session_scope(self._session_factory) as session:
            conversation = await conversation_repo.claim_next_due_handoff(session)
            if conversation is None:
                return None

            moment = await db_statement_now(session)
            try:
                if conversation.handoff_state == HandoffState.HUMAN_ACTIVE.value:
                    cancelled_plans = (
                        await reply_plan_repo.cancel_open_plans_for_takeover(
                            session,
                            conversation_id=conversation.id,
                            reason="HANDOFF_EXPIRED_NO_CLIENT",
                        )
                    )
                    cancelled_outbound = (
                        await outbound_repo.cancel_unadmitted_for_manager_message(
                            session,
                            conversation_id=conversation.id,
                        )
                    )
                    conversation = await conversation_repo.return_due_handoff_to_bot(
                        session,
                        conversation=conversation,
                        moment=moment,
                        active_reply_plan_id=None,
                    )
                    return HandoffExpiryResult(
                        conversation_id=conversation.id,
                        transition=HandoffExpiryTransition.HUMAN_ACTIVE_TO_BOT,
                        active_reply_plan_id=None,
                        cancelled_plans=cancelled_plans,
                        cancelled_outbound=cancelled_outbound,
                    )

                if conversation.handoff_state == HandoffState.HUMAN_PAUSE.value:
                    plan = await _lock_current_deferred_plan(session, conversation)
                    conversation = await conversation_repo.return_due_handoff_to_bot(
                        session,
                        conversation=conversation,
                        moment=moment,
                        active_reply_plan_id=plan.id,
                    )
                    return HandoffExpiryResult(
                        conversation_id=conversation.id,
                        transition=HandoffExpiryTransition.HUMAN_PAUSE_TO_BOT,
                        active_reply_plan_id=plan.id,
                        cancelled_plans=0,
                        cancelled_outbound=0,
                    )

                raise HandoffExpiryInvariantError("HANDOFF_EXPIRY_UNSUPPORTED_STATE")
            except HandoffExpiryInvariantError as exc:
                reason_code = str(exc)
                conversation = await conversation_repo.quarantine_due_handoff_expiry(
                    session,
                    conversation=conversation,
                    reason_code=reason_code,
                    moment=moment,
                )
                return HandoffExpiryResult(
                    conversation_id=conversation.id,
                    transition=HandoffExpiryTransition.QUARANTINED,
                    active_reply_plan_id=None,
                    cancelled_plans=0,
                    cancelled_outbound=0,
                    quarantine_reason=reason_code,
                )

    async def tick(self, *, max_items: int = 100) -> list[HandoffExpiryResult]:
        """Drain at most ``max_items`` due rows, one transaction per dialog."""
        if max_items <= 0:
            raise ValueError("max_items must be positive")
        results: list[HandoffExpiryResult] = []
        for _ in range(max_items):
            result = await self.expire_one()
            if result is None:
                break
            results.append(result)
        return results


async def _lock_current_deferred_plan(
    session: AsyncSession,
    conversation: Conversation,
) -> ReplyPlan:
    conversation_id = conversation.id
    plan_id = conversation.active_reply_plan_id
    if not isinstance(plan_id, uuid.UUID):
        raise HandoffExpiryInvariantError("HANDOFF_DEFERRED_PLAN_MISSING")

    plan = await session.get(ReplyPlan, plan_id, with_for_update=True)
    if plan is None or plan.conversation_id != conversation_id:
        raise HandoffExpiryInvariantError("HANDOFF_DEFERRED_PLAN_MISSING")
    if plan.plan_type != ReplyPlanType.CLIENT_REPLY.value:
        raise HandoffExpiryInvariantError("HANDOFF_DEFERRED_PLAN_TYPE")
    if plan.status not in {
        ReplyPlanStatus.PENDING.value,
        ReplyPlanStatus.READY.value,
    }:
        raise HandoffExpiryInvariantError("HANDOFF_DEFERRED_PLAN_NOT_OPEN")
    if plan.context_version != conversation.context_version:
        raise HandoffExpiryInvariantError("HANDOFF_DEFERRED_PLAN_CONTEXT")
    if plan.manager_epoch != conversation.manager_epoch:
        raise HandoffExpiryInvariantError("HANDOFF_DEFERRED_PLAN_MANAGER_EPOCH")
    if plan.event_seq_hwm != conversation.current_event_seq:
        raise HandoffExpiryInvariantError("HANDOFF_DEFERRED_PLAN_EVENT_SEQ")
    if plan.not_before != conversation.handoff_deadline_at:
        raise HandoffExpiryInvariantError("HANDOFF_DEFERRED_PLAN_DEADLINE")
    if plan.payload_json.get("deferred_for_handoff") is not True:
        raise HandoffExpiryInvariantError("HANDOFF_DEFERRED_PLAN_MARKER")
    return plan
