"""A2.3b2 acquisition-source analytics unit proofs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from app.core.acquisition_source_http import (
    AcquisitionSourceHttpClient,
    AcquisitionSourceHttpError,
)
from app.core.acquisition_source_remote import (
    parse_acquisition_source_context_payload,
    parse_acquisition_source_feed_item,
    parse_acquisition_source_feed_payload,
)
from app.core.acquisition_source_types import (
    ACQUISITION_SOURCE_ANALYTICS_LOOP,
    ACQUISITION_SOURCE_KEY_TO_ENUM_ID,
    FEED_CURSOR_ID,
    TERMINAL_ACQUISITION_SOURCE_STATES,
    AcquisitionSourceOwnerKind,
    AcquisitionSourcePendingState,
    enum_id_for_source_key,
)
from app.core.amocrm_analytics_fields import AmoCrmAnalyticsSourcePrimaryEnum
from app.core.booking_eligibility_http import BookingEligibilityHttpConfig
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.models.worker_heartbeat import REQUIRED_WORKER_LOOPS

_TOKEN = "a" * 32
_EID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_OID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@dataclass
class _FakeTransport:
    responses: list[S2sHttpResponse | BaseException]
    calls: list[S2sHttpRequest] = field(default_factory=list)

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        if not self.responses:
            raise AssertionError("no more responses")
        nxt = self.responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def _config() -> BookingEligibilityHttpConfig:
    return BookingEligibilityHttpConfig(
        base_url="https://online.example",
        bearer_token=_TOKEN,
        timeout_seconds=5.0,
        max_response_bytes=65536,
    )


def _json_response(status: int, body: dict | None) -> S2sHttpResponse:
    import json

    raw = b""
    if body is not None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return S2sHttpResponse(
        status_code=status,
        headers={"content-type": "application/json; charset=utf-8"},
        body=raw,
    )


def test_exact_four_source_mappings() -> None:
    assert ACQUISITION_SOURCE_KEY_TO_ENUM_ID == {
        "VK_ADS": int(AmoCrmAnalyticsSourcePrimaryEnum.VK_ADS),
        "VK_CONTENT": int(AmoCrmAnalyticsSourcePrimaryEnum.VK_CONTENT),
        "YANDEX": int(AmoCrmAnalyticsSourcePrimaryEnum.YANDEX),
        "TWO_GIS": int(AmoCrmAnalyticsSourcePrimaryEnum.TWO_GIS),
    }
    assert enum_id_for_source_key("VK_ADS") == 723323
    assert enum_id_for_source_key("YANDEX") == 730677


def test_undefined_unreachable() -> None:
    with pytest.raises(ValueError):
        enum_id_for_source_key("UNDEFINED")
    with pytest.raises(ValueError):
        enum_id_for_source_key("SITE")
    with pytest.raises(ValueError):
        parse_acquisition_source_feed_item(
            {
                "evidenceId": _EID,
                "ownerKind": "APPOINTMENT",
                "ownerId": _OID,
                "sourceKey": "UNDEFINED",
            "consumedAt": "2026-08-28T10:00:00.000Z",
            "feedOrder": "1",
            }
        )


def test_feed_cursor_isolated_from_booking_method() -> None:
    assert FEED_CURSOR_ID == "acquisition_source"
    assert FEED_CURSOR_ID != "booking_method"


def test_worker_loop_registered() -> None:
    assert ACQUISITION_SOURCE_ANALYTICS_LOOP in REQUIRED_WORKER_LOOPS
    assert ACQUISITION_SOURCE_ANALYTICS_LOOP == "acquisition_source_analytics"


def test_parse_feed_and_context() -> None:
    item = parse_acquisition_source_feed_item(
        {
            "evidenceId": _EID,
            "ownerKind": "BOOKING_REQUEST",
            "ownerId": _OID,
            "sourceKey": "TWO_GIS",
            "consumedAt": "2026-08-28T10:00:00.000Z",
            "feedOrder": "1",
        }
    )
    assert item.evidence_id == _EID
    assert item.owner_kind is AcquisitionSourceOwnerKind.BOOKING_REQUEST
    page = parse_acquisition_source_feed_payload(
        {
            "ok": True,
            "items": [
                {
                    "evidenceId": _EID,
                    "ownerKind": "BOOKING_REQUEST",
                    "ownerId": _OID,
                    "sourceKey": "TWO_GIS",
                    "consumedAt": "2026-08-28T10:00:00.000Z",
                    "feedOrder": "2",
                }
            ],
            "nextCursor": None,
        }
    )
    assert len(page.items) == 1
    ctx = parse_acquisition_source_context_payload(
        {
            "ok": True,
            "evidenceId": _EID,
            "ownerKind": "APPOINTMENT",
            "ownerId": _OID,
            "sourceKey": "VK_CONTENT",
            "phoneE164": "+79001234567",
        }
    )
    assert "+79001234567" not in repr(ctx)
    assert ctx.source_key == "VK_CONTENT"


def test_context_not_found_permanent() -> None:
    transport = _FakeTransport(
        [_json_response(404, {"ok": False, "code": "NOT_FOUND", "error": "Not found"})]
    )
    client = AcquisitionSourceHttpClient(_config(), transport)
    with pytest.raises(AcquisitionSourceHttpError) as exc:
        client.context(
            evidence_id=_EID,
            owner_kind="APPOINTMENT",
            owner_id=_OID,
        )
    assert exc.value.code == "NOT_FOUND"


def test_feed_404_unavailable() -> None:
    transport = _FakeTransport([_json_response(404, {"ok": False, "code": "NOT_FOUND"})])
    client = AcquisitionSourceHttpClient(_config(), transport)
    with pytest.raises(AcquisitionSourceHttpError) as exc:
        client.feed()
    assert exc.value.code == "FEED_UNAVAILABLE"


def test_terminal_states() -> None:
    assert AcquisitionSourcePendingState.SKIPPED in TERMINAL_ACQUISITION_SOURCE_STATES
    assert AcquisitionSourcePendingState.DISCOVERED not in TERMINAL_ACQUISITION_SOURCE_STATES


def test_worker_runtime_includes_acquisition_source_loop() -> None:
    from unittest.mock import AsyncMock

    from app.config import Settings
    from app.services.worker_runtime import build_default_loop_specs

    settings = Settings(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:5432/bot_tv_foundation_test",
        worker_heartbeat_interval_seconds=1,
        worker_heartbeat_stale_seconds=45,
    )
    specs = build_default_loop_specs(
        settings=settings,
        session_factory=AsyncMock(),
        worker_id="unit-worker",
    )
    assert tuple(spec.name for spec in specs) == REQUIRED_WORKER_LOOPS
