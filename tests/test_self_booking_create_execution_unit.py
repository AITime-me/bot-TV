"""Unit tests for SELF-BOOKING-COMMAND-02 execution path."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.core.booking_create_http import BookingCreateHttpError
from app.core.booking_create_remote import BookingCreateRemoteSuccess
from app.core.client_ref_resolution import (
    ClientRefResolutionOutcome,
    ClientRefResolutionResult,
)
from app.core.ephemeral_pii_types import (
    EphemeralPiiKind,
    EphemeralPiiPurpose,
    EphemeralPiiReference,
)
from app.core.self_booking_create_types import (
    SelfBookingCreateExecutionOutcome,
    SelfBookingCreateExecutionResult,
    SelfBookingCreatePendingState,
)
from app.services.booking_flow import BookingFlowService
from app.services.self_booking_create_execution import (
    SelfBookingCreateExecutionService,
    _SELF_BOOKING_PII_PURPOSE,
)

_REPO = Path(__file__).resolve().parents[1]
_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_BOOKING = "33333333-3333-4333-8333-333333333333"
_CLIENT_REF = "44444444-4444-4444-8444-444444444444"
_KEY = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_SLOT = f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.1000"
_STARTS = "2026-08-20T10:00:00+05:00"
_PHONE = "+79001234567"
_NAME = "Иван Тестов"
_PHONE_REF = EphemeralPiiReference.generate().to_token()
_NAME_REF = EphemeralPiiReference.generate().to_token()
_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


class RecordingCreateClient:
    def __init__(
        self,
        *,
        result: BookingCreateRemoteSuccess | None = None,
        error: BaseException | None = None,
        errors: list[BaseException] | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.errors = list(errors or [])
        self.calls: list[dict[str, Any]] = []

    def create_booking(self, **kwargs: Any) -> BookingCreateRemoteSuccess:
        self.calls.append(dict(kwargs))
        if self.errors:
            raise self.errors.pop(0)
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("result not configured")
        return self.result


class FakeClientRefResolver:
    def __init__(self, result: ClientRefResolutionResult) -> None:
        self._result = result
        self.calls: list[object] = []

    async def resolve_for_conversation(
        self,
        *,
        conversation_id: object,
    ) -> ClientRefResolutionResult:
        self.calls.append(conversation_id)
        return self._result


class FakePiiStore:
    def __init__(
        self,
        *,
        phone: str | None = _PHONE,
        name: str | None = _NAME,
        fail_read: bool = False,
    ) -> None:
        self.phone = phone
        self.name = name
        self.fail_read = fail_read
        self.read_calls: list[tuple[str, EphemeralPiiKind]] = []
        self.delete_calls: list[tuple[str, EphemeralPiiKind]] = []
        self.consume_calls = 0

    async def read_plaintext(
        self,
        reference: EphemeralPiiReference,
        *,
        conversation_id: uuid.UUID,
        kind: EphemeralPiiKind,
        purpose: EphemeralPiiPurpose,
    ) -> str:
        assert purpose is _SELF_BOOKING_PII_PURPOSE
        self.read_calls.append((reference.to_token(), kind))
        if self.fail_read:
            raise RuntimeError("PII_GONE")
        if kind is EphemeralPiiKind.PHONE:
            if self.phone is None:
                raise RuntimeError("NO_PHONE")
            return self.phone
        if kind is EphemeralPiiKind.CLIENT_NAME:
            if self.name is None:
                raise RuntimeError("NO_NAME")
            return self.name
        raise RuntimeError("BAD_KIND")

    async def delete(
        self,
        reference: EphemeralPiiReference,
        *,
        conversation_id: uuid.UUID,
        kind: EphemeralPiiKind,
        purpose: EphemeralPiiPurpose,
    ) -> None:
        assert purpose is _SELF_BOOKING_PII_PURPOSE
        self.delete_calls.append((reference.to_token(), kind))

    async def consume_once(self, *args: object, **kwargs: object) -> str:
        self.consume_calls += 1
        raise AssertionError("consume_once must not be used in execution")


class FakePendingRow:
    def __init__(self, **kwargs: object) -> None:
        self.id = kwargs.get("id", uuid.uuid4())
        self.conversation_id = kwargs.get("conversation_id", uuid.uuid4())
        self.state = kwargs.get("state", SelfBookingCreatePendingState.EXECUTING.value)
        self.idempotency_key = kwargs.get("idempotency_key", _KEY)
        self.slot_id = kwargs.get("slot_id", _SLOT)
        self.starts_at = kwargs.get("starts_at", _STARTS)
        self.phone_ref_token = kwargs.get("phone_ref_token", _PHONE_REF)
        self.name_ref_token = kwargs.get("name_ref_token", _NAME_REF)
        self.personal_data_consent = True
        self.offer_acknowledgement = True
        self.execution_lease_token = kwargs.get("execution_lease_token", uuid.uuid4())
        self.result_code = kwargs.get("result_code")


def test_execution_result_repr_redacts() -> None:
    result = SelfBookingCreateExecutionResult(
        outcome=SelfBookingCreateExecutionOutcome.SUCCEEDED,
        pending_id=uuid.uuid4(),
        pending_state=SelfBookingCreatePendingState.SUCCEEDED,
        result_code="OK",
        idempotency_key=_KEY,
        booking_id=_BOOKING,
    )
    rendered = repr(result)
    assert _KEY not in rendered
    assert _BOOKING not in rendered
    assert "idempotency_key=<redacted>" in rendered


def test_execution_uses_read_plaintext_not_consume() -> None:
    text = (
        _REPO / "app/services/self_booking_create_execution.py"
    ).read_text(encoding="utf-8")
    assert "read_plaintext" in text
    assert "consume_once" not in text.split("class SelfBookingPiiStore", 1)[0]
    # Protocol may declare delete; execute path must not call consume_once.
    exec_region = text.split("async def execute", 1)[1]
    assert "consume_once" not in exec_region
    assert "read_plaintext" in exec_region
    assert "confirm_selected_slot_for_conversation" in exec_region


def test_execution_reuses_pending_idempotency_key_source() -> None:
    text = (
        _REPO / "app/services/self_booking_create_execution.py"
    ).read_text(encoding="utf-8")
    assert "idempotency_key=claimed.idempotency_key" in text
    assert "uuid.uuid4()" not in text.split("idempotency_key=claimed.idempotency_key", 1)[0][
        -200:
    ]


@pytest.mark.asyncio
async def test_outcome_mapping_retry_later_releases_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambiguous/retryable CREATE → READY + same key (no new key mint)."""

    from app.repositories import self_booking_create_pendings as pending_repo

    lease = uuid.uuid4()
    row = FakePendingRow(execution_lease_token=lease)
    create = RecordingCreateClient(
        error=BookingCreateHttpError("IDEMPOTENCY_IN_PROGRESS"),
    )
    pii = FakePiiStore()
    resolver = FakeClientRefResolver(
        ClientRefResolutionResult(
            outcome=ClientRefResolutionOutcome.FOUND,
            client_ref=_CLIENT_REF,
        )
    )
    flow = BookingFlowService(None, booking_create_client=create)

    released: dict[str, object] = {}

    class PendingSvc:
        async def claim_for_execution(self, **kwargs: object) -> FakePendingRow:
            return row

        async def cancel_if_conversation_fences_stale(self, **kwargs: object) -> bool:
            return False

    async def fake_release(session: object, **kwargs: object) -> bool:
        released.update(kwargs)
        return True

    monkeypatch.setattr(pending_repo, "release_to_ready", fake_release)
    monkeypatch.setattr(
        pending_repo,
        "get_by_id",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected")),
    )

    svc = SelfBookingCreateExecutionService(
        session=object(),  # type: ignore[arg-type]
        pending_service=PendingSvc(),  # type: ignore[arg-type]
        booking_flow=flow,
        client_ref_resolver=resolver,
        pii_store=pii,
        clock=lambda: _NOW,
    )
    result = await svc.execute(pending_id=row.id)
    assert result.outcome is SelfBookingCreateExecutionOutcome.RETRY_SCHEDULED
    assert result.idempotency_key == _KEY
    assert result.pending_state is SelfBookingCreatePendingState.READY
    assert result.result_code == "IDEMPOTENCY_IN_PROGRESS"
    assert create.calls[0]["idempotency_key"] == _KEY
    assert released["lease_token"] == lease
    assert pii.consume_calls == 0
    assert pii.delete_calls == []


@pytest.mark.asyncio
async def test_client_ref_fail_closed_zero_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.repositories import self_booking_create_pendings as pending_repo

    lease = uuid.uuid4()
    row = FakePendingRow(execution_lease_token=lease)
    create = RecordingCreateClient(result=BookingCreateRemoteSuccess(
        booking_id=_BOOKING,
        slot_id=_SLOT,
        starts_at=_STARTS,
        idempotent_replay=False,
    ))
    pii = FakePiiStore()
    resolver = FakeClientRefResolver(
        ClientRefResolutionResult(outcome=ClientRefResolutionOutcome.NOT_FOUND)
    )
    flow = BookingFlowService(None, booking_create_client=create)

    terminal: dict[str, object] = {}

    class PendingSvc:
        async def claim_for_execution(self, **kwargs: object) -> FakePendingRow:
            return row

        async def cancel_if_conversation_fences_stale(self, **kwargs: object) -> bool:
            return False

    async def fake_terminal(session: object, **kwargs: object) -> bool:
        terminal.update(kwargs)
        return True

    monkeypatch.setattr(pending_repo, "mark_terminal", fake_terminal)

    svc = SelfBookingCreateExecutionService(
        session=object(),  # type: ignore[arg-type]
        pending_service=PendingSvc(),  # type: ignore[arg-type]
        booking_flow=flow,
        client_ref_resolver=resolver,
        pii_store=pii,
        clock=lambda: _NOW,
    )
    result = await svc.execute(pending_id=row.id)
    assert result.outcome is SelfBookingCreateExecutionOutcome.FAILED
    assert result.result_code == "CLIENT_REF_NOT_FOUND"
    assert create.calls == []
    assert terminal["state"] is SelfBookingCreatePendingState.FAILED
    assert terminal["lease_token"] == lease


@pytest.mark.asyncio
async def test_success_deletes_pii_after_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.repositories import self_booking_create_pendings as pending_repo

    lease = uuid.uuid4()
    row = FakePendingRow(execution_lease_token=lease)
    create = RecordingCreateClient(
        result=BookingCreateRemoteSuccess(
            booking_id=_BOOKING,
            slot_id=_SLOT,
            starts_at=_STARTS,
            idempotent_replay=False,
        )
    )
    pii = FakePiiStore()
    resolver = FakeClientRefResolver(
        ClientRefResolutionResult(
            outcome=ClientRefResolutionOutcome.FOUND,
            client_ref=_CLIENT_REF,
        )
    )
    flow = BookingFlowService(None, booking_create_client=create)

    class PendingSvc:
        async def claim_for_execution(self, **kwargs: object) -> FakePendingRow:
            return row

        async def cancel_if_conversation_fences_stale(self, **kwargs: object) -> bool:
            return False

    async def fake_terminal(session: object, **kwargs: object) -> bool:
        return True

    monkeypatch.setattr(pending_repo, "mark_terminal", fake_terminal)

    svc = SelfBookingCreateExecutionService(
        session=object(),  # type: ignore[arg-type]
        pending_service=PendingSvc(),  # type: ignore[arg-type]
        booking_flow=flow,
        client_ref_resolver=resolver,
        pii_store=pii,
        clock=lambda: _NOW,
    )
    result = await svc.execute(pending_id=row.id)
    assert result.outcome is SelfBookingCreateExecutionOutcome.SUCCEEDED
    assert result.idempotency_key == _KEY
    assert create.calls[0]["idempotency_key"] == _KEY
    assert create.calls[0]["client_ref"] == _CLIENT_REF
    assert pii.consume_calls == 0
    assert len(pii.read_calls) == 2
    assert len(pii.delete_calls) == 2
    kinds = {kind for _, kind in pii.delete_calls}
    assert kinds == {EphemeralPiiKind.PHONE, EphemeralPiiKind.CLIENT_NAME}
