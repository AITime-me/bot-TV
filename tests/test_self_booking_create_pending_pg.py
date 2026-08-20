"""PostgreSQL tests for SELF-BOOKING-COMMAND-01 durable foundation."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.self_booking_create_types import (
    SelfBookingCreateAdmitOutcome,
    SelfBookingCreatePendingState,
)
from app.db.session import session_scope
from app.models.conversation import Channel, Conversation
from app.models.self_booking_create_pending import SelfBookingCreatePending
from app.repositories import conversations as conversation_repo
from app.repositories import self_booking_create_pendings as pending_repo
from app.services.self_booking_create_pending import SelfBookingCreatePendingService
from app.services.takeover import apply_manager_takeover_in_session
from tests.pg_harness import truncate_foundation_tables

_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_SLOT = f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.1000"
_STARTS = "2026-08-20T10:00:00+05:00"
_PHONE_REF = "epii_" + ("a" * 27)
_NAME_REF = "epii_" + ("b" * 27)
_KEY = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture(autouse=True)
async def self_booking_row_cleanup(
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
            external_conversation_id=f"sbc-{uuid.uuid4().hex[:12]}",
        )
        await session.refresh(conversation)
        return conversation


def _admit_kwargs(conversation: Conversation, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "conversation_id": conversation.id,
        "channel": "synthetic",
        "confirm_external_message_id": f"confirm-{uuid.uuid4().hex[:10]}",
        "slot_id": _SLOT,
        "starts_at": _STARTS,
        "fence_context_version": conversation.context_version,
        "fence_manager_epoch": conversation.manager_epoch,
        "fence_event_seq_hwm": conversation.current_event_seq,
        "personal_data_consent": True,
        "offer_acknowledgement": True,
        "phone_ref_token": _PHONE_REF,
        "name_ref_token": _NAME_REF,
        "idempotency_key": _KEY,
        "max_attempts": 3,
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_migration_self_booking_create_pendings(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        exists = await session.scalar(
            text(
                "SELECT to_regclass('public.self_booking_create_pendings') "
                "IS NOT NULL"
            )
        )
        assert exists is True
        partial = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = "
                "'uq_self_booking_create_pendings_active_conversation'"
            )
        )
        assert partial is not None
        assert "UNIQUE" in partial.upper()
        assert "READY" in partial
        confirm = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'uq_self_booking_create_pendings_confirm'"
            )
        )
        assert confirm is not None
        assert "UNIQUE" in confirm.upper()


@pytest.mark.asyncio
async def test_admit_and_dedupe_confirmation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    confirm_id = "confirm-dedupe-1"
    kwargs = _admit_kwargs(conversation, confirm_external_message_id=confirm_id)

    async with session_scope(session_factory) as session:
        svc = SelfBookingCreatePendingService(session, clock=lambda: _NOW)
        first = await svc.admit_confirmed(**kwargs)
        assert first.outcome is SelfBookingCreateAdmitOutcome.ADMITTED
        assert first.pending_id is not None
        assert first.idempotency_key == _KEY

        second = await svc.admit_confirmed(**kwargs)
        assert second.outcome is SelfBookingCreateAdmitOutcome.DUPLICATE
        assert second.pending_id == first.pending_id


@pytest.mark.asyncio
async def test_concurrent_claim_admits_one_executor(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    kwargs = _admit_kwargs(conversation)

    async with session_scope(session_factory) as session:
        svc = SelfBookingCreatePendingService(session, clock=lambda: _NOW)
        admitted = await svc.admit_confirmed(**kwargs)
        assert admitted.outcome is SelfBookingCreateAdmitOutcome.ADMITTED
        pending_id = admitted.pending_id
        assert pending_id is not None

    async def _claim(token: uuid.UUID) -> uuid.UUID | None:
        async with session_scope(session_factory) as session:
            svc = SelfBookingCreatePendingService(session, clock=lambda: _NOW)
            row = await svc.claim_for_execution(
                pending_id=pending_id, lease_token=token
            )
            return row.execution_lease_token if row is not None else None

    t1 = uuid.uuid4()
    t2 = uuid.uuid4()
    results = await asyncio.gather(_claim(t1), _claim(t2))
    winners = [token for token in results if token is not None]
    assert len(winners) == 1
    assert winners[0] in {t1, t2}


@pytest.mark.asyncio
async def test_stale_lease_reclaim_same_idempotency(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    kwargs = _admit_kwargs(conversation)

    async with session_scope(session_factory) as session:
        svc = SelfBookingCreatePendingService(session, clock=lambda: _NOW)
        admitted = await svc.admit_confirmed(**kwargs)
        pending_id = admitted.pending_id
        assert pending_id is not None
        first = await svc.claim_for_execution(
            pending_id=pending_id, lease_token=uuid.uuid4()
        )
        assert first is not None
        assert first.state == SelfBookingCreatePendingState.EXECUTING.value
        key = first.idempotency_key
        # Force lease expiry in the past.
        await session.execute(
            update(SelfBookingCreatePending)
            .where(SelfBookingCreatePending.id == first.id)
            .values(execution_lease_expires_at=_NOW - timedelta(seconds=1))
        )
        await session.flush()

        reclaimed = await svc.claim_for_execution(
            pending_id=pending_id, lease_token=uuid.uuid4()
        )
        assert reclaimed is not None
        assert reclaimed.state == SelfBookingCreatePendingState.EXECUTING.value
        assert reclaimed.idempotency_key == key
        assert reclaimed.attempt_count == 2


@pytest.mark.asyncio
async def test_stale_conversation_fence_cancels(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    kwargs = _admit_kwargs(conversation)

    async with session_scope(session_factory) as session:
        svc = SelfBookingCreatePendingService(session, clock=lambda: _NOW)
        admitted = await svc.admit_confirmed(**kwargs)
        pending_id = admitted.pending_id
        assert pending_id is not None

        locked = await conversation_repo.get_by_id_for_update(
            session, conversation_id=conversation.id
        )
        assert locked is not None
        await conversation_repo.bump_context_for_new_message(
            session, conversation=locked, activity_at=_NOW
        )

        cancelled = await svc.cancel_if_conversation_fences_stale(
            pending_id=pending_id
        )
        assert cancelled is True
        row = await pending_repo.get_by_id(session, pending_id=pending_id)
        assert row is not None
        assert row.state == SelfBookingCreatePendingState.CANCELLED.value
        assert row.result_code == "FENCE_STALE_OR_TAKEOVER"


@pytest.mark.asyncio
async def test_manager_takeover_cancels(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    kwargs = _admit_kwargs(conversation)

    async with session_scope(session_factory) as session:
        svc = SelfBookingCreatePendingService(session, clock=lambda: _NOW)
        admitted = await svc.admit_confirmed(**kwargs)
        pending_id = admitted.pending_id
        assert pending_id is not None

        takeover_conversation, _cancelled, changed = (
            await apply_manager_takeover_in_session(
                session,
                conversation_id=conversation.id,
                now=_NOW,
            )
        )
        assert changed is True
        assert takeover_conversation.manager_takeover_at is not None

        cancelled = await svc.cancel_if_conversation_fences_stale(
            pending_id=pending_id
        )
        assert cancelled is True
        row = await pending_repo.get_by_id(session, pending_id=pending_id)
        assert row is not None
        assert row.state == SelfBookingCreatePendingState.CANCELLED.value
        assert row.result_code == "FENCE_STALE_OR_TAKEOVER"


@pytest.mark.asyncio
async def test_max_attempts_expires(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    kwargs = _admit_kwargs(conversation, max_attempts=1, idempotency_key=_KEY)

    async with session_scope(session_factory) as session:
        svc = SelfBookingCreatePendingService(session, clock=lambda: _NOW)
        admitted = await svc.admit_confirmed(**kwargs)
        pending_id = admitted.pending_id
        assert pending_id is not None

        first = await svc.claim_for_execution(pending_id=pending_id)
        assert first is not None
        # Release back to READY after one attempt.
        ok = await pending_repo.release_to_ready(
            session,
            row=first,
            lease_token=first.execution_lease_token,
            result_code="RETRY_LATER",
            now=_NOW,
        )
        assert ok is True

        second = await svc.claim_for_execution(pending_id=pending_id)
        assert second is None
        row = await pending_repo.get_by_id(session, pending_id=pending_id)
        assert row is not None
        assert row.state == SelfBookingCreatePendingState.EXPIRED.value
        assert row.result_code == "MAX_ATTEMPTS_EXCEEDED"


@pytest.mark.asyncio
async def test_pending_repr_hides_pii_in_pg_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    kwargs = _admit_kwargs(conversation)

    async with session_scope(session_factory) as session:
        svc = SelfBookingCreatePendingService(session, clock=lambda: _NOW)
        admitted = await svc.admit_confirmed(**kwargs)
        assert admitted.pending_id is not None
        row = await pending_repo.get_by_id(session, pending_id=admitted.pending_id)
        assert row is not None
        rendered = repr(row)
        assert _PHONE_REF not in rendered
        assert _NAME_REF not in rendered
        assert _SLOT not in rendered
        assert _KEY not in rendered
        assert kwargs["confirm_external_message_id"] not in rendered
