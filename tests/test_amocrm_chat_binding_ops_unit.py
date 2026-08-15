"""AMO-PROD-ENABLEMENT-OPS-01: Chat binding seed ops (unit/static)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import yaml

from app.services.amocrm_chat_binding_ops import (
    AmoCrmChatBindingOpsOutcome,
    seed_active_chat_binding,
)
from tests.docker_runtime_allowlist import (
    dockerignore_lines,
    is_included_in_docker_build_context,
)

_REPO = Path(__file__).resolve().parents[1]


def _fake_session_scope(session: AsyncMock):
    class _Scope:
        async def __aenter__(self) -> AsyncMock:
            return session

        async def __aexit__(self, *args: object) -> None:
            return None

    def _factory(*_a: object, **_k: object) -> _Scope:
        return _Scope()

    return _factory


@pytest.mark.asyncio
async def test_seed_binding_idempotent_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = uuid4()
    chat_id = "amo-chat-1"
    integ = "integ-conv-1"

    existing = MagicMock()
    existing.conversation_id = conversation_id
    existing.amocrm_chat_id = chat_id
    existing.integration_conversation_id = integ

    session = AsyncMock()
    session_factory = MagicMock()
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.session_scope",
        _fake_session_scope(session),
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.conversation_repo.lock_for_update",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.get_active_by_amocrm_chat_id",
        AsyncMock(return_value=existing),
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.get_active_by_conversation_id",
        AsyncMock(return_value=existing),
    )
    insert = AsyncMock()
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.insert_active_if_absent",
        insert,
    )

    result = await seed_active_chat_binding(
        session_factory,
        conversation_id=conversation_id,
        amocrm_chat_id=chat_id,
        integration_conversation_id=integ,
    )
    assert result.outcome is AmoCrmChatBindingOpsOutcome.ALREADY_PRESENT
    assert result.created is False
    assert result.error_code is None
    insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_binding_conflict_chat_repoint_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = uuid4()
    other_conversation = uuid4()
    existing = MagicMock()
    existing.conversation_id = other_conversation
    existing.amocrm_chat_id = "amo-chat-1"
    existing.integration_conversation_id = "integ-1"

    session = AsyncMock()
    session_factory = MagicMock()
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.session_scope",
        _fake_session_scope(session),
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.conversation_repo.lock_for_update",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.get_active_by_amocrm_chat_id",
        AsyncMock(return_value=existing),
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.get_active_by_conversation_id",
        AsyncMock(return_value=None),
    )
    insert = AsyncMock()
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.insert_active_if_absent",
        insert,
    )

    result = await seed_active_chat_binding(
        session_factory,
        conversation_id=conversation_id,
        amocrm_chat_id="amo-chat-1",
        integration_conversation_id="integ-1",
    )
    assert result.outcome is AmoCrmChatBindingOpsOutcome.REFUSED
    assert result.error_code == "BINDING_CHAT_CONFLICT"
    insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_binding_conflict_integ_repoint_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = uuid4()
    existing = MagicMock()
    existing.conversation_id = conversation_id
    existing.amocrm_chat_id = "amo-chat-1"
    existing.integration_conversation_id = "integ-old"

    session = AsyncMock()
    session_factory = MagicMock()
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.session_scope",
        _fake_session_scope(session),
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.conversation_repo.lock_for_update",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.get_active_by_amocrm_chat_id",
        AsyncMock(return_value=existing),
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.get_active_by_conversation_id",
        AsyncMock(return_value=existing),
    )
    insert = AsyncMock()
    capture = AsyncMock()
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.insert_active_if_absent",
        insert,
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.capture_integration_conversation_id",
        capture,
    )

    result = await seed_active_chat_binding(
        session_factory,
        conversation_id=conversation_id,
        amocrm_chat_id="amo-chat-1",
        integration_conversation_id="integ-new",
    )
    assert result.outcome is AmoCrmChatBindingOpsOutcome.REFUSED
    assert result.error_code == "BINDING_INTEGRATION_CONVERSATION_CONFLICT"
    insert.assert_not_awaited()
    capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_binding_conflict_conversation_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = uuid4()
    existing = MagicMock()
    existing.conversation_id = conversation_id
    existing.amocrm_chat_id = "amo-chat-a"
    existing.integration_conversation_id = "integ-a"

    session = AsyncMock()
    session_factory = MagicMock()
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.session_scope",
        _fake_session_scope(session),
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.conversation_repo.lock_for_update",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.get_active_by_amocrm_chat_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.get_active_by_conversation_id",
        AsyncMock(return_value=existing),
    )
    insert = AsyncMock()
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.insert_active_if_absent",
        insert,
    )

    result = await seed_active_chat_binding(
        session_factory,
        conversation_id=conversation_id,
        amocrm_chat_id="amo-chat-b",
        integration_conversation_id="integ-b",
    )
    assert result.outcome is AmoCrmChatBindingOpsOutcome.REFUSED
    assert result.error_code == "BINDING_CONVERSATION_CONFLICT"
    insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_binding_null_integ_fill_returns_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = uuid4()
    binding_id = uuid4()
    existing = MagicMock()
    existing.id = binding_id
    existing.conversation_id = conversation_id
    existing.amocrm_chat_id = "amo-chat-1"
    existing.integration_conversation_id = None

    filled = MagicMock()
    filled.conversation_id = conversation_id
    filled.amocrm_chat_id = "amo-chat-1"
    filled.integration_conversation_id = "integ-filled"

    session = AsyncMock()
    session_factory = MagicMock()
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.session_scope",
        _fake_session_scope(session),
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.conversation_repo.lock_for_update",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.get_active_by_amocrm_chat_id",
        AsyncMock(return_value=existing),
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.get_active_by_conversation_id",
        AsyncMock(return_value=existing),
    )
    insert = AsyncMock()
    capture = AsyncMock(return_value=filled)
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.insert_active_if_absent",
        insert,
    )
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.binding_repo.capture_integration_conversation_id",
        capture,
    )

    result = await seed_active_chat_binding(
        session_factory,
        conversation_id=conversation_id,
        amocrm_chat_id="amo-chat-1",
        integration_conversation_id="integ-filled",
    )
    assert result.outcome is AmoCrmChatBindingOpsOutcome.UPDATED
    assert result.created is False
    assert result.error_code is None
    insert.assert_not_awaited()
    capture.assert_awaited_once()
    assert capture.await_args.kwargs["binding_id"] == binding_id
    assert capture.await_args.kwargs["integration_conversation_id"] == "integ-filled"


@pytest.mark.asyncio
async def test_seed_binding_rejects_blank_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = MagicMock()
    lock = AsyncMock()
    monkeypatch.setattr(
        "app.services.amocrm_chat_binding_ops.conversation_repo.lock_for_update",
        lock,
    )
    result = await seed_active_chat_binding(
        session_factory,
        conversation_id=uuid4(),
        amocrm_chat_id="  ",
        integration_conversation_id="integ",
    )
    assert result.outcome is AmoCrmChatBindingOpsOutcome.REFUSED
    assert result.error_code == "AMOCRM_CHAT_ID_INVALID"
    lock.assert_not_awaited()


def test_binding_ops_zero_external_http_surface() -> None:
    service = (_REPO / "app" / "services" / "amocrm_chat_binding_ops.py").read_text(
        encoding="utf-8"
    )
    cli = (_REPO / "app" / "amocrm_chat_binding_ops.py").read_text(encoding="utf-8")
    for source in (service, cli):
        assert "send_silent_text" not in source
        assert "AmoCrmChatEgressHttpClient" not in source
        assert "AmoCrmCrmRestHttpClient" not in source
        assert "httpx" not in source
        assert "urllib" not in source
    assert "seed-binding" in cli
    assert "seed_active_chat_binding" in cli
    assert "No Chat HTTP" in cli or "no Chat HTTP" in cli
    assert "No discovery, bulk" in service or "no discovery" in service.lower()


def test_cli_exit_codes_success_zero_refused_nonzero() -> None:
    import ast
    import inspect

    from app import amocrm_chat_binding_ops as cli_mod

    source = inspect.getsource(cli_mod._run)
    tree = ast.parse(source)
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)

    # Walk for the final outcome→exit mapping: success set returns 0, else 2.
    returns = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Return) and node.value is not None
    ]
    exit_consts = {
        node.value.value
        for node in returns
        if isinstance(node.value, ast.Constant) and type(node.value.value) is int
    }
    assert 0 in exit_consts
    assert 2 in exit_consts
    assert 1 not in exit_consts
    assert "UPDATED" in source
    assert "ALREADY_PRESENT" in source
    assert "SEEDED" in source


def test_binding_ops_cli_not_in_docker_image() -> None:
    lines = dockerignore_lines(_REPO)
    assert (
        is_included_in_docker_build_context(
            "app/amocrm_chat_binding_ops.py",
            lines,
            repo_root=_REPO,
        )
        is False
    )
    assert (
        is_included_in_docker_build_context(
            "app/services/amocrm_chat_binding_ops.py",
            lines,
            repo_root=_REPO,
        )
        is False
    )


def test_compose_amocrm_defaults_fail_closed_no_secret_material() -> None:
    compose_text = (_REPO / "docker-compose.yml").read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)

    assert compose_text.count("BOT_MODE: ${BOT_MODE:-OFF}") == 1
    assert "EMERGENCY_LOCK: ${EMERGENCY_LOCK:-true}" in compose_text

    for service_name in ("api", "worker"):
        env = compose["services"][service_name]["environment"]
        assert env["BOT_MODE"] == "${BOT_MODE:-OFF}"
        assert env["EMERGENCY_LOCK"] == "${EMERGENCY_LOCK:-true}"
        assert env["AMOCRM_CHAT_WEBHOOK_ENABLED"] == (
            "${AMOCRM_CHAT_WEBHOOK_ENABLED:-false}"
        )
        assert env["AMOCRM_CHAT_EGRESS_ENABLED"] == (
            "${AMOCRM_CHAT_EGRESS_ENABLED:-false}"
        )
        assert env["AMOCRM_CRM_REST_ENABLED"] == (
            "${AMOCRM_CRM_REST_ENABLED:-false}"
        )
        assert env["AMOCRM_CRM_DEAL_CREATE_ENABLED"] == (
            "${AMOCRM_CRM_DEAL_CREATE_ENABLED:-false}"
        )
        assert env["AMOCRM_CHAT_CHANNEL_SECRET"] == (
            "${AMOCRM_CHAT_CHANNEL_SECRET:-}"
        )
        assert env["AMOCRM_CLIENT_SECRET"] == "${AMOCRM_CLIENT_SECRET:-}"
        assert "AMOCRM_CRM_OAUTH_KEY_" not in "".join(env.keys())
        env_files = compose["services"][service_name]["env_file"]
        assert len(env_files) == 1
        assert env_files[0]["required"] is False
        assert "amocrm-crm-oauth-keys.env" in env_files[0]["path"]

    migrate_env = compose["services"]["migrate"]["environment"]
    assert "AMOCRM_CHAT_WEBHOOK_ENABLED" not in migrate_env
    maintenance_env = compose["services"]["attachment-maintenance"]["environment"]
    assert "AMOCRM_CHAT_WEBHOOK_ENABLED" not in maintenance_env

    assert "AMOCRM_CRM_OAUTH_KEY_" not in compose_text
    assert "sk-" not in compose_text
    assert "Bearer " not in compose_text
