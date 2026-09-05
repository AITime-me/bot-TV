from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.vk_client_outbound_config import (
    VkClientOutboundConfig,
    VkClientPeerResolutionError,
    parse_vk_client_peer_id,
    vk_client_outbound_send_allowed,
)
from app.channels.vk_client_outbound_http import (
    NullVkClientSender,
    VkClientSender,
    VkClientSendOutcome,
)
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
from app.services.amocrm_chat_projection import enqueue_bot_outbound_projection
from app.services.amocrm_mirror import enqueue_outbound_delivered
from app.services.outbound_reply_text import (
    OutboundReplyTextError,
    is_machine_only_outbound_payload,
    require_persisted_outbound_text,
)
from app.services.synthetic_outbound import (
    SyntheticOutboundAdapter,
    SyntheticOutboundOutcome,
    SyntheticOutboundRequest,
    SyntheticOutboundResult,
)

logger = logging.getLogger(__name__)

_LIVE_DESTINATIONS = frozenset(
    {
        DestinationType.SYNTHETIC_OUTBOUND.value,
        DestinationType.VK_CLIENT_OUTBOUND.value,
    }
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
    """Single gate for SYNTHETIC_OUTBOUND and VK_CLIENT_OUTBOUND delivery.

    No repository shortcut may mark these destinations DELIVERED outside this
    service. Global ``is_automatic_outbound_allowed`` stays fail-closed; VK
    sends use a separate narrow allowlist gate after durable ADMITTED.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings | None = None,
        sink: SyntheticOutboundAdapter | None = None,
        vk_config: VkClientOutboundConfig | None = None,
        vk_sender: VkClientSender | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings if settings is not None else Settings()
        self._sink = sink if sink is not None else SyntheticOutboundAdapter()
        self._vk_config = vk_config
        self._vk_sender = vk_sender if vk_sender is not None else NullVkClientSender()

    @property
    def sink(self) -> SyntheticOutboundAdapter:
        return self._sink

    async def admit_claimed(
        self,
        claim: OutboundClaim,
        *,
        now: datetime | None = None,
    ) -> ArbiterAdmitResult:
        """Durably admit, then invoke the destination sink outside SQL.

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

        # Transport is deliberately outside every database transaction.
        if claim.destination_type == DestinationType.SYNTHETIC_OUTBOUND.value:
            result = self._sink.deliver(request)
        elif claim.destination_type == DestinationType.VK_CLIENT_OUTBOUND.value:
            result = await self._deliver_vk_client(claim, request)
        else:
            await self._fail_admitted_delivery(claim, permanent=True, now=now)
            raise OutboundArbiterDenied("UNSUPPORTED_DESTINATION")

        if result.outcome is SyntheticOutboundOutcome.TRANSIENT_ERROR:
            await self._fail_admitted_delivery(claim, permanent=False, now=now)
            raise OutboundArbiterDenied(result.error_code or "TRANSPORT_TRANSIENT")
        if result.outcome is SyntheticOutboundOutcome.PERMANENT_ERROR:
            await self._fail_admitted_delivery(claim, permanent=True, now=now)
            raise OutboundArbiterDenied(result.error_code or "TRANSPORT_PERMANENT")

        # Persist VK provider message id before DELIVERED to shrink
        # callback-vs-receipt race for own-echo detection.
        if (
            claim.destination_type == DestinationType.VK_CLIENT_OUTBOUND.value
            and type(result.provider_message_id) is int
            and result.provider_message_id > 0
        ):
            async with session_scope(self._session_factory) as session:
                await conversation_repo.lock_for_update(
                    session,
                    conversation_id=claim.conversation_id,
                )
                await outbound_repo.set_vk_provider_message_id_with_lease(
                    session,
                    outbound_id=claim.outbound_id,
                    lease_token=claim.lease_token,
                    lease_version=claim.lease_version,
                    provider_message_id=result.provider_message_id,
                    now=now,
                )

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
                provider_message_id=result.provider_message_id,
            )
            # SELF-BOOKING-COMMAND-03C: same UoW as mark_delivered.
            # OFFER_SLOTS bind failures must roll back DELIVERED (no swallow).
            # Non-offer outbounds return REJECTED/NOT_OFFER_SLOTS and continue.
            from app.core.self_booking_active_offer_types import (
                ActiveOfferActivateOutcome,
            )
            from app.services.self_booking_active_offer import (
                SelfBookingActiveOfferService,
            )

            activate_result = await SelfBookingActiveOfferService(
                session
            ).activate_from_delivered_outbound(outbound=delivered)
            if activate_result.outcome is ActiveOfferActivateOutcome.REJECTED:
                if activate_result.reason_code != "NOT_OFFER_SLOTS":
                    raise OutboundArbiterDenied(
                        (
                            "ACTIVE_OFFER_"
                            f"{activate_result.reason_code or 'REJECTED'}"
                        )[:64]
                    )
            correlation_id = (
                delivered.correlation_id
                if delivered.correlation_id is not None
                else uuid.uuid4()
            )
            await enqueue_outbound_delivered(
                session,
                conversation_id=delivered.conversation_id,
                outbound_id=delivered.id,
                context_version=delivered.context_version,
                correlation_id=correlation_id,
            )
            # Commit authoritative DELIVERED (+ mirror) before Chat projection.
            # Projection must not share this transaction (B1).
            delivered_id = delivered.id
            delivered_conversation_id = delivered.conversation_id
            delivered_status = delivered.delivery_status
            delivered_destination = delivered.destination_type

        # AMO-01B1b: post-commit BOT_OUTBOUND projection for SYNTHETIC only.
        # First VK_CLIENT_OUTBOUND closed proof intentionally skips Chat
        # projection — native VK↔amoCRM visibility must be verified first to
        # avoid duplicate manager-visible messages (ADR-003 supplement).
        if delivered_destination == DestinationType.SYNTHETIC_OUTBOUND.value:
            try:
                async with session_scope(self._session_factory) as session:
                    await enqueue_bot_outbound_projection(
                        session,
                        conversation_id=delivered_conversation_id,
                        outbound_id=delivered_id,
                        correlation_id=correlation_id,
                    )
            except Exception as exc:
                logger.error(
                    "amocrm bot outbound projection enqueue failed "
                    "outbound_id=%s error_code=%s",
                    delivered_id,
                    type(exc).__name__,
                )

        return ArbiterAdmitResult(
            admitted=True,
            outbound_id=delivered_id,
            delivery_status=delivered_status,
        )

    async def _deliver_vk_client(
        self,
        claim: OutboundClaim,
        request: SyntheticOutboundRequest,
    ) -> SyntheticOutboundResult:
        async with session_scope(self._session_factory) as session:
            conversation = await conversation_repo.get_by_id_for_update(
                session,
                conversation_id=claim.conversation_id,
            )
            if conversation is None:
                return SyntheticOutboundResult(
                    outcome=SyntheticOutboundOutcome.PERMANENT_ERROR,
                    error_code="CONVERSATION_MISSING",
                )
            external_id = conversation.external_conversation_id

        config = self._vk_config
        if config is None or not vk_client_outbound_send_allowed(
            self._settings,
            config,
            external_conversation_id=external_id,
        ):
            return SyntheticOutboundResult(
                outcome=SyntheticOutboundOutcome.PERMANENT_ERROR,
                error_code="VK_CLIENT_SEND_GATE_DENIED",
            )
        assert config.group_id is not None
        try:
            peer_id = parse_vk_client_peer_id(
                external_conversation_id=external_id,
                expected_group_id=config.group_id,
            )
        except VkClientPeerResolutionError:
            return SyntheticOutboundResult(
                outcome=SyntheticOutboundOutcome.PERMANENT_ERROR,
                error_code="VK_CLIENT_PEER_INVALID",
            )
        text = request._text
        if type(text) is not str or not text:
            return SyntheticOutboundResult(
                outcome=SyntheticOutboundOutcome.PERMANENT_ERROR,
                error_code="OUTBOUND_REPLY_TEXT_MISSING",
            )
        send_result = self._vk_sender.send_text(
            peer_id=peer_id,
            text=text,
            outbound_id=claim.outbound_id,
        )
        if send_result.outcome is VkClientSendOutcome.SUCCESS:
            return SyntheticOutboundResult(
                outcome=SyntheticOutboundOutcome.SUCCESS,
                provider_message_id=send_result.provider_message_id,
            )
        if send_result.outcome is VkClientSendOutcome.TRANSIENT_ERROR:
            return SyntheticOutboundResult(
                outcome=SyntheticOutboundOutcome.TRANSIENT_ERROR,
                error_code=send_result.error_code or "VK_CLIENT_SEND_TRANSIENT",
            )
        return SyntheticOutboundResult(
            outcome=SyntheticOutboundOutcome.PERMANENT_ERROR,
            error_code=send_result.error_code or "VK_CLIENT_SEND_PERMANENT",
        )

    async def _admit_in_session(
        self,
        session: AsyncSession,
        claim: OutboundClaim,
        *,
        now: datetime | None = None,
    ) -> SyntheticOutboundRequest:
        moment = await resolve_moment(session, now)

        # Safety mode: global automatic outbound remains fail-closed.
        # VK uses a separate narrow allowlist gate after ADMITTED.
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
        if outbound.destination_type not in _LIVE_DESTINATIONS:
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
            if outbound.destination_type not in _LIVE_DESTINATIONS:
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
    """Build sink request from the immutable outbound row only — never re-render."""

    payload = outbound.payload_json if isinstance(outbound.payload_json, dict) else {}
    try:
        text = require_persisted_outbound_text(payload)
    except OutboundReplyTextError:
        if is_machine_only_outbound_payload(payload):
            text = None
        else:
            raise OutboundArbiterDenied("OUTBOUND_REPLY_TEXT_MISSING") from None
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
        _payload_schema=str(payload.get("schema", "unknown")),
        _text=text,
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
