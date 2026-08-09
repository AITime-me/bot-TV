"""Unit tests for CURSOR-28 master command flow (no live channels / PG)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.master_channel_binding import (
    ResolveMasterBindingOutcome,
    ResolveMasterBindingResult,
)
from app.core.master_command_http import (
    MasterCommandAdapterReasonCode,
    MasterCommandHttpClient,
    MasterCommandHttpError,
)
from app.core.master_command_parser import (
    MasterCommandControlIntent,
    MasterCommandParseStatus,
    classify_control_intent,
    parse_master_command_text,
)
from app.core.master_command_remote import (
    MASTER_BOOKINGS_ROUTE_PATH,
    MASTER_CLOSE_DAY_ROUTE_PATH,
    MASTER_CLOSE_INTERVAL_ROUTE_PATH,
    MASTER_SCHEDULE_ROUTE_PATH,
    MasterMutationRemoteSuccess,
    MasterScheduleRemoteSuccess,
    build_close_day_request_body,
    build_master_booking_request_body,
    parse_schedule_success_payload,
)
from app.core.master_command_types import (
    MasterCommandClarificationNeed,
    MasterCommandFlowOutcome,
    MasterCommandKind,
    build_master_command_envelope,
)
from app.core.s2s_http_transport import S2sHttpResponse, S2sHttpTransportError
from app.services.master_command_flow import MasterCommandFlowService

_MASTER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
_VALID_TOKEN = "a" * 32


def test_parser_close_interval_ready() -> None:
    result = parse_master_command_text(
        "закрыть интервал 10.08 с 14:00 до 15:00",
        now=_NOW,
    )
    assert result.status is MasterCommandParseStatus.READY
    assert result.kind is MasterCommandKind.CLOSE_INTERVAL
    assert result.payload is not None
    assert result.payload.date_key == "2026-08-10"
    assert result.payload.start_time == "14:00"
    assert result.payload.end_time == "15:00"
    assert result.payload.block_type == "BREAK"


def test_parser_day_off_tomorrow_yekaterinburg() -> None:
    result = parse_master_command_text("выходной завтра", now=_NOW)
    assert result.status is MasterCommandParseStatus.READY
    assert result.kind is MasterCommandKind.CLOSE_DAY
    assert result.payload is not None
    assert result.payload.date_key == "2026-08-11"
    assert result.payload.block_type == "DAY_OFF"


def test_parser_booking_requires_slot_and_contacts() -> None:
    result = parse_master_command_text(
        "запись клиенту на 12.08 в 15:00",
        now=_NOW,
    )
    assert result.status is MasterCommandParseStatus.CLARIFICATION_REQUIRED
    assert result.kind is MasterCommandKind.CREATE_BOOKING
    assert MasterCommandClarificationNeed.SLOT_ID in result.needs
    assert MasterCommandClarificationNeed.PHONE in result.needs
    assert MasterCommandClarificationNeed.CLIENT_NAME in result.needs


def test_parser_booking_with_slot_phone_name_ready() -> None:
    slot = (
        "bs1.11111111-1111-4111-8111-111111111111."
        f"{_MASTER}.2026-08-12.1500"
    )
    result = parse_master_command_text(
        f"запись клиенту Иван +79991234567 {slot}",
        now=_NOW,
    )
    assert result.status is MasterCommandParseStatus.READY
    assert result.kind is MasterCommandKind.CREATE_BOOKING
    assert result.payload is not None
    assert result.payload.slot_id == slot.lower()
    assert result.phone == "+79991234567"
    assert result.client_name == "Иван"


def test_parser_unknown_and_control() -> None:
    assert (
        parse_master_command_text("привет как дела", now=_NOW).status
        is MasterCommandParseStatus.UNKNOWN
    )
    assert classify_control_intent("да") is MasterCommandControlIntent.CONFIRM
    assert classify_control_intent("отмена") is MasterCommandControlIntent.CANCEL


def test_parser_relative_date_without_timezone_fails_closed() -> None:
    naive = datetime(2026, 8, 10, 12, 0)
    result = parse_master_command_text("выходной завтра", now=naive)
    assert result.status is MasterCommandParseStatus.UNKNOWN


def test_envelope_redacts_sensitive_repr() -> None:
    env = build_master_command_envelope(
        channel="vk",
        external_account_id="vk-1",
        external_message_id="msg-1",
        text="выходной завтра",
        occurred_at=_NOW,
    )
    rendered = repr(env)
    assert "vk-1" not in rendered
    assert "выходной" not in rendered
    assert "<redacted>" in rendered


def test_remote_builders_and_schedule_strips_client_name() -> None:
    body = build_close_day_request_body(
        idempotency_key="11111111-1111-4111-8111-111111111111",
        master_id=_MASTER,
        date_key="2026-08-10",
        block_type="DAY_OFF",
    )
    assert body["masterId"] == _MASTER
    booking = build_master_booking_request_body(
        idempotency_key="11111111-1111-4111-8111-111111111111",
        master_id=_MASTER,
        slot_id=(
            "bs1.11111111-1111-4111-8111-111111111111."
            f"{_MASTER}.2026-08-12.1500"
        ),
        client_name="Иван",
        phone="+79991234567",
    )
    assert booking["personalDataConsent"] is True
    parsed = parse_schedule_success_payload(
        {
            "ok": True,
            "masterId": _MASTER,
            "fromDateKey": "2026-08-10",
            "toDateKey": "2026-08-10",
            "days": [
                {
                    "dateKey": "2026-08-10",
                    "appointments": [
                        {
                            "id": "appt-1",
                            "startsAt": "2026-08-10T10:00:00+05:00",
                            "endsAt": "2026-08-10T11:00:00+05:00",
                            "clientName": "Секрет",
                            "serviceName": "Стрижка",
                        }
                    ],
                    "scheduleBlocks": [],
                    "extraWorkWindows": [],
                }
            ],
        }
    )
    appt = parsed.days[0]["appointments"][0]
    assert "clientName" not in appt
    assert "id" not in appt
    assert appt["serviceName"] == "Стрижка"
    assert "Секрет" not in repr(parsed)


@dataclass
class _FakeTransport:
    responses: list[S2sHttpResponse | Exception]
    calls: list[Any]

    def request(self, request: Any) -> S2sHttpResponse:
        self.calls.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _json_response(payload: dict[str, Any], status: int = 200) -> S2sHttpResponse:
    import json

    body = json.dumps(payload).encode("utf-8")
    return S2sHttpResponse(
        status_code=status,
        headers={"Content-Type": "application/json"},
        body=body,
    )


def test_http_client_schedule_and_timeout() -> None:
    from app.core.booking_eligibility_http import BookingEligibilityHttpConfig

    config = BookingEligibilityHttpConfig(
        base_url="https://booking.example",
        bearer_token=_VALID_TOKEN,
        timeout_seconds=1.0,
        max_response_bytes=65536,
    )
    transport = _FakeTransport(
        responses=[
            _json_response(
                {
                    "ok": True,
                    "masterId": _MASTER,
                    "fromDateKey": "2026-08-10",
                    "toDateKey": "2026-08-10",
                    "days": [
                        {
                            "dateKey": "2026-08-10",
                            "appointments": [],
                            "scheduleBlocks": [],
                            "extraWorkWindows": [],
                        }
                    ],
                }
            )
        ],
        calls=[],
    )
    client = MasterCommandHttpClient(config, transport)
    result = client.read_schedule(
        master_id=_MASTER,
        from_date_key="2026-08-10",
        to_date_key="2026-08-10",
    )
    assert isinstance(result, MasterScheduleRemoteSuccess)
    assert transport.calls[0].url.endswith(MASTER_SCHEDULE_ROUTE_PATH)

    transport2 = _FakeTransport(
        responses=[S2sHttpTransportError("TIMEOUT")],
        calls=[],
    )
    client2 = MasterCommandHttpClient(config, transport2)
    with pytest.raises(MasterCommandHttpError) as exc:
        client2.close_day(
            idempotency_key="11111111-1111-4111-8111-111111111111",
            master_id=_MASTER,
            date_key="2026-08-10",
            block_type="DAY_OFF",
        )
    assert exc.value.code == MasterCommandAdapterReasonCode.TIMEOUT.value
    assert MASTER_CLOSE_DAY_ROUTE_PATH in transport2.calls[0].url


def test_http_client_malformed_fail_closed() -> None:
    from app.core.booking_eligibility_http import BookingEligibilityHttpConfig

    config = BookingEligibilityHttpConfig(
        base_url="https://booking.example",
        bearer_token=_VALID_TOKEN,
        timeout_seconds=1.0,
        max_response_bytes=65536,
    )
    transport = _FakeTransport(
        responses=[
            S2sHttpResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body=b"not-json",
            )
        ],
        calls=[],
    )
    client = MasterCommandHttpClient(config, transport)
    with pytest.raises(MasterCommandHttpError) as exc:
        client.close_interval(
            idempotency_key="11111111-1111-4111-8111-111111111111",
            master_id=_MASTER,
            date_key="2026-08-10",
            start_time="14:00",
            end_time="15:00",
            block_type="BREAK",
        )
    assert exc.value.code == "RESPONSE_INVALID"
    assert MASTER_CLOSE_INTERVAL_ROUTE_PATH in transport.calls[0].url


@pytest.mark.asyncio
async def test_flow_unbound_never_calls_booking() -> None:
    session = MagicMock()
    client = MagicMock()
    flow = MasterCommandFlowService(session, master_client=client)
    flow._bindings = MagicMock()
    flow._bindings.resolve = AsyncMock(
        return_value=ResolveMasterBindingResult(
            outcome=ResolveMasterBindingOutcome.NOT_FOUND
        )
    )
    from app.repositories import master_command_pendings as pending_repo

    pending_repo.get_by_inbound = AsyncMock(return_value=None)  # type: ignore[method-assign]
    pending_repo.lock_active_by_identity = AsyncMock(return_value=None)  # type: ignore[method-assign]

    env = build_master_command_envelope(
        channel="vk",
        external_account_id="vk-1",
        external_message_id="m1",
        text="выходной завтра",
        occurred_at=_NOW,
    )
    # Patch module-level repo used inside service
    import app.services.master_command_flow as flow_mod

    flow_mod.pending_repo.get_by_inbound = AsyncMock(return_value=None)
    flow_mod.pending_repo.lock_active_by_identity = AsyncMock(return_value=None)

    result = await flow.handle(env)
    assert result.outcome is MasterCommandFlowOutcome.BINDING_REQUIRED
    client.read_schedule.assert_not_called()
    client.close_day.assert_not_called()


@pytest.mark.asyncio
async def test_flow_ambiguous_binding_fail_closed() -> None:
    session = MagicMock()
    client = MagicMock()
    flow = MasterCommandFlowService(session, master_client=client)
    flow._bindings = MagicMock()
    flow._bindings.resolve = AsyncMock(
        return_value=ResolveMasterBindingResult(
            outcome=ResolveMasterBindingOutcome.AMBIGUOUS
        )
    )
    import app.services.master_command_flow as flow_mod

    flow_mod.pending_repo.get_by_inbound = AsyncMock(return_value=None)
    env = build_master_command_envelope(
        channel="vk",
        external_account_id="vk-1",
        external_message_id="m2",
        text="выходной завтра",
        occurred_at=_NOW,
    )
    result = await flow.handle(env)
    assert result.outcome is MasterCommandFlowOutcome.BINDING_AMBIGUOUS
    client.close_day.assert_not_called()


def test_architecture_no_live_vk_or_booking_db() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "app/services/master_command_flow.py",
        root / "app/core/master_command_http.py",
        root / "app/core/master_command_parser.py",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        lower = text.lower()
        assert "vk_api" not in lower
        assert "vkontakte" not in lower
        assert "max_api" not in lower
        assert "prisma" not in lower
        assert "EMERGENCY_LOCK=False" not in text
        assert "BotMode.ON" not in text
    assert MASTER_BOOKINGS_ROUTE_PATH.startswith("/api/internal/bot/v1/master/")
    factory = (root / "app/core/booking_eligibility_factory.py").read_text(
        encoding="utf-8"
    )
    assert "MasterCommandHttpClient" in factory
    assert "master_command" in factory


def test_mutation_success_repr_has_no_pii() -> None:
    success = MasterMutationRemoteSuccess(idempotent_replay=False, resource_kind="block")
    assert "phone" not in repr(success).lower()
    err = MasterCommandHttpError("TIMEOUT")
    assert "token" not in repr(err)


def test_create_booking_uses_non_destructive_pii_read() -> None:
    from pathlib import Path

    from app.services.master_command_flow import _RETRYABLE_REMOTE

    text = (
        Path(__file__).resolve().parents[1]
        / "app/services/master_command_flow.py"
    ).read_text(encoding="utf-8")
    assert "async def _read_booking_pii" in text
    assert "read_plaintext" in text
    assert "_cleanup_booking_pii" not in text
    assert "_consume_booking_pii" not in text
    assert ".delete(" not in text
    assert "consume_once" not in text
    confirm_region = text.split("async def _execute_confirmed", 1)[1].split(
        "async def _handle_remote_error", 1
    )[0]
    assert "read_plaintext" in confirm_region or "_read_booking_pii" in confirm_region
    assert "IDEMPOTENCY_IN_PROGRESS" in _RETRYABLE_REMOTE
    assert "TIMEOUT" in _RETRYABLE_REMOTE
    assert "TRANSPORT_ERROR" in _RETRYABLE_REMOTE
    assert "RESPONSE_INVALID" in _RETRYABLE_REMOTE
    assert "RESPONSE_TOO_LARGE" in _RETRYABLE_REMOTE
    assert "REQUEST_INVALID" not in _RETRYABLE_REMOTE
    assert "CONFIG_INVALID" not in _RETRYABLE_REMOTE


@pytest.mark.asyncio
async def test_response_invalid_is_retryable_not_terminal() -> None:
    """Malformed/unknown 2xx must release to confirmation, not destroy the command."""

    from app.services.master_command_flow import MasterCommandFlowService

    session = MagicMock()
    active = MagicMock()
    active.command_kind = MasterCommandKind.CREATE_BOOKING.value
    active.command_version = 1
    active.idempotency_key = str(uuid.uuid4())
    active.safe_payload = {"slot_id": "bs1.x"}
    lease = uuid.uuid4()

    import app.services.master_command_flow as flow_mod

    flow_mod.pending_repo.release_execution_to_confirmation = AsyncMock(
        return_value=True
    )
    flow_mod.pending_repo.complete_execution = AsyncMock(return_value=True)
    flow_mod.pending_repo.insert_pending = AsyncMock()

    flow = MasterCommandFlowService(session, master_client=MagicMock())
    env = build_master_command_envelope(
        channel="vk",
        external_account_id="vk-ri",
        external_message_id="ri-1",
        text="да",
        occurred_at=_NOW,
    )
    result = await flow._handle_remote_error(
        envelope=env,
        master_id=_MASTER,
        active=active,
        lease=lease,
        kind=MasterCommandKind.CREATE_BOOKING,
        code="RESPONSE_INVALID",
        now=_NOW,
    )
    assert result.outcome is MasterCommandFlowOutcome.UNAVAILABLE
    assert result.result_code == "RESPONSE_INVALID"
    flow_mod.pending_repo.release_execution_to_confirmation.assert_awaited_once()
    flow_mod.pending_repo.complete_execution.assert_not_awaited()


@pytest.mark.asyncio
async def test_dedupe_mirror_persists_real_mutation_kind() -> None:
    """Audit/dedupe rows must not lie as SCHEDULE_READ when a mutation key exists."""

    session = MagicMock()
    session.flush = AsyncMock()
    inserted: dict[str, Any] = {}

    async def _insert_pending(_session: Any, **kwargs: Any) -> None:
        inserted.update(kwargs)

    import app.services.master_command_flow as flow_mod

    flow_mod.pending_repo.insert_pending = AsyncMock(side_effect=_insert_pending)
    flow = MasterCommandFlowService(session, master_client=MagicMock())
    env = build_master_command_envelope(
        channel="vk",
        external_account_id="vk-dedupe",
        external_message_id="dedupe-1",
        text="да",
        occurred_at=_NOW,
    )
    key = str(uuid.uuid4())
    result = await flow._insert_dedupe_mirror(
        env,
        _MASTER,
        _NOW,
        kind=MasterCommandKind.CREATE_BOOKING,
        outcome=MasterCommandFlowOutcome.SUCCESS,
        version=1,
        result_code="OK",
        idempotency_key=key,
    )
    assert result.outcome is MasterCommandFlowOutcome.SUCCESS
    assert inserted["command_kind"] is MasterCommandKind.CREATE_BOOKING
    assert inserted["idempotency_key"] == key
    assert inserted["command_kind"] is not MasterCommandKind.SCHEDULE_READ
