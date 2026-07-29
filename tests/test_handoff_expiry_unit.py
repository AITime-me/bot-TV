from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.services.handoff_expiry import HandoffExpiryWorker

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_handoff_runtime_settings_are_strict_and_bounded() -> None:
    settings = Settings.from_env({})
    assert settings.handoff_pause_seconds == 900
    assert settings.handoff_expiry_poll_seconds == 1

    custom = Settings.from_env(
        {
            "HANDOFF_PAUSE_SECONDS": "600",
            "HANDOFF_EXPIRY_POLL_SECONDS": "60",
        }
    )
    assert custom.handoff_pause_seconds == 600
    assert custom.handoff_expiry_poll_seconds == 60

    for value in ("599", "901", "15m", "900.0"):
        with pytest.raises(ValueError):
            Settings.from_env({"HANDOFF_PAUSE_SECONDS": value})
    for value in ("0", "61", "1s"):
        with pytest.raises(ValueError):
            Settings.from_env({"HANDOFF_EXPIRY_POLL_SECONDS": value})


def test_handoff_due_claim_uses_only_postgresql_clock() -> None:
    repository_source = (
        _REPO_ROOT / "app" / "repositories" / "conversations.py"
    ).read_text(encoding="utf-8")
    expiry_source = (
        _REPO_ROOT / "app" / "services" / "handoff_expiry.py"
    ).read_text(encoding="utf-8")
    claim_source = inspect.getsource(
        __import__(
            "app.repositories.conversations",
            fromlist=["claim_next_due_handoff"],
        ).claim_next_due_handoff
    )

    assert "statement_timestamp" in claim_source
    assert ":now" not in claim_source
    for source in (repository_source, expiry_source):
        assert "datetime.now(" not in source
        assert "utcnow(" not in source


@pytest.mark.asyncio
async def test_tick_is_bounded_and_stops_when_no_more_rows() -> None:
    worker = HandoffExpiryWorker(AsyncMock())
    first = AsyncMock()
    worker.expire_one = AsyncMock(  # type: ignore[method-assign]
        side_effect=[first, None, AssertionError("must not be called")]
    )

    results = await worker.tick(max_items=10)

    assert results == [first]
    assert worker.expire_one.await_count == 2  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        await worker.tick(max_items=0)
