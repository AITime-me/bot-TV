"""BOT-REPLY-DURABLE-01: durable authoritative outbound reply text."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.booking_types import (
    BookingClientMessageKind,
    BookingDialogAction,
    BookingEligibilityOutcome,
    BookingEligibilityResult,
    BookingInternalReasonCode,
    SelectedMaster,
    SelectedService,
    render_client_message,
)
from app.models.conversation import (
    ConversationOwnership,
    ConversationStatus,
    HandoffState,
)
from app.models.outbox import DeliveryStatus, DestinationType
from app.repositories.outbound import OutboundClaim
from app.schemas.booking_input import SyntheticBookingInput, SyntheticBookingSlot
from app.services.booking_eligibility_flow import BookingEligibilityFlowService
from app.services.booking_flow import BookingFlowService
from app.services.booking_synthetic import (
    build_synthetic_outbound_payload,
    client_reply_plan_payload,
    decision_to_outbound_fields,
    interrupted_booking_fields,
)
from app.services.outbound_arbiter import OutboundArbiter, OutboundArbiterDenied
from app.services.outbound_reply_text import (
    OutboundReplyTextError,
    persisted_outbound_reply_text,
    render_text_for_booking_fields,
    require_persisted_outbound_text,
)
from app.services.synthetic_outbound import SyntheticOutboundAdapter

_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_SLOT_START = datetime(2026, 8, 6, 5, 0, tzinfo=timezone.utc)
_DECISION_AT = datetime(2026, 8, 5, 12, 0, tzinfo=timezone(timedelta(hours=5)))
_FIXED_NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


class FakeEligibilityClient:
    def __init__(self, result: BookingEligibilityResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def check_eligibility(
        self,
        service: SelectedService,
        master: SelectedMaster | None = None,
        *,
        include_alternatives: bool = False,
    ) -> BookingEligibilityResult:
        self.calls.append(
            {
                "service": service,
                "master": master,
                "include_alternatives": include_alternatives,
            }
        )
        return self.result


def _allowed_result() -> BookingEligibilityResult:
    return BookingEligibilityResult(
        outcome=BookingEligibilityOutcome.SELF_BOOKING_ALLOWED,
        selected_service=SelectedService(_SERVICE),
        selected_master=SelectedMaster(_MASTER),
        other_online_master_ids=(),
        internal_reason_code=None,
    )


def _booking() -> SyntheticBookingInput:
    return SyntheticBookingInput(
        service_id=_SERVICE,
        master_id=_MASTER,
        include_alternatives=False,
        alternate_master_consent=False,
        slots=(
            SyntheticBookingSlot(
                slot_id="s1",
                starts_at=_SLOT_START,
                master_id=_MASTER,
                service_id=_SERVICE,
            ),
        ),
        decision_at=_DECISION_AT,
    )


def test_persisted_outbound_contains_rendered_user_facing_text() -> None:
    flow = BookingFlowService(BookingEligibilityFlowService(FakeEligibilityClient(_allowed_result())))
    plan = client_reply_plan_payload(inbox_id="i1", booking=_booking())
    outbound = build_synthetic_outbound_payload(plan, booking_flow=flow)
    expected = render_client_message(BookingClientMessageKind.OFFER_SLOTS)
    assert outbound["text"] == expected
    assert require_persisted_outbound_text(outbound) == expected
    assert outbound["synthetic_token"] == "SYNTHETIC_OK"
    assert outbound["text"] != outbound["synthetic_token"]


def test_booking_reply_persists_rendered_text_and_booking_wire() -> None:
    flow = BookingFlowService(BookingEligibilityFlowService(FakeEligibilityClient(_allowed_result())))
    plan = client_reply_plan_payload(inbox_id="i1", booking=_booking())
    outbound = build_synthetic_outbound_payload(plan, booking_flow=flow)
    assert outbound["booking_action"] == BookingDialogAction.OFFER_SLOTS.value
    assert outbound["booking_offered_slot_ids"] == ["s1"]
    assert "booking_offered_slots" in outbound
    assert outbound["text"] == render_client_message(BookingClientMessageKind.OFFER_SLOTS)
    assert outbound["client_message_kind"] == BookingClientMessageKind.OFFER_SLOTS.value


def test_handoff_persists_kind_for_deterministic_text() -> None:
    client = FakeEligibilityClient(
        BookingEligibilityResult(
            outcome=BookingEligibilityOutcome.MANAGER_HANDOFF,
            selected_service=SelectedService(_SERVICE),
            selected_master=SelectedMaster(_MASTER),
            other_online_master_ids=(),
            internal_reason_code="MANAGER_ONLY",
        )
    )
    flow = BookingFlowService(BookingEligibilityFlowService(client))
    plan = client_reply_plan_payload(inbox_id="i1", booking=_booking())
    outbound = build_synthetic_outbound_payload(plan, booking_flow=flow)
    assert outbound["booking_action"] == BookingDialogAction.MANAGER_HANDOFF.value
    assert outbound["client_message_kind"] in {
        BookingClientMessageKind.HANDOFF_DURING_MANAGER_HOURS.value,
        BookingClientMessageKind.HANDOFF_OUTSIDE_MANAGER_HOURS.value,
    }
    assert outbound["text"] == render_text_for_booking_fields(
        {
            "booking_action": outbound["booking_action"],
            "client_message_kind": outbound["client_message_kind"],
        }
    )


@pytest.mark.asyncio
async def test_retry_delivery_uses_persisted_text_without_rerender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted = "Могу предложить ближайшие свободные окна. Выберите удобное время."
    service_id = "11111111-1111-4111-8111-111111111111"
    master_id = "22222222-2222-4222-8222-222222222222"
    slot_id = f"bs1.{service_id}.{master_id}.2026-08-20.1000"
    starts_at = "2026-08-20T10:00:00+05:00"
    payload = {
        "schema": "synthetic.outbound.v1",
        "synthetic_token": "SYNTHETIC_OK",
        "booking_action": BookingDialogAction.OFFER_SLOTS.value,
        "booking_offered_slot_ids": [slot_id],
        "booking_offered_slots": [{"slot_id": slot_id, "starts_at": starts_at}],
        "text": persisted,
    }

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("delivery must not re-render")

    monkeypatch.setattr(
        "app.services.outbound_reply_text.render_client_message",
        _boom,
    )
    monkeypatch.setattr(
        "app.core.booking_types.render_client_message",
        _boom,
    )

    claim = OutboundClaim(
        outbound_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        reply_plan_id=uuid.uuid4(),
        context_version=1,
        manager_epoch=0,
        event_seq_hwm=1,
        idempotency_key="k",
        destination_type=DestinationType.SYNTHETIC_OUTBOUND.value,
        delivery_status=DeliveryStatus.ADMITTED.value,
        not_before=_FIXED_NOW,
        attempt_count=2,
        max_attempts=5,
        lease_owner="w",
        lease_token=uuid.uuid4(),
        lease_version=2,
        lease_until=_FIXED_NOW + timedelta(seconds=30),
        correlation_id=uuid.uuid4(),
        payload_json=payload,
    )

    conversation = MagicMock()
    conversation.ownership = ConversationOwnership.BOT.value
    conversation.status = ConversationStatus.OPEN.value
    conversation.handoff_state = HandoffState.BOT_ACTIVE.value
    conversation.manager_takeover_at = None
    conversation.context_version = 1
    conversation.manager_epoch = 0
    conversation.current_event_seq = 1

    outbound = MagicMock()
    outbound.id = claim.outbound_id
    outbound.conversation_id = claim.conversation_id
    outbound.reply_plan_id = claim.reply_plan_id
    outbound.destination_type = DestinationType.SYNTHETIC_OUTBOUND.value
    outbound.delivery_status = DeliveryStatus.ADMITTED.value
    outbound.admitted_at = _FIXED_NOW
    outbound.lease_token = claim.lease_token
    outbound.lease_version = claim.lease_version
    outbound.context_version = 1
    outbound.manager_epoch = 0
    outbound.event_seq_hwm = 1
    outbound.correlation_id = claim.correlation_id
    outbound.payload_json = payload

    async def _mark_delivered_with_lease(*_a: object, **_k: object) -> MagicMock:
        # Honest mark_delivered_with_lease contract: ADMITTED → DELIVERED.
        outbound.delivery_status = DeliveryStatus.DELIVERED.value
        return outbound

    session = AsyncMock()
    session.get = AsyncMock(return_value=outbound)
    session_factory = MagicMock()

    arbiter = OutboundArbiter(session_factory, sink=SyntheticOutboundAdapter())
    monkeypatch.setattr(
        "app.services.outbound_arbiter.session_scope",
        _fake_session_scope(session),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.conversation_repo.lock_for_update",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.outbound_repo.mark_delivered_with_lease",
        AsyncMock(side_effect=_mark_delivered_with_lease),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.enqueue_outbound_delivered",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.repositories.self_booking_active_offers.upsert_if_newer_or_same_outbound",
        AsyncMock(return_value="activated"),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.enqueue_bot_outbound_projection",
        AsyncMock(),
    )

    result = await arbiter.admit_claimed(claim, now=_FIXED_NOW)
    assert result.admitted is True
    assert arbiter.sink.calls[0]._text == persisted
    assert outbound.delivery_status == DeliveryStatus.DELIVERED.value


def _fake_session_scope(session: AsyncMock):
    class _Scope:
        async def __aenter__(self) -> AsyncMock:
            return session

        async def __aexit__(self, *args: object) -> None:
            return None

    def _factory(*_a: object, **_k: object) -> _Scope:
        return _Scope()

    return _factory


def test_client_echo_manager_hint_and_token_cannot_substitute() -> None:
    echo = "client-inbound-secret"
    token = "SYNTHETIC_OK"
    # Token stuffed into text is rejected.
    assert persisted_outbound_reply_text(
        {"schema": "synthetic.outbound.v1", "synthetic_token": token, "text": token}
    ) is None
    with pytest.raises(OutboundReplyTextError):
        require_persisted_outbound_text(
            {
                "schema": "synthetic.outbound.v1",
                "synthetic_token": token,
                "draft_text": echo,
                "text": token,
            }
        )
    # draft_text / inbound echo alone never become authoritative body.
    assert persisted_outbound_reply_text(
        {
            "schema": "synthetic.outbound.v1",
            "synthetic_token": token,
            "draft_text": echo,
        }
    ) is None
    with pytest.raises(OutboundReplyTextError):
        require_persisted_outbound_text(
            {"schema": "synthetic.outbound.v1", "synthetic_token": token}
        )


def test_missing_and_invalid_text_fail_closed() -> None:
    with pytest.raises(OutboundReplyTextError):
        build_synthetic_outbound_payload(
            client_reply_plan_payload(inbox_id="i1", booking=None)
        )
    with pytest.raises(OutboundReplyTextError):
        require_persisted_outbound_text(
            {"schema": "synthetic.outbound.v1", "text": "   "}
        )
    with pytest.raises(OutboundReplyTextError):
        render_text_for_booking_fields(
            {
                "booking_action": BookingDialogAction.MANAGER_HANDOFF.value,
                "booking_reason": "MANAGER_ONLY",
            }
        )
    with pytest.raises(OutboundReplyTextError) as exc:
        render_text_for_booking_fields(
            {
                "booking_action": BookingDialogAction.OFFER_DAYS.value,
                "booking_available_date_keys": ["2026-08-06"],
                "booking_studio_today": "2026-08-05",
                "booking_reason": None,
            }
        )
    assert exc.value.code == "OUTBOUND_REPLY_TEXT_NOT_RENDERABLE"


def test_interrupted_fields_render_unavailable_copy() -> None:
    fields = interrupted_booking_fields()
    text = render_text_for_booking_fields(fields)
    assert text == render_client_message(
        BookingClientMessageKind.SERVICE_TEMPORARILY_UNAVAILABLE
    )
    outbound = build_synthetic_outbound_payload(
        client_reply_plan_payload(inbox_id="i1", booking=_booking()),
        booking_fields=fields,
    )
    assert outbound["text"] == text
    assert outbound["booking_reason"] == (
        BookingInternalReasonCode.BOOKING_RESOLUTION_INTERRUPTED.value
    )


def test_offer_days_omits_text_keeps_machine_wire() -> None:
    fields = {
        "booking_action": BookingDialogAction.OFFER_DAYS.value,
        "booking_reason": None,
        "booking_available_date_keys": ["2026-08-06", "2026-08-07"],
        "booking_studio_today": "2026-08-05",
    }
    outbound = build_synthetic_outbound_payload(
        client_reply_plan_payload(inbox_id="i1", booking=_booking()),
        booking_fields=fields,
    )
    assert "text" not in outbound
    assert outbound["booking_available_date_keys"] == ["2026-08-06", "2026-08-07"]


def test_enqueue_bot_outbound_projection_uses_persisted_text_only() -> None:
    from app.services.amocrm_chat_projection import enqueue_bot_outbound_projection
    import inspect

    source = inspect.getsource(enqueue_bot_outbound_projection)
    assert "persisted_outbound_reply_text" in source
    assert "BOT_OUTBOUND" in source
    assert "DELIVERED" in source
    assert "draft_text" not in source
    assert "synthetic_token" not in source or "never" in source.lower()


@pytest.mark.asyncio
async def test_arbiter_denies_token_only_outbound_without_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "schema": "synthetic.outbound.v1",
        "synthetic_token": "SYNTHETIC_OK",
    }
    claim = OutboundClaim(
        outbound_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        reply_plan_id=uuid.uuid4(),
        context_version=1,
        manager_epoch=0,
        event_seq_hwm=1,
        idempotency_key="k",
        destination_type=DestinationType.SYNTHETIC_OUTBOUND.value,
        delivery_status=DeliveryStatus.ADMITTED.value,
        not_before=_FIXED_NOW,
        attempt_count=1,
        max_attempts=5,
        lease_owner="w",
        lease_token=uuid.uuid4(),
        lease_version=1,
        lease_until=_FIXED_NOW + timedelta(seconds=30),
        correlation_id=uuid.uuid4(),
        payload_json=payload,
    )
    outbound = MagicMock()
    outbound.id = claim.outbound_id
    outbound.conversation_id = claim.conversation_id
    outbound.reply_plan_id = claim.reply_plan_id
    outbound.destination_type = DestinationType.SYNTHETIC_OUTBOUND.value
    outbound.delivery_status = DeliveryStatus.ADMITTED.value
    outbound.admitted_at = _FIXED_NOW
    outbound.lease_token = claim.lease_token
    outbound.lease_version = claim.lease_version
    outbound.context_version = 1
    outbound.correlation_id = claim.correlation_id
    outbound.payload_json = payload

    session = AsyncMock()
    session.get = AsyncMock(return_value=outbound)
    arbiter = OutboundArbiter(MagicMock(), sink=SyntheticOutboundAdapter())
    monkeypatch.setattr(
        "app.services.outbound_arbiter.session_scope",
        _fake_session_scope(session),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.conversation_repo.lock_for_update",
        AsyncMock(),
    )
    with pytest.raises(OutboundArbiterDenied) as denied:
        await arbiter.admit_claimed(claim, now=_FIXED_NOW)
    assert str(denied.value) == "OUTBOUND_REPLY_TEXT_MISSING"
    assert arbiter.sink.calls == []


def test_decision_fields_include_handoff_kind() -> None:
    from app.core.booking_types import ManagerHandoffDecision

    fields = decision_to_outbound_fields(
        ManagerHandoffDecision(
            action=BookingDialogAction.MANAGER_HANDOFF,
            client_message_kind=BookingClientMessageKind.HANDOFF_DURING_MANAGER_HOURS,
            during_manager_hours=True,
            internal_reason_code="MANAGER_ONLY",
        )
    )
    assert fields["client_message_kind"] == (
        BookingClientMessageKind.HANDOFF_DURING_MANAGER_HOURS.value
    )
    assert render_text_for_booking_fields(fields) == render_client_message(
        BookingClientMessageKind.HANDOFF_DURING_MANAGER_HOURS
    )
