"""PostgreSQL tests for SELF-BOOKING-COMMAND-03K1 confirm admission."""

from __future__ import annotations

import base64
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.booking_types import BookingClientMessageKind, BookingDialogAction
from app.core.ephemeral_pii_keys import EnvEphemeralPiiKeyProvider
from app.core.ephemeral_pii_types import EphemeralPiiTtlPolicy
from app.core.pii_admission_mac_keys import EnvPiiAdmissionMacKeyProvider
from app.core.self_booking_confirm_admission_types import (
    SelfBookingConfirmAdmissionOutcome,
)
from app.core.self_booking_create_types import SelfBookingCreatePendingState
from app.db.session import session_scope
from app.models.conversation import Channel, Conversation
from app.models.outbox import DeliveryStatus, DestinationType, OutboxMessage
from app.models.reply_plan import ReplyPlan
from app.models.self_booking_create_pending import SelfBookingCreatePending
from app.repositories import conversations as conversation_repo
from app.repositories import self_booking_create_pendings as pending_repo
from app.schemas.self_booking_confirm_action import SyntheticConfirmSelectedSlotAction
from app.services.ephemeral_pii_store import EphemeralPiiStore
from app.services.self_booking_active_offer import SelfBookingActiveOfferService
from app.services.self_booking_confirm_admission import (
    SelfBookingConfirmAdmissionService,
)
from app.services.self_booking_pii_admission import SelfBookingPiiAdmissionService
from app.services.takeover import apply_manager_takeover_in_session
from tests.pg_harness import truncate_foundation_tables

_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_SLOT_A = f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.1000"
_SLOT_B = f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.1100"
_STARTS_A = "2026-08-20T10:00:00+05:00"
_STARTS_B = "2026-08-20T11:00:00+05:00"
_PHONE = "+79001234567"
_NAME = "Test Client"
_PII_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
_MAC_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
_TTL_SECONDS = 900
_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _as_uuid(value: object) -> uuid.UUID:
    if type(value) is uuid.UUID:
        return value
    return uuid.UUID(str(value))


@pytest_asyncio.fixture(autouse=True)
async def confirm_admission_cleanup(
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


def _pii_store(
    session_factory: async_sessionmaker[AsyncSession],
) -> EphemeralPiiStore:
    return EphemeralPiiStore(
        session_factory=session_factory,
        key_provider=EnvEphemeralPiiKeyProvider(
            {
                "EPHEMERAL_PII_ACTIVE_KEY_ID": "TESTK1",
                "EPHEMERAL_PII_KEY_TESTK1": _PII_KEY_B64,
            }
        ),
        ttl_policy=EphemeralPiiTtlPolicy(_TTL_SECONDS),
    )


def _pii_admission(
    session_factory: async_sessionmaker[AsyncSession],
) -> SelfBookingPiiAdmissionService:
    return SelfBookingPiiAdmissionService(
        session_factory=session_factory,
        pii_store=_pii_store(session_factory),
        mac_key_provider=EnvPiiAdmissionMacKeyProvider(
            {
                "PII_ADMISSION_MAC_ACTIVE_KEY_ID": "MACK1",
                "PII_ADMISSION_MAC_KEY_MACK1": _MAC_KEY_B64,
            }
        ),
    )


def _confirm_action(
    *,
    slot_id: str = _SLOT_A,
    pii_admission_request_id: str | None = None,
) -> SyntheticConfirmSelectedSlotAction:
    return SyntheticConfirmSelectedSlotAction(
        kind="CONFIRM_SELECTED_SLOT",
        slot_id=slot_id,
        pii_admission_request_id=pii_admission_request_id
        or f"req-{uuid.uuid4().hex[:12]}",
        personal_data_consent=True,
        offer_acknowledgement=True,
    )


async def _seed_conversation(
    session_factory: async_sessionmaker[AsyncSession],
) -> Conversation:
    async with session_scope(session_factory) as session:
        conversation, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.SYNTHETIC,
            external_conversation_id=f"ca-{uuid.uuid4().hex[:12]}",
        )
        await session.refresh(conversation)
        return conversation


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


async def _activate_offer(
    session: AsyncSession,
    *,
    conversation: Conversation,
    slot_id: str,
    starts_at: str,
) -> None:
    outbound = OutboxMessage(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        idempotency_key=f"synthetic-outbound:ca:{uuid.uuid4()}",
        context_version=conversation.context_version,
        manager_epoch=conversation.manager_epoch,
        event_seq_hwm=conversation.current_event_seq,
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
    session.add(outbound)
    await session.flush()
    offer_svc = SelfBookingActiveOfferService(session, clock=lambda: _NOW)
    result = await offer_svc.activate_from_delivered_outbound(outbound=outbound)
    assert result.outcome.value in {"ACTIVATED", "REPLACED", "REPLAYED"}


async def _admit_pii(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    conversation: Conversation,
    request_id: str,
) -> None:
    await _pii_admission(session_factory).admit(
        conversation_id=_as_uuid(conversation.id),
        request_id=request_id,
        phone=_PHONE,
        client_name=_NAME,
    )


def _orch(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> SelfBookingConfirmAdmissionService:
    return SelfBookingConfirmAdmissionService(
        session,
        pii_store=_pii_store(session_factory),
        clock=lambda: _NOW,
    )


async def _count_pendings(session: AsyncSession) -> int:
    value = await session.scalar(
        select(func.count()).select_from(SelfBookingCreatePending)
    )
    return int(value or 0)


@pytest.mark.asyncio
async def test_happy_path_creates_pending_with_bindings(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _admit_pii(
        session_factory, conversation=conversation, request_id=request_id
    )
    action = _confirm_action(pii_admission_request_id=request_id)
    confirm_id = f"confirm-{uuid.uuid4().hex[:10]}"

    async with session_scope(session_factory) as session:
        await _activate_offer(
            session,
            conversation=conversation,
            slot_id=_SLOT_A,
            starts_at=_STARTS_A,
        )
        svc = _orch(session, session_factory)
        result = await svc.admit_from_confirm(
            conversation_id=conversation.id,
            channel="synthetic",
            confirm_external_message_id=confirm_id,
            action=action,
            fence_context_version=conversation.context_version,
            fence_manager_epoch=conversation.manager_epoch,
            fence_event_seq_hwm=conversation.current_event_seq,
        )
        assert result.outcome is SelfBookingConfirmAdmissionOutcome.ADMITTED
        assert result.pending_id is not None
        assert result.idempotency_key is not None
        assert len(result.idempotency_key) == 36

        row = await pending_repo.get_by_id(session, pending_id=result.pending_id)
        assert row is not None
        assert row.state == SelfBookingCreatePendingState.READY.value
        assert row.slot_id == _SLOT_A
        assert row.starts_at == _STARTS_A
        assert row.phone_ref_token
        assert row.name_ref_token
        assert row.fence_context_version == conversation.context_version
        assert row.fence_manager_epoch == conversation.manager_epoch
        assert row.fence_event_seq_hwm == conversation.current_event_seq
        assert row.idempotency_key == result.idempotency_key
        assert row.confirm_external_message_id == confirm_id
        assert await _count_pendings(session) == 1


@pytest.mark.asyncio
async def test_wrong_slot_zero_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _admit_pii(
        session_factory, conversation=conversation, request_id=request_id
    )
    action = _confirm_action(
        slot_id=_SLOT_B, pii_admission_request_id=request_id
    )

    async with session_scope(session_factory) as session:
        await _activate_offer(
            session,
            conversation=conversation,
            slot_id=_SLOT_A,
            starts_at=_STARTS_A,
        )
        svc = _orch(session, session_factory)
        result = await svc.admit_from_confirm(
            conversation_id=conversation.id,
            channel="synthetic",
            confirm_external_message_id=f"confirm-{uuid.uuid4().hex[:10]}",
            action=action,
            fence_context_version=conversation.context_version,
            fence_manager_epoch=conversation.manager_epoch,
            fence_event_seq_hwm=conversation.current_event_seq,
        )
        assert result.outcome is SelfBookingConfirmAdmissionOutcome.OFFER_NOT_ACTIVE
        assert result.pending_id is None
        assert await _count_pendings(session) == 0


@pytest.mark.asyncio
async def test_wrong_pii_admission_request_id_zero_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _admit_pii(
        session_factory, conversation=conversation, request_id=request_id
    )
    action = _confirm_action(
        pii_admission_request_id=f"req-{uuid.uuid4().hex[:12]}"
    )

    async with session_scope(session_factory) as session:
        await _activate_offer(
            session,
            conversation=conversation,
            slot_id=_SLOT_A,
            starts_at=_STARTS_A,
        )
        svc = _orch(session, session_factory)
        result = await svc.admit_from_confirm(
            conversation_id=conversation.id,
            channel="synthetic",
            confirm_external_message_id=f"confirm-{uuid.uuid4().hex[:10]}",
            action=action,
            fence_context_version=conversation.context_version,
            fence_manager_epoch=conversation.manager_epoch,
            fence_event_seq_hwm=conversation.current_event_seq,
        )
        assert result.outcome is SelfBookingConfirmAdmissionOutcome.PII_NOT_FOUND
        assert await _count_pendings(session) == 0


@pytest.mark.asyncio
async def test_expired_refs_zero_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _admit_pii(
        session_factory, conversation=conversation, request_id=request_id
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE ephemeral_pii_values
                    SET
                        created_at = statement_timestamp() - interval '2 hours',
                        expires_at = statement_timestamp() - interval '1 second'
                    """
                )
            )
    action = _confirm_action(pii_admission_request_id=request_id)

    async with session_scope(session_factory) as session:
        await _activate_offer(
            session,
            conversation=conversation,
            slot_id=_SLOT_A,
            starts_at=_STARTS_A,
        )
        svc = _orch(session, session_factory)
        result = await svc.admit_from_confirm(
            conversation_id=conversation.id,
            channel="synthetic",
            confirm_external_message_id=f"confirm-{uuid.uuid4().hex[:10]}",
            action=action,
            fence_context_version=conversation.context_version,
            fence_manager_epoch=conversation.manager_epoch,
            fence_event_seq_hwm=conversation.current_event_seq,
        )
        assert result.outcome is SelfBookingConfirmAdmissionOutcome.PII_EXPIRED
        assert await _count_pendings(session) == 0


@pytest.mark.asyncio
async def test_stale_fence_zero_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _admit_pii(
        session_factory, conversation=conversation, request_id=request_id
    )
    action = _confirm_action(pii_admission_request_id=request_id)

    async with session_scope(session_factory) as session:
        await _activate_offer(
            session,
            conversation=conversation,
            slot_id=_SLOT_A,
            starts_at=_STARTS_A,
        )
        svc = _orch(session, session_factory)
        result = await svc.admit_from_confirm(
            conversation_id=conversation.id,
            channel="synthetic",
            confirm_external_message_id=f"confirm-{uuid.uuid4().hex[:10]}",
            action=action,
            fence_context_version=conversation.context_version + 1,
            fence_manager_epoch=conversation.manager_epoch,
            fence_event_seq_hwm=conversation.current_event_seq,
        )
        assert result.outcome is SelfBookingConfirmAdmissionOutcome.FAIL_CLOSED
        assert result.reason_code == "FENCE_STALE"
        assert await _count_pendings(session) == 0


@pytest.mark.asyncio
async def test_manager_takeover_zero_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _admit_pii(
        session_factory, conversation=conversation, request_id=request_id
    )
    action = _confirm_action(pii_admission_request_id=request_id)

    async with session_scope(session_factory) as session:
        await _activate_offer(
            session,
            conversation=conversation,
            slot_id=_SLOT_A,
            starts_at=_STARTS_A,
        )
        takeover_conversation, _cancelled, changed = (
            await apply_manager_takeover_in_session(
                session,
                conversation_id=conversation.id,
                now=_NOW,
            )
        )
        assert changed is True
        assert takeover_conversation.manager_takeover_at is not None

        svc = _orch(session, session_factory)
        result = await svc.admit_from_confirm(
            conversation_id=conversation.id,
            channel="synthetic",
            confirm_external_message_id=f"confirm-{uuid.uuid4().hex[:10]}",
            action=action,
            fence_context_version=takeover_conversation.context_version,
            fence_manager_epoch=takeover_conversation.manager_epoch,
            fence_event_seq_hwm=takeover_conversation.current_event_seq,
        )
        assert result.outcome is SelfBookingConfirmAdmissionOutcome.HANDOFF_BLOCKED
        assert await _count_pendings(session) == 0


@pytest.mark.asyncio
async def test_duplicate_confirm_returns_existing_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _admit_pii(
        session_factory, conversation=conversation, request_id=request_id
    )
    action = _confirm_action(pii_admission_request_id=request_id)
    confirm_id = f"confirm-{uuid.uuid4().hex[:10]}"

    async with session_scope(session_factory) as session:
        await _activate_offer(
            session,
            conversation=conversation,
            slot_id=_SLOT_A,
            starts_at=_STARTS_A,
        )
        svc = _orch(session, session_factory)
        kwargs = dict(
            conversation_id=conversation.id,
            channel="synthetic",
            confirm_external_message_id=confirm_id,
            action=action,
            fence_context_version=conversation.context_version,
            fence_manager_epoch=conversation.manager_epoch,
            fence_event_seq_hwm=conversation.current_event_seq,
        )
        first = await svc.admit_from_confirm(**kwargs)
        assert first.outcome is SelfBookingConfirmAdmissionOutcome.ADMITTED
        second = await svc.admit_from_confirm(**kwargs)
        assert second.outcome is SelfBookingConfirmAdmissionOutcome.DUPLICATE
        assert second.pending_id == first.pending_id
        assert second.idempotency_key == first.idempotency_key
        assert await _count_pendings(session) == 1


@pytest.mark.asyncio
async def test_no_plaintext_leakage_in_result_or_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _admit_pii(
        session_factory, conversation=conversation, request_id=request_id
    )
    action = _confirm_action(pii_admission_request_id=request_id)

    async with session_scope(session_factory) as session:
        await _activate_offer(
            session,
            conversation=conversation,
            slot_id=_SLOT_A,
            starts_at=_STARTS_A,
        )
        svc = _orch(session, session_factory)
        result = await svc.admit_from_confirm(
            conversation_id=conversation.id,
            channel="synthetic",
            confirm_external_message_id=f"confirm-{uuid.uuid4().hex[:10]}",
            action=action,
            fence_context_version=conversation.context_version,
            fence_manager_epoch=conversation.manager_epoch,
            fence_event_seq_hwm=conversation.current_event_seq,
        )
        assert result.outcome is SelfBookingConfirmAdmissionOutcome.ADMITTED
        rendered = repr(result)
        assert _PHONE not in rendered
        assert _NAME not in rendered
        assert _SLOT_A not in rendered
        assert result.idempotency_key not in rendered

        row = await pending_repo.get_by_id(session, pending_id=result.pending_id)
        assert row is not None
        row_repr = repr(row)
        assert _PHONE not in row_repr
        assert _NAME not in row_repr
        assert row.phone_ref_token not in row_repr
        assert row.name_ref_token not in row_repr


@pytest.mark.asyncio
async def test_no_reply_plan_mutation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _admit_pii(
        session_factory, conversation=conversation, request_id=request_id
    )
    action = _confirm_action(pii_admission_request_id=request_id)

    async with session_scope(session_factory) as session:
        await _activate_offer(
            session,
            conversation=conversation,
            slot_id=_SLOT_A,
            starts_at=_STARTS_A,
        )
        before = await session.scalar(select(func.count()).select_from(ReplyPlan))
        svc = _orch(session, session_factory)
        result = await svc.admit_from_confirm(
            conversation_id=conversation.id,
            channel="synthetic",
            confirm_external_message_id=f"confirm-{uuid.uuid4().hex[:10]}",
            action=action,
            fence_context_version=conversation.context_version,
            fence_manager_epoch=conversation.manager_epoch,
            fence_event_seq_hwm=conversation.current_event_seq,
        )
        assert result.outcome is SelfBookingConfirmAdmissionOutcome.ADMITTED
        after = await session.scalar(select(func.count()).select_from(ReplyPlan))
        assert before == 0
        assert after == 0
