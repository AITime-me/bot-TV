"""Closed-test application service (BOT-CLOSED-TEST-01A).

POST → existing SyntheticIngressAdapter (durable ingress only).
GET → read-only projection of durable pipeline stages. No worker claims,
network, or sink calls.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import session_scope
from app.models.conversation import Channel
from app.models.outbox import DestinationType, OutboxMessage
from app.models.reply_plan import ReplyPlan
from app.repositories import ingress as ingress_repo
from app.repositories import messages as messages_repo
from app.schemas.closed_test import (
    ClosedTestEventAck,
    ClosedTestEventCreate,
    ClosedTestEventStatus,
    ClosedTestStageInbound,
    ClosedTestStageIngress,
    ClosedTestStageOutbound,
    ClosedTestStageReplyPlan,
)
from app.schemas.ingress import SyntheticIngressEvent
from app.services.booking_synthetic import sanitize_booking_result_fields
from app.services.ingress import IngressPersistError, SyntheticIngressAdapter

_SAFE_RESULT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema",
        "source_schema",
        "plan_type",
        "synthetic_token",
        "booking_action",
        "booking_reason",
        "booking_available_date_keys",
        "booking_studio_today",
        "booking_offered_slot_ids",
        "booking_offered_slots",
        "client_message_kind",
    }
)


class ClosedTestIdempotencyConflict(Exception):
    """Same request_id reused with a different session_id and/or text."""

    def __init__(self) -> None:
        super().__init__("IDEMPOTENCY_CONFLICT")

    def __repr__(self) -> str:
        return "ClosedTestIdempotencyConflict('IDEMPOTENCY_CONFLICT')"


def project_safe_synthetic_result(payload: object) -> dict[str, Any] | None:
    """Allowlisted synthetic.outbound.v1 fields only. Never returns secrets/text."""

    if type(payload) is not dict:
        return None
    if payload.get("schema") != "synthetic.outbound.v1":
        return None

    out: dict[str, Any] = {"schema": "synthetic.outbound.v1"}
    token = payload.get("synthetic_token")
    if type(token) is str and token:
        out["synthetic_token"] = token
    source_schema = payload.get("source_schema")
    if type(source_schema) is str and source_schema:
        out["source_schema"] = source_schema
    plan_type = payload.get("plan_type")
    if type(plan_type) is str and plan_type:
        out["plan_type"] = plan_type
    kind = payload.get("client_message_kind")
    if type(kind) is str and kind:
        out["client_message_kind"] = kind

    if "booking_action" in payload:
        booking = sanitize_booking_result_fields(payload)
        for key, value in booking.items():
            if key in _SAFE_RESULT_KEYS:
                out[key] = value

    # Drop any accidental non-allowlisted keys (defense in depth), including
    # authoritative user-facing ``text`` which must never appear on this surface.
    return {k: v for k, v in out.items() if k in _SAFE_RESULT_KEYS}


def _texts_match(persisted: object, submitted: str) -> bool:
    if type(persisted) is not str:
        return False
    try:
        return secrets.compare_digest(persisted, submitted)
    except (TypeError, ValueError):
        return False


class ClosedTestService:
    """Composition helper for the closed-test HTTP surface."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._ingress = SyntheticIngressAdapter(session_factory)

    async def accept_event(self, body: ClosedTestEventCreate) -> ClosedTestEventAck:
        event = SyntheticIngressEvent(
            channel="synthetic",
            external_event_id=body.request_id,
            external_conversation_id=body.session_id,
            text=body.text,
        )
        try:
            ack = await self._ingress.accept(event)
        except IngressPersistError:
            raise

        if ack.duplicate:
            # Post-insert check: handles concurrent races where another writer
            # won the unique (channel, external_event_id) constraint first.
            await self._assert_duplicate_matches(ack.event_id, body)

        return ClosedTestEventAck(
            accepted=ack.accepted,
            duplicate=ack.duplicate,
            event_id=ack.event_id,
            status=ack.status,
            correlation_id=ack.correlation_id,
        )

    async def _assert_duplicate_matches(
        self,
        event_id: uuid.UUID,
        body: ClosedTestEventCreate,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            row = await ingress_repo.get_by_id(session, event_id=event_id)
            if row is None:
                raise IngressPersistError(
                    "INGRESS_PERSIST_FAILED (LookupError)"
                ) from None
            if row.channel != Channel.SYNTHETIC.value:
                raise ClosedTestIdempotencyConflict() from None
            if row.external_event_id != body.request_id:
                raise ClosedTestIdempotencyConflict() from None
            if row.external_conversation_id != body.session_id:
                raise ClosedTestIdempotencyConflict() from None
            envelope = row.envelope_json
            persisted_text = (
                envelope.get("text") if type(envelope) is dict else None
            )
            if not _texts_match(persisted_text, body.text):
                raise ClosedTestIdempotencyConflict() from None

    async def get_event_status(
        self, event_id: uuid.UUID
    ) -> ClosedTestEventStatus | None:
        """Read-only status. Returns None when the ingress event is unknown."""

        async with session_scope(self._session_factory) as session:
            ingress = await ingress_repo.get_by_id(session, event_id=event_id)
            if ingress is None:
                return None
            if ingress.channel != Channel.SYNTHETIC.value:
                return None

            inbound_stage: ClosedTestStageInbound | None = None
            reply_stage: ClosedTestStageReplyPlan | None = None
            outbound_stage: ClosedTestStageOutbound | None = None
            synthetic_result: dict[str, Any] | None = None

            inbox = await messages_repo.get_inbox_by_external(
                session,
                channel=Channel.SYNTHETIC,
                external_message_id=ingress.external_event_id,
            )
            if inbox is not None:
                inbound_stage = ClosedTestStageInbound(
                    processing_status=inbox.processing_status,
                )
                plan = await _find_reply_plan_for_inbox(
                    session,
                    conversation_id=inbox.conversation_id,
                    inbox_id=inbox.id,
                )
                if plan is not None:
                    reply_stage = ClosedTestStageReplyPlan(
                        reply_plan_id=plan.id,
                        status=plan.status,
                        context_version=plan.context_version,
                    )
                    outbound = await _find_synthetic_outbound(
                        session, reply_plan_id=plan.id
                    )
                    if outbound is not None:
                        outbound_stage = ClosedTestStageOutbound(
                            delivery_status=outbound.delivery_status,
                            outbound_id=outbound.id,
                        )
                        synthetic_result = project_safe_synthetic_result(
                            outbound.payload_json
                        )

            return ClosedTestEventStatus(
                event_id=ingress.id,
                correlation_id=ingress.correlation_id,
                ingress=ClosedTestStageIngress(status=ingress.status),
                inbound=inbound_stage,
                reply_plan=reply_stage,
                outbound=outbound_stage,
                synthetic_result=synthetic_result,
            )


async def _find_reply_plan_for_inbox(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    inbox_id: uuid.UUID,
) -> ReplyPlan | None:
    inbox_key = str(inbox_id)
    stmt = (
        select(ReplyPlan)
        .where(ReplyPlan.conversation_id == conversation_id)
        .order_by(ReplyPlan.created_at.desc())
    )
    rows = (await session.scalars(stmt)).all()
    for plan in rows:
        payload = plan.payload_json
        if type(payload) is dict and payload.get("inbox_id") == inbox_key:
            return plan
    return None


async def _find_synthetic_outbound(
    session: AsyncSession,
    *,
    reply_plan_id: uuid.UUID,
) -> OutboxMessage | None:
    stmt = select(OutboxMessage).where(
        OutboxMessage.reply_plan_id == reply_plan_id,
        OutboxMessage.destination_type == DestinationType.SYNTHETIC_OUTBOUND.value,
    )
    return await session.scalar(stmt)
