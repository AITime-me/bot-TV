"""CP-04 contract hardening: stableKey, response size, poll cadence.

Regression for the production blocker where ACTIVE knowledge HTTP 200 with
``procedure.*`` / ``*_`` keys was rejected as RESPONSE_INVALID because bot-TV
accepted hyphen-only keys while online-zapis-tv allows ``.``, ``_``, ``-``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.config import BotMode, Settings
from app.core.booking_eligibility_http import (
    DEFAULT_MAX_RESPONSE_BYTES,
    BookingEligibilityHttpConfig,
    BookingEligibilityHttpError,
)
from app.core.control_plane_http import (
    ControlPlaneFetchCode,
    ControlPlaneHttpClient,
)
from app.core.control_plane_remote import (
    CONTROL_PLANE_S2S_GETS_PER_REFRESH,
    KNOWLEDGE_ROUTE_PATH,
)
from app.core.control_plane_types import (
    BOT_KNOWLEDGE_STABLE_KEY_RE,
    ControlPlaneParseError,
    KnowledgeCategory,
    parse_knowledge_publication_v1,
)
from app.core.s2s_http_stdlib import _MAX_RESPONSE_BYTES_CAP
from app.core.s2s_http_transport import S2sHttpResponse
from app.models.worker_heartbeat import (
    CONTROL_PLANE_SNAPSHOT_LOOP,
    INGRESS_LOOP,
    TEYA_REQUEST_ORCHESTRATOR_LOOP,
)
from app.services.worker_runtime import build_default_loop_specs

_CHECKSUM = "a" * 64
_PUB_ID = "47ccd84e-1464-4f37-aa45-3a1111111111"
_TOKEN = "t" * 32
# Measured production ACTIVE knowledge body size during CP-04 apply.
_PRODUCTION_KB_BODY_BYTES = 85_621
# online-zapis-tv botInternal policy (shared S2S bucket).
_OZ_BOT_INTERNAL_MAX_PER_MINUTE = 120


def _entry(
    key: str,
    *,
    category: str = "PROCEDURE_EXPLANATION",
    title: str = "Title",
    content: str = "Content without prices.",
) -> dict[str, Any]:
    return {
        "key": key,
        "category": category,
        "title": title,
        "content": content,
        "tags": ["celosom", "injections"],
        "serviceId": None,
    }


def _production_style_knowledge_envelope() -> dict[str, Any]:
    return {
        "ok": True,
        "schemaVersion": 1,
        "knowledgePublicationId": _PUB_ID,
        "version": 1,
        "checksum": _CHECKSUM,
        "publishedAt": "2026-08-30T10:45:08.999Z",
        "entries": [
            _entry("procedure.celosom"),
            _entry("procedure.pm_general"),
            _entry("faq-general", category="FAQ", title="FAQ", content="Answer."),
            _entry(
                "procedure.pm_laser_removal_brows",
                title="Laser brows",
                content="Procedure note.",
            ),
        ],
    }


def test_stable_key_regex_matches_online_zapis_tv_contract() -> None:
    # Exact upstream pattern from stable-key.ts
    assert BOT_KNOWLEDGE_STABLE_KEY_RE.pattern == (
        r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"
    )


@pytest.mark.parametrize(
    "key",
    [
        "procedure.celosom",
        "procedure.pm_general",
        "faq-general",
        "procedure.pm_laser_removal_brows",
        "a",
        "a.b_c-d",
    ],
)
def test_production_style_stable_keys_accepted(key: str) -> None:
    assert BOT_KNOWLEDGE_STABLE_KEY_RE.fullmatch(key)


@pytest.mark.parametrize(
    "key",
    [
        ".leading",
        "trailing.",
        "empty..seg",
        "-leading",
        "trailing-",
        "_leading",
        "trailing_",
        "has space",
        "UPPER",
        "slash/seg",
        "",
        "a..b",
        "a.-b",
    ],
)
def test_malformed_stable_keys_rejected(key: str) -> None:
    assert BOT_KNOWLEDGE_STABLE_KEY_RE.fullmatch(key) is None
    envelope = _production_style_knowledge_envelope()
    envelope["entries"] = [_entry(key or "x")]
    if key == "":
        envelope["entries"][0]["key"] = ""
    with pytest.raises(ControlPlaneParseError):
        parse_knowledge_publication_v1(envelope)


def test_production_style_knowledge_publication_parses() -> None:
    pub = parse_knowledge_publication_v1(_production_style_knowledge_envelope())
    assert pub.version == 1
    assert len(pub.entries) == 4
    keys = {entry.key for entry in pub.entries}
    assert "procedure.celosom" in keys
    assert "procedure.pm_general" in keys
    assert "faq-general" in keys
    assert pub.entries[0].category is KnowledgeCategory.PROCEDURE_EXPLANATION


def test_http_fetch_knowledge_ok_for_production_style_keys() -> None:
    body = _production_style_knowledge_envelope()
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    assert len(encoded) < DEFAULT_MAX_RESPONSE_BYTES

    class _Transport:
        def request(self, request):  # type: ignore[no-untyped-def]
            assert request.url.endswith(KNOWLEDGE_ROUTE_PATH)
            assert request.max_response_bytes == DEFAULT_MAX_RESPONSE_BYTES
            return S2sHttpResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                body=encoded,
            )

    client = ControlPlaneHttpClient(
        BookingEligibilityHttpConfig(
            base_url="https://example.test",
            bearer_token=_TOKEN,
        ),
        _Transport(),
    )
    result = client.fetch_knowledge()
    assert result.code is ControlPlaneFetchCode.OK
    assert result.publication is not None
    assert len(result.publication.entries) == 4


def test_tracked_max_response_bytes_fits_production_kb_with_hard_cap() -> None:
    assert DEFAULT_MAX_RESPONSE_BYTES == 262_144
    assert DEFAULT_MAX_RESPONSE_BYTES >= _PRODUCTION_KB_BODY_BYTES
    assert DEFAULT_MAX_RESPONSE_BYTES < _MAX_RESPONSE_BYTES_CAP
    assert _MAX_RESPONSE_BYTES_CAP == 1_000_000
    cfg = BookingEligibilityHttpConfig(
        base_url="https://example.test",
        bearer_token=_TOKEN,
    )
    assert cfg.max_response_bytes == 262_144
    with pytest.raises(BookingEligibilityHttpError):
        BookingEligibilityHttpConfig(
            base_url="https://example.test",
            bearer_token=_TOKEN,
            max_response_bytes=_MAX_RESPONSE_BYTES_CAP + 1,
        )


def test_control_plane_poll_independent_of_worker_poll() -> None:
    settings = Settings(
        database_url=(
            "postgresql+asyncpg://u:p@127.0.0.1:5432/bot_tv_foundation_test"
        ),
        bot_mode=BotMode.OFF,
        emergency_lock=True,
        worker_poll_seconds=1,
        control_plane_refresh_seconds=30,
        worker_max_consecutive_failures=3,
    )
    specs = build_default_loop_specs(
        settings=settings,
        session_factory=AsyncMock(),
        worker_id="unit-worker",
    )
    by_name = {spec.name: spec for spec in specs}
    assert by_name[CONTROL_PLANE_SNAPSHOT_LOOP].poll_seconds == 30
    assert by_name[CONTROL_PLANE_SNAPSHOT_LOOP].poll_seconds != (
        settings.worker_poll_seconds
    )
    assert by_name[INGRESS_LOOP].poll_seconds == 1
    assert by_name[TEYA_REQUEST_ORCHESTRATOR_LOOP].poll_seconds == 5


def test_control_plane_s2s_cadence_has_rate_limit_headroom() -> None:
    assert CONTROL_PLANE_S2S_GETS_PER_REFRESH == 2
    poll = Settings.from_env({}).control_plane_refresh_seconds
    assert poll == 30
    req_per_min = (60 / poll) * CONTROL_PLANE_S2S_GETS_PER_REFRESH
    assert req_per_min == 4.0
    # At least 4x headroom vs shared botInternal 120/min bucket for CP alone.
    assert req_per_min * 4 <= _OZ_BOT_INTERNAL_MAX_PER_MINUTE


def test_control_plane_poll_env_alias_and_mismatch() -> None:
    assert (
        Settings.from_env({"CONTROL_PLANE_POLL_SECONDS": "45"}).control_plane_refresh_seconds
        == 45
    )
    assert (
        Settings.from_env(
            {"CONTROL_PLANE_REFRESH_SECONDS": "40"}
        ).control_plane_refresh_seconds
        == 40
    )
    assert (
        Settings.from_env(
            {
                "CONTROL_PLANE_POLL_SECONDS": "30",
                "CONTROL_PLANE_REFRESH_SECONDS": "30",
            }
        ).control_plane_refresh_seconds
        == 30
    )
    with pytest.raises(ValueError, match="must be equal"):
        Settings.from_env(
            {
                "CONTROL_PLANE_POLL_SECONDS": "30",
                "CONTROL_PLANE_REFRESH_SECONDS": "45",
            }
        )


def test_bot_mode_and_emergency_lock_defaults_unchanged() -> None:
    settings = Settings.from_env({})
    assert settings.bot_mode is BotMode.OFF
    assert settings.emergency_lock is True
    assert settings.worker_poll_seconds == 1
    assert settings.worker_max_consecutive_failures == 3
