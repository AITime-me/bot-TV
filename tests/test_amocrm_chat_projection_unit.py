"""AMO-01B1a Chat projection unit/static coverage (CLIENT_INBOUND only)."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.core.amocrm_chat_egress_config import (
    AmoCrmChatEgressConfig,
    AmoCrmChatEgressConfigError,
)
from app.core.amocrm_chat_egress_http import (
    AmoCrmChatEgressHttpClient,
    AmoCrmChatEgressOutcome,
    AmoCrmChatHistoryScan,
    build_amocrm_chat_signature,
    content_md5_hex,
    find_msgid_in_history_body,
    parse_history_page_for_msgid,
)
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.models.amocrm_message_projection import (
    AmocrmMessageProjection,
    AmocrmProjectionSourceKind,
    integration_msgid_for_source,
)
from app.schemas.amocrm_manager_ingress import AmoCrmChatWebhookPayload
from app.services.amocrm_chat_projection import (
    _load_source_text,
    chat_egress_enabled,
    enqueue_bot_outbound_projection,
    load_chat_egress_config_fail_closed,
)
from tests.docker_runtime_allowlist import (
    AMO01B1_DOCKER_RUNTIME_PATHS,
    EXPECTED_DOCKER_ALLOW_RULES,
    assert_canonical_docker_runtime_allowlist,
    dockerignore_lines,
    is_included_in_docker_build_context,
)

_REPO = Path(__file__).resolve().parents[1]
_SECRET = "s" * 32
_SCOPE = "scope-test-001"


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[S2sHttpRequest] = []
        self.responses: list[S2sHttpResponse] = []

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        if not self.responses:
            raise AssertionError("no fake response queued")
        return self.responses.pop(0)


def test_egress_default_off() -> None:
    config = AmoCrmChatEgressConfig.from_env({})
    assert config.enabled is False
    assert chat_egress_enabled({}) is False


def test_egress_enabled_missing_secret_or_scope_fail_closed() -> None:
    with pytest.raises(AmoCrmChatEgressConfigError, match="SECRET_REQUIRED"):
        AmoCrmChatEgressConfig.from_env({"AMOCRM_CHAT_EGRESS_ENABLED": "true"})
    with pytest.raises(AmoCrmChatEgressConfigError, match="SCOPE_REQUIRED"):
        AmoCrmChatEgressConfig.from_env(
            {
                "AMOCRM_CHAT_EGRESS_ENABLED": "true",
                "AMOCRM_CHAT_CHANNEL_SECRET": _SECRET,
            }
        )


def test_load_chat_egress_config_fail_closed_does_not_raise() -> None:
    config = load_chat_egress_config_fail_closed(
        {"AMOCRM_CHAT_EGRESS_ENABLED": "true"}
    )
    assert config.enabled is False


def test_egress_valid_config_redacts_secret() -> None:
    config = AmoCrmChatEgressConfig.from_env(
        {
            "AMOCRM_CHAT_EGRESS_ENABLED": "true",
            "AMOCRM_CHAT_CHANNEL_SECRET": _SECRET,
            "AMOCRM_CHAT_SCOPE_ID": _SCOPE,
        }
    )
    assert config.enabled is True
    assert _SECRET not in repr(config)
    assert _SCOPE not in repr(config)


def test_hmac_signature_and_body_taxonomy() -> None:
    body = b'{"event_type":"new_message"}'
    md5 = content_md5_hex(body)
    date = "Mon, 03 Oct 2020 15:11:21 +0000"
    path = f"/v2/origin/custom/{_SCOPE}"
    sig = build_amocrm_chat_signature(
        method="POST",
        content_md5=md5,
        content_type="application/json",
        date_header=date,
        path=path,
        channel_secret=_SECRET,
    )
    expected = hmac.new(
        _SECRET.encode("utf-8"),
        f"POST\n{md5}\napplication/json\n{date}\n{path}".encode("utf-8"),
        hashlib.sha1,
    ).hexdigest()
    assert sig == expected

    transport = _FakeTransport()
    config = AmoCrmChatEgressConfig(
        enabled=True,
        channel_secret=_SECRET,
        scope_id=_SCOPE,
    )
    client = AmoCrmChatEgressHttpClient(config, transport=transport)

    transport.responses.append(
        S2sHttpResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {"new_message": {"msgid": "amo-msg-1", "ref_id": "c" + "0" * 32}}
            ).encode("utf-8"),
        )
    )
    ok = client.send_silent_text(
        integration_msgid="c" + "0" * 32,
        integration_conversation_id="convhex",
        conversation_ref_id="chat-1",
        sender_id="cli1",
        sender_name="Client",
        text="hello",
        timestamp_unix=1,
    )
    assert ok.outcome is AmoCrmChatEgressOutcome.SUCCESS
    assert ok.amocrm_message_id == "amo-msg-1"
    assert b'"silent":true' in transport.calls[0].body
    assert b"hello" in transport.calls[0].body
    assert b'"conversation_id":"convhex"' in transport.calls[0].body
    assert b'"conversation_ref_id":"chat-1"' in transport.calls[0].body

    transport.responses.append(
        S2sHttpResponse(status_code=403, headers={}, body=b"{}")
    )
    permanent = client.send_silent_text(
        integration_msgid="c" + "1" * 32,
        integration_conversation_id="convhex",
        conversation_ref_id="chat-1",
        sender_id="cli1",
        sender_name="Client",
        text="x",
        timestamp_unix=1,
    )
    assert permanent.outcome is AmoCrmChatEgressOutcome.PERMANENT_ERROR

    transport.responses.append(
        S2sHttpResponse(status_code=503, headers={}, body=b"{}")
    )
    transient = client.send_silent_text(
        integration_msgid="c" + "2" * 32,
        integration_conversation_id="convhex",
        conversation_ref_id="chat-1",
        sender_id="cli1",
        sender_name="Client",
        text="x",
        timestamp_unix=1,
    )
    assert transient.outcome is AmoCrmChatEgressOutcome.TRANSIENT_ERROR


def test_official_history_json_parser_and_path() -> None:
    msgid = "c" + "a" * 32
    body = json.dumps(
        {
            "messages": [
                {
                    "message": {
                        "id": "amo-hist-1",
                        "client_id": msgid,
                        "type": "text",
                        "text": "hello",
                    }
                }
            ]
        }
    ).encode("utf-8")
    assert find_msgid_in_history_body(body, integration_msgid=msgid) == "amo-hist-1"
    page = parse_history_page_for_msgid(body, integration_msgid=msgid)
    assert page is not None
    assert page.found is True
    assert page.amocrm_message_id == "amo-hist-1"

    transport = _FakeTransport()
    config = AmoCrmChatEgressConfig(
        enabled=True,
        channel_secret=_SECRET,
        scope_id=_SCOPE,
    )
    client = AmoCrmChatEgressHttpClient(config, transport=transport)
    transport.responses.append(
        S2sHttpResponse(status_code=200, headers={}, body=body)
    )
    scan = client.scan_msgid_in_history(
        integration_conversation_id="integ-conv-1",
        integration_msgid=msgid,
    )
    assert scan.scan is AmoCrmChatHistoryScan.FOUND
    assert scan.amocrm_message_id == "amo-hist-1"
    assert "/chats/integ-conv-1/history?limit=50&offset=0" in transport.calls[0].url


def test_history_absence_proven_via_short_page() -> None:
    transport = _FakeTransport()
    config = AmoCrmChatEgressConfig(
        enabled=True,
        channel_secret=_SECRET,
        scope_id=_SCOPE,
    )
    client = AmoCrmChatEgressHttpClient(config, transport=transport)
    transport.responses.append(
        S2sHttpResponse(
            status_code=200,
            headers={},
            body=json.dumps({"messages": []}).encode("utf-8"),
        )
    )
    scan = client.scan_msgid_in_history(
        integration_conversation_id="integ-conv-1",
        integration_msgid="c" + "b" * 32,
    )
    assert scan.scan is AmoCrmChatHistoryScan.ABSENT


def test_webhook_lifts_message_conversation_client_id() -> None:
    payload = AmoCrmChatWebhookPayload.model_validate(
        {
            "amocrm_chat_id": "amo-chat-1",
            "message_id": "amo-msg-1",
            "provider_sequence": 1,
            "text": "hi",
            "message": {"conversation": {"client_id": "integ-conv-xyz"}},
        }
    )
    assert payload.conversation_client_id == "integ-conv-xyz"
    event = payload.to_ingress_event()
    assert event.conversation_client_id == "integ-conv-xyz"
    assert event.safe_envelope()["conversation_client_id"] == "integ-conv-xyz"


@pytest.mark.asyncio
async def test_bot_outbound_rejects_synthetic_token(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Outbox:
        payload_json = {"schema": "synthetic.outbound.v1", "synthetic_token": "SYNTHETIC_OK"}
        created_at = None

    class _Session:
        async def get(self, model: object, source_id: object) -> object:
            return _Outbox()

    text, _ts = await _load_source_text(
        _Session(),  # type: ignore[arg-type]
        source_kind=AmocrmProjectionSourceKind.BOT_OUTBOUND.value,
        source_id=uuid4(),
    )
    assert text == ""


@pytest.mark.asyncio
async def test_enqueue_bot_outbound_deferred_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AMOCRM_CHAT_EGRESS_ENABLED", "true")
    monkeypatch.setenv("AMOCRM_CHAT_CHANNEL_SECRET", _SECRET)
    monkeypatch.setenv("AMOCRM_CHAT_SCOPE_ID", _SCOPE)
    assert chat_egress_enabled() is True
    result = await enqueue_bot_outbound_projection(
        MagicMock(),  # type: ignore[arg-type]
        conversation_id=uuid4(),
        outbound_id=uuid4(),
        correlation_id=uuid4(),
        egress_enabled=True,
    )
    assert result is None


def test_outbound_arbiter_does_not_enqueue_bot_projection() -> None:
    source = (_REPO / "app" / "services" / "outbound_arbiter.py").read_text(
        encoding="utf-8"
    )
    assert "enqueue_bot_outbound_projection" not in source
    assert "amocrm_chat_projection" not in source


def test_projection_model_has_no_text_columns() -> None:
    cols = {c.name for c in AmocrmMessageProjection.__table__.columns}
    assert "text" not in cols
    assert "body" not in cols
    assert "payload_json" not in cols
    assert "amocrm_message_id" in cols
    assert "integration_msgid" in cols
    source_id = uuid4()
    msgid = integration_msgid_for_source(
        source_kind=AmocrmProjectionSourceKind.CLIENT_INBOUND,
        source_id=source_id,
    )
    assert msgid.startswith("c")
    assert len(msgid) == 33
    assert msgid == f"c{source_id.hex}"


def test_docker_allowlist_includes_amo01b1() -> None:
    assert_canonical_docker_runtime_allowlist()
    lines = dockerignore_lines(_REPO)
    for rel in AMO01B1_DOCKER_RUNTIME_PATHS:
        assert f"!{rel}" in EXPECTED_DOCKER_ALLOW_RULES
        assert is_included_in_docker_build_context(rel, lines, repo_root=_REPO) is True
        assert (_REPO / rel).is_file()


def test_worker_reuses_amocrm_mirror_loop_without_fsm_change() -> None:
    from app.services import worker_runtime as runtime
    from app.services.amocrm_chat_projection import AmocrmChatProjectionWorker

    source = inspect.getsource(runtime.build_default_loop_specs)
    assert "AmocrmChatProjectionWorker" in source
    assert "CrmRestMirrorAdapter" in source
    assert "AmoCrmMirrorRejected" in source
    assert "AMOCRM_MIRROR_LOOP" in source
    assert "handoff_pause_seconds" in source
    assert "load_chat_egress_config_fail_closed" in inspect.getsource(
        AmocrmChatProjectionWorker.__init__
    )
