"""R1.2 regression: F1 exhausted partial, F2 canonical fingerprints, F3 ops read-only."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.amocrm_crm_writes_http import (
    TASK_TEXT_DEFAULT,
    AmoCrmCrmWritesHttpClient,
    task_text_fingerprint,
)
from app.core.amocrm_deal_discovery import (
    AmoCrmDealDiscoveryOutcome,
    AmoCrmDealDiscoveryResult,
)
from app.core.amocrm_identity_lookup import (
    AmoCrmIdentityLookupOutcome,
    AmoCrmIdentityLookupResult,
)
from app.core.booking_request_remote import (
    BotBookingRequestDto,
    BotBookingRequestGameContext,
)
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.core.teya_request_types import TeyaRequestPendingState
from app.repositories import integration_circuit_breakers as breaker_repo
from app.services.teya_request_crm import (
    TeyaCrmActionOutcome,
    TeyaCrmActionResult,
    TeyaRequestCrmService,
    build_teya_crm_task_text,
    build_teya_structured_note,
)
from app.services.teya_request_reconciliation_worker import (
    TeyaRequestReconciliationWorker,
)
from app.teya_ops_router import TeyaOpsConfig, build_teya_ops_router

_TEST_PIPELINE = 1001
_TEST_STATUS = 2002
_TEST_MANAGER = 3003
_TEST_TASK_TYPE = 4004
_NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


@dataclass
class _FakeTransport:
    responses: list[S2sHttpResponse | BaseException]
    calls: list[S2sHttpRequest] = field(default_factory=list)

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        if not self.responses:
            raise AssertionError("unexpected HTTP call")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _json_response(status: int, payload: object) -> S2sHttpResponse:
    return S2sHttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
    )


def _writes(transport: _FakeTransport) -> AmoCrmCrmWritesHttpClient:
    return AmoCrmCrmWritesHttpClient(
        AmoCrmCrmRestConfig(
            enabled=True,
            client_id="crm-client-id-001",
            client_secret="crm-secret-xxxxxxxxxx",
            api_base_url="https://example.amocrm.ru",
            redirect_uri="https://example.com/oauth",
            connection_scope="default",
        ),
        transport=transport,
        pipeline_id=_TEST_PIPELINE,
        open_status_id=_TEST_STATUS,
        manager_id=_TEST_MANAGER,
        task_type_id=_TEST_TASK_TYPE,
    )


@dataclass
class _Identity:
    result: AmoCrmIdentityLookupResult

    async def lookup_by_phone(self, *, phone_e164: str) -> AmoCrmIdentityLookupResult:
        return self.result


@dataclass
class _Deals:
    result: AmoCrmDealDiscoveryResult

    async def discover_deal_candidates(
        self, *, contact_id: str, known_technical_deal_ids: tuple[str, ...] = ()
    ):
        return self.result


class _Tokens:
    async def access_token(self) -> str | None:
        return "token"

    async def refresh_access_token(self) -> str | None:
        return None


def _crm(
    *,
    identity: AmoCrmIdentityLookupResult,
    deals: AmoCrmDealDiscoveryResult,
    transport: _FakeTransport | None = None,
) -> TeyaRequestCrmService:
    return TeyaRequestCrmService(
        identity_lookup=_Identity(identity),
        deal_discovery=_Deals(deals),
        writes=_writes(transport or _FakeTransport([])),
        tokens=_Tokens(),
    )


def _normal_dto() -> BotBookingRequestDto:
    return BotBookingRequestDto(
        request_id=str(uuid4()),
        status="NEW",
        request_type="MANAGER_REQUEST",
        phone_e164="+79001234567",
    )


def _game_dto() -> BotBookingRequestDto:
    return BotBookingRequestDto(
        request_id=str(uuid4()),
        status="NEW",
        request_type="GAME_REQUEST",
        phone_e164="+79001234567",
        game_context=BotBookingRequestGameContext(
            gift="mask", procedure="clean"
        ),
    )


# --- F2: canonical fingerprints ---


def test_normal_note_create_equals_reconcile_fingerprint() -> None:
    dto = _normal_dto()
    create_note = build_teya_structured_note(dto)
    reconcile_note = build_teya_structured_note(dto)
    assert create_note == reconcile_note
    assert create_note == "type=MANAGER_REQUEST; status=NEW"
    assert task_text_fingerprint(create_note) == task_text_fingerprint(
        reconcile_note
    )


def test_game_note_create_equals_reconcile_fingerprint() -> None:
    dto = _game_dto()
    create_note = build_teya_structured_note(dto)
    reconcile_note = build_teya_structured_note(dto)
    assert create_note == reconcile_note
    assert "gift=mask" in create_note
    assert "procedure=clean" in create_note
    assert task_text_fingerprint(create_note) == task_text_fingerprint(
        reconcile_note
    )


def test_normal_task_create_equals_reconcile() -> None:
    dto = _normal_dto()
    assert build_teya_crm_task_text(dto) == TASK_TEXT_DEFAULT
    assert build_teya_crm_task_text(dto) == build_teya_crm_task_text(dto)


def test_game_task_create_equals_reconcile() -> None:
    dto = _game_dto()
    created = build_teya_crm_task_text(dto, appointment_id=None)
    looked = build_teya_crm_task_text(dto, appointment_id=None)
    assert created == looked
    assert "mask" in created
    assert "clean" in created
    assert created != TASK_TEXT_DEFAULT


@pytest.mark.asyncio
async def test_game_note_lost_response_reconcile_finds_no_duplicate_post() -> None:
    dto = _game_dto()
    note = build_teya_structured_note(dto)
    transport = _FakeTransport(
        [
            # ensure_lead_note: list finds existing after "lost create"
            _json_response(
                200,
                {
                    "_embedded": {
                        "notes": [{"id": 91, "params": {"text": note}}]
                    }
                },
            ),
            # ensure_lead_task: list empty then would create — we only test note via
            # reconcile_readonly find path
        ]
    )
    crm = _crm(
        identity=AmoCrmIdentityLookupResult(
            outcome=AmoCrmIdentityLookupOutcome.FOUND, contact_id="101"
        ),
        deals=AmoCrmDealDiscoveryResult(
            outcome=AmoCrmDealDiscoveryOutcome.FOUND,
            contact_id="101",
            business_active_lead_ids=("201",),
        ),
        transport=transport,
    )
    result = await crm.reconcile_readonly(
        phone_e164="+79001234567",
        note_text=note,
        task_text=None,
    )
    assert result.outcome is TeyaCrmActionOutcome.READY
    assert result.note_id == "91"
    assert all(c.method == "GET" for c in transport.calls)
    assert not any(c.method == "POST" for c in transport.calls)


@pytest.mark.asyncio
async def test_game_task_lost_response_reconcile_finds_no_duplicate_post() -> None:
    dto = _game_dto()
    task = build_teya_crm_task_text(dto)
    transport = _FakeTransport(
        [
            _json_response(
                200,
                {
                    "_embedded": {
                        "tasks": [
                            {
                                "id": 88,
                                "text": task,
                                "task_type_id": _TEST_TASK_TYPE,
                                "responsible_user_id": _TEST_MANAGER,
                            }
                        ]
                    }
                },
            ),
        ]
    )
    crm = _crm(
        identity=AmoCrmIdentityLookupResult(
            outcome=AmoCrmIdentityLookupOutcome.FOUND, contact_id="101"
        ),
        deals=AmoCrmDealDiscoveryResult(
            outcome=AmoCrmDealDiscoveryOutcome.FOUND,
            contact_id="101",
            business_active_lead_ids=("201",),
        ),
        transport=transport,
    )
    result = await crm.reconcile_readonly(
        phone_e164="+79001234567",
        note_text=None,
        task_text=task,
    )
    assert result.outcome is TeyaCrmActionOutcome.READY
    assert result.task_id == "88"
    assert all(c.method == "GET" for c in transport.calls)
    assert not any(c.method == "POST" for c in transport.calls)


@pytest.mark.asyncio
async def test_game_reconcile_idempotent_repeat() -> None:
    dto = _game_dto()
    note = build_teya_structured_note(dto)
    task = build_teya_crm_task_text(dto)

    def _transport() -> _FakeTransport:
        return _FakeTransport(
            [
                _json_response(
                    200,
                    {
                        "_embedded": {
                            "notes": [{"id": 91, "params": {"text": note}}]
                        }
                    },
                ),
                _json_response(
                    200,
                    {
                        "_embedded": {
                            "tasks": [
                                {
                                    "id": 88,
                                    "text": task,
                                    "task_type_id": _TEST_TASK_TYPE,
                                    "responsible_user_id": _TEST_MANAGER,
                                }
                            ]
                        }
                    },
                ),
            ]
        )

    identity = AmoCrmIdentityLookupResult(
        outcome=AmoCrmIdentityLookupOutcome.FOUND, contact_id="101"
    )
    deals = AmoCrmDealDiscoveryResult(
        outcome=AmoCrmDealDiscoveryOutcome.FOUND,
        contact_id="101",
        business_active_lead_ids=("201",),
    )
    first = await _crm(
        identity=identity, deals=deals, transport=_transport()
    ).reconcile_readonly(
        phone_e164="+79001234567", note_text=note, task_text=task
    )
    second = await _crm(
        identity=identity, deals=deals, transport=_transport()
    ).reconcile_readonly(
        phone_e164="+79001234567", note_text=note, task_text=task
    )
    assert first == second
    assert first.note_id == "91"
    assert first.task_id == "88"


# --- F1: exhausted partial stays MANUAL ---


@pytest.mark.asyncio
async def test_exhausted_partial_stays_manual_no_claimable_crm_ready() -> None:
    row = SimpleNamespace(
        state=TeyaRequestPendingState.MANUAL_REVIEW.value,
        attempt_count=8,
        max_attempts=8,
        amocrm_contact_id=None,
        amocrm_deal_id=None,
        amocrm_task_id=None,
        structured_note=None,
        result_code="MAX_ATTEMPTS_EXCEEDED",
        result_outcome=TeyaRequestPendingState.MANUAL_REVIEW.value,
        manual_review_reason="MAX_ATTEMPTS_EXCEEDED",
        lease_token=None,
        lease_expires_at=None,
        next_retry_at=None,
        updated_at=_NOW,
    )
    dto = _normal_dto()

    class _Crm:
        writes = 0

        async def reconcile_readonly(self, **_kwargs: object):
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.READY,
                contact_id="101",
                deal_id="201",
                note_id=None,
                task_id=None,
            )

    session = AsyncMock()
    session.flush = AsyncMock()
    worker = TeyaRequestReconciliationWorker(
        MagicMock(), remote=MagicMock(), crm=_Crm()  # type: ignore[arg-type]
    )
    changed = await worker._reconcile_crm(session, row, dto, _NOW)  # type: ignore[arg-type]
    assert changed is True
    assert row.state == TeyaRequestPendingState.MANUAL_REVIEW.value
    assert row.amocrm_contact_id == "101"
    assert row.amocrm_deal_id == "201"
    assert row.amocrm_task_id is None
    assert row.manual_review_reason == "RECON_CRM_PARTIAL"
    assert row.result_code == "RECON_CRM_PARTIAL"
    # Not claimable: still terminal MANUAL, attempts unchanged.
    assert row.attempt_count == 8
    assert row.attempt_count >= row.max_attempts


@pytest.mark.asyncio
async def test_non_exhausted_partial_may_advance_to_crm_ready() -> None:
    row = SimpleNamespace(
        state=TeyaRequestPendingState.IDENTITY.value,
        attempt_count=1,
        max_attempts=8,
        amocrm_contact_id=None,
        amocrm_deal_id=None,
        amocrm_task_id=None,
        structured_note=None,
        result_code=None,
        result_outcome=None,
        manual_review_reason=None,
        lease_token=None,
        lease_expires_at=None,
        next_retry_at=None,
        updated_at=_NOW,
    )
    dto = _normal_dto()

    class _Crm:
        async def reconcile_readonly(self, **_kwargs: object):
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.READY,
                contact_id="101",
                deal_id="201",
            )

    session = AsyncMock()
    session.flush = AsyncMock()
    worker = TeyaRequestReconciliationWorker(
        MagicMock(), remote=MagicMock(), crm=_Crm()  # type: ignore[arg-type]
    )
    assert await worker._reconcile_crm(session, row, dto, _NOW)  # type: ignore[arg-type]
    assert row.state == TeyaRequestPendingState.CRM_READY.value
    assert row.amocrm_contact_id == "101"
    assert row.amocrm_deal_id == "201"


@pytest.mark.asyncio
async def test_exhausted_full_exact_advances_to_reconciled() -> None:
    row = SimpleNamespace(
        state=TeyaRequestPendingState.MANUAL_REVIEW.value,
        attempt_count=8,
        max_attempts=8,
        amocrm_contact_id=None,
        amocrm_deal_id=None,
        amocrm_task_id=None,
        structured_note=None,
        result_code="MAX_ATTEMPTS_EXCEEDED",
        result_outcome=TeyaRequestPendingState.MANUAL_REVIEW.value,
        manual_review_reason="MAX_ATTEMPTS_EXCEEDED",
        lease_token=None,
        lease_expires_at=None,
        next_retry_at=None,
        updated_at=_NOW,
    )
    dto = _normal_dto()

    class _Crm:
        async def reconcile_readonly(self, **_kwargs: object):
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.READY,
                contact_id="101",
                deal_id="201",
                note_id="9",
                task_id="77",
            )

    session = AsyncMock()
    session.flush = AsyncMock()
    worker = TeyaRequestReconciliationWorker(
        MagicMock(), remote=MagicMock(), crm=_Crm()  # type: ignore[arg-type]
    )
    assert await worker._reconcile_crm(session, row, dto, _NOW)  # type: ignore[arg-type]
    assert row.state == TeyaRequestPendingState.RECONCILED.value
    assert row.amocrm_task_id == "77"
    assert row.manual_review_reason is None


# --- F3: ops GET read-only ---


@pytest.mark.asyncio
async def test_breaker_get_returns_none_without_insert() -> None:
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    snap = await breaker_repo.get(session)
    assert snap is None
    session.get.assert_awaited_once()
    session.execute.assert_not_awaited()


def test_ops_snapshot_missing_breaker_is_read_only_default() -> None:
    class _Scope:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        async def __aenter__(self) -> object:
            session = MagicMock()

            async def _scalars(*_a: object, **_k: object):
                class _R:
                    def all(self) -> list[object]:
                        return []

                return _R()

            session.scalars = _scalars
            return session

        async def __aexit__(self, *_a: object) -> None:
            return None

    async def _now(_session: object):
        return _NOW

    async def _get(_session: object, **_k: object):
        return None

    from unittest.mock import patch

    app = FastAPI()
    app.include_router(
        build_teya_ops_router(
            config=TeyaOpsConfig("x" * 16),
            session_factory=MagicMock(),
        )
    )
    with (
        patch("app.teya_ops_router.session_scope", _Scope),
        patch("app.teya_ops_router.breaker_repo.get", _get),
        patch("app.db.clock.db_statement_now", _now),
    ):
        client = TestClient(app)
        resp = client.get(
            "/internal/teya-ops/snapshot",
            headers={"X-Teya-Ops-Token": "x" * 16},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["breaker"]["state"] == "CLOSED"
    assert body["breaker"]["failure_count"] == 0
    assert body["pendings"] == []
