"""PostgreSQL tests for SELF-BOOKING-COMMAND-03C active-offer binding."""

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
from app.repositories import self_booking_active_offers as offer_repo
from app.services.self_booking_active_offer import SelfBookingActiveOfferService
from tests.pg_harness import truncate_foundation_tables

_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_SLOT_A = f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.1000"
_SLOT_B = f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.1100"
_STARTS_A = "2026-08-20T10:00:00+05:00"
_STARTS_B = "2026-08-20T11:00:00+05:00"
_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture(autouse=True)
async def active_offer_cleanup(
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


async def _seed_conversation(
    session_factory: async_sessionmaker[AsyncSession],
) -> Conversation:
    async with session_scope(session_factory) as session:
        conversation, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.SYNTHETIC,
            external_conversation_id=f"ao-{uuid.uuid4().hex[:12]}",
        )
        await session.refresh(conversation)
        return conversation


def _offer_payload(
    *,
    slot_id: str,
    starts_at: str,
) -> dict[str, Any]:
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


async def _insert_delivered_outbound(
    session: AsyncSession,
    *,
    conversation: Conversation,
    outbound_id: uuid.UUID,
    context_version: int,
    manager_epoch: int,
    event_seq_hwm: int,
    slot_id: str,
    starts_at: str,
) -> OutboxMessage:
    row = OutboxMessage(
        id=outbound_id,
        conversation_id=conversation.id,
        idempotency_key=f"synthetic-outbound:active-offer:{outbound_id}",
        context_version=context_version,
        manager_epoch=manager_epoch,
        event_seq_hwm=event_seq_hwm,
        destination_type=DestinationType.SYNTHETIC_OUTBOUND.value,
        payload_json=_offer_payload(slot_id=slot_id, starts_at=starts_at),
        delivery_status=DeliveryStatus.DELIVERED.value,
        admitted_at=_NOW,
        attempt_count=1,
        max_attempts=5,
        lease_version=1,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_activate_offer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    outbound_id = uuid.uuid4()

    async with session_scope(session_factory) as session:
        outbound = await _insert_delivered_outbound(
            session,
            conversation=conversation,
            outbound_id=outbound_id,
            context_version=1,
            manager_epoch=0,
            event_seq_hwm=1,
            slot_id=_SLOT_A,
            starts_at=_STARTS_A,
        )
        svc = SelfBookingActiveOfferService(session, clock=lambda: _NOW)
        result = await svc.activate_from_delivered_outbound(outbound=outbound)
        assert result.outcome is ActiveOfferActivateOutcome.ACTIVATED

        resolved = await svc.resolve_slot(
            conversation_id=conversation.id, slot_id=_SLOT_A
        )
        assert resolved.outcome is ActiveOfferResolveOutcome.FOUND
        assert resolved.starts_at == _STARTS_A
        assert resolved.source_outbound_id == outbound_id


@pytest.mark.asyncio
async def test_replace_newer_offer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    old_id = uuid.uuid4()
    new_id = uuid.uuid4()

    async with session_scope(session_factory) as session:
        old = await _insert_delivered_outbound(
            session,
            conversation=conversation,
            outbound_id=old_id,
            context_version=1,
            manager_epoch=0,
            event_seq_hwm=1,
            slot_id=_SLOT_A,
            starts_at=_STARTS_A,
        )
        new = await _insert_delivered_outbound(
            session,
            conversation=conversation,
            outbound_id=new_id,
            context_version=2,
            manager_epoch=0,
            event_seq_hwm=2,
            slot_id=_SLOT_B,
            starts_at=_STARTS_B,
        )
        svc = SelfBookingActiveOfferService(session, clock=lambda: _NOW)
        assert (
            await svc.activate_from_delivered_outbound(outbound=old)
        ).outcome is ActiveOfferActivateOutcome.ACTIVATED
        replaced = await svc.activate_from_delivered_outbound(outbound=new)
        assert replaced.outcome is ActiveOfferActivateOutcome.REPLACED

        miss = await svc.resolve_slot(
            conversation_id=conversation.id, slot_id=_SLOT_A
        )
        assert miss.outcome is ActiveOfferResolveOutcome.NOT_ACTIVE
        hit = await svc.resolve_slot(
            conversation_id=conversation.id, slot_id=_SLOT_B
        )
        assert hit.outcome is ActiveOfferResolveOutcome.FOUND
        assert hit.starts_at == _STARTS_B
        assert hit.source_outbound_id == new_id


@pytest.mark.asyncio
async def test_delayed_old_cannot_win(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    newer_id = uuid.uuid4()
    older_id = uuid.uuid4()

    async with session_scope(session_factory) as session:
        newer = await _insert_delivered_outbound(
            session,
            conversation=conversation,
            outbound_id=newer_id,
            context_version=5,
            manager_epoch=0,
            event_seq_hwm=5,
            slot_id=_SLOT_B,
            starts_at=_STARTS_B,
        )
        older = await _insert_delivered_outbound(
            session,
            conversation=conversation,
            outbound_id=older_id,
            context_version=2,
            manager_epoch=0,
            event_seq_hwm=2,
            slot_id=_SLOT_A,
            starts_at=_STARTS_A,
        )
        svc = SelfBookingActiveOfferService(session, clock=lambda: _NOW)
        assert (
            await svc.activate_from_delivered_outbound(outbound=newer)
        ).outcome is ActiveOfferActivateOutcome.ACTIVATED
        delayed = await svc.activate_from_delivered_outbound(outbound=older)
        assert delayed.outcome is ActiveOfferActivateOutcome.IGNORED_STALE

        hit = await svc.resolve_slot(
            conversation_id=conversation.id, slot_id=_SLOT_B
        )
        assert hit.outcome is ActiveOfferResolveOutcome.FOUND
        assert hit.source_outbound_id == newer_id
        row = await offer_repo.get_by_conversation(
            session, conversation_id=conversation.id
        )
        assert row is not None
        assert row.source_outbound_id == newer_id


@pytest.mark.asyncio
async def test_replay_same_outbound(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    outbound_id = uuid.uuid4()

    async with session_scope(session_factory) as session:
        outbound = await _insert_delivered_outbound(
            session,
            conversation=conversation,
            outbound_id=outbound_id,
            context_version=1,
            manager_epoch=0,
            event_seq_hwm=1,
            slot_id=_SLOT_A,
            starts_at=_STARTS_A,
        )
        svc = SelfBookingActiveOfferService(session, clock=lambda: _NOW)
        first = await svc.activate_from_delivered_outbound(outbound=outbound)
        second = await svc.activate_from_delivered_outbound(outbound=outbound)
        assert first.outcome is ActiveOfferActivateOutcome.ACTIVATED
        assert second.outcome is ActiveOfferActivateOutcome.REPLAYED
        resolved = await svc.resolve_slot(
            conversation_id=conversation.id, slot_id=_SLOT_A
        )
        assert resolved.outcome is ActiveOfferResolveOutcome.FOUND
        assert resolved.source_outbound_id == outbound_id


@pytest.mark.asyncio
async def test_membership_miss(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    outbound_id = uuid.uuid4()

    async with session_scope(session_factory) as session:
        outbound = await _insert_delivered_outbound(
            session,
            conversation=conversation,
            outbound_id=outbound_id,
            context_version=1,
            manager_epoch=0,
            event_seq_hwm=1,
            slot_id=_SLOT_A,
            starts_at=_STARTS_A,
        )
        svc = SelfBookingActiveOfferService(session, clock=lambda: _NOW)
        await svc.activate_from_delivered_outbound(outbound=outbound)
        miss = await svc.resolve_slot(
            conversation_id=conversation.id, slot_id=_SLOT_B
        )
        assert miss.outcome is ActiveOfferResolveOutcome.NOT_ACTIVE


@pytest.mark.asyncio
async def test_newer_manager_epoch_beats_higher_context(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Post-takeover epoch must replace a pre-takeover offer with higher context."""

    conversation = await _seed_conversation(session_factory)
    pre_takeover_id = uuid.uuid4()
    post_takeover_id = uuid.uuid4()

    async with session_scope(session_factory) as session:
        pre = await _insert_delivered_outbound(
            session,
            conversation=conversation,
            outbound_id=pre_takeover_id,
            context_version=50,
            manager_epoch=0,
            event_seq_hwm=50,
            slot_id=_SLOT_A,
            starts_at=_STARTS_A,
        )
        post = await _insert_delivered_outbound(
            session,
            conversation=conversation,
            outbound_id=post_takeover_id,
            context_version=1,
            manager_epoch=1,
            event_seq_hwm=1,
            slot_id=_SLOT_B,
            starts_at=_STARTS_B,
        )
        svc = SelfBookingActiveOfferService(session, clock=lambda: _NOW)
        assert (
            await svc.activate_from_delivered_outbound(outbound=pre)
        ).outcome is ActiveOfferActivateOutcome.ACTIVATED
        replaced = await svc.activate_from_delivered_outbound(outbound=post)
        assert replaced.outcome is ActiveOfferActivateOutcome.REPLACED
        hit = await svc.resolve_slot(
            conversation_id=conversation.id, slot_id=_SLOT_B
        )
        assert hit.outcome is ActiveOfferResolveOutcome.FOUND
        assert hit.source_outbound_id == post_takeover_id
        stale = await svc.activate_from_delivered_outbound(outbound=pre)
        assert stale.outcome is ActiveOfferActivateOutcome.IGNORED_STALE


@pytest.mark.asyncio
async def test_explicit_invalidate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    outbound_id = uuid.uuid4()

    async with session_scope(session_factory) as session:
        outbound = await _insert_delivered_outbound(
            session,
            conversation=conversation,
            outbound_id=outbound_id,
            context_version=1,
            manager_epoch=0,
            event_seq_hwm=1,
            slot_id=_SLOT_A,
            starts_at=_STARTS_A,
        )
        svc = SelfBookingActiveOfferService(session, clock=lambda: _NOW)
        await svc.activate_from_delivered_outbound(outbound=outbound)
        assert await svc.invalidate(conversation_id=conversation.id) is True
        miss = await svc.resolve_slot(
            conversation_id=conversation.id, slot_id=_SLOT_A
        )
        assert miss.outcome is ActiveOfferResolveOutcome.NOT_ACTIVE
        assert await svc.invalidate(conversation_id=conversation.id) is False
