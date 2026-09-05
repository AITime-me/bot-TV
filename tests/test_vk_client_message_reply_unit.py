"""Unit tests: VK message_reply own-echo vs external takeover."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels.vk_client_external_takeover_config import (
    VkClientExternalTakeoverConfig,
    VkClientExternalTakeoverConfigError,
    VkClientExternalTakeoverMode,
    load_vk_client_external_takeover_config,
)
from app.channels.vk_client_outbound_provenance import (
    build_vk_outbound_provenance_payload,
    verify_vk_outbound_provenance_payload,
)
from app.models.conversation import (
    ConversationOwnership,
    ConversationStatus,
    HandoffState,
)
from app.services.vk_client_message_reply import (
    VkClientMessageReplyOwnEchoRace,
    VkClientReplyClassification,
    apply_vk_client_message_reply_in_session,
)

_GROUP = 154387737
_USER = 145508039
_CONV = f"vk-{_GROUP}-{_USER}"
_KEY = "callback-secret-01234567"
_NOW = datetime(2026, 9, 5, 15, 0, tzinfo=timezone.utc)


def _envelope(
    *,
    cmid: int = 3639,
    provider_id: int = 82727,
    payload: object | None = {"known_event": True},
    conv: str = _CONV,
) -> dict[str, Any]:
    env: dict[str, Any] = {
        "schema": "vk.client.message_reply.v1",
        "event_type": "VK_CLIENT_MESSAGE_REPLY",
        "group_id": _GROUP,
        "peer_id": _USER,
        "conversation_message_id": cmid,
        "provider_message_id": provider_id,
        "occurred_at": _NOW.isoformat(),
        "external_conversation_id": conv,
        "random_id": 0,
    }
    if payload is not None:
        env["payload"] = payload
    return env


def test_config_default_off() -> None:
    cfg = load_vk_client_external_takeover_config({})
    assert cfg.mode is VkClientExternalTakeoverMode.OFF
    assert cfg.fsm_mutation_allowed(external_conversation_id=_CONV) is False


def test_config_all_requires_exact_all() -> None:
    cfg = load_vk_client_external_takeover_config(
        {
            "VK_CLIENT_EXTERNAL_TAKEOVER_MODE": "ALL",
            "VK_CLIENT_CALLBACK_SECRET": _KEY,
        }
    )
    assert cfg.mode is VkClientExternalTakeoverMode.ALL
    assert cfg.fsm_mutation_allowed(external_conversation_id=_CONV) is True


def test_config_all_requires_provenance_key() -> None:
    with pytest.raises(VkClientExternalTakeoverConfigError):
        load_vk_client_external_takeover_config(
            {"VK_CLIENT_EXTERNAL_TAKEOVER_MODE": "ALL"}
        )


def test_config_allowlist_fail_closed_incomplete() -> None:
    with pytest.raises(VkClientExternalTakeoverConfigError):
        load_vk_client_external_takeover_config(
            {"VK_CLIENT_EXTERNAL_TAKEOVER_MODE": "ALLOWLIST"}
        )


def test_config_allowlist_exact() -> None:
    cfg = load_vk_client_external_takeover_config(
        {
            "VK_CLIENT_EXTERNAL_TAKEOVER_MODE": "ALLOWLIST",
            "VK_CLIENT_EXTERNAL_TAKEOVER_ALLOWLIST": _CONV,
            "VK_CLIENT_CALLBACK_SECRET": _KEY,
        }
    )
    assert cfg.fsm_mutation_allowed(external_conversation_id=_CONV) is True
    assert cfg.fsm_mutation_allowed(external_conversation_id=f"vk-{_GROUP}-1") is False


def test_provenance_roundtrip_and_foreign_payload() -> None:
    oid = uuid.uuid4()
    raw = build_vk_outbound_provenance_payload(
        outbound_id=oid,
        provenance_key=_KEY,
    )
    assert verify_vk_outbound_provenance_payload(raw, provenance_key=_KEY) == oid
    assert (
        verify_vk_outbound_provenance_payload(
            {"known_event": True},
            provenance_key=_KEY,
        )
        is None
    )
    forged = json.loads(raw)
    forged["mac"] = "0" * 32
    assert (
        verify_vk_outbound_provenance_payload(forged, provenance_key=_KEY) is None
    )


@pytest.mark.asyncio
async def test_feature_off_no_fsm(monkeypatch: pytest.MonkeyPatch) -> None:
    session = AsyncMock()
    cfg = VkClientExternalTakeoverConfig(mode=VkClientExternalTakeoverMode.OFF)
    result = await apply_vk_client_message_reply_in_session(
        session,
        envelope=_envelope(),
        handoff_pause_seconds=900,
        takeover_config=cfg,
    )
    assert result.classification is VkClientReplyClassification.FEATURE_OFF
    assert result.fsm_changed is False


@pytest.mark.asyncio
async def test_unresolved_conversation_no_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()

    async def _missing(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.get_by_channel_external",
        _missing,
    )
    cfg = VkClientExternalTakeoverConfig(
        mode=VkClientExternalTakeoverMode.ALL,
        provenance_key=_KEY,
    )
    result = await apply_vk_client_message_reply_in_session(
        session,
        envelope=_envelope(),
        handoff_pause_seconds=900,
        takeover_config=cfg,
    )
    assert (
        result.classification is VkClientReplyClassification.UNRESOLVED_CONVERSATION
    )
    assert result.fsm_changed is False


@pytest.mark.asyncio
async def test_foreign_payload_is_external_not_own_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    conv_id = uuid.uuid4()
    conversation = MagicMock()
    conversation.id = conv_id
    conversation.status = ConversationStatus.OPEN.value
    conversation.ownership = ConversationOwnership.BOT.value
    conversation.handoff_state = HandoffState.BOT_ACTIVE.value
    conversation.vk_client_external_reply_hwm = None
    conversation.manager_epoch = 0
    conversation.context_version = 0

    async def _get(*a: object, **k: object) -> MagicMock:
        return conversation

    async def _lock(*a: object, **k: object) -> MagicMock:
        return conversation

    async def _none(*a: object, **k: object) -> None:
        return None

    async def _false(*a: object, **k: object) -> bool:
        return False

    async def _apply(*a: object, **k: object) -> tuple[MagicMock, bool]:
        conversation.ownership = ConversationOwnership.MANAGER.value
        conversation.status = ConversationStatus.HANDOFF.value
        conversation.handoff_state = HandoffState.HUMAN_ACTIVE.value
        return conversation, True

    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.get_by_channel_external",
        _get,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.lock_for_update",
        _lock,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.outbound_repo.find_vk_outbound_by_id",
        _none,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.outbound_repo.find_vk_outbound_by_provider_message_id",
        _none,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.outbound_repo.has_admitted_vk_outbound_without_provider_id",
        _false,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.apply_chronologically_new_vk_external_reply",
        _apply,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.reply_plan_repo.cancel_open_plans_for_takeover",
        AsyncMock(return_value=1),
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.outbound_repo.cancel_unadmitted_for_manager_message",
        AsyncMock(return_value=2),
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.enqueue_manager_takeover",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.db_statement_now",
        AsyncMock(return_value=_NOW),
    )

    cfg = VkClientExternalTakeoverConfig(
        mode=VkClientExternalTakeoverMode.ALL,
        provenance_key=_KEY,
    )
    result = await apply_vk_client_message_reply_in_session(
        session,
        envelope=_envelope(payload={"known_event": True}),
        handoff_pause_seconds=900,
        takeover_config=cfg,
    )
    assert result.classification is VkClientReplyClassification.EXTERNAL_ACTOR
    assert result.fsm_changed is True
    assert result.cancelled_plans == 1
    assert result.cancelled_outbound == 2


@pytest.mark.asyncio
async def test_own_provenance_suppresses_takeover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    conv_id = uuid.uuid4()
    outbound_id = uuid.uuid4()
    conversation = MagicMock()
    conversation.id = conv_id
    conversation.status = ConversationStatus.OPEN.value
    conversation.vk_client_external_reply_hwm = None

    row = MagicMock()
    row.id = outbound_id
    row.conversation_id = conv_id

    async def _get(*a: object, **k: object) -> MagicMock:
        return conversation

    async def _lock(*a: object, **k: object) -> MagicMock:
        return conversation

    async def _by_id(*a: object, **k: object) -> MagicMock:
        return row

    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.get_by_channel_external",
        _get,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.lock_for_update",
        _lock,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.outbound_repo.find_vk_outbound_by_id",
        _by_id,
    )
    apply_mock = AsyncMock()
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.apply_chronologically_new_vk_external_reply",
        apply_mock,
    )

    marker = build_vk_outbound_provenance_payload(
        outbound_id=outbound_id,
        provenance_key=_KEY,
    )
    cfg = VkClientExternalTakeoverConfig(
        mode=VkClientExternalTakeoverMode.ALL,
        provenance_key=_KEY,
    )
    result = await apply_vk_client_message_reply_in_session(
        session,
        envelope=_envelope(payload=json.loads(marker)),
        handoff_pause_seconds=900,
        takeover_config=cfg,
    )
    assert result.classification is VkClientReplyClassification.OWN_TEYA_ECHO
    assert result.fsm_changed is False
    apply_mock.assert_not_called()


@pytest.mark.asyncio
async def test_admitted_without_receipt_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    conversation = MagicMock()
    conversation.id = uuid.uuid4()
    conversation.status = ConversationStatus.OPEN.value

    async def _get(*a: object, **k: object) -> MagicMock:
        return conversation

    async def _none(*a: object, **k: object) -> None:
        return None

    async def _true(*a: object, **k: object) -> bool:
        return True

    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.get_by_channel_external",
        _get,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.lock_for_update",
        _get,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.outbound_repo.find_vk_outbound_by_provider_message_id",
        _none,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.outbound_repo.has_admitted_vk_outbound_without_provider_id",
        _true,
    )

    cfg = VkClientExternalTakeoverConfig(
        mode=VkClientExternalTakeoverMode.ALL,
        provenance_key=_KEY,
    )
    with pytest.raises(VkClientMessageReplyOwnEchoRace):
        await apply_vk_client_message_reply_in_session(
            session,
            envelope=_envelope(payload=None),
            handoff_pause_seconds=900,
            takeover_config=cfg,
        )
