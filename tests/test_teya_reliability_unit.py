"""Reliability R1 unit tests: 5xx, backoff, breaker, verify-before-retry, ops."""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.amocrm_circuit_breaker import (
    CircuitBreakerPolicy,
    CircuitBreakerState,
    is_breaker_failure_code,
    load_amocrm_breaker_policy,
)
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.amocrm_crm_writes_http import AmoCrmCrmWritesHttpClient
from app.core.amocrm_deal_discovery import (
    AmoCrmDealDiscoveryOutcome,
    AmoCrmDealDiscoveryResult,
)
from app.core.amocrm_identity_lookup import (
    AmoCrmIdentityLookupOutcome,
    AmoCrmIdentityLookupResult,
)
from app.core.booking_eligibility_http import BookingEligibilityHttpConfig
from app.core.booking_request_http import (
    BookingRequestHttpClient,
    BookingRequestHttpError,
)
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransportError,
)
from app.core.teya_request_retry import (
    TeyaRetryPolicy,
    classify_remote_code,
    compute_next_retry_delay_seconds,
    load_teya_retry_policy,
)
from app.core.teya_request_types import (
    TeyaRequestOrchestratorOutcome,
    TeyaRequestPendingState,
)
from app.models.teya_request_pending import TeyaRequestPending
from app.services.teya_request_crm import (
    TeyaCrmActionOutcome,
    TeyaRequestCrmService,
)
from app.services.teya_request_orchestrator import TeyaRequestOrchestratorService
from app.services.teya_request_pending import TeyaRequestPendingService
from app.teya_ops_router import PendingOpsItem, load_teya_ops_config

_REPO = Path(__file__).resolve().parents[1]
_NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
_REQUEST = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_LEASE = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_TEST_PIPELINE = 1001
_TEST_STATUS = 2002
_TEST_MANAGER = 3003
_TEST_TASK_TYPE = 4004
_TOKEN = "t" * 32


class _SeqTransport:
    def __init__(
        self,
        responses: list[S2sHttpResponse | BaseException],
    ) -> None:
        self._responses = list(responses)
        self.calls: list[S2sHttpRequest] = []

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        if not self._responses:
            raise AssertionError("unexpected HTTP call")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _json_response(status: int, payload: object | None = None) -> S2sHttpResponse:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return S2sHttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        body=body,
    )


def _booking_config() -> BookingEligibilityHttpConfig:
    return BookingEligibilityHttpConfig(
        base_url="https://booking.example",
        bearer_token=_TOKEN,
        timeout_seconds=3.0,
        max_response_bytes=65536,
    )


def _booking_client(transport: _SeqTransport) -> BookingRequestHttpClient:
    return BookingRequestHttpClient(_booking_config(), transport)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (500, "INTERNAL_ERROR"),
        (502, "SERVICE_UNAVAILABLE"),
        (503, "SERVICE_UNAVAILABLE"),
        (504, "SERVICE_UNAVAILABLE"),
    ],
)
def test_booking_5xx_is_retryable(status: int, expected: str) -> None:
    client = _booking_client(_SeqTransport([_json_response(status)]))
    with pytest.raises(BookingRequestHttpError) as exc:
        client.get(request_id=_REQUEST)
    assert exc.value.code == expected
    assert classify_remote_code(exc.value.code) == "RETRY"
    assert exc.value.code != "REMOTE_REJECTED"


def test_booking_timeout_is_retryable() -> None:
    client = _booking_client(
        _SeqTransport([S2sHttpTransportError("TIMEOUT")])
    )
    with pytest.raises(BookingRequestHttpError) as exc:
        client.get(request_id=_REQUEST)
    assert exc.value.code == "TIMEOUT"
    assert classify_remote_code("TIMEOUT") == "RETRY"


def test_booking_network_is_retryable() -> None:
    client = _booking_client(
        _SeqTransport([S2sHttpTransportError("TRANSPORT_ERROR")])
    )
    with pytest.raises(BookingRequestHttpError) as exc:
        client.get(request_id=_REQUEST)
    assert exc.value.code == "TRANSPORT_ERROR"
    assert classify_remote_code("TRANSPORT_ERROR") == "RETRY"


def test_backoff_bounds_and_jitter_determinism() -> None:
    policy = TeyaRetryPolicy(
        base_seconds=30.0, max_seconds=900.0, jitter_ratio=0.2, max_attempts=8
    )
    rng = random.Random(42)
    delays = [
        compute_next_retry_delay_seconds(
            attempt_count=i, policy=policy, rng=rng
        )
        for i in range(1, 6)
    ]
    assert delays[0] == pytest.approx(30.0, abs=6.1)
    assert max(delays) <= 900.0
    assert min(delays) >= 0.0
    rng2 = random.Random(42)
    delays2 = [
        compute_next_retry_delay_seconds(
            attempt_count=i, policy=policy, rng=rng2
        )
        for i in range(1, 6)
    ]
    assert delays == delays2
    no_jitter = TeyaRetryPolicy(
        base_seconds=30.0, max_seconds=120.0, jitter_ratio=0.0
    )
    assert compute_next_retry_delay_seconds(
        attempt_count=3, policy=no_jitter, rng=random.Random(1)
    ) == 120.0


def test_retry_policy_env_defaults() -> None:
    policy = load_teya_retry_policy({})
    assert policy.base_seconds == 30.0
    assert policy.max_seconds == 900.0
    assert policy.max_attempts == 8


def test_business_4xx_does_not_trip_breaker() -> None:
    assert not is_breaker_failure_code("VALIDATION_ERROR")
    assert not is_breaker_failure_code("UNAUTHORIZED")
    assert not is_breaker_failure_code("BOOKING_REQUEST_CONFLICT")
    assert not is_breaker_failure_code("IDENTITY_AMBIGUOUS")
    assert is_breaker_failure_code("AMOCRM_CONTACT_CREATE_TRANSIENT")


def test_breaker_policy_env() -> None:
    policy = load_amocrm_breaker_policy(
        {
            "AMOCRM_BREAKER_FAILURE_THRESHOLD": "3",
            "AMOCRM_BREAKER_COOLDOWN_SECONDS": "45",
        }
    )
    assert policy.failure_threshold == 3
    assert policy.cooldown_seconds == 45.0


@dataclass
class _IdentitySeq:
    results: list[AmoCrmIdentityLookupResult]
    calls: int = 0

    async def lookup_by_phone(
        self, *, phone_e164: str
    ) -> AmoCrmIdentityLookupResult:
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]


@dataclass
class _DealSeq:
    results: list[AmoCrmDealDiscoveryResult]
    calls: int = 0

    async def discover_deal_candidates(
        self, *, contact_id: str, known_technical_deal_ids: tuple[str, ...] = ()
    ) -> AmoCrmDealDiscoveryResult:
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]


class _Tokens:
    async def access_token(self) -> str | None:
        return "token"

    async def refresh_access_token(self) -> str | None:
        return None


def _writes(transport: _SeqTransport) -> AmoCrmCrmWritesHttpClient:
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


@pytest.mark.asyncio
async def test_crm_contact_timeout_then_verify_reuse() -> None:
    transport = _SeqTransport([S2sHttpTransportError("TIMEOUT")])
    identity = _IdentitySeq(
        [
            AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.NOT_FOUND
            ),
            AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.FOUND, contact_id="101"
            ),
        ]
    )
    deals = _DealSeq(
        [
            AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.FOUND,
                contact_id="101",
                business_active_lead_ids=("201",),
            )
        ]
    )
    crm = TeyaRequestCrmService(
        identity_lookup=identity,
        deal_discovery=deals,
        writes=_writes(transport),
        tokens=_Tokens(),
    )
    result = await crm.ensure_contact_and_deal(
        phone_e164="+79001234567", client_name="Test"
    )
    assert result.outcome is TeyaCrmActionOutcome.READY
    assert result.contact_id == "101"
    assert result.deal_id == "201"
    assert identity.calls == 2
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_crm_deal_timeout_then_verify_reuse() -> None:
    transport = _SeqTransport([S2sHttpTransportError("TIMEOUT")])
    identity = _IdentitySeq(
        [
            AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.FOUND, contact_id="100"
            )
        ]
    )
    deals = _DealSeq(
        [
            AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.NOT_FOUND, contact_id="100"
            ),
            AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.FOUND,
                contact_id="100",
                business_active_lead_ids=("202",),
            ),
        ]
    )
    crm = TeyaRequestCrmService(
        identity_lookup=identity,
        deal_discovery=deals,
        writes=_writes(transport),
        tokens=_Tokens(),
    )
    result = await crm.ensure_contact_and_deal(
        phone_e164="+79001234567", client_name="Test"
    )
    assert result.outcome is TeyaCrmActionOutcome.READY
    assert result.deal_id == "202"
    assert deals.calls == 2


@pytest.mark.asyncio
async def test_crm_task_timeout_no_duplicate_on_retry() -> None:
    note_text = "type=MANAGER_REQUEST"
    task_text = "Обработать заявку из онлайн-записи"
    # First attempt: note ok, task create times out.
    transport1 = _SeqTransport(
        [
            _json_response(200, {"_embedded": {"notes": []}}),
            _json_response(200, {"_embedded": {"notes": [{"id": 9}]}}),
            _json_response(200, {"id": 9}),
            _json_response(200, {"_embedded": {"tasks": []}}),
            S2sHttpTransportError("TIMEOUT"),
        ]
    )
    crm1 = TeyaRequestCrmService(
        identity_lookup=_IdentitySeq([]),
        deal_discovery=_DealSeq([]),
        writes=_writes(transport1),
        tokens=_Tokens(),
    )
    first = await crm1.attach_note_and_task(
        deal_id="200", note_text=note_text, task_text=task_text
    )
    assert first.outcome is TeyaCrmActionOutcome.RETRY
    assert first.error_code == "AMOCRM_TASK_CREATE_TRANSIENT"

    # Retry: note+task already present → reuse, no POST_TASK.
    transport2 = _SeqTransport(
        [
            _json_response(
                200,
                {
                    "_embedded": {
                        "notes": [{"id": 9, "params": {"text": note_text}}]
                    }
                },
            ),
            _json_response(
                200,
                {
                    "_embedded": {
                        "tasks": [
                            {
                                "id": 77,
                                "text": task_text,
                                "task_type_id": _TEST_TASK_TYPE,
                                "responsible_user_id": _TEST_MANAGER,
                            }
                        ]
                    }
                },
            ),
        ]
    )
    crm2 = TeyaRequestCrmService(
        identity_lookup=_IdentitySeq([]),
        deal_discovery=_DealSeq([]),
        writes=_writes(transport2),
        tokens=_Tokens(),
    )
    second = await crm2.attach_note_and_task(
        deal_id="200", note_text=note_text, task_text=task_text
    )
    assert second.outcome is TeyaCrmActionOutcome.READY
    assert second.task_id == "77"
    assert "POST_TASK" not in crm2._writes.http_calls  # noqa: SLF001


@pytest.mark.asyncio
async def test_crm_note_timeout_no_duplicate_on_retry() -> None:
    note_text = "type=MANAGER_REQUEST;status=NEW"
    transport1 = _SeqTransport(
        [
            _json_response(200, {"_embedded": {"notes": []}}),
            S2sHttpTransportError("TIMEOUT"),
        ]
    )
    crm1 = TeyaRequestCrmService(
        identity_lookup=_IdentitySeq([]),
        deal_discovery=_DealSeq([]),
        writes=_writes(transport1),
        tokens=_Tokens(),
    )
    first = await crm1.attach_note_and_task(
        deal_id="200", note_text=note_text
    )
    assert first.outcome is TeyaCrmActionOutcome.RETRY

    transport2 = _SeqTransport(
        [
            _json_response(
                200,
                {
                    "_embedded": {
                        "notes": [
                            {"id": 11, "params": {"text": note_text}}
                        ]
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
                                "text": "Обработать заявку из онлайн-записи",
                                "task_type_id": _TEST_TASK_TYPE,
                                "responsible_user_id": _TEST_MANAGER,
                            }
                        ]
                    }
                },
            ),
        ]
    )
    crm2 = TeyaRequestCrmService(
        identity_lookup=_IdentitySeq([]),
        deal_discovery=_DealSeq([]),
        writes=_writes(transport2),
        tokens=_Tokens(),
    )
    second = await crm2.attach_note_and_task(
        deal_id="200", note_text=note_text
    )
    assert second.outcome is TeyaCrmActionOutcome.READY
    assert second.note_id == "11"
    assert "POST_NOTE" not in crm2._writes.http_calls  # noqa: SLF001


@pytest.mark.asyncio
async def test_invalid_crm_config_manual_no_endless_retry() -> None:
    transport = _SeqTransport([])
    writes = AmoCrmCrmWritesHttpClient(
        AmoCrmCrmRestConfig(
            enabled=False,
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
    crm = TeyaRequestCrmService(
        identity_lookup=_IdentitySeq(
            [
                AmoCrmIdentityLookupResult(
                    outcome=AmoCrmIdentityLookupOutcome.DISABLED,
                    error_code="AMOCRM_CRM_REST_DISABLED",
                )
            ]
        ),
        deal_discovery=_DealSeq([]),
        writes=writes,
        tokens=_Tokens(),
    )
    result = await crm.ensure_contact_and_deal(
        phone_e164="+79001234567", client_name="Test"
    )
    assert result.outcome is TeyaCrmActionOutcome.MANUAL_REVIEW
    assert result.error_code == "AMOCRM_CRM_REST_DISABLED"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_booking_5xx_schedules_retry_not_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.teya_request_orchestrator.pending_repo.release_lease",
        release,
    )
    monkeypatch.setattr(
        "app.services.teya_request_orchestrator.breaker_repo.record_failure",
        AsyncMock(),
    )

    class _Remote:
        def get(self, *, request_id: object):
            raise BookingRequestHttpError("SERVICE_UNAVAILABLE")

        def appointments_lookup(self, **_kwargs: object):
            raise AssertionError("unused")

        def book(self, **_kwargs: object):
            raise AssertionError("unused")

    row = TeyaRequestPending()
    row.id = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    row.request_id = uuid.UUID(_REQUEST)
    row.state = TeyaRequestPendingState.DISCOVERED.value
    row.attempt_count = 1
    row.max_attempts = 8
    row.lease_token = _LEASE
    row.created_at = _NOW
    row.updated_at = _NOW
    orch = TeyaRequestOrchestratorService(
        MagicMock(),
        pending_service=TeyaRequestPendingService(
            MagicMock(), clock=lambda: _NOW
        ),
        remote=_Remote(),
        clock=lambda: _NOW,
        retry_policy=TeyaRetryPolicy(jitter_ratio=0.0),
    )
    result = await orch.process_claimed(row)
    assert result.outcome is TeyaRequestOrchestratorOutcome.RETRY_SCHEDULED
    assert result.result_code == "SERVICE_UNAVAILABLE"
    release.assert_awaited_once()
    next_retry = release.await_args.kwargs["next_retry_at"]
    assert next_retry == _NOW + timedelta(seconds=30)


@pytest.mark.asyncio
async def test_max_attempts_goes_manual_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mark = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.teya_request_orchestrator.pending_repo.mark_manual_review",
        mark,
    )

    class _Remote:
        def get(self, *, request_id: object):
            raise BookingRequestHttpError("TIMEOUT")

        def appointments_lookup(self, **_kwargs: object):
            raise AssertionError("unused")

        def book(self, **_kwargs: object):
            raise AssertionError("unused")

    row = TeyaRequestPending()
    row.id = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    row.request_id = uuid.UUID(_REQUEST)
    row.state = TeyaRequestPendingState.DISCOVERED.value
    row.attempt_count = 8
    row.max_attempts = 8
    row.lease_token = _LEASE
    row.created_at = _NOW
    row.updated_at = _NOW
    orch = TeyaRequestOrchestratorService(
        MagicMock(),
        pending_service=TeyaRequestPendingService(
            MagicMock(), clock=lambda: _NOW
        ),
        remote=_Remote(),
        clock=lambda: _NOW,
    )
    result = await orch.process_claimed(row)
    assert result.outcome is TeyaRequestOrchestratorOutcome.TERMINAL
    assert result.pending_state is TeyaRequestPendingState.MANUAL_REVIEW
    assert result.result_code == "MAX_ATTEMPTS_EXCEEDED"
    mark.assert_awaited_once()


def test_ops_snapshot_schema_has_no_pii_fields() -> None:
    fields = set(PendingOpsItem.model_fields)
    forbidden = {
        "phone",
        "client_name",
        "clientName",
        "clientPhone",
        "message",
        "body",
        "token",
        "secret",
    }
    assert fields.isdisjoint(forbidden)
    assert load_teya_ops_config({"TEYA_OPS_TOKEN": "short"}) is None
    assert load_teya_ops_config({"TEYA_OPS_TOKEN": "x" * 16}) is not None


def test_no_outbox_in_reliability_migration() -> None:
    text = (
        _REPO / "alembic/versions/20260825_33_teya_reliability.py"
    ).read_text(encoding="utf-8")
    assert "outbox_messages" not in text
    assert "teya_request_feed_cursors" in text
    assert "integration_circuit_breakers" in text


def test_zip_still_unrelated() -> None:
    sources = [
        (_REPO / "app/services/teya_request_orchestrator.py").read_text(
            encoding="utf-8"
        ),
        (_REPO / "app/teya_ops_router.py").read_text(encoding="utf-8"),
        (
            _REPO / "alembic/versions/20260825_33_teya_reliability.py"
        ).read_text(encoding="utf-8"),
    ]
    for src in sources:
        assert "bot-tv-staging-backup-restore.zip" not in src


@pytest.mark.asyncio
async def test_breaker_open_and_half_open_recovery() -> None:
    from app.repositories import integration_circuit_breakers as breaker_repo

    policy = CircuitBreakerPolicy(
        failure_threshold=2, cooldown_seconds=60.0, probe_lease_seconds=30.0
    )

    class _Row:
        key = "amocrm_business_writes"
        state = CircuitBreakerState.CLOSED.value
        failure_count = 0
        opened_at = None
        half_open_successes = 0
        updated_at = _NOW

    row = _Row()
    session = MagicMock()

    async def _ensure(*_a: object, **_k: object) -> _Row:
        return row

    original = breaker_repo._ensure
    breaker_repo._ensure = _ensure  # type: ignore[method-assign]
    try:
        await breaker_repo.record_failure(session, now=_NOW, policy=policy)
        assert row.failure_count == 1
        assert row.state == CircuitBreakerState.CLOSED.value
        await breaker_repo.record_failure(session, now=_NOW, policy=policy)
        assert row.state == CircuitBreakerState.OPEN.value
        row.state = CircuitBreakerState.HALF_OPEN.value
        row.opened_at = _NOW + timedelta(seconds=61)
        row.half_open_successes = 0
        later = _NOW + timedelta(seconds=61)
        await breaker_repo.record_success(session, now=later, policy=policy)
        assert row.state == CircuitBreakerState.CLOSED.value
        row.state = CircuitBreakerState.HALF_OPEN.value
        row.opened_at = later
        await breaker_repo.record_failure(session, now=later, policy=policy)
        assert row.state == CircuitBreakerState.OPEN.value
    finally:
        breaker_repo._ensure = original  # type: ignore[method-assign]


def test_oauth_not_found_is_manual_not_retry() -> None:
    from app.core.teya_request_retry import (
        MANUAL_REVIEW_CODES,
        RETRYABLE_CRM_ERROR_CODES,
    )

    assert "AMOCRM_CRM_OAUTH_NOT_FOUND" in MANUAL_REVIEW_CODES
    assert "AMOCRM_CRM_OAUTH_NOT_FOUND" not in RETRYABLE_CRM_ERROR_CODES
    assert not is_breaker_failure_code("AMOCRM_CRM_OAUTH_NOT_FOUND")
