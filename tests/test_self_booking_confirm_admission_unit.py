"""Unit tests for SELF-BOOKING-COMMAND-03K1 confirm admission orchestration."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.self_booking_active_offer_types import (
    ActiveOfferResolveOutcome,
    ActiveOfferResolveResult,
)
from app.core.self_booking_confirm_admission_types import (
    SelfBookingConfirmAdmissionOutcome,
    SelfBookingConfirmAdmissionResult,
)
from app.core.self_booking_create_types import (
    SelfBookingCreateAdmitOutcome,
    SelfBookingCreateAdmitResult,
)
from app.schemas.self_booking_confirm_action import SyntheticConfirmSelectedSlotAction
from app.services.self_booking_confirm_admission import (
    SelfBookingConfirmAdmissionService,
)

_REPO = Path(__file__).resolve().parents[1]
_SERVICE_PATH = _REPO / "app" / "services" / "self_booking_confirm_admission.py"
_TYPES_PATH = _REPO / "app" / "core" / "self_booking_confirm_admission_types.py"
_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_SLOT = f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.1000"
_STARTS = "2026-08-20T10:00:00+05:00"
_KEY = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_PHONE_REF = "epii_" + ("a" * 27)
_NAME_REF = "epii_" + ("b" * 27)
_CONV = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_PENDING = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
_OUTBOUND = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")


def _action(*, slot_id: str = _SLOT, request_id: str = "req-confirm-1") -> (
    SyntheticConfirmSelectedSlotAction
):
    return SyntheticConfirmSelectedSlotAction(
        kind="CONFIRM_SELECTED_SLOT",
        slot_id=slot_id,
        pii_admission_request_id=request_id,
        personal_data_consent=True,
        offer_acknowledgement=True,
    )


class _FakePiiStore:
    def __init__(self, *, alive: bool = True, error: BaseException | None = None) -> None:
        self.alive = alive
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.read_plaintext = AsyncMock(
            side_effect=AssertionError("read_plaintext must not be called")
        )

    async def booking_phone_write_pair_alive(self, session: object, **kwargs: Any) -> bool:
        self.calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        return self.alive


def _build_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    offer: ActiveOfferResolveResult | None = None,
    admission: object | None = ...,
    existing_pending: object | None = None,
    admit_result: SelfBookingCreateAdmitResult | None = None,
    pii_store: _FakePiiStore | None = None,
) -> tuple[SelfBookingConfirmAdmissionService, _FakePiiStore, AsyncMock]:
    store = pii_store or _FakePiiStore()
    session = object()
    svc = SelfBookingConfirmAdmissionService(session, pii_store=store)  # type: ignore[arg-type]

    if offer is None:
        offer = ActiveOfferResolveResult(
            outcome=ActiveOfferResolveOutcome.FOUND,
            starts_at=_STARTS,
            source_outbound_id=_OUTBOUND,
        )
    svc._offers.resolve_slot = AsyncMock(return_value=offer)  # type: ignore[method-assign]

    if admission is ...:
        admission = SimpleNamespace(
            phone_ref_token=_PHONE_REF,
            name_ref_token=_NAME_REF,
        )
    monkeypatch.setattr(
        "app.services.self_booking_confirm_admission.admission_repo.get_by_request",
        AsyncMock(return_value=admission),
    )
    monkeypatch.setattr(
        "app.services.self_booking_confirm_admission.pending_repo.get_by_confirm",
        AsyncMock(return_value=existing_pending),
    )

    if admit_result is None:
        admit_result = SelfBookingCreateAdmitResult(
            outcome=SelfBookingCreateAdmitOutcome.ADMITTED,
            pending_id=_PENDING,
            idempotency_key=_KEY,
        )
    admit_mock = AsyncMock(return_value=admit_result)
    svc._pendings.admit_confirmed = admit_mock  # type: ignore[method-assign]
    return svc, store, admit_mock


def test_result_repr_redacts_ids() -> None:
    result = SelfBookingConfirmAdmissionResult(
        outcome=SelfBookingConfirmAdmissionOutcome.ADMITTED,
        pending_id=uuid.uuid4(),
        idempotency_key=_KEY,
    )
    rendered = repr(result)
    assert _KEY not in rendered
    assert "idempotency_key=<redacted>" in rendered
    assert "pending_id=<redacted>" in rendered
    assert str(result) == rendered


def test_service_source_has_no_create_pii_read_or_replyplan() -> None:
    text = _SERVICE_PATH.read_text(encoding="utf-8")
    assert "admit_from_confirm" in text
    assert "resolve_slot" in text
    assert "get_by_request" in text
    assert "booking_phone_write_pair_alive" in text
    assert "admit_confirmed" in text
    assert "uuid.uuid4" in text

    assert "read_plaintext" not in text
    assert "confirm_selected_slot" not in text
    assert "BookingCreateHttp" not in text
    assert "create_booking" not in text
    assert "online-zapis" not in text
    assert "ReplyPlan" not in text
    assert "client_reply_plan" not in text
    assert "IngressEvent" not in text
    assert "SelfBookingPiiAdmissionService" not in text
    assert ".admit(" not in text


def test_service_not_wired_into_confirm_schema() -> None:
    inbound = (_REPO / "app" / "services" / "inbound.py").read_text(encoding="utf-8")
    confirm = (
        _REPO / "app" / "schemas" / "self_booking_confirm_action.py"
    ).read_text(encoding="utf-8")
    # 03K2: inbound may call admit_from_confirm; schema stays admission-free.
    assert "admit_from_confirm" in inbound
    assert "SelfBookingConfirmAdmissionService" in inbound
    assert "admit_confirmed" not in inbound
    assert ".confirm_selected_slot" not in inbound
    assert "SelfBookingConfirmAdmissionService" not in confirm
    assert "admit_from_confirm" not in confirm
    assert "admit_confirmed" not in confirm


def test_outcomes_cover_required_set() -> None:
    names = {m.value for m in SelfBookingConfirmAdmissionOutcome}
    assert names == {
        "ADMITTED",
        "DUPLICATE",
        "OFFER_NOT_ACTIVE",
        "PII_NOT_FOUND",
        "PII_EXPIRED",
        "HANDOFF_BLOCKED",
        "FAIL_CLOSED",
    }


def test_types_module_has_no_pii_or_create_surface() -> None:
    text = _TYPES_PATH.read_text(encoding="utf-8")
    assert "read_plaintext" not in text
    assert "confirm_selected_slot" not in text
    assert "ReplyPlan" not in text
    assert "BookingCreateHttp" not in text
    assert "phone_ref" not in text
    assert "client_name" not in text


@pytest.mark.asyncio
async def test_happy_path_admits_with_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, store, admit_mock = _build_service(monkeypatch)
    action = _action()
    result = await svc.admit_from_confirm(
        conversation_id=_CONV,
        channel="synthetic",
        confirm_external_message_id="confirm-1",
        action=action,
        fence_context_version=1,
        fence_manager_epoch=0,
        fence_event_seq_hwm=2,
    )
    assert result.outcome is SelfBookingConfirmAdmissionOutcome.ADMITTED
    assert result.pending_id == _PENDING
    assert result.idempotency_key == _KEY
    admit_mock.assert_awaited_once()
    kwargs = admit_mock.await_args.kwargs
    assert kwargs["slot_id"] == _SLOT
    assert kwargs["starts_at"] == _STARTS
    assert kwargs["phone_ref_token"] == _PHONE_REF
    assert kwargs["name_ref_token"] == _NAME_REF
    assert kwargs["fence_context_version"] == 1
    assert kwargs["fence_manager_epoch"] == 0
    assert kwargs["fence_event_seq_hwm"] == 2
    assert kwargs["idempotency_key"]
    assert len(kwargs["idempotency_key"]) == 36
    assert store.calls
    store.read_plaintext.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_slot_zero_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _store, admit_mock = _build_service(
        monkeypatch,
        offer=ActiveOfferResolveResult(
            outcome=ActiveOfferResolveOutcome.NOT_ACTIVE
        ),
    )
    result = await svc.admit_from_confirm(
        conversation_id=_CONV,
        channel="synthetic",
        confirm_external_message_id="confirm-1",
        action=_action(),
        fence_context_version=0,
        fence_manager_epoch=0,
        fence_event_seq_hwm=0,
    )
    assert result.outcome is SelfBookingConfirmAdmissionOutcome.OFFER_NOT_ACTIVE
    assert result.pending_id is None
    admit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_pii_request_zero_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _store, admit_mock = _build_service(monkeypatch, admission=None)
    result = await svc.admit_from_confirm(
        conversation_id=_CONV,
        channel="synthetic",
        confirm_external_message_id="confirm-1",
        action=_action(),
        fence_context_version=0,
        fence_manager_epoch=0,
        fence_event_seq_hwm=0,
    )
    assert result.outcome is SelfBookingConfirmAdmissionOutcome.PII_NOT_FOUND
    admit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_refs_zero_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _store, admit_mock = _build_service(
        monkeypatch, pii_store=_FakePiiStore(alive=False)
    )
    result = await svc.admit_from_confirm(
        conversation_id=_CONV,
        channel="synthetic",
        confirm_external_message_id="confirm-1",
        action=_action(),
        fence_context_version=0,
        fence_manager_epoch=0,
        fence_event_seq_hwm=0,
    )
    assert result.outcome is SelfBookingConfirmAdmissionOutcome.PII_EXPIRED
    admit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_fence_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _store, admit_mock = _build_service(
        monkeypatch,
        admit_result=SelfBookingCreateAdmitResult(
            outcome=SelfBookingCreateAdmitOutcome.FENCE_STALE,
            reason_code="FENCE_STALE",
        ),
    )
    result = await svc.admit_from_confirm(
        conversation_id=_CONV,
        channel="synthetic",
        confirm_external_message_id="confirm-1",
        action=_action(),
        fence_context_version=9,
        fence_manager_epoch=0,
        fence_event_seq_hwm=0,
    )
    assert result.outcome is SelfBookingConfirmAdmissionOutcome.FAIL_CLOSED
    assert result.reason_code == "FENCE_STALE"
    admit_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_takeover_handoff_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _store, admit_mock = _build_service(
        monkeypatch,
        admit_result=SelfBookingCreateAdmitResult(
            outcome=SelfBookingCreateAdmitOutcome.HANDOFF_BLOCKED,
            reason_code="HANDOFF_OR_TAKEOVER",
        ),
    )
    result = await svc.admit_from_confirm(
        conversation_id=_CONV,
        channel="synthetic",
        confirm_external_message_id="confirm-1",
        action=_action(),
        fence_context_version=0,
        fence_manager_epoch=0,
        fence_event_seq_hwm=0,
    )
    assert result.outcome is SelfBookingConfirmAdmissionOutcome.HANDOFF_BLOCKED
    admit_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_returns_existing_without_new_admit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = SimpleNamespace(id=_PENDING, idempotency_key=_KEY)
    svc, _store, admit_mock = _build_service(
        monkeypatch, existing_pending=existing
    )
    result = await svc.admit_from_confirm(
        conversation_id=_CONV,
        channel="synthetic",
        confirm_external_message_id="confirm-1",
        action=_action(),
        fence_context_version=0,
        fence_manager_epoch=0,
        fence_event_seq_hwm=0,
    )
    assert result.outcome is SelfBookingConfirmAdmissionOutcome.DUPLICATE
    assert result.pending_id == _PENDING
    assert result.idempotency_key == _KEY
    admit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_plaintext_in_result_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _store, _admit = _build_service(monkeypatch)
    result = await svc.admit_from_confirm(
        conversation_id=_CONV,
        channel="synthetic",
        confirm_external_message_id="confirm-1",
        action=_action(),
        fence_context_version=0,
        fence_manager_epoch=0,
        fence_event_seq_hwm=0,
    )
    rendered = repr(result)
    assert "+7900" not in rendered
    assert _PHONE_REF not in rendered
    assert _NAME_REF not in rendered
    assert _SLOT not in rendered
    assert _KEY not in rendered
