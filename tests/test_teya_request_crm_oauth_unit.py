"""Teya CRM OAuth stale-401 wiring (guarded refresh_tokens path)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.core.amocrm_crm_rest_http import (
    AmoCrmCrmRestOutcome,
    AmoCrmCrmTokenRefreshResult,
)
from app.services.teya_request_crm_wiring import _OauthTokenAdapter


@pytest.mark.asyncio
async def test_wiring_adapter_forwards_rejected_access_token_to_guarded_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth = AsyncMock()
    oauth.refresh_tokens = AsyncMock(
        return_value=AmoCrmCrmTokenRefreshResult(
            outcome=AmoCrmCrmRestOutcome.SUCCESS,
        )
    )

    @asynccontextmanager
    async def _scope(_factory):  # type: ignore[no-untyped-def]
        yield AsyncMock()

    get_by_scope = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.services.teya_request_crm_wiring.session_scope",
        _scope,
    )
    monkeypatch.setattr(
        "app.services.teya_request_crm_wiring.oauth_repo.get_by_scope",
        get_by_scope,
    )
    monkeypatch.setattr(
        "app.services.teya_request_crm_wiring.oauth_repo.decrypt_row",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("unexpected decrypt")),
    )

    adapter = _OauthTokenAdapter(
        session_factory=object(),  # type: ignore[arg-type]
        connection_scope="default",
        key_provider=object(),  # type: ignore[arg-type]
        oauth=oauth,  # type: ignore[arg-type]
    )

    result = await adapter.refresh_access_token(rejected_access_token="token-A")

    oauth.refresh_tokens.assert_awaited_once_with(
        if_still_access_token="token-A",
    )
    assert result is None


@pytest.mark.asyncio
async def test_wiring_adapter_returns_current_access_after_guarded_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.repositories import amocrm_crm_oauth_tokens as oauth_repo

    oauth = AsyncMock()
    oauth.refresh_tokens = AsyncMock(
        return_value=AmoCrmCrmTokenRefreshResult(
            outcome=AmoCrmCrmRestOutcome.SUCCESS,
        )
    )

    @asynccontextmanager
    async def _scope(_factory):  # type: ignore[no-untyped-def]
        yield AsyncMock()

    monkeypatch.setattr(
        "app.services.teya_request_crm_wiring.session_scope",
        _scope,
    )
    monkeypatch.setattr(
        "app.services.teya_request_crm_wiring.oauth_repo.get_by_scope",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        "app.services.teya_request_crm_wiring.oauth_repo.decrypt_row",
        lambda *_a, **_k: oauth_repo.DecryptedOauthTokens(
            access_token="token-B",
            refresh_token="refresh-B",
            access_expires_at=None,
        ),
    )

    adapter = _OauthTokenAdapter(
        session_factory=object(),  # type: ignore[arg-type]
        connection_scope="default",
        key_provider=object(),  # type: ignore[arg-type]
        oauth=oauth,  # type: ignore[arg-type]
    )

    refreshed = await adapter.refresh_access_token(rejected_access_token="token-A")

    assert refreshed == "token-B"
    oauth.refresh_tokens.assert_awaited_once_with(
        if_still_access_token="token-A",
    )
