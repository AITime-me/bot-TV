"""VK CLIENT shadow ingress unit tests (callback, isolation, worker path)."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.channels.vk_client_config import VkClientCallbackConfig, VkClientConfigError
from app.channels.vk_client_http import (
    VK_CLIENT_WEBHOOK_PATH,
    handle_vk_client_callback,
)
from app.channels.vk_client_types import VkClientWebhookKind, VK_CLIENT_TEXT_MAX_LEN
from app.channels.vk_client_webhook import parse_vk_client_callback
from app.channels.vk_master_config import VkMasterAdapterConfig
from app.channels.vk_master_webhook import parse_vk_master_callback
from app.models.ingress import IngressChannel, IngressEventType
from app.repositories.ingress import IngressClaim
from app.schemas.vk_client_ingress import VkClientIngressEvent
from app.services.ingress import IngressProcessResult, IngressWorker
from app.services.vk_client_inbound import assert_no_client_outbound_path
from app.services.vk_client_ingress import (
    VkClientIngressAdapter,
    VkClientIngressIdempotencyConflict,
    _assert_vk_duplicate_matches,
)
from app.services.vk_master_adapter import VkMasterAdapterService

_REPO = Path(__file__).resolve().parents[1]
_GROUP = 202501
_SECRET = "vk-client-callback-secret"
_CONFIRM = "client-confirm-token"
_USER = 9001001
_NOW_TS = 1723200000
_CMID = 77


def _cfg(**overrides: Any) -> VkClientCallbackConfig:
    base = dict(
        enabled=True,
        group_id=_GROUP,
        callback_secret=_SECRET,
        confirmation=_CONFIRM,
    )
    base.update(overrides)
    return VkClientCallbackConfig(**base)


def _message_payload(
    *,
    text: str = "Здравствуйте, хочу записаться",
    out: int = 0,
    from_id: int = _USER,
    peer_id: int | None = None,
    cmid: int | None = _CMID,
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


def test_config_default_off() -> None:
    cfg = VkClientCallbackConfig.from_env({})
    assert cfg.enabled is False
    assert cfg.callback_config_complete() is False


def test_config_enabled_incomplete_fail_closed() -> None:
    with pytest.raises(VkClientConfigError):
        VkClientCallbackConfig.from_env(
            {
                "VK_CLIENT_CALLBACK_ENABLED": "true",
                "VK_CLIENT_GROUP_ID": str(_GROUP),
            }
        )


def test_config_enabled_complete() -> None:
    cfg = VkClientCallbackConfig.from_env(
        {
            "VK_CLIENT_CALLBACK_ENABLED": "true",
            "VK_CLIENT_GROUP_ID": str(_GROUP),
            "VK_CLIENT_CALLBACK_SECRET": _SECRET,
            "VK_CLIENT_CONFIRMATION": _CONFIRM,
        }
    )
    assert cfg.enabled is True
    assert cfg.callback_config_complete() is True
    assert "secret" not in repr(cfg).lower() or "<redacted>" in repr(cfg)
    assert _SECRET not in repr(cfg)


def test_parse_confirmation() -> None:
    parsed = parse_vk_client_callback(
        {"type": "confirmation", "group_id": _GROUP, "secret": _SECRET},
        config=_cfg(),
    )
    assert parsed.kind is VkClientWebhookKind.CONFIRMATION
    assert parsed.confirmation_response == _CONFIRM


def test_parse_message_new_private() -> None:
    parsed = parse_vk_client_callback(_message_payload(), config=_cfg())
    assert parsed.kind is VkClientWebhookKind.MESSAGE
    assert parsed.message is not None
    assert parsed.message.external_conversation_id == f"vk-{_GROUP}-{_USER}"
    assert parsed.message.external_event_id == f"vk-{_GROUP}-{_USER}-{_CMID}"
    assert "Здравствуйте" not in repr(parsed.message)


@pytest.mark.parametrize(
    "payload",
    [
        _message_payload(out=1),
        _message_payload(peer_id=_USER + 1),
        _message_payload(action={"type": "chat_invite_user"}),
        _message_payload(text="   "),
        _message_payload(text="ok\x01ctrl"),
        _message_payload(text="X" * (VK_CLIENT_TEXT_MAX_LEN + 1)),
        _message_payload(cmid=None),
        _message_payload(secret="wrong-secret-value"),
        _message_payload(group_id=_GROUP + 1),
        _message_payload(event_type="message_typing_state"),
    ],
)
def test_parse_ignores_or_rejects_non_client(payload: dict[str, Any]) -> None:
    parsed = parse_vk_client_callback(payload, config=_cfg())
    assert parsed.kind in {
        VkClientWebhookKind.IGNORED,
        VkClientWebhookKind.REJECTED,
    }
    assert parsed.message is None
    assert parsed.message_reply is None


def _reply_payload(
    *,
    out: int = 1,
    from_id: int | None = None,
    peer_id: int = _USER,
    cmid: int = _CMID + 100,
    provider_id: int = 82727,
    payload: object | None = None,
    event_type: str = "message_reply",
    group_id: int = _GROUP,
    secret: str = _SECRET,
    date: int = _NOW_TS,
    random_id: int = 0,
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "id": provider_id,
        "date": date,
        "from_id": -group_id if from_id is None else from_id,
        "peer_id": peer_id,
        "out": out,
        "conversation_message_id": cmid,
        "random_id": random_id,
        "text": "MANAGER_TEXT_MUST_NOT_BE_STORED",
    }
    if payload is not None:
        obj["payload"] = payload
    return {
        "type": event_type,
        "group_id": group_id,
        "secret": secret,
        "object": obj,
    }


def test_parse_message_reply_private_outgoing() -> None:
    parsed = parse_vk_client_callback(
        _reply_payload(payload={"known_event": True}),
        config=_cfg(),
    )
    assert parsed.kind is VkClientWebhookKind.MESSAGE_REPLY
    assert parsed.message_reply is not None
    assert parsed.message_reply.external_conversation_id == f"vk-{_GROUP}-{_USER}"
    assert parsed.message_reply.provider_message_id == 82727
    assert "MANAGER_TEXT" not in repr(parsed.message_reply)
    assert "MANAGER_TEXT" not in str(parsed.message_reply.technical_envelope())


@pytest.mark.parametrize(
    "payload",
    [
        _reply_payload(out=0),
        _reply_payload(from_id=_USER),
        _reply_payload(peer_id=2_000_000_001),
        _reply_payload(group_id=_GROUP + 1),
        _reply_payload(secret="wrong-secret-value"),
    ],
)
def test_parse_message_reply_fail_closed(payload: dict[str, Any]) -> None:
    parsed = parse_vk_client_callback(payload, config=_cfg())
    assert parsed.kind in {
        VkClientWebhookKind.IGNORED,
        VkClientWebhookKind.REJECTED,
    }
    assert parsed.message_reply is None


def test_master_secret_cannot_authorize_client_callback() -> None:
    master_secret = "master-callback-secret-xx"
    client = _cfg(callback_secret=_SECRET)
    parsed = parse_vk_client_callback(
        _message_payload(secret=master_secret),
        config=client,
    )
    assert parsed.kind is VkClientWebhookKind.REJECTED


def test_client_secret_cannot_authorize_master_callback() -> None:
    master = VkMasterAdapterConfig(
        enabled=True,
        group_id=_GROUP,
        callback_secret="master-callback-secret-xx",
        confirmation="master-confirm",
        access_token="a" * 32,
    )
    parsed = parse_vk_master_callback(
        _message_payload(secret=_SECRET),
        config=master,
    )
    assert parsed.kind.value == "REJECTED"


def test_client_path_does_not_import_master_command_flow() -> None:
    for rel in (
        "app/channels/vk_client_http.py",
        "app/services/vk_client_ingress.py",
        "app/services/vk_client_inbound.py",
    ):
        source = (_REPO / rel).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "vk_master" not in node.module
                assert "master_command" not in node.module
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "vk_master" not in alias.name
                    assert "master_command" not in alias.name
        assert "VkMasterAdapterService" not in source
        assert "MasterCommandFlowService" not in source


def test_webhook_path_isolated_from_master() -> None:
    assert VK_CLIENT_WEBHOOK_PATH == "/webhooks/vk/client"
    master_source = (_REPO / "app" / "main.py").read_text(encoding="utf-8")
    assert '"/webhooks/vk/master"' in master_source
    assert "VK_CLIENT_CALLBACK_ENABLED" in (
        _REPO / ".env.example"
    ).read_text(encoding="utf-8")
    assert "VK_MASTER_ADAPTER_ENABLED" in (
        _REPO / ".env.example"
    ).read_text(encoding="utf-8")


def test_main_registers_client_only_when_enabled() -> None:
    source = (_REPO / "app" / "main.py").read_text(encoding="utf-8")
    assert "_register_vk_client_route" in source
    assert "build_vk_client_router" in source
    assert "if not config.enabled:" in source
    # Master registration still present and separate.
    assert "_register_vk_master_route" in source
    assert "parse_vk_master_callback" in source


def test_ingress_schema_redacts_text() -> None:
    event = VkClientIngressEvent(
        external_event_id=f"vk-{_GROUP}-{_USER}-{_CMID}",
        external_conversation_id=f"vk-{_GROUP}-{_USER}",
        text="секретный текст клиента",
    )
    assert "секретный" not in repr(event)
    assert event.safe_envelope()["schema"] == "vk.client.ingress.v1"
    assert event.channel == IngressChannel.VK.value
    assert event.event_type == IngressEventType.VK_CLIENT_MESSAGE.value


def test_observer_inbound_forbids_crm_and_outbound_symbols() -> None:
    assert_no_client_outbound_path()
    inbound_src = (
        _REPO / "app" / "services" / "vk_client_inbound.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(inbound_src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    assert "app.services.amocrm_mirror" not in imported
    assert "app.services.amocrm_chat_projection" not in imported
    assert "app.repositories.reply_plans" not in imported
    # Call sites (not the static-guard string list) must stay absent.
    assert "enqueue_client_message_received(" not in inbound_src
    assert "enqueue_client_inbound_projection(" not in inbound_src
    assert "create_client_reply_plan(" not in inbound_src
    assert "create_internal_draft_outbox(" not in inbound_src


@pytest.mark.asyncio
async def test_handle_callback_acks_only_after_durable_accept() -> None:
    adapter = MagicMock(spec=VkClientIngressAdapter)
    adapter.accept = AsyncMock(
        return_value=MagicMock(accepted=True, duplicate=False)
    )
    result = await handle_vk_client_callback(
        _message_payload(),
        config=_cfg(),
        adapter=adapter,
    )
    assert result.body == "ok"
    assert result.status_code == 200
    adapter.accept.assert_awaited_once()
    event = adapter.accept.await_args.args[0]
    assert isinstance(event, VkClientIngressEvent)
    assert event.external_event_id == f"vk-{_GROUP}-{_USER}-{_CMID}"


@pytest.mark.asyncio
async def test_handle_callback_ignored_does_not_persist() -> None:
    adapter = MagicMock(spec=VkClientIngressAdapter)
    adapter.accept = AsyncMock()
    result = await handle_vk_client_callback(
        _message_payload(out=1),
        config=_cfg(),
        adapter=adapter,
    )
    assert result.body == "ok"
    adapter.accept.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_text_ignored_no_raise_no_persist_no_pii(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """P1 regression: text > max must IGNORED before Pydantic ValidationError."""

    marker = "PII_MARKER_OVERSIZE_UNIQUE_zz99"
    oversized = marker + ("Y" * VK_CLIENT_TEXT_MAX_LEN)
    assert len(oversized) > VK_CLIENT_TEXT_MAX_LEN

    parsed = parse_vk_client_callback(
        _message_payload(text=oversized),
        config=_cfg(),
    )
    assert parsed.kind is VkClientWebhookKind.IGNORED
    assert parsed.message is None

    adapter = MagicMock(spec=VkClientIngressAdapter)
    adapter.accept = AsyncMock()
    with caplog.at_level("DEBUG"):
        result = await handle_vk_client_callback(
            _message_payload(text=oversized),
            config=_cfg(),
            adapter=adapter,
        )
    assert result.body == "ok"
    assert result.status_code == 200
    adapter.accept.assert_not_awaited()
    joined = "\n".join(
        f"{rec.getMessage()}\n{rec.exc_text or ''}" for rec in caplog.records
    )
    assert marker not in joined
    assert marker not in repr(result)


@pytest.mark.asyncio
async def test_control_char_text_ignored_no_persist_no_pii(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "PII_MARKER_CTRL_UNIQUE_aa77"
    dirty = f"{marker}\x01tail"
    parsed = parse_vk_client_callback(
        _message_payload(text=dirty),
        config=_cfg(),
    )
    assert parsed.kind is VkClientWebhookKind.IGNORED
    assert parsed.message is None

    adapter = MagicMock(spec=VkClientIngressAdapter)
    adapter.accept = AsyncMock()
    with caplog.at_level("DEBUG"):
        result = await handle_vk_client_callback(
            _message_payload(text=dirty),
            config=_cfg(),
            adapter=adapter,
        )
    assert result.body == "ok"
    adapter.accept.assert_not_awaited()
    joined = "\n".join(
        f"{rec.getMessage()}\n{rec.exc_text or ''}" for rec in caplog.records
    )
    assert marker not in joined


def test_altered_body_duplicate_match_raises_conflict() -> None:
    event = VkClientIngressEvent(
        external_event_id=f"vk-{_GROUP}-{_USER}-{_CMID}",
        external_conversation_id=f"vk-{_GROUP}-{_USER}",
        text="original client body",
    )
    row = MagicMock()
    row.channel = IngressChannel.VK.value
    row.event_type = IngressEventType.VK_CLIENT_MESSAGE.value
    row.external_event_id = event.external_event_id
    row.external_conversation_id = event.external_conversation_id
    row.envelope_json = event.safe_envelope()
    _assert_vk_duplicate_matches(row, event)

    altered = dict(row.envelope_json)
    altered["text"] = "mutated client body"
    row.envelope_json = altered
    with pytest.raises(VkClientIngressIdempotencyConflict):
        _assert_vk_duplicate_matches(row, event)


@pytest.mark.asyncio
async def test_altered_body_callback_returns_409() -> None:
    adapter = MagicMock(spec=VkClientIngressAdapter)
    adapter.accept = AsyncMock(side_effect=VkClientIngressIdempotencyConflict())
    result = await handle_vk_client_callback(
        _message_payload(text="mutated client body"),
        config=_cfg(),
        adapter=adapter,
    )
    assert result.status_code == 409
    assert result.body == "conflict"
    adapter.accept.assert_awaited_once()


def _vk_claim(*, text: str = "client hello") -> IngressClaim:
    from datetime import datetime, timezone

    return IngressClaim(
        event_id=uuid4(),
        channel="vk",
        external_event_id=f"vk-{_GROUP}-{_USER}-{_CMID}",
        external_conversation_id=f"vk-{_GROUP}-{_USER}",
        event_type=IngressEventType.VK_CLIENT_MESSAGE.value,
        status="PROCESSING",
        attempt_count=1,
        max_attempts=5,
        lease_owner="test-worker",
        lease_token=uuid4(),
        lease_version=1,
        lease_until=datetime.now(timezone.utc),
        correlation_id=uuid4(),
        envelope_json={
            "schema": "vk.client.ingress.v1",
            "event_type": "VK_CLIENT_MESSAGE",
            "text": text,
        },
    )


@pytest.mark.asyncio
async def test_worker_vk_path_returns_inbox_conversation_no_outbox() -> None:
    claim = _vk_claim()
    conversation_id = uuid4()
    inbox_id = uuid4()

    accept = MagicMock()
    accept.duplicate = False
    accept.inbox.id = inbox_id
    accept.conversation.id = conversation_id

    completed = MagicMock()
    completed.id = claim.event_id
    completed.status = "PROCESSED"

    worker = IngressWorker(
        MagicMock(),
        worker_id="test-worker",
    )

    with (
        patch("app.services.ingress.session_scope") as scope_cm,
        patch(
            "app.services.ingress.VkClientInboundService"
        ) as inbound_cls,
        patch(
            "app.services.ingress.ingress_repo.complete_with_lease",
            new_callable=AsyncMock,
            return_value=completed,
        ),
    ):
        session = MagicMock()
        scope_cm.return_value.__aenter__ = AsyncMock(return_value=session)
        scope_cm.return_value.__aexit__ = AsyncMock(return_value=None)
        inbound_cls.return_value.accept = AsyncMock(return_value=accept)

        result = await worker.process_claimed(claim)

    assert isinstance(result, IngressProcessResult)
    assert result.conversation_id == conversation_id
    assert result.inbox_id == inbox_id
    assert result.outbox_id is None
    assert result.duplicate_business is False
    inbound_cls.assert_called_once()
    src = inspect.getsource(worker._process_vk_client)
    assert "VkClientInboundService" in src
    assert "enqueue_client_message_received" not in src


@pytest.mark.asyncio
async def test_worker_vk_duplicate_business_flag() -> None:
    claim = _vk_claim()
    accept = MagicMock()
    accept.duplicate = True
    accept.inbox.id = uuid4()
    accept.conversation.id = uuid4()
    completed = MagicMock()
    completed.id = claim.event_id
    completed.status = "PROCESSED"

    worker = IngressWorker(MagicMock(), worker_id="test-worker")
    with (
        patch("app.services.ingress.session_scope") as scope_cm,
        patch(
            "app.services.ingress.VkClientInboundService"
        ) as inbound_cls,
        patch(
            "app.services.ingress.ingress_repo.complete_with_lease",
            new_callable=AsyncMock,
            return_value=completed,
        ),
    ):
        session = MagicMock()
        scope_cm.return_value.__aenter__ = AsyncMock(return_value=session)
        scope_cm.return_value.__aexit__ = AsyncMock(return_value=None)
        inbound_cls.return_value.accept = AsyncMock(return_value=accept)
        result = await worker.process_claimed(claim)

    assert result.duplicate_business is True


@pytest.mark.asyncio
async def test_shadow_hook_once_for_new_vk_then_skip_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-style worker tick: new VK delivery → shadow once; retry → none."""

    from app.services import worker_runtime as worker_runtime_mod
    from app.services.shadow_draft_generation import ShadowDraftGenerationService

    conversation_id = uuid4()
    inbox_id = uuid4()
    calls: list[object] = []

    class _Ingress:
        def __init__(self) -> None:
            self._n = 0

        async def claim_one(self) -> object | None:
            self._n += 1
            if self._n > 2:
                return None
            return object()

        async def process_claimed(self, claim: object) -> IngressProcessResult:
            # First delivery new; second is Callback retry / duplicate business.
            duplicate = self._n > 1
            return IngressProcessResult(
                event_id=uuid4(),
                status="PROCESSED",
                duplicate_business=duplicate,
                inbox_id=inbox_id,
                outbox_id=None,
                conversation_id=conversation_id,
            )

    async def _hook(**kwargs: object) -> None:
        calls.append(kwargs)

    shadow = ShadowDraftGenerationService(
        port=None,
        shadow_feature_enabled=True,
        allow_under_emergency_lock=True,
    )

    # Reconstruct the same condition as worker_runtime.ingress_tick.
    ingress = _Ingress()
    for _ in range(2):
        claim = await ingress.claim_one()
        assert claim is not None
        result = await ingress.process_claimed(claim)
        if (
            shadow.shadow_feature_enabled
            and result.conversation_id is not None
            and result.inbox_id is not None
            and not result.duplicate_business
        ):
            await _hook(conversation_id=result.conversation_id)

    assert len(calls) == 1
    assert calls[0]["conversation_id"] == conversation_id

    # Source-level: worker still uses the shared post-ingress condition.
    source = inspect.getsource(worker_runtime_mod)
    assert "run_shadow_draft_after_client_inbound" in source
    assert "duplicate_business" in source


def test_compose_vk_client_api_only_not_worker() -> None:
    import yaml

    compose = yaml.safe_load(
        (_REPO / "docker-compose.yml").read_text(encoding="utf-8")
    )
    api_env = compose["services"]["api"]["environment"]
    worker_env = compose["services"]["worker"]["environment"]
    assert api_env["VK_CLIENT_CALLBACK_ENABLED"] == (
        "${VK_CLIENT_CALLBACK_ENABLED:-false}"
    )
    assert "VK_CLIENT_CALLBACK_SECRET" in api_env
    for key in (
        "VK_CLIENT_CALLBACK_ENABLED",
        "VK_CLIENT_CALLBACK_SECRET",
        "VK_CLIENT_CONFIRMATION",
    ):
        assert key not in worker_env
    # Worker receives outbound send vars (not callback secret).
    assert "VK_CLIENT_OUTBOUND_ENABLED" in worker_env
    assert "VK_CLIENT_ACCESS_TOKEN" in worker_env
    assert "VK_CLIENT_ACCESS_TOKEN" not in api_env


def test_vk_master_adapter_unchanged_entry() -> None:
    # Master service still exists and is what /webhooks/vk/master uses.
    assert callable(VkMasterAdapterService)
    source = (_REPO / "app" / "main.py").read_text(encoding="utf-8")
    assert "VkMasterAdapterService" in source
    assert source.index("_register_vk_master_route") < source.index(
        "_register_vk_client_route"
    )


def test_idempotency_uses_stable_cmid_not_random() -> None:
    source = (
        _REPO / "app" / "channels" / "vk_client_webhook.py"
    ).read_text(encoding="utf-8")
    assert "conversation_message_id" in source
    assert "uuid4" not in source
    assert "time.time" not in source
    payload = _message_payload()
    assert payload["object"]["message"]["id"] == 9
    parsed = parse_vk_client_callback(payload, config=_cfg())
    assert parsed.message is not None
    assert parsed.message.external_event_id == f"vk-{_GROUP}-{_USER}-{_CMID}"
    assert parsed.message.external_event_id != f"vk-{_GROUP}-{_USER}-9"
