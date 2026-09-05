from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.clock import resolve_moment
from app.db.session import session_scope
from app.models.conversation import Channel, ConversationOwnership, HandoffState
from app.models.outbox import DestinationType
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
from app.services.booking_flow import BookingFlowService
from app.services.booking_synthetic import (
    BookingResolutionPhase,
    booking_resolution_phase,
    build_synthetic_outbound_payload,
    interrupted_booking_fields,
    plan_has_booking_fixture,
    read_booking_resolution_result,
    resolve_booking_outbound_fields,
    sanitize_booking_result_fields,
)
from app.services.outbound_arbiter import ArbiterAdmitResult, OutboundArbiter
from app.services.vk_client_outbound_proof import (
    is_vk_client_proof_reply_plan,
    vk_client_outbound_payload,
)


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


class _BookingPrepareKind(Enum):
    FINALIZED = auto()
    RUN_REMOTE = auto()
    INTERRUPTED = auto()
    USE_SAVED_RESULT = auto()


@dataclass(frozen=True)
class _BookingPrepare:
    kind: _BookingPrepareKind
    plan_payload: dict[str, Any]
    max_attempts: int
    booking_fields: dict[str, Any] | None = None
    result: ReplyPlanDispatchResult | None = None


class ReplyPlanWorker:
    """Claims due ReplyPlans and creates exactly one SYNTHETIC_OUTBOUND row."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        worker_id: str,
        booking_flow: BookingFlowService | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._worker_id = worker_id
        # Fail-closed default when tests omit injection; composition root always
        # passes an explicit BookingFlowService.
        self._booking_flow = (
            booking_flow if booking_flow is not None else BookingFlowService(None)
        )
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
            if not plan_has_booking_fixture(claim.payload_json):
                return await self._dispatch_non_booking(claim)
            return await self._dispatch_booking(claim)
        except StaleReplyPlanLeaseError:
            raise
        except Exception as exc:
            await self.fail_claimed(claim, error_code=type(exc).__name__)
            raise

    async def _dispatch_non_booking(
        self,
        claim: ReplyPlanClaim,
    ) -> ReplyPlanDispatchResult:
        """Legacy single-transaction path for plans without booking fixtures."""

        async with session_scope(self._session_factory) as session:
            plan_row = await self._lock_and_validate(session, claim)
            conversation = await conversation_repo.get_by_id_for_update(
                session,
                conversation_id=claim.conversation_id,
            )
            if conversation is None:
                raise RuntimeError("CONVERSATION_MISSING")

            if is_vk_client_proof_reply_plan(claim.payload_json):
                if conversation.channel != Channel.VK.value:
                    raise RuntimeError("VK_OUTBOUND_CHANNEL_MISMATCH")
                text = claim.payload_json.get("text")
                if type(text) is not str or not text:
                    raise RuntimeError("VK_OUTBOUND_TEXT_MISSING")
                key = outbound_repo.vk_client_outbound_idempotency_key(claim.plan_id)
                existing = await outbound_repo.get_by_idempotency_key(
                    session,
                    idempotency_key=key,
                )
                if existing is not None:
                    outbound, created = existing, False
                else:
                    outbound, created = (
                        await outbound_repo.insert_vk_client_outbound_if_absent(
                            session,
                            conversation_id=claim.conversation_id,
                            reply_plan_id=claim.plan_id,
                            context_version=claim.context_version,
                            manager_epoch=claim.manager_epoch,
                            event_seq_hwm=claim.event_seq_hwm,
                            payload_json=vk_client_outbound_payload(text=text),
                            correlation_id=claim.correlation_id,
                            not_before=claim.not_before,
                            max_attempts=plan_row.max_attempts,
                        )
                    )
                if outbound.destination_type != DestinationType.VK_CLIENT_OUTBOUND.value:
                    raise RuntimeError("VK_OUTBOUND_DESTINATION_MISMATCH")
            else:
                if conversation.channel == Channel.VK.value:
                    # Fail closed: non-proof VK plans must not become synthetic send.
                    raise RuntimeError("VK_OUTBOUND_PROOF_REQUIRED")
                existing = await outbound_repo.get_by_idempotency_key(
                    session,
                    idempotency_key=outbound_repo.synthetic_outbound_idempotency_key(
                        claim.plan_id
                    ),
                )
                if existing is not None:
                    outbound, created = existing, False
                else:
                    outbound_payload = build_synthetic_outbound_payload(claim.payload_json)
                    outbound, created = (
                        await outbound_repo.insert_synthetic_outbound_if_absent(
                            session,
                            conversation_id=claim.conversation_id,
                            reply_plan_id=claim.plan_id,
                            context_version=claim.context_version,
                            manager_epoch=claim.manager_epoch,
                            event_seq_hwm=claim.event_seq_hwm,
                            payload_json=outbound_payload,
                            correlation_id=claim.correlation_id,
                            not_before=claim.not_before,
                            max_attempts=plan_row.max_attempts,
                        )
                    )
            return await self._complete_dispatched(
                session,
                claim=claim,
                outbound_id=outbound.id,
                outbound_created=created,
            )

    async def _dispatch_booking(
        self,
        claim: ReplyPlanClaim,
    ) -> ReplyPlanDispatchResult:
        """Two-phase durable booking: mark → off-tx resolve → persist + outbound."""

        prepare = await self._booking_phase1_prepare(claim)
        if prepare.kind == _BookingPrepareKind.FINALIZED:
            assert prepare.result is not None
            return prepare.result

        booking_fields: dict[str, Any]
        if prepare.kind == _BookingPrepareKind.USE_SAVED_RESULT:
            assert prepare.booking_fields is not None
            booking_fields = prepare.booking_fields
        elif prepare.kind == _BookingPrepareKind.INTERRUPTED:
            booking_fields = interrupted_booking_fields()
        elif prepare.kind == _BookingPrepareKind.RUN_REMOTE:
            # Resolve only after phase-1 commit released locks/txn.
            booking_fields = await self._resolve_booking_off_transaction(
                prepare.plan_payload
            )
        else:
            raise RuntimeError("BOOKING_PREPARE_KIND_INVALID")

        return await self._booking_phase2_finalize(
            claim,
            plan_payload=prepare.plan_payload,
            booking_fields=booking_fields,
            persist_result=prepare.kind
            in {_BookingPrepareKind.RUN_REMOTE, _BookingPrepareKind.INTERRUPTED},
        )

    async def _resolve_booking_off_transaction(
        self,
        plan_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Run synchronous booking_flow.resolve without blocking the event loop."""

        return await asyncio.to_thread(
            resolve_booking_outbound_fields,
            plan_payload,
            booking_flow=self._booking_flow,
        )

    async def _booking_phase1_prepare(
        self,
        claim: ReplyPlanClaim,
    ) -> _BookingPrepare:
        """Txn 1: fences, outbound short-circuit, durable started marker, commit."""

        async with session_scope(self._session_factory) as session:
            plan_row = await self._lock_and_validate(session, claim)
            plan_payload = dict(plan_row.payload_json)

            existing = await outbound_repo.get_by_idempotency_key(
                session,
                idempotency_key=outbound_repo.synthetic_outbound_idempotency_key(
                    claim.plan_id
                ),
            )
            if existing is not None:
                result = await self._complete_dispatched(
                    session,
                    claim=claim,
                    outbound_id=existing.id,
                    outbound_created=False,
                )
                return _BookingPrepare(
                    kind=_BookingPrepareKind.FINALIZED,
                    plan_payload=plan_payload,
                    max_attempts=plan_row.max_attempts,
                    result=result,
                )

            phase = booking_resolution_phase(plan_payload)
            if phase == BookingResolutionPhase.HAS_RESULT:
                fields = read_booking_resolution_result(plan_payload)
                assert fields is not None
                return _BookingPrepare(
                    kind=_BookingPrepareKind.USE_SAVED_RESULT,
                    plan_payload=plan_payload,
                    max_attempts=plan_row.max_attempts,
                    booking_fields=fields,
                )
            if phase == BookingResolutionPhase.INTERRUPTED:
                return _BookingPrepare(
                    kind=_BookingPrepareKind.INTERRUPTED,
                    plan_payload=plan_payload,
                    max_attempts=plan_row.max_attempts,
                )

            acquired = await reply_plan_repo.try_mark_booking_resolution_started(
                session,
                plan_id=claim.plan_id,
                lease_token=claim.lease_token,
                lease_version=claim.lease_version,
            )
            if acquired:
                marked = dict(plan_payload)
                marked["booking_resolution_started"] = True
                return _BookingPrepare(
                    kind=_BookingPrepareKind.RUN_REMOTE,
                    plan_payload=marked,
                    max_attempts=plan_row.max_attempts,
                )

            # Lost the marker race: re-read and never start a second remote.
            refreshed = await reply_plan_repo.get_by_id(
                session, plan_id=claim.plan_id
            )
            if refreshed is None:
                raise StaleReplyPlanLeaseError("REPLY_PLAN_STALE_LEASE")
            plan_payload = dict(refreshed.payload_json)
            phase = booking_resolution_phase(plan_payload)
            if phase == BookingResolutionPhase.HAS_RESULT:
                fields = read_booking_resolution_result(plan_payload)
                assert fields is not None
                return _BookingPrepare(
                    kind=_BookingPrepareKind.USE_SAVED_RESULT,
                    plan_payload=plan_payload,
                    max_attempts=plan_row.max_attempts,
                    booking_fields=fields,
                )
            return _BookingPrepare(
                kind=_BookingPrepareKind.INTERRUPTED,
                plan_payload=plan_payload,
                max_attempts=plan_row.max_attempts,
            )

    async def _booking_phase2_finalize(
        self,
        claim: ReplyPlanClaim,
        *,
        plan_payload: dict[str, Any],
        booking_fields: dict[str, Any],
        persist_result: bool,
    ) -> ReplyPlanDispatchResult:
        """Txn 2: re-fence, persist result, idempotent outbound, complete."""

        safe_fields = sanitize_booking_result_fields(booking_fields)
        async with session_scope(self._session_factory) as session:
            plan_row = await self._lock_and_validate(session, claim)
            current_payload = dict(plan_row.payload_json)

            existing = await outbound_repo.get_by_idempotency_key(
                session,
                idempotency_key=outbound_repo.synthetic_outbound_idempotency_key(
                    claim.plan_id
                ),
            )
            if existing is not None:
                return await self._complete_dispatched(
                    session,
                    claim=claim,
                    outbound_id=existing.id,
                    outbound_created=False,
                )

            if persist_result and not isinstance(
                current_payload.get("booking_resolution_result"), dict
            ):
                current_payload = (
                    await reply_plan_repo.persist_booking_resolution_result(
                        session,
                        plan_id=claim.plan_id,
                        lease_token=claim.lease_token,
                        lease_version=claim.lease_version,
                        result=safe_fields,
                    )
                )

            stored = read_booking_resolution_result(current_payload)
            fields_for_outbound = stored if stored is not None else safe_fields
            outbound_payload = build_synthetic_outbound_payload(
                current_payload if stored is not None else plan_payload,
                booking_fields=fields_for_outbound,
            )
            outbound, created = await outbound_repo.insert_synthetic_outbound_if_absent(
                session,
                conversation_id=claim.conversation_id,
                reply_plan_id=claim.plan_id,
                context_version=claim.context_version,
                manager_epoch=claim.manager_epoch,
                event_seq_hwm=claim.event_seq_hwm,
                payload_json=outbound_payload,
                correlation_id=claim.correlation_id,
                not_before=claim.not_before,
                max_attempts=plan_row.max_attempts,
            )
            return await self._complete_dispatched(
                session,
                claim=claim,
                outbound_id=outbound.id,
                outbound_created=created,
            )

    async def _lock_and_validate(
        self,
        session: AsyncSession,
        claim: ReplyPlanClaim,
    ) -> ReplyPlan:
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
        return plan_row

    async def _complete_dispatched(
        self,
        session: AsyncSession,
        *,
        claim: ReplyPlanClaim,
        outbound_id: uuid.UUID,
        outbound_created: bool,
    ) -> ReplyPlanDispatchResult:
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
            outbound_id=outbound_id,
            outbound_created=outbound_created,
        )

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
