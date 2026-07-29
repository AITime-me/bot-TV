from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.clock import resolve_moment
from app.db.session import session_scope
from app.models.conversation import ConversationOwnership, HandoffState
from app.models.reply_plan import ReplyPlan, ReplyPlanStatus
from app.repositories import conversations as conversation_repo
from app.repositories import outbound as outbound_repo
from app.repositories import reply_plans as reply_plan_repo
from app.repositories.outbound import OutboundClaim
from app.repositories.reply_plans import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_RETRY_DELAY_SECONDS,
    ReplyPlanClaim,
    StaleReplyPlanLeaseError,
)
from app.services.amocrm_mirror import enqueue_reply_plan_state_changed
from app.services.outbound_arbiter import ArbiterAdmitResult, OutboundArbiter


@dataclass(frozen=True, repr=False)
class ReplyPlanDispatchResult:
    plan_id: uuid.UUID
    plan_status: str
    outbound_id: uuid.UUID
    outbound_created: bool

    def __repr__(self) -> str:
        return (
            f"ReplyPlanDispatchResult(plan_id={self.plan_id!r}, "
            f"plan_status={self.plan_status!r}, "
            f"outbound_id={self.outbound_id!r}, "
            f"outbound_created={self.outbound_created!r})"
        )


class ReplyPlanWorker:
    """Claims due ReplyPlans and creates exactly one SYNTHETIC_OUTBOUND row."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        worker_id: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._retry_delay_seconds = retry_delay_seconds

    async def claim_one(self, *, now: datetime | None = None) -> ReplyPlanClaim | None:
        async with session_scope(self._session_factory) as session:
            moment = await resolve_moment(session, now)
            exhausted = await reply_plan_repo.find_exhausted_lease(
                session,
                now=moment,
            )
            if exhausted is not None:
                # Recovery preserves the dialog lock order and emits the same
                # terminal mirror fact as an explicit final failure.
                await conversation_repo.lock_for_update(
                    session,
                    conversation_id=exhausted.conversation_id,
                )
                recovered = await reply_plan_repo.recover_exhausted_lease(
                    session,
                    plan_id=exhausted.plan_id,
                    now=moment,
                )
                if recovered is not None:
                    await enqueue_reply_plan_state_changed(
                        session,
                        conversation_id=recovered.conversation_id,
                        plan_id=recovered.id,
                        plan_status=recovered.status,
                        context_version=recovered.context_version,
                        correlation_id=recovered.correlation_id,
                    )
            return await reply_plan_repo.claim_next(
                session,
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
                now=moment,
            )

    async def dispatch_claimed(
        self,
        claim: ReplyPlanClaim,
    ) -> ReplyPlanDispatchResult:
        try:
            async with session_scope(self._session_factory) as session:
                # Lock order: Conversation FOR UPDATE first, before any INSERT
                # that takes FOR KEY SHARE on conversations/reply_plans and
                # before exclusive updates to the ReplyPlan row.
                conversation = await conversation_repo.lock_for_update(
                    session,
                    conversation_id=claim.conversation_id,
                )
                if conversation.context_version != claim.context_version:
                    raise StaleReplyPlanLeaseError("REPLY_PLAN_STALE_CONTEXT")
                if conversation.ownership != ConversationOwnership.BOT.value:
                    raise StaleReplyPlanLeaseError("REPLY_PLAN_MANAGER_OWNED")
                if conversation.handoff_state != HandoffState.BOT_ACTIVE.value:
                    raise StaleReplyPlanLeaseError("REPLY_PLAN_HANDOFF_NOT_BOT_ACTIVE")
                if conversation.manager_takeover_at is not None:
                    raise StaleReplyPlanLeaseError("REPLY_PLAN_MANAGER_TAKEOVER")
                if conversation.manager_epoch != claim.manager_epoch:
                    raise StaleReplyPlanLeaseError("REPLY_PLAN_STALE_MANAGER_EPOCH")
                if conversation.current_event_seq != claim.event_seq_hwm:
                    raise StaleReplyPlanLeaseError("REPLY_PLAN_STALE_EVENT_SEQUENCE")

                plan_row = await reply_plan_repo.get_by_id(
                    session,
                    plan_id=claim.plan_id,
                )
                if (
                    plan_row is None
                    or plan_row.status != ReplyPlanStatus.PROCESSING.value
                    or plan_row.lease_token != claim.lease_token
                    or plan_row.lease_version != claim.lease_version
                    or plan_row.manager_epoch != claim.manager_epoch
                    or plan_row.event_seq_hwm != claim.event_seq_hwm
                ):
                    raise StaleReplyPlanLeaseError("REPLY_PLAN_STALE_LEASE")

                outbound, created = (
                    await outbound_repo.insert_synthetic_outbound_if_absent(
                        session,
                        conversation_id=claim.conversation_id,
                        reply_plan_id=claim.plan_id,
                        context_version=claim.context_version,
                        manager_epoch=claim.manager_epoch,
                        event_seq_hwm=claim.event_seq_hwm,
                        payload_json=_outbound_payload_from_plan(claim.payload_json),
                        correlation_id=claim.correlation_id,
                        not_before=claim.not_before,
                        max_attempts=plan_row.max_attempts,
                    )
                )
                plan = await reply_plan_repo.complete_dispatched_with_lease(
                    session,
                    plan_id=claim.plan_id,
                    lease_token=claim.lease_token,
                    lease_version=claim.lease_version,
                )
                # Last table in the lock order.
                await enqueue_reply_plan_state_changed(
                    session,
                    conversation_id=claim.conversation_id,
                    plan_id=plan.id,
                    plan_status=plan.status,
                    context_version=claim.context_version,
                    correlation_id=claim.correlation_id,
                )
                return ReplyPlanDispatchResult(
                    plan_id=plan.id,
                    plan_status=plan.status,
                    outbound_id=outbound.id,
                    outbound_created=created,
                )
        except StaleReplyPlanLeaseError:
            raise
        except Exception as exc:
            await self.fail_claimed(claim, error_code=type(exc).__name__)
            raise

    async def fail_claimed(
        self,
        claim: ReplyPlanClaim,
        *,
        error_code: str,
    ) -> ReplyPlan:
        async with session_scope(self._session_factory) as session:
            # A DEAD plan is mirrored from here, and that INSERT takes FOR KEY
            # SHARE on the dialog row, so the conversation lock must come first
            # even though the plan update alone would not need it.
            await conversation_repo.lock_for_update(
                session,
                conversation_id=claim.conversation_id,
            )
            plan = await reply_plan_repo.fail_with_lease(
                session,
                plan_id=claim.plan_id,
                lease_token=claim.lease_token,
                lease_version=claim.lease_version,
                error_code=error_code,
                retry_delay_seconds=self._retry_delay_seconds,
            )
            if plan.status == ReplyPlanStatus.DEAD.value:
                await enqueue_reply_plan_state_changed(
                    session,
                    conversation_id=claim.conversation_id,
                    plan_id=plan.id,
                    plan_status=plan.status,
                    context_version=claim.context_version,
                    correlation_id=claim.correlation_id,
                )
            return plan


class OutboundWorker:
    """Claims SYNTHETIC_OUTBOUND rows and routes them through OutboundArbiter."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        worker_id: str,
        arbiter: OutboundArbiter,
        lease_seconds: int = outbound_repo.DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._worker_id = worker_id
        self._arbiter = arbiter
        self._lease_seconds = lease_seconds

    async def claim_one(self, *, now: datetime | None = None) -> OutboundClaim | None:
        async with session_scope(self._session_factory) as session:
            return await outbound_repo.claim_next(
                session,
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
                now=now,
            )

    async def process_claimed(
        self,
        claim: OutboundClaim,
        *,
        now: datetime | None = None,
    ) -> ArbiterAdmitResult:
        return await self._arbiter.admit_claimed(claim, now=now)


def _outbound_payload_from_plan(plan_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "synthetic.outbound.v1",
        "source_schema": plan_payload.get("schema"),
        "plan_type": plan_payload.get("plan_type"),
        "synthetic_token": plan_payload.get("synthetic_token", "SYNTHETIC_OK"),
    }
