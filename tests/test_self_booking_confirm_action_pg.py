"""PostgreSQL tests for SELF-BOOKING-COMMAND-03D confirm + active-offer rule."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.booking_types import BookingClientMessageKind, BookingDialogAction
from app.core.self_booking_active_offer_types import (
    ActiveOfferActivateOutcome,
    ActiveOfferResolveOutcome,
)
from app.db.session import session_scope
from app.models.conversation import Channel, Conversation
from app.models.outbox import DeliveryStatus, DestinationType, OutboxMessage
from app.repositories import conversations as conversation_repo
from app.schemas.inbound import SyntheticInboundEvent
from app.schemas.self_booking_confirm_action import SyntheticConfirmSelectedSlotAction
from app.services.inbound import InboundService
from app.services.self_booking_active_offer import SelfBookingActiveOfferService
from tests.pg_harness import truncate_foundation_tables

_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_SLOT = f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.1000"
_STARTS = "2026-08-20T10:00:00+05:00"
_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture(autouse=True)
async def confirm_action_cleanup(
    request: pytest.FixtureRequest,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    if request.node.get_closest_marker("no_foundation_row_cleanup"):
        yield
        return
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


def _offer_payload(*, slot_id: str, starts_at: str) -> dict[str, Any]:
    return {
        "schema": "synthetic.outbound.v1",
        "booking_action": BookingDialogAction.OFFER_SLOTS.value,
        "booking_reason": None,
        "booking_offered_slot_ids": [slot_id],
        "booking_offered_slots": [
            {"slot_id": slot_id, "starts_at": starts_at},
        ],
        "client_message_kind": BookingClientMessageKind.OFFER_SLOTS.value,
        "text": "slots",
    }


async def _seed_with_active_offer(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    external_conversation_id: str,
) -> Conversation:
    async with session_scope(session_factory) as session:
        conversation, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.SYNTHETIC,
            external_conversation_id=external_conversation_id,
        )
        outbound = OutboxMessage(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            idempotency_key=f"synthetic-outbound:confirm-03d:{uuid.uuid4()}",
            context_version=1,
            manager_epoch=0,
            event_seq_hwm=1,
            destination_type=DestinationType.SYNTHETIC_OUTBOUND.value,
            payload_json=_offer_payload(slot_id=_SLOT, starts_at=_STARTS),
            delivery_status=DeliveryStatus.DELIVERED.value,
            admitted_at=_NOW,
            attempt_count=1,
            max_attempts=5,
            lease_version=1,
            created_at=_NOW,
            updated_at=_NOW,
        )
        session.add(outbound)
        await session.flush()
        result = await SelfBookingActiveOfferService(
            session, clock=lambda: _NOW
        ).activate_from_delivered_outbound(outbound=outbound)
        assert result.outcome is ActiveOfferActivateOutcome.ACTIVATED
        await session.refresh(conversation)
        return conversation


@pytest.mark.asyncio
async def test_plain_inbound_invalidates_active_offer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conv_ext = f"confirm-plain-{uuid.uuid4().hex[:10]}"
    conversation = await _seed_with_active_offer(
        session_factory, external_conversation_id=conv_ext
    )

    async with session_scope(session_factory) as session:
        await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conv_ext,
                external_message_id=f"plain-{uuid.uuid4().hex[:10]}",
                text="hello again",
            )
        )
        miss = await SelfBookingActiveOfferService(session).resolve_slot(
            conversation_id=conversation.id, slot_id=_SLOT
        )
        assert miss.outcome is ActiveOfferResolveOutcome.NOT_ACTIVE


@pytest.mark.asyncio
async def test_confirm_inbound_preserves_active_offer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conv_ext = f"confirm-keep-{uuid.uuid4().hex[:10]}"
    conversation = await _seed_with_active_offer(
        session_factory, external_conversation_id=conv_ext
    )

    async with session_scope(session_factory) as session:
        await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conv_ext,
                external_message_id=f"confirm-{uuid.uuid4().hex[:10]}",
                text="structured-confirm",
                action=SyntheticConfirmSelectedSlotAction.model_validate(
                    {
                        "kind": "CONFIRM_SELECTED_SLOT",
                        "slot_id": _SLOT,
                        "pii_admission_request_id": f"pii-req-{uuid.uuid4().hex[:10]}",
                        "personal_data_consent": True,
                        "offer_acknowledgement": True,
                    }
                ),
            )
        )
        hit = await SelfBookingActiveOfferService(session).resolve_slot(
            conversation_id=conversation.id, slot_id=_SLOT
        )
        assert hit.outcome is ActiveOfferResolveOutcome.FOUND
        assert hit.starts_at == _STARTS
