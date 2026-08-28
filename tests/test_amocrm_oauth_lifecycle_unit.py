from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.amocrm_crm_rest_http import (
    AmoCrmCrmRestOutcome,
    AmoCrmCrmTokenRefreshResult,
)
from app.repositories import amocrm_crm_oauth_tokens as oauth_repo
from app.services.amocrm_crm_oauth_lifecycle_worker import (
    AmoCrmCrmOauthLifecycleError,
    AmoCrmCrmOauthLifecycleWorker,
    PROACTIVE_REFRESH_WINDOW,
)

_NOW = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)
_CONFIG = AmoCrmCrmRestConfig(
    enabled=True,
    client_id="client-id",
    client_secret="client-secret",
    api_base_url="https://example.amocrm.ru",
    redirect_uri="https://example.com/oauth",
    connection_scope="default",
)


class _FakeOauth:
    def __init__(self, result: AmoCrmCrmTokenRefreshResult) -> None:
        self.result = result
        self.calls: list[datetime | None] = []

    async def refresh_tokens(
        self,
        *,
        if_expires_at_lte: datetime | None = None,
        if_still_access_token: str | None = None,
    ) -> AmoCrmCrmTokenRefreshResult:
        del if_still_access_token
        self.calls.append(if_expires_at_lte)
        return self.result


def _success() -> AmoCrmCrmTokenRefreshResult:
    return AmoCrmCrmTokenRefreshResult(AmoCrmCrmRestOutcome.SUCCESS)


def _worker(
    oauth: _FakeOauth,
    *,
    enabled: bool = True,
    now: Callable[[], datetime] | None = None,
) -> AmoCrmCrmOauthLifecycleWorker:
    config = _CONFIG if enabled else AmoCrmCrmRestConfig(enabled=False)
    return AmoCrmCrmOauthLifecycleWorker(
        object(),  # type: ignore[arg-type]
        worker_id="unit-oauth",
        config=config,
        oauth=oauth,  # type: ignore[arg-type]
        now=now if now is not None else (lambda: _NOW),
    )


def _mock_expiry(
    monkeypatch: pytest.MonkeyPatch,
    access_expires_at: datetime | None,
) -> AsyncMock:
    @asynccontextmanager
    async def scope(_factory):  # type: ignore[no-untyped-def]
        yield object()

    get_by_scope = AsyncMock(
        return_value=SimpleNamespace(
            access_expires_at=access_expires_at,
            lease_until=None,
            lease_owner=None,
        )
    )
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.session_scope",
        scope,
    )
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.oauth_repo.get_by_scope",
        get_by_scope,
    )
    return get_by_scope


@pytest.mark.asyncio
async def test_fresh_token_delegates_cutoff_to_guarded_refresh() -> None:
    oauth = _FakeOauth(_success())

    await _worker(oauth).tick()

    assert oauth.calls == [_NOW + PROACTIVE_REFRESH_WINDOW]


@pytest.mark.asyncio
async def test_token_at_fifteen_minutes_refreshes_once() -> None:
    oauth = _FakeOauth(_success())

    await _worker(oauth).tick()

    assert oauth.calls == [_NOW + PROACTIVE_REFRESH_WINDOW]


@pytest.mark.asyncio
async def test_expired_token_refreshes() -> None:
    oauth = _FakeOauth(_success())

    await _worker(oauth).tick()

    assert oauth.calls == [_NOW + PROACTIVE_REFRESH_WINDOW]


@pytest.mark.asyncio
async def test_unknown_expiry_refreshes_once_to_establish_deadline() -> None:
    oauth = _FakeOauth(_success())

    await _worker(oauth).tick()

    assert len(oauth.calls) == 1


@pytest.mark.asyncio
async def test_rest_disabled_is_safe_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_by_scope = AsyncMock()
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.oauth_repo.get_by_scope",
        get_by_scope,
    )
    oauth = _FakeOauth(_success())

    await _worker(oauth, enabled=False).tick()

    get_by_scope.assert_not_awaited()
    assert oauth.calls == []


@pytest.mark.asyncio
async def test_invalid_enabled_config_is_heartbeat_failure_not_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AMOCRM_CRM_REST_ENABLED", "true")
    monkeypatch.delenv("AMOCRM_CLIENT_ID", raising=False)
    monkeypatch.delenv("AMOCRM_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("AMOCRM_CRM_REDIRECT_URI", raising=False)
    oauth = _FakeOauth(_success())
    worker = AmoCrmCrmOauthLifecycleWorker(
        object(),  # type: ignore[arg-type]
        worker_id="unit-oauth",
        oauth=oauth,  # type: ignore[arg-type]
        now=lambda: _NOW,
    )

    with pytest.raises(AmoCrmCrmOauthLifecycleError) as caught:
        await worker.tick()

    assert caught.value.code == "AMOCRM_CRM_CLIENT_ID_REQUIRED"
    assert oauth.calls == []


@pytest.mark.asyncio
async def test_refresh_failure_is_safe_and_diagnosable() -> None:
    secret = "refresh-token-must-not-leak"
    oauth = _FakeOauth(
        AmoCrmCrmTokenRefreshResult(
            AmoCrmCrmRestOutcome.PERMANENT_ERROR,
            error_code="AMOCRM_CRM_HTTP_401",
        )
    )

    with pytest.raises(AmoCrmCrmOauthLifecycleError) as caught:
        await _worker(oauth).tick()

    assert caught.value.code == "AMOCRM_CRM_HTTP_401"
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


@pytest.mark.asyncio
async def test_contention_lease_cleared_but_still_due_fails_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_expiry(monkeypatch, _NOW)
    oauth = _FakeOauth(
        AmoCrmCrmTokenRefreshResult(
            AmoCrmCrmRestOutcome.TRANSIENT_ERROR,
            error_code="AMOCRM_CRM_OAUTH_STALE_LEASE",
        )
    )

    with pytest.raises(AmoCrmCrmOauthLifecycleError) as caught:
        await _worker(oauth).tick()

    assert caught.value.code == "AMOCRM_CRM_OAUTH_REFRESH_CONTENTION_UNRESOLVED"
    assert len(oauth.calls) == 1


@pytest.mark.asyncio
async def test_contention_waits_for_in_flight_peer_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    due = _NOW
    fresh = _NOW + PROACTIVE_REFRESH_WINDOW + timedelta(minutes=5)
    lease_until = _NOW + timedelta(seconds=30)

    @asynccontextmanager
    async def scope(_factory):  # type: ignore[no-untyped-def]
        yield object()

    in_flight = SimpleNamespace(
        access_expires_at=due,
        lease_until=lease_until,
        lease_owner="peer",
    )
    resolved = SimpleNamespace(
        access_expires_at=fresh,
        lease_until=None,
        lease_owner=None,
    )
    get_by_scope = AsyncMock(side_effect=[in_flight, resolved])
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.session_scope",
        scope,
    )
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.oauth_repo.get_by_scope",
        get_by_scope,
    )
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.asyncio.sleep",
        AsyncMock(),
    )
    oauth = _FakeOauth(
        AmoCrmCrmTokenRefreshResult(
            AmoCrmCrmRestOutcome.TRANSIENT_ERROR,
            error_code="AMOCRM_CRM_OAUTH_STALE_LEASE",
        )
    )

    await _worker(oauth).tick()

    assert len(oauth.calls) == 1
    assert get_by_scope.await_count == 2


@pytest.mark.asyncio
async def test_contention_waits_beyond_legacy_short_poll_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Peer stays in-flight longer than the old 25×0.05s budget, then succeeds."""
    due = _NOW
    fresh = _NOW + PROACTIVE_REFRESH_WINDOW + timedelta(minutes=5)
    lease_until = _NOW + timedelta(seconds=5)
    clock = _NOW

    def now() -> datetime:
        return clock

    async def _advance_sleep(seconds: float) -> None:
        nonlocal clock
        clock = clock + timedelta(seconds=seconds)

    @asynccontextmanager
    async def scope(_factory):  # type: ignore[no-untyped-def]
        yield object()

    in_flight = SimpleNamespace(
        access_expires_at=due,
        lease_until=lease_until,
        lease_owner="peer",
    )
    resolved = SimpleNamespace(
        access_expires_at=fresh,
        lease_until=None,
        lease_owner=None,
    )

    def _snapshot(*_a, **_k) -> SimpleNamespace:
        if clock > _NOW + timedelta(seconds=1.25):
            return resolved
        return in_flight

    get_by_scope = AsyncMock(side_effect=_snapshot)
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.session_scope",
        scope,
    )
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.oauth_repo.get_by_scope",
        get_by_scope,
    )
    sleep_mock = AsyncMock(side_effect=_advance_sleep)
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.asyncio.sleep",
        sleep_mock,
    )
    oauth = _FakeOauth(
        AmoCrmCrmTokenRefreshResult(
            AmoCrmCrmRestOutcome.TRANSIENT_ERROR,
            error_code="AMOCRM_CRM_OAUTH_STALE_LEASE",
        )
    )

    await _worker(oauth, now=now).tick()

    assert len(oauth.calls) == 1
    assert sleep_mock.await_count >= 1
    assert clock > _NOW + timedelta(seconds=1.25)
    assert get_by_scope.await_count >= 3


@pytest.mark.asyncio
async def test_expired_lease_uses_current_time_not_tick_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    due = _NOW
    lease_until = _NOW + timedelta(seconds=1)
    clock = _NOW

    def now() -> datetime:
        return clock

    async def _advance_sleep(seconds: float) -> None:
        nonlocal clock
        clock = clock + timedelta(seconds=seconds)

    @asynccontextmanager
    async def scope(_factory):  # type: ignore[no-untyped-def]
        yield object()

    active = SimpleNamespace(
        access_expires_at=due,
        lease_until=lease_until,
        lease_owner="peer",
    )

    def _snapshot(*_a, **_k) -> SimpleNamespace:
        if clock >= lease_until:
            return SimpleNamespace(
                access_expires_at=due,
                lease_until=None,
                lease_owner=None,
            )
        return active

    get_by_scope = AsyncMock(side_effect=_snapshot)
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.session_scope",
        scope,
    )
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.oauth_repo.get_by_scope",
        get_by_scope,
    )
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.asyncio.sleep",
        AsyncMock(side_effect=_advance_sleep),
    )
    oauth = _FakeOauth(
        AmoCrmCrmTokenRefreshResult(
            AmoCrmCrmRestOutcome.TRANSIENT_ERROR,
            error_code="AMOCRM_CRM_OAUTH_STALE_LEASE",
        )
    )

    with pytest.raises(AmoCrmCrmOauthLifecycleError) as caught:
        await _worker(oauth, now=now).tick()

    assert caught.value.code == "AMOCRM_CRM_OAUTH_REFRESH_CONTENTION_UNRESOLVED"
    assert len(oauth.calls) == 1
    assert clock >= lease_until
    assert get_by_scope.await_count >= 2


@pytest.mark.asyncio
async def test_contention_deadline_exhausted_while_lease_active_fails_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    due = _NOW
    lease_until = _NOW + timedelta(hours=1)
    clock = _NOW
    wait_budget = 2

    def now() -> datetime:
        return clock

    async def _advance_sleep(seconds: float) -> None:
        nonlocal clock
        clock = clock + timedelta(seconds=seconds)

    monkeypatch.setattr(oauth_repo, "PRE_HTTP_REFRESH_LEASE_SECONDS", wait_budget)

    @asynccontextmanager
    async def scope(_factory):  # type: ignore[no-untyped-def]
        yield object()

    stuck = SimpleNamespace(
        access_expires_at=due,
        lease_until=lease_until,
        lease_owner="peer",
    )
    get_by_scope = AsyncMock(return_value=stuck)
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.session_scope",
        scope,
    )
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.oauth_repo.get_by_scope",
        get_by_scope,
    )
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.asyncio.sleep",
        AsyncMock(side_effect=_advance_sleep),
    )
    oauth = _FakeOauth(
        AmoCrmCrmTokenRefreshResult(
            AmoCrmCrmRestOutcome.TRANSIENT_ERROR,
            error_code="AMOCRM_CRM_OAUTH_STALE_LEASE",
        )
    )

    with pytest.raises(AmoCrmCrmOauthLifecycleError) as caught:
        await _worker(oauth, now=now).tick()

    assert caught.value.code == "AMOCRM_CRM_OAUTH_REFRESH_CONTENTION_UNRESOLVED"
    assert len(oauth.calls) == 1
    assert clock >= _NOW + timedelta(seconds=wait_budget)
    assert get_by_scope.await_count >= 1


@pytest.mark.asyncio
async def test_contention_resolved_by_peer_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _NOW + PROACTIVE_REFRESH_WINDOW + timedelta(minutes=5)

    @asynccontextmanager
    async def scope(_factory):  # type: ignore[no-untyped-def]
        yield object()

    get_by_scope = AsyncMock(
        return_value=SimpleNamespace(
            access_expires_at=fresh,
            lease_until=None,
            lease_owner=None,
        )
    )
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.session_scope",
        scope,
    )
    monkeypatch.setattr(
        "app.services.amocrm_crm_oauth_lifecycle_worker.oauth_repo.get_by_scope",
        get_by_scope,
    )
    oauth = _FakeOauth(
        AmoCrmCrmTokenRefreshResult(
            AmoCrmCrmRestOutcome.TRANSIENT_ERROR,
            error_code="AMOCRM_CRM_OAUTH_STALE_LEASE",
        )
    )

    await _worker(oauth).tick()

    assert len(oauth.calls) == 1
    get_by_scope.assert_awaited_once()
