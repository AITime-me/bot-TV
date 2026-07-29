from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.outbound_policy import OutboundAction, is_automatic_outbound_allowed
from app.db.clock import resolve_moment
from app.db.session import session_scope
from app.models.conversation import (
    ConversationOwnership,
    ConversationStatus,
    HandoffState,
)
from app.models.outbox import DeliveryStatus, DestinationType, OutboxMessage
from app.models.reply_plan import ReplyPlan, ReplyPlanStatus
from app.repositories import conversations as conversation_repo
from app.repositories import outbound as outbound_repo
from app.repositories import reply_plans as reply_plan_repo
from app.repositories.outbound import OutboundClaim, StaleOutboundLeaseError
from app.services.amocrm_mirror import enqueue_outbound_delivered
from app.services.synthetic_outbound import (
    SyntheticOutboundAdapter,
    SyntheticOutboundOutcome,
    SyntheticOutboundRequest,
)


class OutboundArbiterDenied(RuntimeError):
    """Fail-closed denial from the single Outbound Arbiter."""

    _EXPECTED_FENCE_REASONS = frozenset(
        {
            "MANAGER_OWNED",
            "CONVERSATION_NOT_OPEN",
            "HANDOFF_NOT_BOT_ACTIVE",
            "MANAGER_TAKEOVER",
            "STALE_CONTEXT_VERSION",
            "STALE_MANAGER_EPOCH",
            "STALE_EVENT_SEQUENCE",
            "REPLY_PLAN_STALE_CONTEXT",
            "REPLY_PLAN_STALE_MANAGER_EPOCH",
            "REPLY_PLAN_STALE_EVENT_SEQUENCE",
        }
    )

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

    @property
    def is_expected_fence_outcome(self) -> bool:
        if self.reason in self._EXPECTED_FENCE_REASONS:
            return True
        return self.reason.startswith(
            (
                "REPLY_PLAN_CANCELLED",
                "REPLY_PLAN_SUPERSEDED",
                "REPLY_PLAN_DEAD",
            )
        )


@dataclass(frozen=True, repr=False)
class ArbiterAdmitResult:
    admitted: bool
    outbound_id: uuid.UUID
    delivery_status: str
    reason: str | None = None

    def __repr__(self) -> str:
        return (
            f"ArbiterAdmitResult(admitted={self.admitted!r}, "
            f"outbound_id={self.outbound_id!r}, "
            f"delivery_status={self.delivery_status!r}, reason={self.reason!r})"
        )


class OutboundArbiter:
    """Single gate for synthetic outbound delivery.

    No repository shortcut may mark SYNTHETIC_OUTBOUND as DELIVERED outside
    this service. Real channel sends remain impossible under fail-closed policy.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings | None = None,
        sink: SyntheticOutboundAdapter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings if settings is not None else Settings()
        self._sink = sink if sink is not None else SyntheticOutboundAdapter()

    @property
    def sink(self) -> SyntheticOutboundAdapter:
        return self._sink

    async def admit_claimed(
        self,
        claim: OutboundClaim,
        *,
        now: datetime | None = None,
    ) -> ArbiterAdmitResult:
        """Durably admit, then invoke the idempotent synthetic sink outside SQL.

        ``now`` exists so callers that already drive a controlled timeline reuse
        it; otherwise the PostgreSQL clock decides whether not_before elapsed.
        """
        if claim.delivery_status == DeliveryStatus.ADMITTED.value:
            request = await self._prepare_reclaimed_admission(claim)
        else:
            try:
                async with session_scope(self._session_factory) as session:
                    request = await self._admit_in_session(session, claim, now=now)
            except StaleOutboundLeaseError:
                raise
            except OutboundArbiterDenied as denied:
                await self._fail_claim(claim, error_code=str(denied)[:64])
                raise
            except Exception as exc:
                await self._fail_claim(claim, error_code=type(exc).__name__)
                raise

        # This call is deliberately outside every database transaction. A live
        # adapter may replace the synthetic sink only when it honors outbound_id
        # as an idempotency key.
        result = self._sink.deliver(request)
        if result.outcome is SyntheticOutboundOutcome.TRANSIENT_ERROR:
            await self._fail_admitted_delivery(claim, permanent=False, now=now)
            raise OutboundArbiterDenied(result.error_code or "SYNTHETIC_TRANSIENT")
        if result.outcome is SyntheticOutboundOutcome.PERMANENT_ERROR:
            await self._fail_admitted_delivery(claim, permanent=True, now=now)
            raise OutboundArbiterDenied(result.error_code or "SYNTHETIC_PERMANENT")

        async with session_scope(self._session_factory) as session:
            # Preserve global lock order even though manager ownership can no
            # longer cancel an ADMITTED row.
            await conversation_repo.lock_for_update(
                session,
                conversation_id=claim.conversation_id,
            )
            delivered = await outbound_repo.mark_delivered_with_lease(
                session,
                outbound_id=claim.outbound_id,
                lease_token=claim.lease_token,
                lease_version=claim.lease_version,
                now=now,
            )
            await enqueue_outbound_delivered(
                session,
                conversation_id=delivered.conversation_id,
                outbound_id=delivered.id,
                context_version=delivered.context_version,
                correlation_id=(
                    delivered.correlation_id
                    if delivered.correlation_id is not None
                    else uuid.uuid4()
                ),
            )
        return ArbiterAdmitResult(
            admitted=True,
            outbound_id=delivered.id,
            delivery_status=delivered.delivery_status,
        )

    async def _admit_in_session(
        self,
        session: AsyncSession,
        claim: OutboundClaim,
        *,
        now: datetime | None = None,
    ) -> SyntheticOutboundRequest:
        moment = await resolve_moment(session, now)

        # Safety mode: automatic outbound to real channels is always false.
        # Arbiter only allows the in-process synthetic sink.
        if is_automatic_outbound_allowed(
            self._settings,
            OutboundAction.SEND_MESSAGE,
        ):
            raise OutboundArbiterDenied("AUTO_OUTBOUND_UNEXPECTEDLY_ENABLED")

        conversation = await conversation_repo.get_by_id_for_update(
            session,
            conversation_id=claim.conversation_id,
        )
        if conversation is None:
            raise OutboundArbiterDenied("CONVERSATION_MISSING")
        if conversation.ownership != ConversationOwnership.BOT.value:
            raise OutboundArbiterDenied("MANAGER_OWNED")
        if conversation.status != ConversationStatus.OPEN.value:
            raise OutboundArbiterDenied("CONVERSATION_NOT_OPEN")
        if conversation.handoff_state != HandoffState.BOT_ACTIVE.value:
            raise OutboundArbiterDenied("HANDOFF_NOT_BOT_ACTIVE")
        if conversation.manager_takeover_at is not None:
            raise OutboundArbiterDenied("MANAGER_TAKEOVER")

        outbound = await session.get(
            OutboxMessage,
            claim.outbound_id,
            with_for_update=True,
        )
        if outbound is None:
            raise OutboundArbiterDenied("OUTBOUND_MISSING")
        if (
            outbound.delivery_status != DeliveryStatus.PROCESSING.value
            or outbound.lease_token != claim.lease_token
            or outbound.lease_version != claim.lease_version
        ):
            raise StaleOutboundLeaseError("OUTBOUND_STALE_LEASE")
        if outbound.destination_type != DestinationType.SYNTHETIC_OUTBOUND.value:
            raise OutboundArbiterDenied("UNSUPPORTED_DESTINATION")
        if outbound.delivery_status == DeliveryStatus.DELIVERED.value:
            raise OutboundArbiterDenied("ALREADY_DELIVERED")
        if outbound.not_before is not None and outbound.not_before > moment:
            raise OutboundArbiterDenied("NOT_BEFORE")
        if outbound.context_version is not None and (
            outbound.context_version != conversation.context_version
        ):
            raise OutboundArbiterDenied("STALE_CONTEXT_VERSION")
        if outbound.manager_epoch != conversation.manager_epoch:
            raise OutboundArbiterDenied("STALE_MANAGER_EPOCH")
        if outbound.event_seq_hwm != conversation.current_event_seq:
            raise OutboundArbiterDenied("STALE_EVENT_SEQUENCE")
        if claim.manager_epoch != outbound.manager_epoch:
            raise StaleOutboundLeaseError("OUTBOUND_STALE_MANAGER_EPOCH_CLAIM")
        if claim.event_seq_hwm != outbound.event_seq_hwm:
            raise StaleOutboundLeaseError("OUTBOUND_STALE_EVENT_SEQUENCE_CLAIM")

        if outbound.reply_plan_id is not None:
            plan = await session.get(
                ReplyPlan,
                outbound.reply_plan_id,
                with_for_update=True,
            )
            if plan is None:
                raise OutboundArbiterDenied("REPLY_PLAN_MISSING")
            if plan.status != ReplyPlanStatus.DISPATCHED.value:
                raise OutboundArbiterDenied(f"REPLY_PLAN_{plan.status}")
            if plan.context_version != conversation.context_version:
                raise OutboundArbiterDenied("REPLY_PLAN_STALE_CONTEXT")
            if plan.manager_epoch != outbound.manager_epoch:
                raise OutboundArbiterDenied("REPLY_PLAN_STALE_MANAGER_EPOCH")
            if plan.event_seq_hwm != outbound.event_seq_hwm:
                raise OutboundArbiterDenied("REPLY_PLAN_STALE_EVENT_SEQUENCE")

        admitted = await outbound_repo.mark_admitted_with_lease(
            session,
            outbound_id=claim.outbound_id,
            lease_token=claim.lease_token,
            lease_version=claim.lease_version,
            now=moment,
        )
        return _request_from_outbound(admitted)

    async def _prepare_reclaimed_admission(
        self,
        claim: OutboundClaim,
    ) -> SyntheticOutboundRequest:
        """Validate the lease of a crash-recovered ADMITTED row."""
        async with session_scope(self._session_factory) as session:
            await conversation_repo.lock_for_update(
                session,
                conversation_id=claim.conversation_id,
            )
            outbound = await session.get(
                OutboxMessage,
                claim.outbound_id,
                with_for_update=True,
            )
            if outbound is None:
                raise OutboundArbiterDenied("OUTBOUND_MISSING")
            if (
                outbound.delivery_status != DeliveryStatus.ADMITTED.value
                or outbound.admitted_at is None
                or outbound.lease_token != claim.lease_token
                or outbound.lease_version != claim.lease_version
            ):
                raise StaleOutboundLeaseError("OUTBOUND_STALE_LEASE")
            if outbound.destination_type != DestinationType.SYNTHETIC_OUTBOUND.value:
                raise OutboundArbiterDenied("UNSUPPORTED_DESTINATION")
            return _request_from_outbound(outbound)

    async def _fail_claim(self, claim: OutboundClaim, *, error_code: str) -> None:
        try:
            async with session_scope(self._session_factory) as session:
                await outbound_repo.fail_with_lease(
                    session,
                    outbound_id=claim.outbound_id,
                    lease_token=claim.lease_token,
                    lease_version=claim.lease_version,
                )
        except StaleOutboundLeaseError:
            return

    async def _fail_admitted_delivery(
        self,
        claim: OutboundClaim,
        *,
        permanent: bool,
        now: datetime | None,
    ) -> None:
        try:
            async with session_scope(self._session_factory) as session:
                await outbound_repo.fail_admitted_delivery_with_lease(
                    session,
                    outbound_id=claim.outbound_id,
                    lease_token=claim.lease_token,
                    lease_version=claim.lease_version,
                    permanent=permanent,
                    now=now,
                )
        except StaleOutboundLeaseError:
            return


def _request_from_outbound(outbound: OutboxMessage) -> SyntheticOutboundRequest:
    return SyntheticOutboundRequest(
        outbound_id=str(outbound.id),
        conversation_id=str(outbound.conversation_id),
        reply_plan_id=(
            str(outbound.reply_plan_id) if outbound.reply_plan_id else None
        ),
        context_version=outbound.context_version,
        correlation_id=(
            str(outbound.correlation_id) if outbound.correlation_id else None
        ),
        _payload_schema=str(outbound.payload_json.get("schema", "unknown")),
    )


def assert_no_arbiter_bypass() -> None:
    """Static guard: DELIVERED for SYNTHETIC_OUTBOUND only via Arbiter."""
    roots = [
        Path(__file__).resolve().parents[1] / "repositories",
        Path(__file__).resolve().parents[1] / "services",
    ]
    allowed = Path(__file__).resolve()
    for package in roots:
        for path in package.rglob("*.py"):
            if path.resolve() == allowed:
                continue
            text = path.read_text(encoding="utf-8")
            if "DELIVERED" in text and "mark_delivered_with_lease" in text:
                # Only outbound repo defines the helper; Arbiter is the caller.
                if path.name not in {"outbound.py", "outbound_arbiter.py"}:
                    raise AssertionError(f"possible arbiter bypass in {path}")
            banned_sent = "delivery_status=" + repr("SENT")
            banned_enum = "DeliveryStatus" + "." + "SENT"
            if banned_sent in text or banned_enum in text:
                raise AssertionError(f"SENT status present in {path}")
