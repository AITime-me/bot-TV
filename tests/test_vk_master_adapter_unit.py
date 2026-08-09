"""Unit tests for CURSOR-29 VK master adapter (no live webhook / PG)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels.vk_master_config import (
    VkMasterAdapterConfig,
    VkMasterConfigError,
    connection_scope_for_group,
    vk_master_business_allowed,
)
from app.channels.vk_master_reply import render_vk_master_reply
from app.channels.vk_master_types import VkMasterWebhookKind
from app.channels.vk_master_webhook import parse_vk_master_callback
from app.config import BotMode, Settings
from app.core.master_channel_binding import ResolveMasterBindingOutcome
from app.core.master_command_types import (
    MasterCommandClarificationNeed,
    MasterCommandFlowOutcome,
    MasterCommandFlowResult,
    MasterCommandKind,
    MasterCommandPreview,
)
from app.services.vk_master_adapter import VkMasterAdapterService

_GROUP = 101
_SECRET = "callback-secret-value"
_CONFIRM = "confirm-string-xyz"
_TOKEN = "a" * 32
_USER = 555001
_NOW_TS = 1723200000


def _cfg(**overrides: Any) -> VkMasterAdapterConfig:
    base = dict(
        enabled=True,
        group_id=_GROUP,
        callback_secret=_SECRET,
        confirmation=_CONFIRM,
        access_token=_TOKEN,
    )
    base.update(overrides)
    return VkMasterAdapterConfig(**base)


def _message_payload(
    *,
    text: str = "выходной завтра",
    out: int = 0,
    from_id: int = _USER,
    peer_id: int | None = None,
    cmid: int | None = 42,
    action: object | None = None,
    event_type: str = "message_new",
    group_id: int = _GROUP,
    secret: str = _SECRET,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": 9,
        "date": _NOW_TS,
        "from_id": from_id,
        "peer_id": _USER if peer_id is None else peer_id,
        "out": out,
        "text": text,
    }
    if cmid is not None:
        message["conversation_message_id"] = cmid
    if action is not None:
        message["action"] = action
    return {
        "type": event_type,
        "group_id": group_id,
        "secret": secret,
        "object": {"message": message},
    }


def test_connection_scope_from_trusted_group() -> None:
    assert connection_scope_for_group(_GROUP) == f"vk-group-{_GROUP}"


def test_confirmation_ok() -> None:
    result = parse_vk_master_callback(
        {"type": "confirmation", "group_id": _GROUP, "secret": _SECRET},
        config=_cfg(enabled=False, access_token=None),
    )
    assert result.kind is VkMasterWebhookKind.CONFIRMATION
    assert result.confirmation_response == _CONFIRM


def test_bad_secret_rejected() -> None:
    result = parse_vk_master_callback(
        _message_payload(secret="wrong-secret-value"),
        config=_cfg(),
    )
    assert result.kind is VkMasterWebhookKind.REJECTED


def test_wrong_group_rejected() -> None:
    result = parse_vk_master_callback(
        _message_payload(group_id=_GROUP + 1),
        config=_cfg(),
    )
    assert result.kind is VkMasterWebhookKind.REJECTED


def test_malformed_schema_rejected() -> None:
    assert (
        parse_vk_master_callback(b"{", config=_cfg()).kind
        is VkMasterWebhookKind.REJECTED
    )
    assert (
        parse_vk_master_callback("not-json", config=_cfg()).kind
        is VkMasterWebhookKind.REJECTED
    )


@pytest.mark.parametrize(
    "payload",
    [
        _message_payload(event_type="message_reply"),
        _message_payload(out=1),
        _message_payload(text="   "),
        _message_payload(text=""),
        _message_payload(peer_id=2_000_000_001),
        _message_payload(from_id=-_GROUP, peer_id=-_GROUP),
        _message_payload(action={"type": "chat_invite_user"}),
    ],
)
def test_non_private_or_service_ignored(payload: dict[str, Any]) -> None:
    result = parse_vk_master_callback(payload, config=_cfg())
    assert result.kind is VkMasterWebhookKind.IGNORED


def test_direct_message_mapping_and_stable_id() -> None:
    result = parse_vk_master_callback(_message_payload(cmid=777), config=_cfg())
    assert result.kind is VkMasterWebhookKind.MESSAGE
    assert result.message is not None
    assert result.message.from_id == _USER
    assert result.message.peer_id == _USER
    assert result.message.external_account_id == str(_USER)
    assert result.message.external_message_id == "777"
    replay = parse_vk_master_callback(_message_payload(cmid=777), config=_cfg())
    assert replay.message is not None
    assert replay.message.external_message_id == result.message.external_message_id


def test_missing_or_invalid_stable_id_ignored() -> None:
    assert (
        parse_vk_master_callback(_message_payload(cmid=None), config=_cfg()).kind
        is VkMasterWebhookKind.IGNORED
    )
    bad = _message_payload()
    bad["object"]["message"]["conversation_message_id"] = "42"
    assert parse_vk_master_callback(bad, config=_cfg()).kind is VkMasterWebhookKind.IGNORED


def test_incomplete_config_fail_closed() -> None:
    with pytest.raises(VkMasterConfigError):
        VkMasterAdapterConfig.from_env(
            {"VK_MASTER_ADAPTER_ENABLED": "false", "VK_MASTER_GROUP_ID": "1"}
        )
    with pytest.raises(VkMasterConfigError):
        VkMasterAdapterConfig.from_env(
            {
                "VK_MASTER_ADAPTER_ENABLED": "true",
                "VK_MASTER_GROUP_ID": str(_GROUP),
                "VK_MASTER_CALLBACK_SECRET": _SECRET,
                "VK_MASTER_CONFIRMATION": _CONFIRM,
            }
        )


def test_business_gates() -> None:
    cfg = _cfg()
    assert vk_master_business_allowed(
        Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=False), cfg
    )
    assert not vk_master_business_allowed(
        Settings(bot_mode=BotMode.OFF, emergency_lock=False), cfg
    )
    assert not vk_master_business_allowed(
        Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=True), cfg
    )
    assert not vk_master_business_allowed(
        Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=False),
        _cfg(enabled=False),
    )
    defaults = Settings()
    assert defaults.bot_mode is BotMode.OFF
    assert defaults.emergency_lock is True


def test_reply_mapper_safe_and_silent_bindings() -> None:
    assert (
        render_vk_master_reply(
            MasterCommandFlowResult(outcome=MasterCommandFlowOutcome.BINDING_REQUIRED)
        )
        is None
    )
    assert (
        render_vk_master_reply(
            MasterCommandFlowResult(outcome=MasterCommandFlowOutcome.DUPLICATE_IGNORED)
        )
        is None
    )
    assert (
        render_vk_master_reply(
            MasterCommandFlowResult(
                outcome=MasterCommandFlowOutcome.CONFLICT,
                result_code="CONFLICT",
            )
        )
        is None
    )
    assert (
        render_vk_master_reply(
            MasterCommandFlowResult(
                outcome=MasterCommandFlowOutcome.CONFLICT,
                result_code="PENDING_COMMAND_ACTIVE",
            )
        )
        is not None
    )
    assert (
        render_vk_master_reply(
            MasterCommandFlowResult(
                outcome=MasterCommandFlowOutcome.MANUAL_HELP,
                result_code="MANUAL_HELP",
            )
        )
        is None
    )
    unknown = render_vk_master_reply(
        MasterCommandFlowResult(
            outcome=MasterCommandFlowOutcome.MANUAL_HELP,
            result_code="UNKNOWN_COMMAND",
        )
    )
    assert unknown is not None
    assert "не понял" in unknown.lower()
    text = render_vk_master_reply(
        MasterCommandFlowResult(
            outcome=MasterCommandFlowOutcome.CONFIRMATION_REQUIRED,
            preview=MasterCommandPreview(
                action="выходной",
                date_key="2026-08-11",
                command_version=1,
            ),
            command_kind=MasterCommandKind.CLOSE_DAY,
        )
    )
    assert text is not None
    assert "выходной" in text
    assert "да" in text
    master = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    assert master not in text
    clarify = render_vk_master_reply(
        MasterCommandFlowResult(
            outcome=MasterCommandFlowOutcome.CLARIFICATION_REQUIRED,
            clarification_needs=(MasterCommandClarificationNeed.DATE,),
        )
    )
    assert clarify is not None and "дат" in clarify


@pytest.mark.asyncio
async def test_unbound_silent_no_c28_no_send(monkeypatch: pytest.MonkeyPatch) -> None:
    sender = MagicMock()
    session = MagicMock()

    class _Scope:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *args: object) -> None:
            return None

    import app.services.vk_master_adapter as mod

    monkeypatch.setattr(mod, "session_scope", lambda _f: _Scope())
    bindings = MagicMock()
    bindings.resolve = AsyncMock(
        return_value=MagicMock(outcome=ResolveMasterBindingOutcome.NOT_FOUND)
    )
    monkeypatch.setattr(mod, "MasterChannelBindingService", MagicMock(return_value=bindings))
    flow_ctor = MagicMock()
    monkeypatch.setattr(mod, "MasterCommandFlowService", flow_ctor)

    adapter = VkMasterAdapterService(
        MagicMock(),
        settings=Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=False),
        config=_cfg(),
        master_client=MagicMock(),
        pii_store=None,
        sender=sender,
    )
    result = await adapter.handle_callback(json.dumps(_message_payload()))
    assert result.body == "ok"
    flow_ctor.assert_not_called()
    sender.send_text.assert_not_called()


@pytest.mark.asyncio
async def test_commit_before_send_and_send_failure_keeps_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    sender = MagicMock()

    def _send(**kwargs: Any) -> None:
        order.append("send")
        raise RuntimeError("transport down")

    sender.send_text = _send
    session = MagicMock()

    class _Scope:
        async def __aenter__(self) -> Any:
            order.append("uow_enter")
            return session

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            order.append("uow_exit")
            return None

    import app.services.vk_master_adapter as mod

    monkeypatch.setattr(mod, "session_scope", lambda _f: _Scope())
    bindings = MagicMock()
    bindings.resolve = AsyncMock(
        return_value=MagicMock(outcome=ResolveMasterBindingOutcome.RESOLVED)
    )
    monkeypatch.setattr(mod, "MasterChannelBindingService", MagicMock(return_value=bindings))
    monkeypatch.setattr(mod.pending_repo, "get_by_inbound", AsyncMock(return_value=None))

    flow = MagicMock()
    flow.handle = AsyncMock(
        return_value=MasterCommandFlowResult(
            outcome=MasterCommandFlowOutcome.CONFIRMATION_REQUIRED,
            preview=MasterCommandPreview(action="выходной", date_key="2026-08-11"),
        )
    )
    monkeypatch.setattr(mod, "MasterCommandFlowService", MagicMock(return_value=flow))

    adapter = VkMasterAdapterService(
        MagicMock(),
        settings=Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=False),
        config=_cfg(),
        master_client=MagicMock(),
        pii_store=None,
        sender=sender,
    )
    result = await adapter.handle_callback(json.dumps(_message_payload()))
    assert result.body == "ok"
    assert order == ["uow_enter", "uow_exit", "send"]
    flow.handle.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_inbound_silent_no_flow_no_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = MagicMock()
    session = MagicMock()

    class _Scope:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *args: object) -> None:
            return None

    import app.services.vk_master_adapter as mod

    monkeypatch.setattr(mod, "session_scope", lambda _f: _Scope())
    bindings = MagicMock()
    bindings.resolve = AsyncMock(
        return_value=MagicMock(outcome=ResolveMasterBindingOutcome.RESOLVED)
    )
    monkeypatch.setattr(mod, "MasterChannelBindingService", MagicMock(return_value=bindings))
    monkeypatch.setattr(mod.pending_repo, "get_by_inbound", AsyncMock(return_value=object()))
    flow_ctor = MagicMock()
    monkeypatch.setattr(mod, "MasterCommandFlowService", flow_ctor)

    adapter = VkMasterAdapterService(
        MagicMock(),
        settings=Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=False),
        config=_cfg(),
        master_client=MagicMock(),
        pii_store=None,
        sender=sender,
    )
    await adapter.handle_callback(json.dumps(_message_payload()))
    flow_ctor.assert_not_called()
    sender.send_text.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_or_locked_gates_skip_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = MagicMock()
    import app.services.vk_master_adapter as mod

    flow_ctor = MagicMock()
    monkeypatch.setattr(mod, "MasterCommandFlowService", flow_ctor)

    adapter = VkMasterAdapterService(
        MagicMock(),
        settings=Settings(bot_mode=BotMode.OFF, emergency_lock=False),
        config=_cfg(),
        master_client=MagicMock(),
        pii_store=None,
        sender=sender,
    )
    await adapter.handle_callback(json.dumps(_message_payload()))
    flow_ctor.assert_not_called()
    sender.send_text.assert_not_called()


def test_architecture_no_forbidden_imports() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for rel in (
        "app/channels/vk_master_webhook.py",
        "app/channels/vk_master_http.py",
        "app/services/vk_master_adapter.py",
        "app/channels/vk_master_config.py",
        "app/channels/vk_master_reply.py",
    ):
        text = (root / rel).read_text(encoding="utf-8").lower()
        assert "vk_api" not in text
        assert "amocrm" not in text
        assert "n8n" not in text
    adapter = (root / "app/services/vk_master_adapter.py").read_text(encoding="utf-8")
    assert "InboundService" not in adapter
    assert "SyntheticIngress" not in adapter
    assert "insert_synthetic" not in adapter


def test_config_repr_redacts_secrets() -> None:
    rendered = repr(_cfg())
    assert _SECRET not in rendered
    assert _TOKEN not in rendered
    assert _CONFIRM not in rendered


def test_production_wiring_injects_pii_store_when_configured() -> None:
    import base64
    import secrets

    from app.main import build_vk_master_adapter_service
    from app.services.ephemeral_pii_store import EphemeralPiiStore

    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    env = {
        "EPHEMERAL_PII_ACTIVE_KEY_ID": "VKH1",
        "EPHEMERAL_PII_KEY_VKH1": key,
    }
    session_factory = MagicMock()
    adapter = build_vk_master_adapter_service(
        session_factory,
        settings=Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=False),
        vk_config=_cfg(),
        master_client=MagicMock(),
        sender=MagicMock(),
        environ=env,
    )
    assert type(adapter._pii) is EphemeralPiiStore
    assert adapter._pii is not None


def test_production_wiring_pii_unset_is_none() -> None:
    from app.main import build_vk_master_adapter_service
    from app.services.ephemeral_pii_store import build_ephemeral_pii_store_from_env

    assert build_ephemeral_pii_store_from_env(MagicMock(), environ={}) is None
    adapter = build_vk_master_adapter_service(
        MagicMock(),
        settings=Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=False),
        vk_config=_cfg(),
        master_client=MagicMock(),
        sender=MagicMock(),
        environ={},
    )
    assert adapter._pii is None


def test_canonical_pii_factory_still_fails_closed_on_incomplete() -> None:
    from app.core.ephemeral_pii_types import EphemeralPiiError
    from app.services.ephemeral_pii_store import build_ephemeral_pii_store_from_env

    with pytest.raises(EphemeralPiiError) as raised:
        build_ephemeral_pii_store_from_env(
            MagicMock(),
            environ={"EPHEMERAL_PII_ACTIVE_KEY_ID": "VKH1"},
        )
    assert raised.value.code in {
        "EPHEMERAL_PII_KEY_UNAVAILABLE",
        "EPHEMERAL_PII_CONFIG_INVALID",
    }


def test_vk_wiring_degrades_partial_pii_without_aborting_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial PII must not kill create_app /health; adapter gets pii_store=None."""

    from fastapi.testclient import TestClient

    import app.main as main_mod
    from app.main import build_vk_master_adapter_service, create_app

    partial = {"EPHEMERAL_PII_ACTIVE_KEY_ID": "VKH1"}
    adapter = build_vk_master_adapter_service(
        MagicMock(),
        settings=Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=False),
        vk_config=_cfg(),
        master_client=MagicMock(),
        sender=MagicMock(),
        environ=partial,
    )
    assert adapter._pii is None

    captured: dict[str, Any] = {}
    real_build = main_mod.build_vk_master_adapter_service

    def _capture_adapter(*args: Any, **kwargs: Any) -> VkMasterAdapterService:
        built = real_build(*args, **kwargs)
        captured["adapter"] = built
        return built

    monkeypatch.setattr(main_mod, "build_vk_master_adapter_service", _capture_adapter)
    monkeypatch.setattr(main_mod, "create_engine", lambda _s: MagicMock())
    monkeypatch.setattr(main_mod, "create_session_factory", lambda _e: MagicMock())
    monkeypatch.setattr(main_mod, "build_master_command_client", lambda _s: None)
    monkeypatch.setenv("VK_MASTER_GROUP_ID", str(_GROUP))
    monkeypatch.setenv("VK_MASTER_CALLBACK_SECRET", _SECRET)
    monkeypatch.setenv("VK_MASTER_CONFIRMATION", _CONFIRM)
    monkeypatch.setenv("EPHEMERAL_PII_ACTIVE_KEY_ID", "VKH1")
    monkeypatch.delenv("EPHEMERAL_PII_KEY_VKH1", raising=False)

    application = create_app(
        Settings(
            database_url="postgresql+asyncpg://bot:x@127.0.0.1:5432/bot_tv_test",
            bot_mode=BotMode.OFF,
            emergency_lock=True,
        )
    )
    client = TestClient(application)
    assert client.get("/health").status_code == 200
    assert client.get("/health/live").status_code == 200
    assert "adapter" in captured
    assert captured["adapter"]._pii is None


@pytest.mark.asyncio
async def test_create_booking_unavailable_when_wiring_degraded_pii(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid PII config → adapter pii_store=None → CREATE_BOOKING cannot store PII."""

    from app.main import build_vk_master_adapter_service

    adapter = build_vk_master_adapter_service(
        MagicMock(),
        settings=Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=False),
        vk_config=_cfg(),
        master_client=MagicMock(),
        sender=MagicMock(),
        environ={"EPHEMERAL_PII_ACTIVE_KEY_ID": "VKH1"},
    )
    assert adapter._pii is None

    sender = MagicMock()
    session = MagicMock()

    class _Scope:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *args: object) -> None:
            return None

    import app.services.vk_master_adapter as mod

    monkeypatch.setattr(mod, "session_scope", lambda _f: _Scope())
    bindings = MagicMock()
    bindings.resolve = AsyncMock(
        return_value=MagicMock(outcome=ResolveMasterBindingOutcome.RESOLVED)
    )
    monkeypatch.setattr(mod, "MasterChannelBindingService", MagicMock(return_value=bindings))
    monkeypatch.setattr(mod.pending_repo, "get_by_inbound", AsyncMock(return_value=None))

    flow = MagicMock()
    flow.handle = AsyncMock(
        return_value=MasterCommandFlowResult(
            outcome=MasterCommandFlowOutcome.UNAVAILABLE,
            result_code="PII_REQUIRED",
            command_kind=MasterCommandKind.CREATE_BOOKING,
        )
    )
    flow_ctor = MagicMock(return_value=flow)
    monkeypatch.setattr(mod, "MasterCommandFlowService", flow_ctor)

    degraded = VkMasterAdapterService(
        MagicMock(),
        settings=Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=False),
        config=_cfg(),
        master_client=MagicMock(),
        pii_store=adapter._pii,
        sender=sender,
    )
    slot = (
        "bs1.11111111-1111-4111-8111-111111111111."
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee.2026-08-12.1500"
    )
    text = f"запись клиенту Иван +79991234567 {slot}"
    await degraded.handle_callback(json.dumps(_message_payload(text=text)))
    assert flow_ctor.call_args.kwargs["pii_store"] is None


@pytest.mark.asyncio
async def test_create_booking_path_passes_injected_pii_to_c28(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CREATE_BOOKING through the VK adapter must receive a real pii_store.

    Under the previous hardcoded ``pii_store=None``, C28 returned
    ``PII_REQUIRED`` / ``PII_STORE_UNCONFIGURED`` and could not store phone/name.
    """

    sender = MagicMock()
    session = MagicMock()
    pii_store = MagicMock(name="ephemeral_pii_store")

    class _Scope:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *args: object) -> None:
            return None

    import app.services.vk_master_adapter as mod

    monkeypatch.setattr(mod, "session_scope", lambda _f: _Scope())
    bindings = MagicMock()
    bindings.resolve = AsyncMock(
        return_value=MagicMock(outcome=ResolveMasterBindingOutcome.RESOLVED)
    )
    monkeypatch.setattr(mod, "MasterChannelBindingService", MagicMock(return_value=bindings))
    monkeypatch.setattr(mod.pending_repo, "get_by_inbound", AsyncMock(return_value=None))

    flow = MagicMock()
    flow.handle = AsyncMock(
        return_value=MasterCommandFlowResult(
            outcome=MasterCommandFlowOutcome.CONFIRMATION_REQUIRED,
            preview=MasterCommandPreview(action="запись", date_key="2026-08-12"),
            command_kind=MasterCommandKind.CREATE_BOOKING,
        )
    )
    flow_ctor = MagicMock(return_value=flow)
    monkeypatch.setattr(mod, "MasterCommandFlowService", flow_ctor)

    adapter = VkMasterAdapterService(
        MagicMock(),
        settings=Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=False),
        config=_cfg(),
        master_client=MagicMock(),
        pii_store=pii_store,
        sender=sender,
    )
    booking_text = "запись клиенту Иван +79991234567 slot_abc"
    await adapter.handle_callback(json.dumps(_message_payload(text=booking_text)))
    flow_ctor.assert_called_once()
    assert flow_ctor.call_args.kwargs["pii_store"] is pii_store
    assert flow_ctor.call_args.kwargs["pii_store"] is not None
    sender.send_text.assert_called_once()


def test_http_sender_installs_and_uses_reject_redirects() -> None:
    """Prove ``_RejectRedirects`` is wired; default urllib follow would fail this."""

    import io
    import traceback
    from email.message import EmailMessage

    import urllib.request
    from urllib.response import addinfourl

    from app.channels import vk_master_http as http_mod
    from app.channels.vk_master_http import (
        VkMasterHttpSender,
        VkMasterSendError,
        _RejectRedirects,
    )

    sender = VkMasterHttpSender(_cfg())
    assert any(isinstance(h, _RejectRedirects) for h in sender._opener.handlers)

    # Handler itself rejects same-host and cross-host 3xx (no follow).
    reject = _RejectRedirects()
    req = urllib.request.Request("https://api.vk.com/method/messages.send")
    for location in (
        "https://api.vk.com/method/messages.send?hijack=1",
        "https://evil.example/steal",
    ):
        with pytest.raises(VkMasterSendError) as raised:
            reject.redirect_request(req, None, 302, "Found", {}, location)
        assert raised.value.code == "VK_MASTER_SEND_FAILED"
        blob = f"{raised.value!s}{raised.value!r}" + "".join(
            traceback.format_exception(raised.value)
        )
        assert _TOKEN not in blob
        assert "evil" not in blob
        assert "hijack" not in blob

    class _FakeHttpsRedirect(urllib.request.BaseHandler):
        """Local fake transport: first open returns 302; never hits the network."""

        handler_order = 100

        def __init__(self, location: str) -> None:
            self.location = location
            self.opened: list[str] = []

        def https_open(self, request: urllib.request.Request):  # noqa: ANN001
            self.opened.append(request.full_url)
            headers = EmailMessage()
            headers["Location"] = self.location
            resp = addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
            resp.msg = "Found"
            return resp

    for location in (
        "https://api.vk.com/method/other",
        "https://evil.example/phish",
    ):
        fake = _FakeHttpsRedirect(location)
        sender._opener = urllib.request.build_opener(_RejectRedirects(), fake)
        with pytest.raises(VkMasterSendError) as raised:
            sender.send_text(peer_id=_USER, text="ping")
        assert raised.value.code == "VK_MASTER_SEND_FAILED"
        assert fake.opened == [f"{_cfg().api_base_url}/method/messages.send"]
        blob = f"{raised.value!s}{raised.value!r}" + "".join(
            traceback.format_exception(raised.value)
        )
        assert _TOKEN not in blob
        assert "evil" not in blob
        assert "phish" not in blob

    # Contrast: default redirect handler would follow (second https_open).
    class _OkSecond(urllib.request.BaseHandler):
        handler_order = 100
        calls = 0

        def https_open(self, request: urllib.request.Request):  # noqa: ANN001
            _OkSecond.calls += 1
            if _OkSecond.calls == 1:
                headers = EmailMessage()
                headers["Location"] = "https://api.vk.com/method/other"
                resp = addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
                resp.msg = "Found"
                return resp
            headers = EmailMessage()
            resp = addinfourl(
                io.BytesIO(b'{"response":1}'), headers, request.full_url, 200
            )
            resp.msg = "OK"
            return resp

    default_opener = urllib.request.build_opener(_OkSecond())
    # Default HTTPRedirectHandler is installed by build_opener — follows 302.
    with default_opener.open(
        urllib.request.Request(
            "https://api.vk.com/method/messages.send",
            data=b"x=1",
            method="POST",
        ),
        timeout=5.0,
    ) as resp:
        assert resp.status == 200
    assert _OkSecond.calls == 2
    # If VkMasterHttpSender dropped _RejectRedirects, the reject path above
    # would also follow; keep the production opener assertion tight.
    assert http_mod._RejectRedirects is _RejectRedirects
    prod = VkMasterHttpSender(_cfg())
    assert any(type(h) is _RejectRedirects for h in prod._opener.handlers)
