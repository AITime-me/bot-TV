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
from app.models.conversation import ConversationOwnership
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
        """Admit a leased SYNTHETIC_OUTBOUND message under fencing + locks.

        ``now`` exists so callers that already drive a controlled timeline reuse
        it; otherwise the PostgreSQL clock decides whether not_before elapsed.
        """
        try:
            async with session_scope(self._session_factory) as session:
                return await self._admit_in_session(session, claim, now=now)
        except StaleOutboundLeaseError:
            raise
        except OutboundArbiterDenied as denied:
            await self._fail_claim(claim, error_code=str(denied)[:64])
            raise
        except Exception as exc:
            await self._fail_claim(claim, error_code=type(exc).__name__)
            raise

    async def _admit_in_session(
        self,
        session: AsyncSession,
        claim: OutboundClaim,
        *,
        now: datetime | None = None,
    ) -> ArbiterAdmitResult:
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

        request = SyntheticOutboundRequest(
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
        result = self._sink.deliver(request)
        if result.outcome is SyntheticOutboundOutcome.TRANSIENT_ERROR:
            raise OutboundArbiterDenied(result.error_code or "SYNTHETIC_TRANSIENT")
        if result.outcome is SyntheticOutboundOutcome.PERMANENT_ERROR:
            # Permanent sink failure still goes through fail/DEAD path.
            raise OutboundArbiterDenied(result.error_code or "SYNTHETIC_PERMANENT")

        delivered = await outbound_repo.mark_delivered_with_lease(
            session,
            outbound_id=claim.outbound_id,
            lease_token=claim.lease_token,
            lease_version=claim.lease_version,
        )
        # Last table in the lock order; the dialog row is already locked above.
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
