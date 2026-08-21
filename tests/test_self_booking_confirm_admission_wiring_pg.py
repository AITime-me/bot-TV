"""PostgreSQL tests for SELF-BOOKING-COMMAND-03K2 inbound confirm wiring."""

from __future__ import annotations

import base64
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.booking_types import BookingClientMessageKind, BookingDialogAction
from app.core.ephemeral_pii_keys import EnvEphemeralPiiKeyProvider
from app.core.ephemeral_pii_types import EphemeralPiiTtlPolicy
from app.core.pii_admission_mac_keys import EnvPiiAdmissionMacKeyProvider
from app.core.self_booking_create_types import SelfBookingCreatePendingState
from app.db.session import session_scope
from app.models.conversation import Channel, Conversation
from app.models.outbox import DeliveryStatus, DestinationType, OutboxMessage
from app.models.self_booking_create_pending import SelfBookingCreatePending
from app.repositories import conversations as conversation_repo
from app.repositories import self_booking_create_pendings as pending_repo
from app.schemas.inbound import SyntheticInboundEvent
from app.schemas.self_booking_confirm_action import SyntheticConfirmSelectedSlotAction
from app.services.ephemeral_pii_store import EphemeralPiiStore
from app.services.inbound import InboundService
from app.services.self_booking_active_offer import SelfBookingActiveOfferService
from app.services.self_booking_pii_admission import SelfBookingPiiAdmissionService
from app.services.takeover import apply_manager_takeover_in_session
from tests.pg_harness import truncate_foundation_tables

_REPO = Path(__file__).resolve().parents[1]
_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_SLOT_A = f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.1000"
_SLOT_B = f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.1100"
_STARTS_A = "2026-08-20T10:00:00+05:00"
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
async def confirm_wiring_cleanup(
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


def _confirm_event(
    *,
    conversation_ext: str,
    message_id: str,
    slot_id: str = _SLOT_A,
    request_id: str,
) -> SyntheticInboundEvent:
    return SyntheticInboundEvent(
        external_conversation_id=conversation_ext,
        external_message_id=message_id,
        text="structured-confirm",
        action=SyntheticConfirmSelectedSlotAction(
            kind="CONFIRM_SELECTED_SLOT",
            slot_id=slot_id,
            pii_admission_request_id=request_id,
            personal_data_consent=True,
            offer_acknowledgement=True,
        ),
    )


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


async def _seed_conversation_with_offer(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    conversation_ext: str,
    slot_id: str = _SLOT_A,
    starts_at: str = _STARTS_A,
) -> Conversation:
    async with session_scope(session_factory) as session:
        conversation, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.SYNTHETIC,
            external_conversation_id=conversation_ext,
        )
        outbound = OutboxMessage(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            idempotency_key=f"synthetic-outbound:wire:{uuid.uuid4()}",
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
        await SelfBookingActiveOfferService(
            session, clock=lambda: _NOW
        ).activate_from_delivered_outbound(outbound=outbound)
        await session.refresh(conversation)
        return conversation


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


async def _count_pendings(session: AsyncSession) -> int:
    value = await session.scalar(
        select(func.count()).select_from(SelfBookingCreatePending)
    )
    return int(value or 0)


@pytest.mark.asyncio
async def test_inbound_confirm_admits_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conv_ext = f"wire-ok-{uuid.uuid4().hex[:10]}"
    conversation = await _seed_conversation_with_offer(
        session_factory, conversation_ext=conv_ext
    )
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _admit_pii(
        session_factory, conversation=conversation, request_id=request_id
    )
    message_id = f"confirm-{uuid.uuid4().hex[:10]}"

    async with session_scope(session_factory) as session:
        accepted = await InboundService(
            session, pii_store=_pii_store(session_factory)
        ).accept(
            _confirm_event(
                conversation_ext=conv_ext,
                message_id=message_id,
                request_id=request_id,
            )
        )
        assert accepted.created_inbox is True
        assert await _count_pendings(session) == 1
        row = await pending_repo.get_by_confirm(
            session, channel="synthetic", confirm_external_message_id=message_id
        )
        assert row is not None
        assert row.state == SelfBookingCreatePendingState.READY.value
        assert row.slot_id == _SLOT_A
        assert row.starts_at == _STARTS_A
        assert row.fence_context_version == accepted.conversation.context_version
        assert row.fence_manager_epoch == accepted.conversation.manager_epoch
        assert row.fence_event_seq_hwm == accepted.conversation.current_event_seq
        assert row.idempotency_key


@pytest.mark.asyncio
async def test_inbound_duplicate_confirm_one_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conv_ext = f"wire-dup-{uuid.uuid4().hex[:10]}"
    conversation = await _seed_conversation_with_offer(
        session_factory, conversation_ext=conv_ext
    )
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _admit_pii(
        session_factory, conversation=conversation, request_id=request_id
    )
    message_id = f"confirm-{uuid.uuid4().hex[:10]}"
    event = _confirm_event(
        conversation_ext=conv_ext,
        message_id=message_id,
        request_id=request_id,
    )

    async with session_scope(session_factory) as session:
        first = await InboundService(
            session, pii_store=_pii_store(session_factory)
        ).accept(event)
        assert first.created_inbox is True
        second = await InboundService(
            session, pii_store=_pii_store(session_factory)
        ).accept(event)
        assert second.duplicate is True
        assert await _count_pendings(session) == 1


@pytest.mark.asyncio
async def test_inbound_inactive_slot_zero_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conv_ext = f"wire-slot-{uuid.uuid4().hex[:10]}"
    conversation = await _seed_conversation_with_offer(
        session_factory, conversation_ext=conv_ext
    )
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _admit_pii(
        session_factory, conversation=conversation, request_id=request_id
    )

    async with session_scope(session_factory) as session:
        await InboundService(
            session, pii_store=_pii_store(session_factory)
        ).accept(
            _confirm_event(
                conversation_ext=conv_ext,
                message_id=f"confirm-{uuid.uuid4().hex[:10]}",
                slot_id=_SLOT_B,
                request_id=request_id,
            )
        )
        assert await _count_pendings(session) == 0


@pytest.mark.asyncio
async def test_inbound_missing_pii_admission_zero_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conv_ext = f"wire-pii-{uuid.uuid4().hex[:10]}"
    await _seed_conversation_with_offer(
        session_factory, conversation_ext=conv_ext
    )

    async with session_scope(session_factory) as session:
        await InboundService(
            session, pii_store=_pii_store(session_factory)
        ).accept(
            _confirm_event(
                conversation_ext=conv_ext,
                message_id=f"confirm-{uuid.uuid4().hex[:10]}",
                request_id=f"req-{uuid.uuid4().hex[:12]}",
            )
        )
        assert await _count_pendings(session) == 0


@pytest.mark.asyncio
async def test_inbound_manager_takeover_zero_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conv_ext = f"wire-take-{uuid.uuid4().hex[:10]}"
    conversation = await _seed_conversation_with_offer(
        session_factory, conversation_ext=conv_ext
    )
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _admit_pii(
        session_factory, conversation=conversation, request_id=request_id
    )

    async with session_scope(session_factory) as session:
        takeover_conversation, _cancelled, changed = (
            await apply_manager_takeover_in_session(
                session,
                conversation_id=conversation.id,
                now=_NOW,
            )
        )
        assert changed is True
        assert takeover_conversation.manager_takeover_at is not None
        await InboundService(
            session, pii_store=_pii_store(session_factory)
        ).accept(
            _confirm_event(
                conversation_ext=conv_ext,
                message_id=f"confirm-{uuid.uuid4().hex[:10]}",
                request_id=request_id,
            )
        )
        assert await _count_pendings(session) == 0


@pytest.mark.asyncio
async def test_inbound_plain_text_zero_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conv_ext = f"wire-plain-{uuid.uuid4().hex[:10]}"
    conversation = await _seed_conversation_with_offer(
        session_factory, conversation_ext=conv_ext
    )
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _admit_pii(
        session_factory, conversation=conversation, request_id=request_id
    )

    async with session_scope(session_factory) as session:
        await InboundService(
            session, pii_store=_pii_store(session_factory)
        ).accept(
            SyntheticInboundEvent(
                external_conversation_id=conv_ext,
                external_message_id=f"plain-{uuid.uuid4().hex[:10]}",
                text="hello free form",
            )
        )
        assert await _count_pendings(session) == 0


@pytest.mark.asyncio
async def test_inbound_confirm_without_pii_store_zero_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conv_ext = f"wire-nopii-{uuid.uuid4().hex[:10]}"
    conversation = await _seed_conversation_with_offer(
        session_factory, conversation_ext=conv_ext
    )
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _admit_pii(
        session_factory, conversation=conversation, request_id=request_id
    )

    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            _confirm_event(
                conversation_ext=conv_ext,
                message_id=f"confirm-{uuid.uuid4().hex[:10]}",
                request_id=request_id,
            )
        )
        assert accepted.created_inbox is True
        assert await _count_pendings(session) == 0


def test_inbound_source_has_no_direct_create_call() -> None:
    inbound = (_REPO / "app" / "services" / "inbound.py").read_text(encoding="utf-8")
    assert "admit_from_confirm" in inbound
    assert ".confirm_selected_slot" not in inbound
    assert "BookingCreateHttp" not in inbound
    assert "create_booking" not in inbound
    assert "read_plaintext" not in inbound
