"""Native amoCRM outgoing CAPTURE-ONLY unit coverage."""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.amocrm_native_outgoing_capture_webhook import (
    amocrm_native_outgoing_capture_path,
    build_amocrm_native_outgoing_capture_router,
)
from app.config import Settings
from app.core.amocrm_native_outgoing_capture_config import (
    AmoCrmNativeOutgoingCaptureConfig,
    AmoCrmNativeOutgoingCaptureConfigError,
)
from app.main import create_app
from app.schemas.amocrm_native_outgoing_capture import (
    extract_outgoing_message_adds,
    parse_native_outgoing_form_body,
)
from tests.docker_runtime_allowlist import (
    AMO_NATIVE_OUTGOING_CAPTURE_DOCKER_RUNTIME_PATHS,
    assert_canonical_docker_runtime_allowlist,
    is_included_in_docker_build_context,
)

_REPO = Path(__file__).resolve().parents[1]
_TOKEN = "n" * 32
_TALK = "1894"
_CHAT = "1af271b6-19b9-4ae5-9b1d-da96f1ca2072"
_CONTACT = "28592745"
_ORIGIN = "vk"
_SOURCE = "19666978"
_FAKE_DB = "postgresql+asyncpg://bot:pass@127.0.0.1:5432/bot_tv"
_PATH = amocrm_native_outgoing_capture_path(_TOKEN)


def _enabled_env(**overrides: str) -> dict[str, str]:
    base = {
        "AMOCRM_NATIVE_OUTGOING_CAPTURE_ENABLED": "true",
        "AMOCRM_NATIVE_OUTGOING_CAPTURE_PATH_TOKEN": _TOKEN,
        "AMOCRM_NATIVE_OUTGOING_CAPTURE_TALK_ID": _TALK,
        "AMOCRM_NATIVE_OUTGOING_CAPTURE_CHAT_ID": _CHAT,
        "AMOCRM_NATIVE_OUTGOING_CAPTURE_CONTACT_ID": _CONTACT,
        "AMOCRM_NATIVE_OUTGOING_CAPTURE_ORIGIN": _ORIGIN,
        "AMOCRM_NATIVE_OUTGOING_CAPTURE_SOURCE_ID": _SOURCE,
    }
    base.update(overrides)
    return base


def _enabled_config() -> AmoCrmNativeOutgoingCaptureConfig:
    return AmoCrmNativeOutgoingCaptureConfig.from_env(_enabled_env())


def _form(**fields: str) -> bytes:
    return urlencode(fields).encode("utf-8")


def _target_outgoing_form(**overrides: str) -> bytes:
    fields = {
        "account[id]": "321321",
        "outgoing_message[add][0][id]": "msg-cap-1",
        "outgoing_message[add][0][chat_id]": _CHAT,
        "outgoing_message[add][0][talk_id]": _TALK,
        "outgoing_message[add][0][contact_id]": _CONTACT,
        "outgoing_message[add][0][text]": "SECRET_MANAGER_TEXT_SHOULD_NOT_PERSIST",
        "outgoing_message[add][0][created_at]": "1725530000",
        "outgoing_message[add][0][message_type]": "text",
        "outgoing_message[add][0][type]": "outgoing",
        "outgoing_message[add][0][origin]": _ORIGIN,
        "outgoing_message[add][0][source_id]": _SOURCE,
        "outgoing_message[add][0][author][id]": "author-tech-1",
        "outgoing_message[add][0][author][type]": "internal",
        "outgoing_message[add][0][author][user_id]": "4242",
        "outgoing_message[add][0][author][name]": "Ivan Secret",
        "outgoing_message[add][0][author][avatar_url]": "https://example/a.jpg",
        "outgoing_message[add][0][recipient][id]": "recipient-tech-1",
        "outgoing_message[add][0][recipient][type]": "external",
        "outgoing_message[add][0][recipient][name]": "Client Secret",
        "outgoing_message[add][0][attachment][link]": "https://example/file.bin",
        "outgoing_message[add][0][attachment][file_name]": "secret.bin",
    }
    fields.update(overrides)
    return _form(**fields)


@contextmanager
def _capture_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    insert_mock: AsyncMock,
) -> Iterator[TestClient]:
    import app.amocrm_native_outgoing_capture_webhook as webhook_mod

    @asynccontextmanager
    async def _fake_scope(_sf):  # noqa: ANN001
        yield MagicMock()

    monkeypatch.setattr(webhook_mod, "session_scope", _fake_scope)
    monkeypatch.setattr(
        webhook_mod.capture_repo,
        "insert_capture_if_absent",
        insert_mock,
    )
    app = FastAPI()
    app.include_router(
        build_amocrm_native_outgoing_capture_router(
            config=_enabled_config(),
            session_factory=MagicMock(),
        )
    )
    with TestClient(app) as client:
        yield client


def test_default_disabled_config() -> None:
    config = AmoCrmNativeOutgoingCaptureConfig.from_env({})
    assert config.enabled is False
    assert config.path_token is None
    assert _TOKEN not in repr(config)


def test_enabled_incomplete_fail_closed() -> None:
    with pytest.raises(
        AmoCrmNativeOutgoingCaptureConfigError,
        match="AMOCRM_NATIVE_OUTGOING_CAPTURE_PATH_TOKEN_REQUIRED",
    ):
        AmoCrmNativeOutgoingCaptureConfig.from_env(
            {"AMOCRM_NATIVE_OUTGOING_CAPTURE_ENABLED": "true"}
        )


@pytest.mark.parametrize(
    "missing",
    [
        "AMOCRM_NATIVE_OUTGOING_CAPTURE_TALK_ID",
        "AMOCRM_NATIVE_OUTGOING_CAPTURE_CHAT_ID",
        "AMOCRM_NATIVE_OUTGOING_CAPTURE_CONTACT_ID",
        "AMOCRM_NATIVE_OUTGOING_CAPTURE_ORIGIN",
        "AMOCRM_NATIVE_OUTGOING_CAPTURE_SOURCE_ID",
    ],
)
def test_enabled_missing_allowlist_field_fail_closed(missing: str) -> None:
    env = _enabled_env()
    del env[missing]
    with pytest.raises(AmoCrmNativeOutgoingCaptureConfigError):
        AmoCrmNativeOutgoingCaptureConfig.from_env(env)


def test_path_token_compare_digest() -> None:
    config = _enabled_config()
    assert config.matches_path_token(_TOKEN) is True
    assert config.matches_path_token("x" * 32) is False
    assert config.matches_path_token("") is False
    assert config.matches_path_token(None) is False


def test_allowlist_source_id_optional_in_payload() -> None:
    config = _enabled_config()
    assert config.matches_allowlist(
        talk_id=1894,
        chat_id=_CHAT,
        contact_id=28592745,
        origin="vk",
        source_id=None,
    )
    assert config.matches_allowlist(
        talk_id=1894,
        chat_id=_CHAT,
        contact_id=28592745,
        origin="vk",
        source_id=19666978,
    )
    assert not config.matches_allowlist(
        talk_id=1894,
        chat_id=_CHAT,
        contact_id=28592745,
        origin="vk",
        source_id=1,
    )


def test_parse_extracts_outgoing_and_ignores_message_add() -> None:
    body = _form(
        **{
            "message[add][0][id]": "incoming-1",
            "message[add][0][talk_id]": _TALK,
            "message[add][0][chat_id]": _CHAT,
            "message[add][0][contact_id]": _CONTACT,
            "message[add][0][origin]": _ORIGIN,
            "message[add][0][type]": "incoming",
            "message[add][0][message_type]": "text",
            "message[add][0][text]": "client secret",
        }
    )
    form = parse_native_outgoing_form_body(body)
    assert extract_outgoing_message_adds(form) == ()


def test_extract_sanitizes_pii_fields() -> None:
    form = parse_native_outgoing_form_body(_target_outgoing_form())
    candidates = extract_outgoing_message_adds(form)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.amocrm_message_id == "msg-cap-1"
    assert cand.talk_id == 1894
    assert cand.chat_id == _CHAT
    assert cand.contact_id == 28592745
    assert cand.origin == "vk"
    assert cand.source_id == 19666978
    assert cand.author_type == "internal"
    assert cand.author_user_id == "4242"
    assert cand.author_id == "author-tech-1"
    assert cand.recipient_id == "recipient-tech-1"
    assert cand.recipient_type == "external"
    blob = repr(cand)
    assert "SECRET_MANAGER_TEXT" not in blob
    assert "Ivan" not in blob
    assert "Client Secret" not in blob
    assert "example/a.jpg" not in blob
    assert "secret.bin" not in blob
    assert "text" not in cand.__dataclass_fields__


def test_non_text_outgoing_skipped() -> None:
    form = parse_native_outgoing_form_body(
        _target_outgoing_form(**{"outgoing_message[add][0][message_type]": "picture"})
    )
    assert extract_outgoing_message_adds(form) == ()


def test_create_app_default_route_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMOCRM_NATIVE_OUTGOING_CAPTURE_ENABLED", raising=False)
    settings = Settings.from_env(
        {
            "BOT_MODE": "OFF",
            "EMERGENCY_LOCK": "true",
            "DATABASE_URL": _FAKE_DB,
        }
    )
    app = create_app(settings)
    paths = {getattr(r, "path", None) for r in app.routes}
    assert not any(
        isinstance(p, str) and p.startswith("/webhooks/amocrm/native-outgoing/")
        for p in paths
    )


def test_create_app_enabled_incomplete_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AMOCRM_NATIVE_OUTGOING_CAPTURE_ENABLED", "true")
    settings = Settings.from_env(
        {
            "BOT_MODE": "OFF",
            "EMERGENCY_LOCK": "true",
            "DATABASE_URL": _FAKE_DB,
        }
    )
    with pytest.raises(AmoCrmNativeOutgoingCaptureConfigError):
        create_app(settings)


def test_wrong_token_401(monkeypatch: pytest.MonkeyPatch) -> None:
    insert_mock = AsyncMock(return_value=(MagicMock(), True))
    with _capture_client(monkeypatch, insert_mock=insert_mock) as client:
        response = client.post(
            amocrm_native_outgoing_capture_path("x" * 32),
            content=_target_outgoing_form(),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 401
    assert response.text == "unauthorized"
    assert insert_mock.await_count == 0


def test_message_add_200_no_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    insert_mock = AsyncMock(return_value=(MagicMock(), True))
    body = _form(
        **{
            "message[add][0][id]": "incoming-1",
            "message[add][0][talk_id]": _TALK,
            "message[add][0][chat_id]": _CHAT,
            "message[add][0][contact_id]": _CONTACT,
            "message[add][0][origin]": _ORIGIN,
            "message[add][0][type]": "incoming",
            "message[add][0][message_type]": "text",
            "message[add][0][text]": "nope",
        }
    )
    with _capture_client(monkeypatch, insert_mock=insert_mock) as client:
        response = client.post(
            _PATH,
            content=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 200
    assert response.text == "ok"
    assert insert_mock.await_count == 0


@pytest.mark.parametrize(
    "override",
    [
        {"outgoing_message[add][0][talk_id]": "9999"},
        {"outgoing_message[add][0][chat_id]": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
        {"outgoing_message[add][0][contact_id]": "111"},
        {"outgoing_message[add][0][origin]": "telegram"},
        {"outgoing_message[add][0][source_id]": "1"},
        {"outgoing_message[add][0][message_type]": "picture"},
        {"outgoing_message[add][0][type]": "incoming"},
    ],
)
def test_non_target_200_no_insert(
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, str],
) -> None:
    insert_mock = AsyncMock(return_value=(MagicMock(), True))
    with _capture_client(monkeypatch, insert_mock=insert_mock) as client:
        response = client.post(
            _PATH,
            content=_target_outgoing_form(**override),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 200
    assert insert_mock.await_count == 0


def test_target_outgoing_inserts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    row = MagicMock()
    row.id = uuid4()
    insert_mock = AsyncMock(return_value=(row, True))
    with _capture_client(monkeypatch, insert_mock=insert_mock) as client:
        response = client.post(
            _PATH,
            content=_target_outgoing_form(),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "X-Amocrm-Requestid": "req-1",
            },
        )
    assert response.status_code == 200
    assert response.text == "ok"
    assert "SECRET_MANAGER_TEXT" not in response.text
    assert insert_mock.await_count == 1
    kwargs = insert_mock.await_args.kwargs
    candidate = kwargs["candidate"]
    assert candidate.amocrm_message_id == "msg-cap-1"
    assert candidate.author_type == "internal"
    assert candidate.author_user_id == "4242"
    assert kwargs["request_id"] == "req-1"
    assert "text" not in candidate.__dataclass_fields__


def test_target_without_source_id_still_inserts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    insert_mock = AsyncMock(return_value=(MagicMock(), True))
    fields = parse_native_outgoing_form_body(_target_outgoing_form())
    flat = {
        k: v[0]
        for k, v in fields.items()
        if k != "outgoing_message[add][0][source_id]"
    }
    body = _form(**flat)
    with _capture_client(monkeypatch, insert_mock=insert_mock) as client:
        response = client.post(
            _PATH,
            content=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 200
    assert insert_mock.await_count == 1
    assert insert_mock.await_args.kwargs["candidate"].source_id is None


def test_webhook_module_has_no_application_logging_of_path() -> None:
    source = (
        _REPO / "app" / "amocrm_native_outgoing_capture_webhook.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in {
                "info",
                "warning",
                "error",
                "debug",
                "exception",
            }:
                pytest.fail("application logging call present in capture webhook")
            if isinstance(func, ast.Name) and func.id == "print":
                pytest.fail("print present in capture webhook")
    assert "amocrm_chat_signature" not in source
    assert "AMOCRM_CHAT_CHANNEL_SECRET" not in source
    assert "verify_amocrm_chat_signature" not in source


def test_docker_runtime_paths_allowlisted() -> None:
    assert_canonical_docker_runtime_allowlist()
    from tests.docker_runtime_allowlist import (
        EXPECTED_DOCKER_ALLOW_RULES,
        dockerignore_lines,
    )

    lines = dockerignore_lines(_REPO)
    for path in AMO_NATIVE_OUTGOING_CAPTURE_DOCKER_RUNTIME_PATHS:
        assert f"!{path}" in EXPECTED_DOCKER_ALLOW_RULES
        assert is_included_in_docker_build_context(path, lines, repo_root=_REPO) is True
        assert (_REPO / path).is_file()
