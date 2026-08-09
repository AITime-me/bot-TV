"""PostgreSQL behavioral tests for CURSOR-29 VK master adapter."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.vk_master_config import VkMasterAdapterConfig, connection_scope_for_group
from app.config import BotMode, Settings
from app.core.ephemeral_pii_types import (
    EphemeralPiiKind,
    EphemeralPiiPurpose,
    EphemeralPiiReference,
)
from app.core.master_command_remote import MasterMutationRemoteSuccess
from app.main import build_vk_master_adapter_service
from app.services.ephemeral_pii_store import EphemeralPiiStore
from app.services.master_channel_binding import MasterChannelBindingService
from app.services.vk_master_adapter import VkMasterAdapterService
from tests.pg_harness import truncate_foundation_tables

_MASTER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_GROUP = 2029
_SCOPE = connection_scope_for_group(_GROUP)
_SECRET = "vk-master-pg-secret-ok"
_CONFIRM = "vk-confirm-token"
_TOKEN = "b" * 32
_USER = 900100
_ACCOUNT = str(_USER)
_NOW_TS = int(datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc).timestamp())
_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
_PII_ENV = {
    "EPHEMERAL_PII_ACTIVE_KEY_ID": "VKPG1",
    "EPHEMERAL_PII_KEY_VKPG1": _KEY_B64,
}
_SLOT = (
    "bs1.11111111-1111-4111-8111-111111111111."
    f"{_MASTER}.2026-08-12.1500"
)
_BOOKING_TEXT = f"запись клиенту Иван +79991234567 {_SLOT}"
_PHONE = "+79991234567"
_NAME = "Иван"


class _ScriptedClient:
    def __init__(self) -> None:
        self.close_day_calls: list[dict[str, Any]] = []
        self.create_booking_calls: list[dict[str, Any]] = []

    def read_schedule(self, **kwargs: Any) -> Any:
        from app.core.master_command_remote import MasterScheduleRemoteSuccess

        return MasterScheduleRemoteSuccess(
            from_date_key=kwargs["from_date_key"],
            to_date_key=kwargs["to_date_key"],
            days=(),
        )

    def close_day(self, **kwargs: Any) -> MasterMutationRemoteSuccess:
        self.close_day_calls.append(kwargs)
        return MasterMutationRemoteSuccess(
            idempotent_replay=False, resource_kind="block"
        )

    def close_interval(self, **kwargs: Any) -> MasterMutationRemoteSuccess:
        return MasterMutationRemoteSuccess(
            idempotent_replay=False, resource_kind="block"
        )

    def create_booking(self, **kwargs: Any) -> MasterMutationRemoteSuccess:
        self.create_booking_calls.append(kwargs)
        return MasterMutationRemoteSuccess(
            idempotent_replay=False, resource_kind="booking"
        )


class _RecordingSender:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = False
        self.on_send: Any | None = None

    def send_text(self, *, peer_id: int, text: str) -> None:
        if self.on_send is not None:
            self.on_send(peer_id=peer_id, text=text)
        self.calls.append({"peer_id": peer_id, "text": text})
        if self.fail:
            raise RuntimeError("send failed")


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


def _payload(*, text: str, cmid: int, from_id: int = _USER) -> str:
    return json.dumps(
        {
            "type": "message_new",
            "group_id": _GROUP,
            "secret": _SECRET,
            "object": {
                "message": {
                    "id": cmid,
                    "conversation_message_id": cmid,
                    "date": _NOW_TS,
                    "from_id": from_id,
                    "peer_id": from_id,
                    "out": 0,
                    "text": text,
                }
            },
        }
    )


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    request: pytest.FixtureRequest,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    if request.node.get_closest_marker("no_foundation_row_cleanup"):
        yield
        return
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


async def _bind(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account: str = _ACCOUNT,
) -> None:
    from app.db.session import session_scope

    async with session_scope(session_factory) as session:
        await MasterChannelBindingService(session).bind(
            channel="vk",
            external_account_id=account,
            master_id=_MASTER,
            connection_scope=_SCOPE,
        )


def _adapter(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    client: _ScriptedClient,
    sender: _RecordingSender,
    settings: Settings | None = None,
    config: VkMasterAdapterConfig | None = None,
    pii_store: EphemeralPiiStore | None = None,
) -> VkMasterAdapterService:
    return VkMasterAdapterService(
        session_factory,
        settings=settings
        or Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=False),
        config=config or _cfg(),
        master_client=client,  # type: ignore[arg-type]
        pii_store=pii_store,
        sender=sender,
    )


def _adapter_via_production_wiring(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    client: _ScriptedClient,
    sender: _RecordingSender,
    environ: dict[str, str],
) -> VkMasterAdapterService:
    """Same PII wiring contract as ``app.main.build_vk_master_adapter_service``."""

    return build_vk_master_adapter_service(
        session_factory,
        settings=Settings(bot_mode=BotMode.AUTO_WRITE, emergency_lock=False),
        vk_config=_cfg(),
        master_client=client,
        sender=sender,
        environ=environ,
    )


@pytest.mark.asyncio
async def test_bound_command_confirmation_persisted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    client = _ScriptedClient()
    sender = _RecordingSender()
    adapter = _adapter(session_factory, client=client, sender=sender)
    result = await adapter.handle_callback(_payload(text="выходной завтра", cmid=1))
    assert result.body == "ok"
    assert client.close_day_calls == []
    assert len(sender.calls) == 1
    assert "подтверд" in sender.calls[0]["text"].lower() or "да" in sender.calls[0]["text"]

    async with session_factory() as session:
        state = await session.scalar(
            text(
                "SELECT state FROM master_command_pendings "
                "WHERE external_account_id = :acc AND inbound_message_id = '1'"
            ),
            {"acc": _ACCOUNT},
        )
        assert state == "AWAITING_CONFIRMATION"


@pytest.mark.asyncio
async def test_confirm_da_one_mutation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    client = _ScriptedClient()
    sender = _RecordingSender()
    adapter = _adapter(session_factory, client=client, sender=sender)
    await adapter.handle_callback(_payload(text="выходной завтра", cmid=10))
    await adapter.handle_callback(_payload(text="да", cmid=11))
    assert len(client.close_day_calls) == 1


@pytest.mark.asyncio
async def test_replay_same_external_message_id_no_second_mutation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    client = _ScriptedClient()
    sender = _RecordingSender()
    adapter = _adapter(session_factory, client=client, sender=sender)
    await adapter.handle_callback(_payload(text="выходной завтра", cmid=20))
    await adapter.handle_callback(_payload(text="да", cmid=21))
    sends_after_first = len(sender.calls)
    await adapter.handle_callback(_payload(text="да", cmid=21))
    assert len(client.close_day_calls) == 1
    assert len(sender.calls) == sends_after_first


@pytest.mark.asyncio
async def test_concurrent_duplicate_delivery_one_execution(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    client = _ScriptedClient()
    sender = _RecordingSender()
    adapter = _adapter(session_factory, client=client, sender=sender)
    await adapter.handle_callback(_payload(text="выходной завтра", cmid=30))
    sends_before = len(sender.calls)

    async def _confirm() -> None:
        await adapter.handle_callback(_payload(text="да", cmid=31))

    await asyncio.gather(_confirm(), _confirm(), _confirm())
    assert len(client.close_day_calls) == 1
    # Exactly one VK reply for the winning confirm delivery; losers silent.
    assert len(sender.calls) == sends_before + 1


@pytest.mark.asyncio
async def test_concurrent_confirmation_required_one_reply(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    client = _ScriptedClient()
    sender = _RecordingSender()
    adapter = _adapter(session_factory, client=client, sender=sender)

    async def _start() -> None:
        await adapter.handle_callback(_payload(text="выходной завтра", cmid=70))

    await asyncio.gather(_start(), _start(), _start())
    assert client.close_day_calls == []
    assert len(sender.calls) == 1
    async with session_factory() as session:
        count = await session.scalar(
            text(
                "SELECT count(*) FROM master_command_pendings "
                "WHERE inbound_message_id = '70' "
                "AND state = 'AWAITING_CONFIRMATION'"
            )
        )
        assert int(count or 0) == 1


@pytest.mark.asyncio
async def test_concurrent_distinct_confirm_messages_one_mutation_one_reply(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two different «да» deliveries race the claim; only the winner replies."""

    await _bind(session_factory)
    client = _ScriptedClient()
    sender = _RecordingSender()
    adapter = _adapter(session_factory, client=client, sender=sender)
    await adapter.handle_callback(_payload(text="выходной завтра", cmid=80))
    sends_before = len(sender.calls)

    async def _confirm_a() -> None:
        await adapter.handle_callback(_payload(text="да", cmid=81))

    async def _confirm_b() -> None:
        await adapter.handle_callback(_payload(text="да", cmid=82))

    await asyncio.gather(_confirm_a(), _confirm_b())
    assert len(client.close_day_calls) == 1
    assert len(sender.calls) == sends_before + 1


@pytest.mark.asyncio
async def test_cross_account_isolation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    other = 900200
    await _bind(session_factory, account=_ACCOUNT)
    await _bind(session_factory, account=str(other))
    client = _ScriptedClient()
    sender = _RecordingSender()
    adapter = _adapter(session_factory, client=client, sender=sender)
    await adapter.handle_callback(
        _payload(text="выходной завтра", cmid=40, from_id=_USER)
    )
    await adapter.handle_callback(
        _payload(text="выходной завтра", cmid=40, from_id=other)
    )
    async with session_factory() as session:
        count = await session.scalar(
            text(
                "SELECT count(*) FROM master_command_pendings "
                "WHERE inbound_message_id = '40' AND state = 'AWAITING_CONFIRMATION'"
            )
        )
        assert int(count or 0) == 2


@pytest.mark.asyncio
async def test_unbound_no_pending_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = _ScriptedClient()
    sender = _RecordingSender()
    adapter = _adapter(session_factory, client=client, sender=sender)
    await adapter.handle_callback(_payload(text="выходной завтра", cmid=50))
    assert sender.calls == []
    assert client.close_day_calls == []
    async with session_factory() as session:
        count = await session.scalar(
            text("SELECT count(*) FROM master_command_pendings")
        )
        assert int(count or 0) == 0


@pytest.mark.asyncio
async def test_send_failure_after_commit_keeps_terminal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    client = _ScriptedClient()
    sender = _RecordingSender()
    sender.fail = True
    adapter = _adapter(session_factory, client=client, sender=sender)
    await adapter.handle_callback(_payload(text="выходной завтра", cmid=60))
    await adapter.handle_callback(_payload(text="да", cmid=61))
    assert len(client.close_day_calls) == 1
    async with session_factory() as session:
        state = await session.scalar(
            text(
                "SELECT state FROM master_command_pendings "
                "WHERE inbound_message_id = '60'"
            )
        )
        assert state == "SUCCEEDED"
    # Retry same confirm callback must not remutate.
    sender.fail = False
    await adapter.handle_callback(_payload(text="да", cmid=61))
    assert len(client.close_day_calls) == 1


@pytest.mark.asyncio
async def test_create_booking_real_pii_store_via_vk_adapter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CREATE_BOOKING through C29 + production PII wiring + real EphemeralPiiStore."""

    await _bind(session_factory)
    client = _ScriptedClient()
    sender = _RecordingSender()
    adapter = _adapter_via_production_wiring(
        session_factory, client=client, sender=sender, environ=_PII_ENV
    )
    assert type(adapter._pii) is EphemeralPiiStore

    await adapter.handle_callback(_payload(text=_BOOKING_TEXT, cmid=90))
    # Commit-before-send: after handle returns, durable pending + reply both exist
    # (adapter exits session_scope/commit before send; unit test covers call order).
    assert len(sender.calls) == 1
    assert client.create_booking_calls == []

    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT state, safe_payload::text, phone_ref_token, name_ref_token, "
                    "pii_conversation_id, idempotency_key "
                    "FROM master_command_pendings WHERE inbound_message_id = '90'"
                )
            )
        ).one()
    state, payload_text, phone_tok, name_tok, conv, key = row
    assert state == "AWAITING_CONFIRMATION"
    assert phone_tok and name_tok and conv and key
    assert _PHONE not in payload_text
    assert _NAME not in payload_text
    assert "79991234567" not in payload_text
    assert _PHONE not in (phone_tok or "")
    assert _NAME not in (name_tok or "")

    store = adapter._pii
    assert store is not None
    phone_plain = await store.read_plaintext(
        EphemeralPiiReference.parse(phone_tok),
        conversation_id=uuid.UUID(str(conv)),
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.MASTER_BOOKING_CLIENT_WRITE,
    )
    name_plain = await store.read_plaintext(
        EphemeralPiiReference.parse(name_tok),
        conversation_id=uuid.UUID(str(conv)),
        kind=EphemeralPiiKind.CLIENT_NAME,
        purpose=EphemeralPiiPurpose.MASTER_BOOKING_CLIENT_WRITE,
    )
    assert phone_plain == _PHONE
    assert name_plain == _NAME

    await adapter.handle_callback(_payload(text="да", cmid=91))
    assert len(client.create_booking_calls) == 1
    assert client.create_booking_calls[0]["phone"] == _PHONE
    assert client.create_booking_calls[0]["client_name"] == _NAME
    assert client.create_booking_calls[0]["idempotency_key"] == key
    assert len(sender.calls) == 2

    async with session_factory() as session:
        terminal = await session.scalar(
            text(
                "SELECT state FROM master_command_pendings "
                "WHERE inbound_message_id = '90'"
            )
        )
        assert terminal == "SUCCEEDED"

    sends_after = len(sender.calls)
    await adapter.handle_callback(_payload(text="да", cmid=91))
    assert len(client.create_booking_calls) == 1
    assert len(sender.calls) == sends_after


@pytest.mark.asyncio
async def test_create_booking_partial_pii_wiring_unavailable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    client = _ScriptedClient()
    sender = _RecordingSender()
    adapter = _adapter_via_production_wiring(
        session_factory,
        client=client,
        sender=sender,
        environ={"EPHEMERAL_PII_ACTIVE_KEY_ID": "VKPG1"},
    )
    assert adapter._pii is None
    await adapter.handle_callback(_payload(text=_BOOKING_TEXT, cmid=92))
    assert client.create_booking_calls == []
    async with session_factory() as session:
        count = await session.scalar(
            text(
                "SELECT count(*) FROM master_command_pendings "
                "WHERE inbound_message_id = '92' AND command_kind = 'CREATE_BOOKING' "
                "AND state = 'AWAITING_CONFIRMATION'"
            )
        )
        assert int(count or 0) == 0
