"""Unit tests for closed-test PII admission HTTP boundary (03I)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from app.schemas.closed_test import (
    ClosedTestPiiAdmissionAck,
    ClosedTestPiiAdmissionCreate,
)

_REPO = Path(__file__).resolve().parents[1]
_TOKEN = "c" * 32
_FAKE_DB = "postgresql+asyncpg://bot:pass@127.0.0.1:5432/bot_tv"
_PHONE = "+79001234567"
_NAME = "Test Client"
_PATH = "/internal/closed-test/pii-admissions"


def _enable_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_CLOSED_TEST_ENABLED", "true")
    monkeypatch.setenv("BOT_CLOSED_TEST_TOKEN", _TOKEN)


def _settings() -> Settings:
    return Settings.from_env(
        {
            "BOT_MODE": "OFF",
            "EMERGENCY_LOCK": "true",
            "DATABASE_URL": _FAKE_DB,
        }
    )


def _closed_test_paths(application) -> set[str]:
    paths: set[str] = set()

    def walk(routes: object) -> None:
        for route in routes:  # type: ignore[attr-defined]
            path = getattr(route, "path", None)
            if isinstance(path, str):
                paths.add(path)
            nested = getattr(route, "routes", None)
            if nested is not None:
                walk(nested)
            original = getattr(route, "original_router", None)
            if original is not None and getattr(original, "routes", None) is not None:
                walk(original.routes)

    walk(application.routes)
    return paths


def test_pii_admission_route_absent_when_disabled() -> None:
    application = create_app(Settings())
    assert _PATH not in _closed_test_paths(application)
    client = TestClient(application)
    assert client.post(_PATH, json={}).status_code == 404


def test_pii_admission_route_registered_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_env(monkeypatch)
    application = create_app(_settings())
    assert _PATH in _closed_test_paths(application)
    client = TestClient(application)
    response = client.post(
        _PATH,
        json={
            "session_id": "s1",
            "request_id": "r1",
            "client_name": _NAME,
            "phone": _PHONE,
        },
    )
    assert response.status_code == 401


def test_pii_admission_unavailable_without_pii_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_env(monkeypatch)
    # Closed-test enabled, but ephemeral PII / MAC keys unset → 503.
    client = TestClient(create_app(_settings()))
    response = client.post(
        _PATH,
        json={
            "session_id": "sess-1",
            "request_id": "req-1",
            "client_name": _NAME,
            "phone": _PHONE,
        },
        headers={"X-Bot-Closed-Test-Token": _TOKEN},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "PII_ADMISSION_UNAVAILABLE"}
    assert _PHONE not in response.text
    assert _NAME not in response.text

    _enable_env(monkeypatch)
    client = TestClient(create_app(_settings()))
    body = {
        "session_id": "sess-1",
        "request_id": "req-1",
        "client_name": _NAME,
        "phone": _PHONE,
    }
    missing = client.post(_PATH, json=body)
    assert missing.status_code == 401
    assert _TOKEN not in missing.text
    assert _PHONE not in missing.text
    assert _NAME not in missing.text

    wrong = client.post(
        _PATH,
        json=body,
        headers={"X-Bot-Closed-Test-Token": "w" * 32},
    )
    assert wrong.status_code == 401
    assert _TOKEN not in wrong.text


def test_pii_admission_schema_redacts_and_forbids_extra() -> None:
    created = ClosedTestPiiAdmissionCreate(
        session_id="sess-1",
        request_id="req-1",
        client_name=_NAME,
        phone=_PHONE,
    )
    rendered = repr(created)
    assert _PHONE not in rendered
    assert _NAME not in rendered
    assert "phone=<redacted>" in rendered
    assert "client_name=<redacted>" in rendered

    with pytest.raises(ValidationError):
        ClosedTestPiiAdmissionCreate(
            session_id="sess-1",
            request_id="req-1",
            client_name=_NAME,
            phone=_PHONE,
            extra="nope",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        ClosedTestPiiAdmissionCreate(
            session_id="bad id!",
            request_id="req-1",
            client_name=_NAME,
            phone=_PHONE,
        )


def test_pii_admission_ack_has_no_ref_fields() -> None:
    ack = ClosedTestPiiAdmissionAck(
        accepted=True,
        reused=False,
        session_id="sess-1",
        request_id="req-1",
        status="ADMITTED",
    )
    payload = ack.model_dump()
    assert "phone_ref_token" not in payload
    assert "name_ref_token" not in payload
    assert "phone" not in payload
    assert "client_name" not in payload


def test_pii_admission_http_validation_redacts_pii(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_env(monkeypatch)
    client = TestClient(create_app(_settings()))
    headers = {"X-Bot-Closed-Test-Token": _TOKEN}
    response = client.post(
        _PATH,
        json={
            "session_id": "sess-1",
            "request_id": "req-1",
            "client_name": "Secret\x00Name",
            "phone": _PHONE,
        },
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "VALIDATION_ERROR"}
    assert "Secret" not in response.text
    assert _PHONE not in response.text


def test_router_and_service_not_wired_to_confirm_or_create() -> None:
    router = (_REPO / "app/closed_test_router.py").read_text(encoding="utf-8")
    service = (_REPO / "app/services/closed_test.py").read_text(encoding="utf-8")
    for source in (router, service):
        assert "CONFIRM_SELECTED_SLOT" not in source
        assert "admit_confirmed" not in source
        assert "BookingCreateHttpClient" not in source
    assert "admit_pii" in service
    assert "/pii-admissions" in router
    assert "phone_ref_token" not in router
    assert "name_ref_token" not in router
