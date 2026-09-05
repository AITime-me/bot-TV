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
    VkReplyPayloadKind,
    build_vk_outbound_provenance_payload,
    classify_vk_reply_payload,
    verify_vk_outbound_provenance_payload,
)
from app.channels.vk_client_types import VkClientNormalizedMessageReply
from app.channels.vk_client_webhook import parse_vk_client_callback
from app.channels.vk_client_config import VkClientCallbackConfig
from app.models.conversation import (
    ConversationOwnership,
    ConversationStatus,
    HandoffState,
)
from app.models.outbox import DeliveryStatus
from app.services.vk_client_message_reply import (
    VkClientMessageReplyOwnEchoRace,
    VkClientReplyClassification,
    apply_vk_client_message_reply_in_session,
)

_GROUP = 154387737
_USER = 145508039
_CONV = f"vk-{_GROUP}-{_USER}"
_KEY = "callback-secret-01234567"
_SECRET = _KEY
_CONFIRM = "confirm-token-xx"
_NOW = datetime(2026, 9, 5, 15, 0, tzinfo=timezone.utc)
_NOW_TS = 1725530000


def _cfg() -> VkClientCallbackConfig:
    return VkClientCallbackConfig(
        enabled=True,
        group_id=_GROUP,
        callback_secret=_SECRET,
        confirmation=_CONFIRM,
    )


def _envelope(
    *,
    cmid: int = 3639,
    provider_id: int = 82727,
    provenance: dict[str, Any] | None = None,
    conv: str = _CONV,
) -> dict[str, Any]:
    if provenance is None:
        provenance = {"kind": "FOREIGN"}
    return {
        "schema": "vk.client.message_reply.v1",
        "event_type": "VK_CLIENT_MESSAGE_REPLY",
        "group_id": _GROUP,
        "peer_id": _USER,
        "conversation_message_id": cmid,
        "provider_message_id": provider_id,
        "occurred_at": _NOW.isoformat(),
        "external_conversation_id": conv,
        "random_id": 0,
        "provenance": provenance,
    }


def _bot_tv_provenance(outbound_id: uuid.UUID) -> dict[str, Any]:
    raw = json.loads(
        build_vk_outbound_provenance_payload(
            outbound_id=outbound_id,
            provenance_key=_KEY,
        )
    )
    return {
        "kind": "BOT_TV_CANDIDATE",
        "v": raw["v"],
        "ns": raw["ns"],
        "oid": raw["oid"],
        "mac": raw["mac"],
    }


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


def test_classify_foreign_payloads_never_keep_raw() -> None:
    cases = [
        {"known_event": True},
        {"name": "Hidden Client", "phone": "+70001112233"},
        "SECRET_FREE_TEXT_PII",
        {"nested": {"a": 1}},
        {"v": 1, "ns": "bot_tv.vk_out", "oid": str(uuid.uuid4()), "mac": "0" * 32, "x": 1},
    ]
    for raw in cases:
        technical = classify_vk_reply_payload(raw)
        assert technical.kind is VkReplyPayloadKind.FOREIGN
        fragment = technical.to_envelope_fragment()
        assert fragment == {"kind": "FOREIGN"}
        blob = json.dumps(fragment)
        assert "Hidden" not in blob
        assert "SECRET" not in blob
        assert "phone" not in blob
        assert "known_event" not in blob


def test_classify_bot_tv_candidate_allowlists_only() -> None:
    oid = uuid.uuid4()
    raw = build_vk_outbound_provenance_payload(
        outbound_id=oid,
        provenance_key=_KEY,
    )
    technical = classify_vk_reply_payload(raw)
    assert technical.kind is VkReplyPayloadKind.BOT_TV_CANDIDATE
    fragment = technical.to_envelope_fragment()
    assert set(fragment.keys()) == {"kind", "v", "ns", "oid", "mac"}
    assert verify_vk_outbound_provenance_payload(raw, provenance_key=_KEY) == oid


def test_parse_message_reply_drops_raw_foreign_payload() -> None:
    payload = {
        "type": "message_reply",
        "group_id": _GROUP,
        "secret": _SECRET,
        "object": {
            "id": 82727,
            "date": _NOW_TS,
            "from_id": -_GROUP,
            "peer_id": _USER,
            "out": 1,
            "conversation_message_id": 3639,
            "random_id": 0,
            "text": "MANAGER_TEXT_MUST_NOT_BE_STORED",
            "payload": {"known_event": True, "name": "Hidden"},
        },
    }
    parsed = parse_vk_client_callback(payload, config=_cfg())
    assert parsed.message_reply is not None
    reply = parsed.message_reply
    assert isinstance(reply, VkClientNormalizedMessageReply)
    assert reply.provenance.kind is VkReplyPayloadKind.FOREIGN
    envelope = reply.technical_envelope()
    assert "payload" not in envelope
    assert envelope["provenance"] == {"kind": "FOREIGN"}
    assert "Hidden" not in json.dumps(envelope)
    assert "MANAGER_TEXT" not in json.dumps(envelope)


@pytest.mark.asyncio
async def test_feature_off_no_fsm() -> None:
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


@pytest.mark.asyncio
async def test_foreign_payload_is_external_not_own_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    conversation = MagicMock()
    conversation.id = uuid.uuid4()
    conversation.status = ConversationStatus.OPEN.value
    conversation.ownership = ConversationOwnership.BOT.value
    conversation.handoff_state = HandoffState.BOT_ACTIVE.value
    conversation.vk_client_external_reply_hwm = None

    async def _get(*a: object, **k: object) -> MagicMock:
        return conversation

    async def _apply(*a: object, **k: object) -> tuple[MagicMock, bool]:
        return conversation, True

    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.get_by_channel_external",
        _get,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.lock_for_update",
        _get,
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
        envelope=_envelope(provenance={"kind": "FOREIGN"}),
        handoff_pause_seconds=900,
        takeover_config=cfg,
    )
    assert result.classification is VkClientReplyClassification.EXTERNAL_ACTOR
    assert result.fsm_changed is True


@pytest.mark.asyncio
async def test_valid_marker_exact_provider_id_is_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    outbound_id = uuid.uuid4()
    provider_id = 555001
    conversation = MagicMock()
    conversation.id = uuid.uuid4()
    conversation.status = ConversationStatus.OPEN.value

    row = MagicMock()
    row.id = outbound_id
    row.conversation_id = conversation.id
    row.provider_message_id = provider_id
    row.delivery_status = DeliveryStatus.DELIVERED.value

    async def _get(*a: object, **k: object) -> MagicMock:
        return conversation

    async def _by_id(*a: object, **k: object) -> MagicMock:
        return row

    apply_mock = AsyncMock()
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.get_by_channel_external",
        _get,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.lock_for_update",
        _get,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.outbound_repo.find_vk_outbound_by_id",
        _by_id,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.apply_chronologically_new_vk_external_reply",
        apply_mock,
    )

    cfg = VkClientExternalTakeoverConfig(
        mode=VkClientExternalTakeoverMode.ALL,
        provenance_key=_KEY,
    )
    result = await apply_vk_client_message_reply_in_session(
        session,
        envelope=_envelope(
            provider_id=provider_id,
            provenance=_bot_tv_provenance(outbound_id),
        ),
        handoff_pause_seconds=900,
        takeover_config=cfg,
    )
    assert result.classification is VkClientReplyClassification.OWN_TEYA_ECHO
    apply_mock.assert_not_called()


@pytest.mark.asyncio
async def test_valid_marker_mismatched_provider_id_is_external(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    outbound_id = uuid.uuid4()
    conversation = MagicMock()
    conversation.id = uuid.uuid4()
    conversation.status = ConversationStatus.OPEN.value
    conversation.vk_client_external_reply_hwm = None
    conversation.handoff_state = HandoffState.BOT_ACTIVE.value

    row = MagicMock()
    row.id = outbound_id
    row.provider_message_id = 111
    row.delivery_status = DeliveryStatus.DELIVERED.value

    async def _get(*a: object, **k: object) -> MagicMock:
        return conversation

    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.get_by_channel_external",
        _get,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.lock_for_update",
        _get,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.outbound_repo.find_vk_outbound_by_id",
        AsyncMock(return_value=row),
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.apply_chronologically_new_vk_external_reply",
        AsyncMock(return_value=(conversation, True)),
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.reply_plan_repo.cancel_open_plans_for_takeover",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.outbound_repo.cancel_unadmitted_for_manager_message",
        AsyncMock(return_value=0),
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
        envelope=_envelope(
            provider_id=999,
            provenance=_bot_tv_provenance(outbound_id),
        ),
        handoff_pause_seconds=900,
        takeover_config=cfg,
    )
    assert result.classification is VkClientReplyClassification.EXTERNAL_ACTOR
    assert result.fsm_changed is True


@pytest.mark.asyncio
async def test_valid_marker_null_provider_admitted_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    outbound_id = uuid.uuid4()
    conversation = MagicMock()
    conversation.id = uuid.uuid4()
    conversation.status = ConversationStatus.OPEN.value

    row = MagicMock()
    row.id = outbound_id
    row.provider_message_id = None
    row.delivery_status = DeliveryStatus.ADMITTED.value

    async def _get(*a: object, **k: object) -> MagicMock:
        return conversation

    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.get_by_channel_external",
        _get,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.lock_for_update",
        _get,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.outbound_repo.find_vk_outbound_by_id",
        AsyncMock(return_value=row),
    )

    cfg = VkClientExternalTakeoverConfig(
        mode=VkClientExternalTakeoverMode.ALL,
        provenance_key=_KEY,
    )
    with pytest.raises(VkClientMessageReplyOwnEchoRace):
        await apply_vk_client_message_reply_in_session(
            session,
            envelope=_envelope(provenance=_bot_tv_provenance(outbound_id)),
            handoff_pause_seconds=900,
            takeover_config=cfg,
        )


@pytest.mark.asyncio
async def test_valid_marker_null_provider_non_inflight_external(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    outbound_id = uuid.uuid4()
    conversation = MagicMock()
    conversation.id = uuid.uuid4()
    conversation.status = ConversationStatus.OPEN.value
    conversation.vk_client_external_reply_hwm = None
    conversation.handoff_state = HandoffState.BOT_ACTIVE.value

    row = MagicMock()
    row.id = outbound_id
    row.provider_message_id = None
    row.delivery_status = DeliveryStatus.FAILED.value

    async def _get(*a: object, **k: object) -> MagicMock:
        return conversation

    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.get_by_channel_external",
        _get,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.lock_for_update",
        _get,
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.outbound_repo.find_vk_outbound_by_id",
        AsyncMock(return_value=row),
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.apply_chronologically_new_vk_external_reply",
        AsyncMock(return_value=(conversation, True)),
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.reply_plan_repo.cancel_open_plans_for_takeover",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.outbound_repo.cancel_unadmitted_for_manager_message",
        AsyncMock(return_value=0),
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
        envelope=_envelope(provenance=_bot_tv_provenance(outbound_id)),
        handoff_pause_seconds=900,
        takeover_config=cfg,
    )
    assert result.classification is VkClientReplyClassification.EXTERNAL_ACTOR


@pytest.mark.asyncio
async def test_provider_id_fallback_without_payload_is_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    conversation = MagicMock()
    conversation.id = uuid.uuid4()
    conversation.status = ConversationStatus.OPEN.value

    row = MagicMock()
    row.provider_message_id = 82727

    async def _get(*a: object, **k: object) -> MagicMock:
        return conversation

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
        AsyncMock(return_value=row),
    )
    apply_mock = AsyncMock()
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.conversation_repo.apply_chronologically_new_vk_external_reply",
        apply_mock,
    )

    cfg = VkClientExternalTakeoverConfig(
        mode=VkClientExternalTakeoverMode.ALL,
        provenance_key=_KEY,
    )
    result = await apply_vk_client_message_reply_in_session(
        session,
        envelope=_envelope(provenance={"kind": "ABSENT"}),
        handoff_pause_seconds=900,
        takeover_config=cfg,
    )
    assert result.classification is VkClientReplyClassification.OWN_TEYA_ECHO
    apply_mock.assert_not_called()


@pytest.mark.asyncio
async def test_absent_payload_admitted_without_receipt_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    conversation = MagicMock()
    conversation.id = uuid.uuid4()
    conversation.status = ConversationStatus.OPEN.value

    async def _get(*a: object, **k: object) -> MagicMock:
        return conversation

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
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.vk_client_message_reply.outbound_repo.has_admitted_vk_outbound_without_provider_id",
        AsyncMock(return_value=True),
    )

    cfg = VkClientExternalTakeoverConfig(
        mode=VkClientExternalTakeoverMode.ALL,
        provenance_key=_KEY,
    )
    with pytest.raises(VkClientMessageReplyOwnEchoRace):
        await apply_vk_client_message_reply_in_session(
            session,
            envelope=_envelope(provenance={"kind": "ABSENT"}),
            handoff_pause_seconds=900,
            takeover_config=cfg,
        )
