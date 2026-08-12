"""AMO-01A: amoCRM manager webhook / signature / binding unit coverage."""

from __future__ import annotations

import ast
import hashlib
import hmac
import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.amocrm_chat_webhook import AMOCRM_CHAT_WEBHOOK_PATH, build_amocrm_chat_router
from app.config import Settings
from app.core.amocrm_chat_config import AmoCrmChatConfig, AmoCrmChatConfigError
from app.core.amocrm_chat_signature import verify_amocrm_chat_signature
from app.core.amocrm_manager_ids import amocrm_manager_namespaced_id
from app.main import create_app
from app.models.ingress import IngressChannel, IngressEventType
from app.schemas.amocrm_manager_ingress import (
    AmoCrmChatWebhookPayload,
    AmoCrmManagerIngressEvent,
)
from app.services.amocrm_manager_ingress import (
    AmoCrmManagerIngressAdapter,
    IngressIdempotencyConflict,
    _assert_amocrm_duplicate_matches,
)
from app.services.ingress import IngressAck, IngressPersistError
from tests.docker_runtime_allowlist import (
    AMO01A_DOCKER_RUNTIME_PATHS,
    EXPECTED_DOCKER_ALLOW_RULES,
    assert_canonical_docker_runtime_allowlist,
    dockerignore_lines,
    is_included_in_docker_build_context,
)

_REPO = Path(__file__).resolve().parents[1]
_SECRET = "s" * 32
_FAKE_DB = "postgresql+asyncpg://bot:pass@127.0.0.1:5432/bot_tv"


def _sign(body: bytes, secret: str = _SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha1).hexdigest()


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "amocrm_chat_id": "amo-chat-1",
        "message_id": "amo-msg-1",
        "provider_sequence": 1,
        "text": "manager says hello",
    }
    base.update(overrides)
    return base


def _event(**overrides: object) -> AmoCrmManagerIngressEvent:
    chat = str(overrides.pop("amocrm_chat_id", "amo-chat-1"))
    msg = str(overrides.pop("amocrm_message_id", "amo-msg-1"))
    namespaced = amocrm_manager_namespaced_id(
        amocrm_chat_id=chat,
        amocrm_message_id=msg,
    )
    payload: dict[str, object] = {
        "amocrm_chat_id": chat,
        "amocrm_message_id": msg,
        "external_message_id": namespaced,
        "provider_sequence": 1,
        "text": "manager says hello",
    }
    payload.update(overrides)
    return AmoCrmManagerIngressEvent.model_validate(payload)


def test_default_disabled_config() -> None:
    config = AmoCrmChatConfig.from_env({})
    assert config.enabled is False
    assert config.channel_secret is None
    assert _SECRET not in repr(config)


def test_enabled_missing_secret_fail_closed() -> None:
    with pytest.raises(AmoCrmChatConfigError, match="AMOCRM_CHAT_SECRET_REQUIRED"):
        AmoCrmChatConfig.from_env({"AMOCRM_CHAT_WEBHOOK_ENABLED": "true"})


@pytest.mark.parametrize("secret", ["", "short", "has space" + "x" * 20])
def test_enabled_invalid_secret_fail_closed(secret: str) -> None:
    with pytest.raises(AmoCrmChatConfigError):
        AmoCrmChatConfig.from_env(
            {
                "AMOCRM_CHAT_WEBHOOK_ENABLED": "true",
                "AMOCRM_CHAT_CHANNEL_SECRET": secret,
            }
        )


def test_valid_enabled_config() -> None:
    config = AmoCrmChatConfig.from_env(
        {
            "AMOCRM_CHAT_WEBHOOK_ENABLED": "true",
            "AMOCRM_CHAT_CHANNEL_SECRET": _SECRET,
        }
    )
    assert config.enabled is True
    assert config.channel_secret == _SECRET
    assert _SECRET not in repr(config)


def test_signature_valid() -> None:
    body = b'{"ok":true}'
    config = AmoCrmChatConfig(enabled=True, channel_secret=_SECRET)
    assert (
        verify_amocrm_chat_signature(
            raw_body=body,
            provided_signature=_sign(body),
            config=config,
        )
        is True
    )


def test_signature_invalid_or_missing() -> None:
    body = b'{"ok":true}'
    config = AmoCrmChatConfig(enabled=True, channel_secret=_SECRET)
    assert (
        verify_amocrm_chat_signature(
            raw_body=body,
            provided_signature="0" * 40,
            config=config,
        )
        is False
    )
    assert (
        verify_amocrm_chat_signature(
            raw_body=body,
            provided_signature=None,
            config=config,
        )
        is False
    )
    assert (
        verify_amocrm_chat_signature(
            raw_body=body,
            provided_signature=_sign(body),
            config=AmoCrmChatConfig(enabled=False, channel_secret=None),
        )
        is False
    )


def test_payload_schema_forbids_extra_and_redacts_text() -> None:
    with pytest.raises(ValidationError):
        AmoCrmChatWebhookPayload.model_validate({**_payload(), "phone": "1"})
    event = _event(text="secret-manager-text")
    assert "secret-manager-text" not in repr(event)
    assert event.redacted_view()["text"] == "<redacted>"
    assert event.safe_envelope()["schema"] == "amocrm.manager.ingress.v1"
    assert event.safe_envelope()["amocrm_message_id"] == "amo-msg-1"
    assert event.external_message_id == "amo:amo-chat-1:amo-msg-1"


def test_namespaced_ingress_and_manager_ids() -> None:
    assert (
        amocrm_manager_namespaced_id(
            amocrm_chat_id="chat-a",
            amocrm_message_id="msg-1",
        )
        == "amo:chat-a:msg-1"
    )
    payload = AmoCrmChatWebhookPayload.model_validate(_payload())
    event = payload.to_ingress_event()
    assert event.external_message_id == "amo:amo-chat-1:amo-msg-1"
    assert event.amocrm_message_id == "amo-msg-1"
    with pytest.raises(ValidationError):
        AmoCrmManagerIngressEvent.model_validate(
            {
                "amocrm_chat_id": "amo-chat-1",
                "amocrm_message_id": "amo-msg-1",
                "external_message_id": "amo-msg-1",
                "provider_sequence": 1,
                "text": "x",
            }
        )


def test_duplicate_envelope_mismatch_is_conflict() -> None:
    event = _event()
    row = MagicMock()
    row.channel = IngressChannel.AMOCRM.value
    row.event_type = IngressEventType.AMOCRM_MANAGER_MESSAGE.value
    row.external_event_id = event.external_message_id
    row.external_conversation_id = event.amocrm_chat_id
    row.envelope_json = event.safe_envelope()
    _assert_amocrm_duplicate_matches(row, event)

    altered = dict(row.envelope_json)
    altered["text"] = "mutated-body"
    row.envelope_json = altered
    with pytest.raises(IngressIdempotencyConflict):
        _assert_amocrm_duplicate_matches(row, event)


def test_webhook_route_absent_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMOCRM_CHAT_WEBHOOK_ENABLED", raising=False)
    app = create_app(
        Settings.from_env(
            {
                "BOT_MODE": "OFF",
                "EMERGENCY_LOCK": "true",
                "DATABASE_URL": _FAKE_DB,
            }
        )
    )
    paths = {getattr(r, "path", None) for r in app.routes}
    assert AMOCRM_CHAT_WEBHOOK_PATH not in paths


def test_webhook_enabled_without_database_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AMOCRM_CHAT_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("AMOCRM_CHAT_CHANNEL_SECRET", _SECRET)
    with pytest.raises(AmoCrmChatConfigError, match="AMOCRM_CHAT_DATABASE_REQUIRED"):
        create_app(
            Settings.from_env(
                {
                    "BOT_MODE": "OFF",
                    "EMERGENCY_LOCK": "true",
                }
            )
        )


def test_webhook_ack_only_after_successful_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AmoCrmChatConfig(enabled=True, channel_secret=_SECRET)
    accept = AsyncMock(
        return_value=IngressAck(
            accepted=True,
            duplicate=False,
            event_id=uuid4(),
            status="RECEIVED",
            correlation_id=uuid4(),
        )
    )
    monkeypatch.setattr(AmoCrmManagerIngressAdapter, "accept", accept)
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(
        build_amocrm_chat_router(config=config, session_factory=MagicMock())
    )
    body = json.dumps(_payload()).encode("utf-8")
    with TestClient(app) as client:
        resp = client.post(
            AMOCRM_CHAT_WEBHOOK_PATH,
            content=body,
            headers={"Content-Type": "application/json", "X-Signature": _sign(body)},
        )
    assert resp.status_code == 200
    assert resp.text == "ok"
    accept.assert_awaited_once()


def test_webhook_rejects_bad_signature_without_persist() -> None:
    config = AmoCrmChatConfig(enabled=True, channel_secret=_SECRET)
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(
        build_amocrm_chat_router(config=config, session_factory=MagicMock())
    )
    body = json.dumps(_payload()).encode("utf-8")
    with TestClient(app) as client:
        missing = client.post(
            AMOCRM_CHAT_WEBHOOK_PATH,
            content=body,
            headers={"Content-Type": "application/json"},
        )
        bad = client.post(
            AMOCRM_CHAT_WEBHOOK_PATH,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Signature": "a" * 40,
            },
        )
    assert missing.status_code == 401
    assert bad.status_code == 401


def test_webhook_persist_failure_no_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AmoCrmChatConfig(enabled=True, channel_secret=_SECRET)
    monkeypatch.setattr(
        AmoCrmManagerIngressAdapter,
        "accept",
        AsyncMock(side_effect=IngressPersistError("INGRESS_PERSIST_FAILED")),
    )
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(
        build_amocrm_chat_router(config=config, session_factory=MagicMock())
    )
    body = json.dumps(_payload()).encode("utf-8")
    with TestClient(app) as client:
        resp = client.post(
            AMOCRM_CHAT_WEBHOOK_PATH,
            content=body,
            headers={"Content-Type": "application/json", "X-Signature": _sign(body)},
        )
    assert resp.status_code == 503
    assert resp.text == "unavailable"


def test_webhook_conflict_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AmoCrmChatConfig(enabled=True, channel_secret=_SECRET)
    monkeypatch.setattr(
        AmoCrmManagerIngressAdapter,
        "accept",
        AsyncMock(side_effect=IngressIdempotencyConflict()),
    )
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(
        build_amocrm_chat_router(config=config, session_factory=MagicMock())
    )
    body = json.dumps(_payload()).encode("utf-8")
    with TestClient(app) as client:
        resp = client.post(
            AMOCRM_CHAT_WEBHOOK_PATH,
            content=body,
            headers={"Content-Type": "application/json", "X-Signature": _sign(body)},
        )
    assert resp.status_code == 409
    assert resp.text == "conflict"


def test_no_global_amocrm_validation_handler() -> None:
    source = (_REPO / "app" / "amocrm_chat_webhook.py").read_text(encoding="utf-8")
    assert "install_amocrm_chat_validation_handler" not in source
    assert "exception_handler" not in source
    main_source = (_REPO / "app" / "main.py").read_text(encoding="utf-8")
    assert "install_amocrm_chat_validation_handler" not in main_source


def test_webhook_handler_has_no_direct_fsm_or_http() -> None:
    source = (_REPO / "app" / "amocrm_chat_webhook.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            for alias in node.names:
                imported.add(f"{node.module}.{alias.name}")
    forbidden_modules = {
        "app.services.manager_messages",
        "app.services.inbound",
        "httpx",
        "aiohttp",
        "urllib.request",
        "app.channels.vk_master_http",
        "app.services.booking_flow",
    }
    assert not (imported & forbidden_modules)
    assert "apply_manager_message_in_session" not in source
    assert "httpx" not in source
    assert "requests." not in source


def test_adapter_and_worker_have_no_forbidden_channel_imports() -> None:
    for rel in (
        "app/services/amocrm_manager_ingress.py",
        "app/amocrm_chat_webhook.py",
        "app/core/amocrm_chat_signature.py",
        "app/core/amocrm_manager_ids.py",
        "app/repositories/amocrm_chat_bindings.py",
    ):
        source = (_REPO / rel).read_text(encoding="utf-8")
        for banned in (
            "app.channels.vk",
            "app.services.booking_flow",
            "openai",
            "anthropic",
        ):
            assert banned not in source
        assert "def send_" not in source


def test_no_second_lease_state_in_binding_model() -> None:
    source = (_REPO / "app" / "models" / "amocrm_chat_binding.py").read_text(
        encoding="utf-8"
    )
    assert "lease_" not in source
    assert "handoff_deadline" not in source
    assert "HUMAN_ACTIVE" not in source


def test_docker_allowlist_includes_amo01a() -> None:
    assert_canonical_docker_runtime_allowlist()
    lines = dockerignore_lines(_REPO)
    for rel in AMO01A_DOCKER_RUNTIME_PATHS:
        assert f"!{rel}" in EXPECTED_DOCKER_ALLOW_RULES
        assert is_included_in_docker_build_context(rel, lines, repo_root=_REPO) is True
        assert (_REPO / rel).is_file()


def test_ingress_worker_branches_amocrm_without_inbound() -> None:
    from app.services import ingress as ingress_mod

    source = inspect.getsource(ingress_mod.IngressWorker.process_claimed)
    assert "INGRESS_CHANNEL_EVENT_MISMATCH" in source
    assert "_process_amocrm_manager" in source
    assert "apply_manager_message_in_session" in inspect.getsource(
        ingress_mod.IngressWorker._process_amocrm_manager
    )


def test_insert_pairing_guard_rejects_amocrm_synthetic_message() -> None:
    from app.repositories import ingress as ingress_repo

    source = inspect.getsource(ingress_repo._assert_channel_event_pairing)
    assert "AMOCRM_MANAGER_MESSAGE" in source
    assert "INGRESS_CHANNEL_EVENT_MISMATCH" in source
    with pytest.raises(ValueError, match="INGRESS_CHANNEL_EVENT_MISMATCH"):
        ingress_repo._assert_channel_event_pairing(
            channel=IngressChannel.AMOCRM,
            event_type=IngressEventType.SYNTHETIC_MESSAGE,
        )
