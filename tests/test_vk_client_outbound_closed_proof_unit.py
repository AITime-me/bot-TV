"""Unit proofs for VK_CLIENT_OUTBOUND closed single-conversation transport."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels.vk_client_outbound_config import (
    VkClientOutboundConfig,
    VkClientOutboundConfigError,
    VkClientPeerResolutionError,
    parse_vk_client_peer_id,
    vk_client_outbound_proof_allowed,
    vk_client_outbound_send_allowed,
)
from app.channels.vk_client_outbound_http import (
    NullVkClientSender,
    VkClientHttpSender,
    VkClientSendOutcome,
    VkClientSendResult,
    vk_client_random_id_from_outbound_id,
)
from app.config import BotMode, Settings
from app.core.outbound_policy import OutboundAction, is_automatic_outbound_allowed
from app.models.conversation import (
    ConversationOwnership,
    ConversationStatus,
    HandoffState,
)
from app.models.outbox import DeliveryStatus, DestinationType
from app.models.reply_plan import ReplyPlanStatus
from app.repositories.outbound import OutboundClaim
from app.services.outbound_arbiter import OutboundArbiter, OutboundArbiterDenied
from app.services.synthetic_outbound import SyntheticOutboundAdapter
from app.services.vk_client_outbound_proof import (
    is_vk_client_proof_reply_plan,
    maybe_create_vk_client_proof_reply_plan,
    vk_client_outbound_payload,
    vk_client_proof_reply_plan_payload,
)

_REPO = Path(__file__).resolve().parents[1]
_GROUP = 12345
_USER = 67890
_ALLOW = f"vk-{_GROUP}-{_USER}"
_FIXED_NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def _settings(*, mode: BotMode = BotMode.AUTO_WRITE, lock: bool = False) -> Settings:
    return Settings(bot_mode=mode, emergency_lock=lock)


def _full_env(**overrides: str) -> dict[str, str]:
    base = {
        "VK_CLIENT_OUTBOUND_ENABLED": "true",
        "VK_CLIENT_ACCESS_TOKEN": "x" * 20,
        "VK_CLIENT_OUTBOUND_ALLOW_CONVERSATION": _ALLOW,
        "VK_CLIENT_OUTBOUND_PROOF_ENABLED": "true",
        "VK_CLIENT_OUTBOUND_PROOF_TRIGGER": "PROOF_TRIGGER",
        "VK_CLIENT_OUTBOUND_PROOF_REPLY": "PROOF_REPLY_OK",
        "VK_CLIENT_GROUP_ID": str(_GROUP),
        "VK_CLIENT_OUTBOUND_PROVENANCE_KEY": "prov-key-01234567",
    }
    base.update(overrides)
    return base


def _fake_session_scope(session: AsyncMock):
    class _Scope:
        async def __aenter__(self) -> AsyncMock:
            return session

        async def __aexit__(self, *args: object) -> None:
            return None

    def _factory(*_a: object, **_k: object) -> _Scope:
        return _Scope()

    return _factory


def test_outbound_config_defaults_fail_closed() -> None:
    cfg = VkClientOutboundConfig.from_env({})
    assert cfg.outbound_enabled is False
    assert cfg.proof_enabled is False
    assert cfg.access_token is None


def test_outbound_enabled_incomplete_fails() -> None:
    with pytest.raises(VkClientOutboundConfigError):
        VkClientOutboundConfig.from_env(
            {
                "VK_CLIENT_OUTBOUND_ENABLED": "true",
                "VK_CLIENT_GROUP_ID": str(_GROUP),
            }
        )


def test_malformed_allow_conversation_rejected() -> None:
    with pytest.raises(VkClientOutboundConfigError):
        VkClientOutboundConfig.from_env(
            _full_env(VK_CLIENT_OUTBOUND_ALLOW_CONVERSATION="not-a-vk-id")
        )


def test_group_mismatch_rejected() -> None:
    with pytest.raises(VkClientOutboundConfigError):
        VkClientOutboundConfig.from_env(
            _full_env(
                VK_CLIENT_OUTBOUND_ALLOW_CONVERSATION=f"vk-999-{_USER}"
            )
        )


def test_send_gate_requires_auto_write_and_allowlist() -> None:
    cfg = VkClientOutboundConfig.from_env(_full_env())
    assert (
        vk_client_outbound_send_allowed(
            _settings(mode=BotMode.AUTO_WRITE),
            cfg,
            external_conversation_id=_ALLOW,
        )
        is True
    )
    assert (
        vk_client_outbound_send_allowed(
            _settings(mode=BotMode.AUTO_READ),
            cfg,
            external_conversation_id=_ALLOW,
        )
        is False
    )
    assert (
        vk_client_outbound_send_allowed(
            _settings(mode=BotMode.OFF),
            cfg,
            external_conversation_id=_ALLOW,
        )
        is False
    )
    assert (
        vk_client_outbound_send_allowed(
            _settings(lock=True),
            cfg,
            external_conversation_id=_ALLOW,
        )
        is False
    )
    assert (
        vk_client_outbound_send_allowed(
            _settings(),
            cfg,
            external_conversation_id=f"vk-{_GROUP}-111",
        )
        is False
    )


def test_proof_gate_exact_trigger() -> None:
    cfg = VkClientOutboundConfig.from_env(_full_env())
    assert (
        vk_client_outbound_proof_allowed(
            _settings(),
            cfg,
            external_conversation_id=_ALLOW,
            inbound_text="PROOF_TRIGGER",
        )
        is True
    )
    assert (
        vk_client_outbound_proof_allowed(
            _settings(),
            cfg,
            external_conversation_id=_ALLOW,
            inbound_text="other",
        )
        is False
    )
    assert (
        vk_client_outbound_proof_allowed(
            _settings(),
            cfg,
            external_conversation_id=_ALLOW,
            inbound_text="PROOF_TRIGGER ",
        )
        is False
    )
    off = VkClientOutboundConfig.from_env(
        _full_env(VK_CLIENT_OUTBOUND_PROOF_ENABLED="false")
    )
    assert (
        vk_client_outbound_proof_allowed(
            _settings(),
            off,
            external_conversation_id=_ALLOW,
            inbound_text="PROOF_TRIGGER",
        )
        is False
    )


def test_peer_parse_ok_and_failures() -> None:
    assert parse_vk_client_peer_id(
        external_conversation_id=_ALLOW,
        expected_group_id=_GROUP,
    ) == _USER
    with pytest.raises(VkClientPeerResolutionError):
        parse_vk_client_peer_id(
            external_conversation_id=f"vk-1-{_USER}",
            expected_group_id=_GROUP,
        )
    with pytest.raises(VkClientPeerResolutionError):
        parse_vk_client_peer_id(
            external_conversation_id="vk-abc-1",
            expected_group_id=_GROUP,
        )
    with pytest.raises(VkClientPeerResolutionError):
        parse_vk_client_peer_id(
            external_conversation_id=f"vk-{_GROUP}-0",
            expected_group_id=_GROUP,
        )
    with pytest.raises(VkClientPeerResolutionError):
        parse_vk_client_peer_id(
            external_conversation_id=f"vk-{_GROUP}--1",
            expected_group_id=_GROUP,
        )


def test_random_id_stable_and_not_python_hash() -> None:
    oid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    a = vk_client_random_id_from_outbound_id(oid)
    b = vk_client_random_id_from_outbound_id(oid)
    assert a == b
    assert 1 <= a <= 0x7FFFFFFF
    other = vk_client_random_id_from_outbound_id(uuid.uuid4())
    assert type(other) is int
    # Retry same outbound → identical random_id (idempotent VK send).
    assert vk_client_random_id_from_outbound_id(oid) == a


def test_sender_success_and_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = VkClientOutboundConfig.from_env(_full_env())
    sender = VkClientHttpSender(cfg)
    outbound_id = uuid.uuid4()
    seen_random: list[int] = []

    class _Resp:
        status = 200

        def read(self, _n: int) -> bytes:
            return json.dumps({"response": 1}).encode("utf-8")

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def _open(request: object, timeout: float = 0) -> _Resp:
        assert timeout > 0
        body = getattr(request, "data", b"") or b""
        decoded = body.decode("utf-8")
        assert "access_token=" in decoded
        assert "random_id=" in decoded
        assert "random_id=0&" not in decoded and not decoded.endswith("random_id=0")
        for part in decoded.split("&"):
            if part.startswith("random_id="):
                seen_random.append(int(part.split("=", 1)[1]))
        return _Resp()

    monkeypatch.setattr(sender._opener, "open", _open)
    ok = sender.send_text(peer_id=_USER, text="hi", outbound_id=outbound_id)
    assert ok.outcome is VkClientSendOutcome.SUCCESS
    assert seen_random == [vk_client_random_id_from_outbound_id(outbound_id)]

    # Same outbound_id on retry → same random_id.
    monkeypatch.setattr(sender._opener, "open", _open)
    sender.send_text(peer_id=_USER, text="hi", outbound_id=outbound_id)
    assert seen_random[0] == seen_random[1]

    class _BadJson:
        status = 200

        def read(self, _n: int) -> bytes:
            return b"{"

        def __enter__(self) -> _BadJson:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(sender._opener, "open", lambda *a, **k: _BadJson())
    bad = sender.send_text(peer_id=_USER, text="hi", outbound_id=outbound_id)
    assert bad.outcome is VkClientSendOutcome.PERMANENT_ERROR

    class _ApiErr:
        status = 200
        code: int = 6

        def read(self, _n: int) -> bytes:
            return json.dumps({"error": {"error_code": self.code}}).encode("utf-8")

        def __enter__(self) -> _ApiErr:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    err = _ApiErr()
    monkeypatch.setattr(sender._opener, "open", lambda *a, **k: err)
    transient = sender.send_text(peer_id=_USER, text="hi", outbound_id=outbound_id)
    assert transient.outcome is VkClientSendOutcome.TRANSIENT_ERROR

    err.code = 5
    permanent = sender.send_text(peer_id=_USER, text="hi", outbound_id=outbound_id)
    assert permanent.outcome is VkClientSendOutcome.PERMANENT_ERROR

    def _timeout(*_a: object, **_k: object) -> None:
        raise TimeoutError()

    monkeypatch.setattr(sender._opener, "open", _timeout)
    timed = sender.send_text(peer_id=_USER, text="hi", outbound_id=outbound_id)
    assert timed.outcome is VkClientSendOutcome.TRANSIENT_ERROR

    class _Huge:
        status = 200

        def read(self, n: int) -> bytes:
            return b"x" * (n)

        def __enter__(self) -> _Huge:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(sender._opener, "open", lambda *a, **k: _Huge())
    huge = sender.send_text(peer_id=_USER, text="hi", outbound_id=outbound_id)
    assert huge.outcome is VkClientSendOutcome.PERMANENT_ERROR

    rendered = repr(sender) + repr(cfg) + repr(ok) + repr(timed)
    assert "x" * 20 not in rendered
    assert "PROOF_REPLY" not in rendered
    assert "hi" not in rendered or "VkClientSendResult" in rendered


def test_null_sender_no_http() -> None:
    null = NullVkClientSender()
    result = null.send_text(
        peer_id=_USER, text="nope", outbound_id=uuid.uuid4()
    )
    assert result.outcome is VkClientSendOutcome.PERMANENT_ERROR


def test_proof_payload_helpers() -> None:
    plan = vk_client_proof_reply_plan_payload(text="hello")
    assert is_vk_client_proof_reply_plan(plan) is True
    assert is_vk_client_proof_reply_plan({"schema": "x"}) is False
    out = vk_client_outbound_payload(text="hello")
    assert out["schema"] == "vk.client.outbound.v1"
    assert out["text"] == "hello"


def test_destination_type_includes_vk() -> None:
    assert DestinationType.VK_CLIENT_OUTBOUND.value == "VK_CLIENT_OUTBOUND"
    assert {d.value for d in DestinationType} == {
        "INTERNAL_DRAFT",
        "SYNTHETIC_OUTBOUND",
        "VK_CLIENT_OUTBOUND",
    }


@pytest.mark.asyncio
async def test_proof_planner_skips_when_gates_fail() -> None:
    cfg = VkClientOutboundConfig.from_env(_full_env())
    conversation = MagicMock()
    conversation.channel = "vk"
    conversation.external_conversation_id = _ALLOW
    conversation.id = uuid.uuid4()
    conversation.context_version = 1
    conversation.manager_epoch = 0
    conversation.current_event_seq = 1
    inbox = MagicMock()

    # Wrong text / feature off / non-created inbox → None, no plan create.
    assert (
        await maybe_create_vk_client_proof_reply_plan(
            AsyncMock(),
            settings=_settings(),
            config=cfg,
            conversation=conversation,
            inbox=inbox,
            inbound_text="nope",
            created_inbox=True,
        )
        is None
    )
    assert (
        await maybe_create_vk_client_proof_reply_plan(
            AsyncMock(),
            settings=_settings(),
            config=cfg,
            conversation=conversation,
            inbox=inbox,
            inbound_text="PROOF_TRIGGER",
            created_inbox=False,
        )
        is None
    )
    conversation.external_conversation_id = f"vk-{_GROUP}-1"
    assert (
        await maybe_create_vk_client_proof_reply_plan(
            AsyncMock(),
            settings=_settings(),
            config=cfg,
            conversation=conversation,
            inbox=inbox,
            inbound_text="PROOF_TRIGGER",
            created_inbox=True,
        )
        is None
    )


def _vk_claim(
    *,
    delivery_status: str = DeliveryStatus.PROCESSING.value,
    external_allow: bool = True,
) -> OutboundClaim:
    plan_id = uuid.uuid4()
    return OutboundClaim(
        outbound_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        reply_plan_id=plan_id,
        context_version=1,
        manager_epoch=0,
        event_seq_hwm=1,
        idempotency_key=f"vk-client-outbound:reply-plan:{plan_id}",
        destination_type=DestinationType.VK_CLIENT_OUTBOUND.value,
        delivery_status=delivery_status,
        not_before=_FIXED_NOW - timedelta(seconds=1),
        attempt_count=1,
        max_attempts=5,
        lease_owner="unit-worker",
        lease_token=uuid.uuid4(),
        lease_version=1 if delivery_status == DeliveryStatus.PROCESSING.value else 2,
        lease_until=_FIXED_NOW + timedelta(seconds=30),
        correlation_id=uuid.uuid4(),
        payload_json=vk_client_outbound_payload(text="PROOF_REPLY_OK"),
    )


@pytest.mark.asyncio
async def test_arbiter_vk_no_sender_before_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP sender must not run until after durable ADMITTED commit."""

    cfg = VkClientOutboundConfig.from_env(_full_env())
    claim = _vk_claim()

    conversation = MagicMock()
    conversation.ownership = ConversationOwnership.BOT.value
    conversation.status = ConversationStatus.OPEN.value
    conversation.handoff_state = HandoffState.BOT_ACTIVE.value
    conversation.manager_takeover_at = None
    conversation.context_version = 1
    conversation.manager_epoch = 0
    conversation.current_event_seq = 1
    conversation.external_conversation_id = _ALLOW
    conversation.channel = "vk"

    outbound = MagicMock()
    outbound.id = claim.outbound_id
    outbound.conversation_id = claim.conversation_id
    outbound.reply_plan_id = claim.reply_plan_id
    outbound.destination_type = DestinationType.VK_CLIENT_OUTBOUND.value
    outbound.delivery_status = DeliveryStatus.PROCESSING.value
    outbound.admitted_at = None
    outbound.lease_token = claim.lease_token
    outbound.lease_version = claim.lease_version
    outbound.context_version = 1
    outbound.manager_epoch = 0
    outbound.event_seq_hwm = 1
    outbound.correlation_id = claim.correlation_id
    outbound.not_before = None
    outbound.payload_json = claim.payload_json

    plan = MagicMock()
    plan.status = ReplyPlanStatus.DISPATCHED.value
    plan.context_version = 1
    plan.manager_epoch = 0
    plan.event_seq_hwm = 1

    call_order: list[str] = []

    async def _mark_admitted(*_a: object, **_k: object) -> MagicMock:
        call_order.append("admitted")
        outbound.delivery_status = DeliveryStatus.ADMITTED.value
        outbound.admitted_at = _FIXED_NOW
        return outbound

    async def _mark_delivered(*_a: object, **_k: object) -> MagicMock:
        call_order.append("delivered")
        outbound.delivery_status = DeliveryStatus.DELIVERED.value
        return outbound

    class _Sender:
        def send_text(self, **kwargs: Any) -> VkClientSendResult:
            call_order.append("send")
            assert kwargs["outbound_id"] == claim.outbound_id
            assert kwargs["peer_id"] == _USER
            return VkClientSendResult(
                outcome=VkClientSendOutcome.SUCCESS,
                provider_message_id=424242,
            )

    session = AsyncMock()

    async def _get(model: type, oid: object, **_k: object) -> MagicMock:
        from app.models.outbox import OutboxMessage
        from app.models.reply_plan import ReplyPlan

        if model is OutboxMessage:
            return outbound
        if model is ReplyPlan:
            return plan
        raise AssertionError(f"unexpected get {model}")

    session.get = AsyncMock(side_effect=_get)
    session_factory = MagicMock()

    arbiter = OutboundArbiter(
        session_factory,
        settings=_settings(),
        sink=SyntheticOutboundAdapter(),
        vk_config=cfg,
        vk_sender=_Sender(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.session_scope",
        _fake_session_scope(session),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.resolve_moment",
        AsyncMock(return_value=_FIXED_NOW),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.conversation_repo.get_by_id_for_update",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.conversation_repo.lock_for_update",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.outbound_repo.mark_admitted_with_lease",
        AsyncMock(side_effect=_mark_admitted),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.outbound_repo.mark_delivered_with_lease",
        AsyncMock(side_effect=_mark_delivered),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.enqueue_outbound_delivered",
        AsyncMock(),
    )
    projection = AsyncMock()
    monkeypatch.setattr(
        "app.services.outbound_arbiter.enqueue_bot_outbound_projection",
        projection,
    )
    monkeypatch.setattr(
        "app.repositories.self_booking_active_offers.upsert_if_newer_or_same_outbound",
        AsyncMock(return_value="activated"),
    )

    result = await arbiter.admit_claimed(claim, now=_FIXED_NOW)
    assert result.admitted is True
    assert call_order == ["admitted", "send", "delivered"]
    projection.assert_not_awaited()


@pytest.mark.asyncio
async def test_arbiter_vk_gate_denied_no_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = VkClientOutboundConfig.from_env(_full_env())
    claim = _vk_claim(delivery_status=DeliveryStatus.ADMITTED.value)
    conversation = MagicMock()
    conversation.external_conversation_id = f"vk-{_GROUP}-999"
    outbound = MagicMock()
    outbound.id = claim.outbound_id
    outbound.conversation_id = claim.conversation_id
    outbound.reply_plan_id = claim.reply_plan_id
    outbound.destination_type = DestinationType.VK_CLIENT_OUTBOUND.value
    outbound.delivery_status = DeliveryStatus.ADMITTED.value
    outbound.admitted_at = _FIXED_NOW
    outbound.lease_token = claim.lease_token
    outbound.lease_version = claim.lease_version
    outbound.context_version = 1
    outbound.manager_epoch = 0
    outbound.event_seq_hwm = 1
    outbound.correlation_id = claim.correlation_id
    outbound.payload_json = claim.payload_json

    class _Sender:
        called = False

        def send_text(self, **kwargs: Any) -> VkClientSendResult:
            self.called = True
            return VkClientSendResult(
                outcome=VkClientSendOutcome.SUCCESS,
                provider_message_id=424242,
            )

    sender = _Sender()
    session = AsyncMock()
    session.get = AsyncMock(return_value=outbound)
    session_factory = MagicMock()
    arbiter = OutboundArbiter(
        session_factory,
        settings=_settings(),
        vk_config=cfg,
        vk_sender=sender,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.session_scope",
        _fake_session_scope(session),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.conversation_repo.lock_for_update",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.conversation_repo.get_by_id_for_update",
        AsyncMock(return_value=conversation),
    )
    fail = AsyncMock()
    monkeypatch.setattr(
        "app.services.outbound_arbiter.OutboundArbiter._fail_admitted_delivery",
        fail,
    )

    with pytest.raises(OutboundArbiterDenied):
        await arbiter.admit_claimed(claim, now=_FIXED_NOW)
    assert sender.called is False
    fail.assert_awaited()


def test_static_no_shadow_to_send_and_no_master_token_cross() -> None:
    shadow_paths = (
        _REPO / "app" / "services" / "shadow_draft_ingress_hook.py",
        _REPO / "app" / "services" / "shadow_draft_generation.py",
        _REPO / "app" / "core" / "shadow_draft_prompt.py",
    )
    for path in shadow_paths:
        source = path.read_text(encoding="utf-8")
        assert "VkClientHttpSender" not in source
        assert "insert_vk_client_outbound" not in source
        if path.name == "shadow_draft_ingress_hook.py":
            assert "create_client_reply_plan" not in source

    client_http = (_REPO / "app" / "channels" / "vk_client_outbound_http.py").read_text(
        encoding="utf-8"
    )
    assert "VK_MASTER" not in client_http
    assert "VkMaster" not in client_http
    assert "from app.channels.vk_master" not in client_http

    master_http = (_REPO / "app" / "channels" / "vk_master_http.py").read_text(
        encoding="utf-8"
    )
    assert "VK_CLIENT_ACCESS_TOKEN" not in master_http
    assert "VkClientOutbound" not in master_http

    webhook = (_REPO / "app" / "channels" / "vk_client_http.py").read_text(
        encoding="utf-8"
    )
    assert "VkClientHttpSender" not in webhook
    assert "messages.send" not in webhook

    inbound = (_REPO / "app" / "services" / "vk_client_inbound.py").read_text(
        encoding="utf-8"
    )
    assert "VkClientHttpSender" not in inbound
    assert ".create_client_reply_plan(" not in inbound
    assert "insert_vk_client_outbound" not in inbound

    arbiter = (_REPO / "app" / "services" / "outbound_arbiter.py").read_text(
        encoding="utf-8"
    )
    assert "VK_CLIENT_OUTBOUND" in arbiter
    assert "intentionally skips Chat" in arbiter

    # messages.send only in approved transport modules.
    allowed_send = {
        "vk_master_http.py",
        "vk_client_outbound_http.py",
        "vk_client_outbound_config.py",  # docstring only
        "vk_client_outbound_provenance.py",  # payload marker helper
    }
    for path in (_REPO / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "/method/messages.send" in text or 'method/messages.send"' in text:
            assert path.name in {"vk_master_http.py", "vk_client_outbound_http.py"}
        elif "messages.send" in text:
            assert path.name in allowed_send


def test_global_outbound_policy_still_fail_closed() -> None:
    assert (
        is_automatic_outbound_allowed(
            _settings(mode=BotMode.AUTO_WRITE),
            OutboundAction.SEND_MESSAGE,
        )
        is False
    )


def test_idempotency_key_stable() -> None:
    from app.repositories import outbound as outbound_repo

    plan_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert outbound_repo.vk_client_outbound_idempotency_key(plan_id) == (
        f"vk-client-outbound:reply-plan:{plan_id}"
    )
    assert outbound_repo.synthetic_outbound_idempotency_key(plan_id) == (
        f"synthetic-outbound:reply-plan:{plan_id}"
    )


def test_db_uuid_normalizes_asyncpg_subclass_for_claim_boundary() -> None:
    """Regression: driver UUID subclass must become stdlib UUID on OutboundClaim."""

    from app.repositories.outbound import _db_uuid, _row_to_claim

    std = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert type(_db_uuid(std)) is uuid.UUID
    assert _db_uuid(std) is std
    assert _db_uuid(str(std)) == std

    class _DriverUuid(uuid.UUID):
        """Stand-in for asyncpg.pgproto.UUID (subclass, not exact type)."""

    driver = _DriverUuid(str(std))
    assert type(driver) is not uuid.UUID
    assert isinstance(driver, uuid.UUID)
    coerced = _db_uuid(driver)
    assert type(coerced) is uuid.UUID
    assert coerced == std

    # Strict transport contract still holds after claim boundary normalization.
    assert vk_client_random_id_from_outbound_id(coerced) == (
        vk_client_random_id_from_outbound_id(std)
    )

    row = MagicMock()
    row.id = driver
    row.conversation_id = _DriverUuid(str(uuid.uuid4()))
    row.reply_plan_id = None
    row.context_version = 1
    row.manager_epoch = 0
    row.event_seq_hwm = 1
    row.idempotency_key = "vk-client-outbound:reply-plan:x"
    row.destination_type = DestinationType.VK_CLIENT_OUTBOUND.value
    row.delivery_status = DeliveryStatus.PROCESSING.value
    row.not_before = _FIXED_NOW
    row.attempt_count = 1
    row.max_attempts = 5
    row.lease_owner = "w"
    row.lease_token = _DriverUuid(str(uuid.uuid4()))
    row.lease_version = 1
    row.lease_until = _FIXED_NOW
    row.correlation_id = None
    row.payload_json = {"schema": "vk.client.outbound.v1", "text": "t"}

    claim = _row_to_claim(row)
    assert type(claim.outbound_id) is uuid.UUID
    assert type(claim.conversation_id) is uuid.UUID
    assert type(claim.lease_token) is uuid.UUID
    assert claim.outbound_id == std


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "not-a-uuid",
        " 12345678-1234-5678-1234-567812345678",
        12345,
        True,
        False,
        object(),
        b"12345678-1234-5678-1234-567812345678",
    ],
)
def test_db_uuid_rejects_invalid(bad: object) -> None:
    from app.repositories.outbound import _db_uuid

    with pytest.raises(RuntimeError, match="OUTBOUND_UUID_INVALID"):
        _db_uuid(bad)


def test_db_uuid_optional_none_and_subclass() -> None:
    from app.repositories.outbound import _db_uuid_optional

    assert _db_uuid_optional(None) is None

    std = uuid.UUID("12345678-1234-5678-1234-567812345678")

    class _DriverUuid(uuid.UUID):
        """Stand-in for asyncpg.pgproto.UUID (subclass, not exact type)."""

    driver = _DriverUuid(str(std))
    coerced = _db_uuid_optional(driver)
    assert coerced is not None
    assert type(coerced) is uuid.UUID
    assert coerced == std
