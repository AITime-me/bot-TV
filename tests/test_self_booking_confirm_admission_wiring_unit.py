"""Unit tests for SELF-BOOKING-COMMAND-03K2 inbound confirm admission wiring."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.booking_input import SyntheticBookingInput
from app.schemas.inbound import SyntheticInboundEvent
from app.schemas.self_booking_confirm_action import SyntheticConfirmSelectedSlotAction
from app.services.inbound import InboundService

_REPO = Path(__file__).resolve().parents[1]
_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_SLOT = f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.1000"
_PII_REQ = "req-wire-confirm-1"


def _confirm_action(
    *,
    slot_id: str = _SLOT,
    request_id: str = _PII_REQ,
) -> SyntheticConfirmSelectedSlotAction:
    return SyntheticConfirmSelectedSlotAction(
        kind="CONFIRM_SELECTED_SLOT",
        slot_id=slot_id,
        pii_admission_request_id=request_id,
        personal_data_consent=True,
        offer_acknowledgement=True,
    )


def _confirm_event(
    *,
    message_id: str = "confirm-msg-1",
    conversation_id: str = "conv-wire-1",
) -> SyntheticInboundEvent:
    return SyntheticInboundEvent(
        external_conversation_id=conversation_id,
        external_message_id=message_id,
        text="structured-confirm",
        action=_confirm_action(),
    )


class _FakePiiStore:
    async def booking_phone_write_pair_alive(self, *_a: Any, **_k: Any) -> bool:
        return True


def _async_session() -> MagicMock:
    session = MagicMock()
    session.flush = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_confirm_calls_admit_from_confirm_with_fences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _async_session()
    conversation = MagicMock()
    conversation.id = uuid.uuid4()
    conversation.context_version = 3
    conversation.manager_epoch = 1
    conversation.current_event_seq = 7
    conversation.status = "OPEN"
    conversation.handoff_state = "BOT_ACTIVE"
    conversation.manager_takeover_at = None
    conversation.ownership = "BOT"
    conversation.active_reply_plan_id = None

    inbox = MagicMock()
    inbox.id = uuid.uuid4()
    inbox.conversation_id = conversation.id

    outbox = MagicMock()
    outbox.id = uuid.uuid4()

    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.get_or_create",
        AsyncMock(return_value=(conversation, False)),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.lock_for_update",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.get_inbox_by_external",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.allocate_next_event_seq",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.insert_inbox_if_absent",
        AsyncMock(return_value=(inbox, True)),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.bump_context_for_new_message",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.inbound.reply_plan_repo.supersede_open_plans",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.inbound.outbound_repo.cancel_unadmitted_for_conversation",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_allows_automatic_reply",
        lambda _c: False,
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.create_internal_draft_outbox",
        AsyncMock(return_value=(outbox, True)),
    )
    monkeypatch.setattr(
        "app.services.inbound.enqueue_client_message_received",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.inbound.enqueue_client_inbound_projection",
        AsyncMock(),
    )

    admit_mock = AsyncMock(return_value=MagicMock())
    fake_svc = MagicMock()
    fake_svc.admit_from_confirm = admit_mock
    monkeypatch.setattr(
        "app.services.self_booking_confirm_admission.SelfBookingConfirmAdmissionService",
        MagicMock(return_value=fake_svc),
    )

    result = await InboundService(session, pii_store=_FakePiiStore()).accept(
        _confirm_event()
    )
    assert result.created_inbox is True
    admit_mock.assert_awaited_once()
    kwargs = admit_mock.await_args.kwargs
    assert kwargs["conversation_id"] == conversation.id
    assert kwargs["channel"] == "synthetic"
    assert kwargs["confirm_external_message_id"] == "confirm-msg-1"
    assert kwargs["fence_context_version"] == 3
    assert kwargs["fence_manager_epoch"] == 1
    assert kwargs["fence_event_seq_hwm"] == 7
    assert kwargs["action"].kind == "CONFIRM_SELECTED_SLOT"


@pytest.mark.asyncio
async def test_plain_text_does_not_call_admit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _async_session()
    conversation = MagicMock()
    conversation.id = uuid.uuid4()
    conversation.context_version = 1
    conversation.manager_epoch = 0
    conversation.current_event_seq = 1
    conversation.status = "OPEN"
    conversation.handoff_state = "BOT_ACTIVE"
    conversation.manager_takeover_at = None
    conversation.ownership = "BOT"
    conversation.active_reply_plan_id = None

    inbox = MagicMock()
    inbox.id = uuid.uuid4()
    inbox.conversation_id = conversation.id
    outbox = MagicMock()
    outbox.id = uuid.uuid4()

    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.get_or_create",
        AsyncMock(return_value=(conversation, False)),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.lock_for_update",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.get_inbox_by_external",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.allocate_next_event_seq",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.insert_inbox_if_absent",
        AsyncMock(return_value=(inbox, True)),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.bump_context_for_new_message",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.inbound.reply_plan_repo.supersede_open_plans",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.inbound.outbound_repo.cancel_unadmitted_for_conversation",
        AsyncMock(),
    )
    invalidate = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.self_booking_active_offer.SelfBookingActiveOfferService",
        MagicMock(
            return_value=MagicMock(invalidate=invalidate),
        ),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_allows_automatic_reply",
        lambda _c: False,
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.create_internal_draft_outbox",
        AsyncMock(return_value=(outbox, True)),
    )
    monkeypatch.setattr(
        "app.services.inbound.enqueue_client_message_received",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.inbound.enqueue_client_inbound_projection",
        AsyncMock(),
    )
    admit_ctor = MagicMock()
    monkeypatch.setattr(
        "app.services.self_booking_confirm_admission.SelfBookingConfirmAdmissionService",
        admit_ctor,
    )

    await InboundService(session, pii_store=_FakePiiStore()).accept(
        SyntheticInboundEvent(
            external_conversation_id="conv-plain",
            external_message_id="plain-1",
            text="hello free form",
        )
    )
    invalidate.assert_awaited_once()
    admit_ctor.assert_not_called()


@pytest.mark.asyncio
async def test_booking_fixture_does_not_call_admit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _async_session()
    conversation = MagicMock()
    conversation.id = uuid.uuid4()
    conversation.context_version = 1
    conversation.manager_epoch = 0
    conversation.current_event_seq = 1
    conversation.status = "OPEN"
    conversation.handoff_state = "BOT_ACTIVE"
    conversation.manager_takeover_at = None
    conversation.ownership = "BOT"
    conversation.active_reply_plan_id = None
    inbox = MagicMock()
    inbox.id = uuid.uuid4()
    inbox.conversation_id = conversation.id
    outbox = MagicMock()
    outbox.id = uuid.uuid4()

    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.get_or_create",
        AsyncMock(return_value=(conversation, False)),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.lock_for_update",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.get_inbox_by_external",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.allocate_next_event_seq",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.insert_inbox_if_absent",
        AsyncMock(return_value=(inbox, True)),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.bump_context_for_new_message",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.inbound.reply_plan_repo.supersede_open_plans",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.inbound.outbound_repo.cancel_unadmitted_for_conversation",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.self_booking_active_offer.SelfBookingActiveOfferService",
        MagicMock(return_value=MagicMock(invalidate=AsyncMock())),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_allows_automatic_reply",
        lambda _c: False,
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.create_internal_draft_outbox",
        AsyncMock(return_value=(outbox, True)),
    )
    monkeypatch.setattr(
        "app.services.inbound.enqueue_client_message_received",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.inbound.enqueue_client_inbound_projection",
        AsyncMock(),
    )
    admit_ctor = MagicMock()
    monkeypatch.setattr(
        "app.services.self_booking_confirm_admission.SelfBookingConfirmAdmissionService",
        admit_ctor,
    )

    from datetime import datetime, timezone

    booking = SyntheticBookingInput(
        service_id=_SERVICE,
        include_alternatives=False,
        decision_at=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
    )
    await InboundService(session, pii_store=_FakePiiStore()).accept(
        SyntheticInboundEvent(
            external_conversation_id="conv-book",
            external_message_id="book-1",
            text="booking-fixture",
            booking=booking,
        )
    )
    admit_ctor.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_inbox_does_not_re_admit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _async_session()
    conversation = MagicMock()
    conversation.id = uuid.uuid4()
    conversation.context_version = 2
    conversation.manager_epoch = 0
    conversation.current_event_seq = 2
    conversation.status = "OPEN"
    conversation.handoff_state = "BOT_ACTIVE"
    conversation.manager_takeover_at = None
    conversation.ownership = "BOT"

    inbox = MagicMock()
    inbox.id = uuid.uuid4()
    inbox.conversation_id = conversation.id
    outbox = MagicMock()
    outbox.id = uuid.uuid4()

    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.get_or_create",
        AsyncMock(return_value=(conversation, False)),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.lock_for_update",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.get_inbox_by_external",
        AsyncMock(return_value=inbox),
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.create_internal_draft_outbox",
        AsyncMock(return_value=(outbox, False)),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_allows_automatic_reply",
        lambda _c: True,
    )
    admit_ctor = MagicMock()
    monkeypatch.setattr(
        "app.services.self_booking_confirm_admission.SelfBookingConfirmAdmissionService",
        admit_ctor,
    )

    result = await InboundService(session, pii_store=_FakePiiStore()).accept(
        _confirm_event()
    )
    assert result.duplicate is True
    assert result.created_inbox is False
    admit_ctor.assert_not_called()


@pytest.mark.asyncio
async def test_missing_pii_store_skips_admit_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _async_session()
    conversation = MagicMock()
    conversation.id = uuid.uuid4()
    conversation.context_version = 1
    conversation.manager_epoch = 0
    conversation.current_event_seq = 1
    conversation.status = "OPEN"
    conversation.handoff_state = "BOT_ACTIVE"
    conversation.manager_takeover_at = None
    conversation.ownership = "BOT"
    conversation.active_reply_plan_id = None
    inbox = MagicMock()
    inbox.id = uuid.uuid4()
    inbox.conversation_id = conversation.id
    outbox = MagicMock()
    outbox.id = uuid.uuid4()

    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.get_or_create",
        AsyncMock(return_value=(conversation, False)),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.lock_for_update",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.get_inbox_by_external",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.allocate_next_event_seq",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.insert_inbox_if_absent",
        AsyncMock(return_value=(inbox, True)),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.bump_context_for_new_message",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.inbound.reply_plan_repo.supersede_open_plans",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.inbound.outbound_repo.cancel_unadmitted_for_conversation",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_allows_automatic_reply",
        lambda _c: False,
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.create_internal_draft_outbox",
        AsyncMock(return_value=(outbox, True)),
    )
    monkeypatch.setattr(
        "app.services.inbound.enqueue_client_message_received",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.inbound.enqueue_client_inbound_projection",
        AsyncMock(),
    )
    admit_ctor = MagicMock()
    monkeypatch.setattr(
        "app.services.self_booking_confirm_admission.SelfBookingConfirmAdmissionService",
        admit_ctor,
    )

    # Default constructor: no pii_store → soft skip, inbound still succeeds.
    result = await InboundService(session).accept(_confirm_event())
    assert result.created_inbox is True
    admit_ctor.assert_not_called()


@pytest.mark.asyncio
async def test_admit_exception_does_not_fail_inbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _async_session()
    conversation = MagicMock()
    conversation.id = uuid.uuid4()
    conversation.context_version = 1
    conversation.manager_epoch = 0
    conversation.current_event_seq = 1
    conversation.status = "OPEN"
    conversation.handoff_state = "BOT_ACTIVE"
    conversation.manager_takeover_at = None
    conversation.ownership = "BOT"
    conversation.active_reply_plan_id = None
    inbox = MagicMock()
    inbox.id = uuid.uuid4()
    inbox.conversation_id = conversation.id
    outbox = MagicMock()
    outbox.id = uuid.uuid4()

    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.get_or_create",
        AsyncMock(return_value=(conversation, False)),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.lock_for_update",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.get_inbox_by_external",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.allocate_next_event_seq",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.insert_inbox_if_absent",
        AsyncMock(return_value=(inbox, True)),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_repo.bump_context_for_new_message",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.inbound.reply_plan_repo.supersede_open_plans",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.inbound.outbound_repo.cancel_unadmitted_for_conversation",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.inbound.conversation_allows_automatic_reply",
        lambda _c: False,
    )
    monkeypatch.setattr(
        "app.services.inbound.message_repo.create_internal_draft_outbox",
        AsyncMock(return_value=(outbox, True)),
    )
    monkeypatch.setattr(
        "app.services.inbound.enqueue_client_message_received",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.inbound.enqueue_client_inbound_projection",
        AsyncMock(),
    )
    fake_svc = MagicMock()
    fake_svc.admit_from_confirm = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(
        "app.services.self_booking_confirm_admission.SelfBookingConfirmAdmissionService",
        MagicMock(return_value=fake_svc),
    )

    result = await InboundService(session, pii_store=_FakePiiStore()).accept(
        _confirm_event()
    )
    assert result.created_inbox is True


def test_inbound_source_wires_admit_not_create() -> None:
    inbound = (_REPO / "app" / "services" / "inbound.py").read_text(encoding="utf-8")
    ingress = (_REPO / "app" / "services" / "ingress.py").read_text(encoding="utf-8")
    worker = (_REPO / "app" / "services" / "worker_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "admit_from_confirm" in inbound
    assert "SelfBookingConfirmAdmissionService" in inbound
    assert "preserves_active_offer" in inbound
    assert "admit_confirmed" not in inbound
    assert ".confirm_selected_slot" not in inbound
    assert "BookingCreateHttp" not in inbound
    assert "read_plaintext" not in inbound
    assert "pii_store" in ingress
    assert "pii_store" in worker
    assert "SelfBookingCreateExecutionWorker" in worker
    assert ".confirm_selected_slot" not in ingress
    assert ".confirm_selected_slot" not in worker
    assert "BookingCreateHttpClient" not in ingress
    assert "BookingCreateHttpClient" not in worker
