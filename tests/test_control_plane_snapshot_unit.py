"""Unit proofs for control-plane publication contracts + HTTP + safety ownership."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import pytest

from app.config import BotMode, Settings
from app.core.booking_eligibility_http import BookingEligibilityHttpConfig
from app.core.control_plane_http import (
    ControlPlaneFetchCode,
    ControlPlaneHttpClient,
)
from app.core.control_plane_remote import (
    BOT_KNOWLEDGE_NOT_PUBLISHED_CODE,
    BOT_KNOWLEDGE_PUBLICATION_INVALID_CODE,
    BOT_SETTINGS_NOT_PUBLISHED_CODE,
    BOT_SETTINGS_PUBLICATION_INVALID_CODE,
    KNOWLEDGE_ROUTE_PATH,
    SETTINGS_ROUTE_PATH,
)
from app.core.control_plane_types import (
    ControlPlaneParseError,
    KnowledgeCategory,
    parse_knowledge_publication_v1,
    parse_settings_publication_v1,
)
from app.core.mode_contract import is_live_booking_s2s_read_allowed
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransportError,
)
from app.models.worker_heartbeat import (
    CONTROL_PLANE_SNAPSHOT_LOOP,
    REQUIRED_WORKER_LOOPS,
)
from app.services.control_plane_snapshot_service import ControlPlaneSnapshotService
from app.services.worker_runtime import build_default_loop_specs

_CHECKSUM_A = "a" * 64
_CHECKSUM_B = "b" * 64
_PUB_ID_V3 = "11111111-1111-4111-8111-111111111111"
_PUB_ID_V1 = "22222222-2222-4222-8222-222222222222"
_TOKEN = "t" * 32


def _settings_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schemaVersion": 1,
        "desiredAdminState": {
            "isEnabled": True,
            "mode": "AUTO",
            "responseMode": "AUTO",
        },
        "provider": "NONE",
        "channels": {
            "siteWidget": False,
            "vk": False,
            "max": False,
            "telegram": False,
            "whatsapp": False,
        },
        "contentPolicy": {
            "mainInstruction": "instruction",
            "knowledgeBaseNote": None,
            "handoffRules": None,
            "taggingRules": None,
            "safetyRules": None,
        },
        "limits": {
            "maxMessagesPerClient": 20,
            "maxDailyMessages": 200,
            "logRetentionDays": 30,
            "errorLogRetentionDays": 90,
            "maxStoredBotEvents": 5000,
        },
        "operationalSafety": {
            "emergencyLockOwnedByBotCoreEnv": True,
            "effectiveRuntimeModeOwnedByBotCoreEnv": True,
        },
    }
    base.update(overrides)
    return base


def _settings_envelope(
    *,
    publication_id: str = _PUB_ID_V3,
    version: int = 3,
    checksum: str = _CHECKSUM_A,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "schemaVersion": 1,
        "publicationId": publication_id,
        "version": version,
        "checksum": checksum,
        "publishedAt": "2026-08-01T12:00:00.000Z",
        "sourceUpdatedAt": "2026-08-01T11:00:00.000Z",
        "settings": settings if settings is not None else _settings_payload(),
    }


def _knowledge_envelope(
    *,
    publication_id: str = _PUB_ID_V3,
    version: int = 3,
    checksum: str = _CHECKSUM_A,
    entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if entries is None:
        entries = [
            {
                "key": "faq-general",
                "category": "FAQ",
                "title": "Общий вопрос",
                "content": "Ответ без цен.",
                "tags": ["general"],
                "serviceId": None,
            }
        ]
    return {
        "ok": True,
        "schemaVersion": 1,
        "knowledgePublicationId": publication_id,
        "version": version,
        "checksum": checksum,
        "publishedAt": "2026-08-01T12:00:00.000Z",
        "entries": entries,
    }


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[S2sHttpRequest] = []
        self.by_path: dict[str, S2sHttpResponse | Exception] = {}

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        path = req.url.split("://", 1)[-1]
        path = "/" + path.split("/", 1)[-1] if "/" in path else ""
        # Match by route suffix.
        for route, response in self.by_path.items():
            if req.url.endswith(route):
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"unexpected url={req.url!r}")


def _json_response(status: int, body: object) -> S2sHttpResponse:
    return S2sHttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        body=json.dumps(body).encode("utf-8"),
    )


def _client(transport: _FakeTransport) -> ControlPlaneHttpClient:
    config = BookingEligibilityHttpConfig(
        base_url="https://example.test",
        bearer_token=_TOKEN,
    )
    return ControlPlaneHttpClient(config, transport)


def test_strict_valid_settings_parse() -> None:
    pub = parse_settings_publication_v1(_settings_envelope())
    assert pub.version == 3
    assert pub.publication_id == _PUB_ID_V3
    assert pub.checksum == _CHECKSUM_A
    assert pub.settings.desired_admin_state.mode == "AUTO"
    assert pub.settings.operational_safety.emergency_lock_owned_by_bot_core_env


def test_strict_valid_knowledge_parse() -> None:
    pub = parse_knowledge_publication_v1(_knowledge_envelope())
    assert pub.version == 3
    assert len(pub.entries) == 1
    assert pub.entries[0].category is KnowledgeCategory.FAQ


def test_unknown_fields_reject() -> None:
    bad = _settings_envelope()
    bad["extra"] = True
    with pytest.raises(ControlPlaneParseError):
        parse_settings_publication_v1(bad)
    bad_k = _knowledge_envelope()
    bad_k["entries"][0]["extra"] = "x"
    with pytest.raises(ControlPlaneParseError):
        parse_knowledge_publication_v1(bad_k)


def test_invalid_categories_reject() -> None:
    env = _knowledge_envelope(
        entries=[
            {
                "key": "faq-general",
                "category": "PRICING",
                "title": "t",
                "content": "c",
                "tags": [],
                "serviceId": None,
            }
        ]
    )
    with pytest.raises(ControlPlaneParseError):
        parse_knowledge_publication_v1(env)


def test_malformed_timestamps_and_checksums_reject() -> None:
    bad_ts = _settings_envelope()
    bad_ts["publishedAt"] = "not-a-timestamp"
    with pytest.raises(ControlPlaneParseError):
        parse_settings_publication_v1(bad_ts)
    naive = _settings_envelope()
    naive["publishedAt"] = "2026-08-01T12:00:00"
    with pytest.raises(ControlPlaneParseError):
        parse_settings_publication_v1(naive)
    bad_sum = _settings_envelope(checksum="zzz")
    with pytest.raises(ControlPlaneParseError):
        parse_settings_publication_v1(bad_sum)
    bad_ver = _settings_envelope(version=0)
    with pytest.raises(ControlPlaneParseError):
        parse_settings_publication_v1(bad_ver)


def test_http_404_settings_fail_closed() -> None:
    transport = _FakeTransport()
    transport.by_path[SETTINGS_ROUTE_PATH] = _json_response(
        404,
        {"ok": False, "code": BOT_SETTINGS_NOT_PUBLISHED_CODE, "error": "x"},
    )
    result = _client(transport).fetch_settings()
    assert result.code is ControlPlaneFetchCode.NOT_PUBLISHED
    assert result.publication is None


def test_http_404_knowledge_fail_closed() -> None:
    transport = _FakeTransport()
    transport.by_path[KNOWLEDGE_ROUTE_PATH] = _json_response(
        404,
        {"ok": False, "code": BOT_KNOWLEDGE_NOT_PUBLISHED_CODE, "error": "x"},
    )
    result = _client(transport).fetch_knowledge()
    assert result.code is ControlPlaneFetchCode.NOT_PUBLISHED


def test_http_409_invalid_fail_closed() -> None:
    transport = _FakeTransport()
    transport.by_path[SETTINGS_ROUTE_PATH] = _json_response(
        409,
        {
            "ok": False,
            "code": BOT_SETTINGS_PUBLICATION_INVALID_CODE,
            "error": "x",
        },
    )
    transport.by_path[KNOWLEDGE_ROUTE_PATH] = _json_response(
        409,
        {
            "ok": False,
            "code": BOT_KNOWLEDGE_PUBLICATION_INVALID_CODE,
            "error": "x",
        },
    )
    client = _client(transport)
    assert client.fetch_settings().code is ControlPlaneFetchCode.INVALID
    assert client.fetch_knowledge().code is ControlPlaneFetchCode.INVALID


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_fail_closed(status: int) -> None:
    transport = _FakeTransport()
    transport.by_path[SETTINGS_ROUTE_PATH] = _json_response(
        status, {"ok": False, "code": "UNAUTHORIZED", "error": "x"}
    )
    assert (
        _client(transport).fetch_settings().code
        is ControlPlaneFetchCode.AUTH_ERROR
    )


def test_http_5xx_unavailable() -> None:
    transport = _FakeTransport()
    transport.by_path[SETTINGS_ROUTE_PATH] = _json_response(
        503, {"ok": False, "code": "INTERNAL_ERROR", "error": "x"}
    )
    assert (
        _client(transport).fetch_settings().code
        is ControlPlaneFetchCode.UNAVAILABLE
    )


def test_http_timeout_unavailable() -> None:
    transport = _FakeTransport()
    transport.by_path[SETTINGS_ROUTE_PATH] = S2sHttpTransportError("TIMEOUT")
    assert (
        _client(transport).fetch_settings().code
        is ControlPlaneFetchCode.UNAVAILABLE
    )


def test_no_bearer_token_in_logs_or_errors(caplog: pytest.LogCaptureFixture) -> None:
    transport = _FakeTransport()
    transport.by_path[SETTINGS_ROUTE_PATH] = _json_response(
        401, {"ok": False, "code": "UNAUTHORIZED", "error": "x"}
    )
    with caplog.at_level(logging.INFO):
        result = _client(transport).fetch_settings()
    assert result.code is ControlPlaneFetchCode.AUTH_ERROR
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert _TOKEN not in joined
    assert "Bearer" not in joined
    err = ControlPlaneHttpClient(
        BookingEligibilityHttpConfig(
            base_url="https://example.test", bearer_token=_TOKEN
        ),
        transport,
    )
    assert _TOKEN not in repr(err)
    assert _TOKEN not in str(result)


def test_rollback_lower_version_identity_accepted() -> None:
    older = parse_settings_publication_v1(
        _settings_envelope(
            publication_id=_PUB_ID_V1, version=1, checksum=_CHECKSUM_B
        )
    )
    newer = parse_settings_publication_v1(
        _settings_envelope(
            publication_id=_PUB_ID_V3, version=3, checksum=_CHECKSUM_A
        )
    )
    assert older.identity != newer.identity
    assert older.version < newer.version
    # Identity is publicationId+checksum; lower version is still a valid parse.
    assert older.publication_id == _PUB_ID_V1
    assert older.checksum == _CHECKSUM_B


def test_http_bare_404_never_unavailable() -> None:
    transport = _FakeTransport()
    transport.by_path[SETTINGS_ROUTE_PATH] = _json_response(404, {"ok": False})
    assert (
        _client(transport).fetch_settings().code
        is ControlPlaneFetchCode.NOT_PUBLISHED
    )


def test_http_bare_409_never_unavailable() -> None:
    transport = _FakeTransport()
    transport.by_path[KNOWLEDGE_ROUTE_PATH] = _json_response(409, {"error": "x"})
    assert (
        _client(transport).fetch_knowledge().code
        is ControlPlaneFetchCode.INVALID
    )


def test_http_mismatched_envelope_404_still_not_published() -> None:
    transport = _FakeTransport()
    transport.by_path[SETTINGS_ROUTE_PATH] = _json_response(
        404, {"ok": False, "code": "SOMETHING_ELSE", "error": "x"}
    )
    assert (
        _client(transport).fetch_settings().code
        is ControlPlaneFetchCode.NOT_PUBLISHED
    )


def test_desired_admin_state_cannot_change_bot_mode() -> None:
    import ast
    from pathlib import Path

    settings = Settings(bot_mode=BotMode.OFF, emergency_lock=True)
    pub = parse_settings_publication_v1(_settings_envelope())
    assert pub.settings.desired_admin_state.is_enabled is True
    assert pub.settings.desired_admin_state.mode == "AUTO"
    assert settings.bot_mode is BotMode.OFF
    assert settings.emergency_lock is True
    assert is_live_booking_s2s_read_allowed(settings) is False

    # AST proof: control-plane consumer modules never assign bot_mode /
    # emergency_lock and never call Settings(...) construction.
    for rel in (
        "app/services/control_plane_snapshot_service.py",
        "app/services/control_plane_snapshot_worker.py",
        "app/core/control_plane_http.py",
    ):
        tree = ast.parse(Path(rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {
                "bot_mode",
                "emergency_lock",
            }:
                raise AssertionError(f"{rel} references {node.attr}")
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "Settings":
                    raise AssertionError(f"{rel} constructs Settings")
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "from_env"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "Settings"
                ):
                    raise AssertionError(f"{rel} calls Settings.from_env")


def test_invalid_settings_enums_and_safety_reject() -> None:
    bad_mode = _settings_envelope(
        settings=_settings_payload()
    )
    bad_mode["settings"]["desiredAdminState"]["mode"] = "AUTO_WRITE"
    with pytest.raises(ControlPlaneParseError):
        parse_settings_publication_v1(bad_mode)
    bad_provider = _settings_envelope()
    bad_provider["settings"]["provider"] = "ANTHROPIC"
    with pytest.raises(ControlPlaneParseError):
        parse_settings_publication_v1(bad_provider)
    bad_safety = _settings_envelope()
    bad_safety["settings"]["operationalSafety"][
        "emergencyLockOwnedByBotCoreEnv"
    ] = False
    with pytest.raises(ControlPlaneParseError):
        parse_settings_publication_v1(bad_safety)

def test_emergency_lock_ownership_unchanged() -> None:
    pub = parse_settings_publication_v1(_settings_envelope())
    assert (
        pub.settings.operational_safety.emergency_lock_owned_by_bot_core_env
        is True
    )
    assert (
        pub.settings.operational_safety.effective_runtime_mode_owned_by_bot_core_env
        is True
    )
    locked = Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=True)
    assert is_live_booking_s2s_read_allowed(locked) is False


def test_control_plane_loop_registered_in_required_loops() -> None:
    assert CONTROL_PLANE_SNAPSHOT_LOOP in REQUIRED_WORKER_LOOPS
    assert CONTROL_PLANE_SNAPSHOT_LOOP == "control_plane_snapshot"


def test_build_default_loop_specs_includes_control_plane() -> None:
    from unittest.mock import AsyncMock

    settings = Settings(
        database_url=(
            "postgresql+asyncpg://u:p@127.0.0.1:5432/bot_tv_foundation_test"
        ),
        bot_mode=BotMode.OFF,
        emergency_lock=True,
        control_plane_refresh_seconds=30,
        control_plane_max_stale_seconds=300,
    )
    specs = build_default_loop_specs(
        settings=settings,
        session_factory=AsyncMock(),
        worker_id="unit-worker",
    )
    assert tuple(spec.name for spec in specs) == REQUIRED_WORKER_LOOPS
    cp = next(s for s in specs if s.name == CONTROL_PLANE_SNAPSHOT_LOOP)
    assert cp.poll_seconds == 30


def test_service_rejects_out_of_bounds_stale() -> None:
    with pytest.raises(ValueError):
        ControlPlaneSnapshotService(
            session_factory=object(),  # type: ignore[arg-type]
            remote=None,
            max_stale_seconds=10,
        )
