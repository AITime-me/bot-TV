"""PostgreSQL behavioral / race tests for CURSOR-28 master commands."""

from __future__ import annotations

import asyncio
import base64
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ephemeral_pii_keys import EnvEphemeralPiiKeyProvider
from app.core.ephemeral_pii_types import (
    EphemeralPiiKind,
    EphemeralPiiPurpose,
    EphemeralPiiReference,
    EphemeralPiiTtlPolicy,
)
from app.core.master_command_http import MasterCommandHttpError
from app.core.master_command_remote import MasterMutationRemoteSuccess
from app.core.master_command_types import (
    EXECUTION_LEASE_SECONDS,
    MasterCommandFlowOutcome,
    MasterCommandKind,
    MasterCommandPendingState,
    build_master_command_envelope,
)
from app.db.session import session_scope
from app.repositories import master_command_pendings as pending_repo
from app.services.ephemeral_pii_store import EphemeralPiiStore
from app.services.master_channel_binding import MasterChannelBindingService
from app.services.master_command_flow import MasterCommandFlowService
from tests.pg_harness import truncate_foundation_tables

_MASTER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_ACCOUNT = "vk-user-28001"
_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
_SLOT = (
    "bs1.11111111-1111-4111-8111-111111111111."
    f"{_MASTER}.2026-08-12.1500"
)
_BOOKING_TEXT = f"запись клиенту Иван +79991234567 {_SLOT}"


def _pii_store(session_factory: async_sessionmaker[AsyncSession]) -> EphemeralPiiStore:
    return EphemeralPiiStore(
        session_factory=session_factory,
        key_provider=EnvEphemeralPiiKeyProvider(
            {
                "EPHEMERAL_PII_ACTIVE_KEY_ID": "TESTK1",
                "EPHEMERAL_PII_KEY_TESTK1": _KEY_B64,
            }
        ),
        ttl_policy=EphemeralPiiTtlPolicy(900),
    )


class _ScriptedClient:
    def __init__(
        self,
        *,
        close_day_results: list[Any] | None = None,
        create_booking_results: list[Any] | None = None,
    ) -> None:
        self.close_day_results = list(close_day_results or [])
        self.create_booking_results = list(create_booking_results or [])
        self.close_day_calls: list[dict[str, Any]] = []
        self.create_booking_calls: list[dict[str, Any]] = []
        self.read_schedule_calls = 0

    def read_schedule(self, **kwargs: Any) -> Any:
        self.read_schedule_calls += 1
        from app.core.master_command_remote import MasterScheduleRemoteSuccess

        return MasterScheduleRemoteSuccess(
            from_date_key=kwargs["from_date_key"],
            to_date_key=kwargs["to_date_key"],
            days=(
                {
                    "dateKey": kwargs["from_date_key"],
                    "appointments": [],
                    "scheduleBlocks": [],
                    "extraWorkWindows": [],
                },
            ),
        )

    def close_day(self, **kwargs: Any) -> MasterMutationRemoteSuccess:
        self.close_day_calls.append(kwargs)
        if not self.close_day_results:
            return MasterMutationRemoteSuccess(
                idempotent_replay=False, resource_kind="block"
            )
        item = self.close_day_results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close_interval(self, **kwargs: Any) -> MasterMutationRemoteSuccess:
        return MasterMutationRemoteSuccess(
            idempotent_replay=False, resource_kind="block"
        )

    def create_booking(self, **kwargs: Any) -> MasterMutationRemoteSuccess:
        self.create_booking_calls.append(kwargs)
        if not self.create_booking_results:
            return MasterMutationRemoteSuccess(
                idempotent_replay=False, resource_kind="booking"
            )
        item = self.create_booking_results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest_asyncio.fixture(autouse=True)
async def master_command_row_cleanup(
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


async def _bind(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(session_factory) as session:
        svc = MasterChannelBindingService(session)
        await svc.bind(
            channel="vk",
            external_account_id=_ACCOUNT,
            master_id=_MASTER,
        )


def _env(text: str, msg_id: str) -> Any:
    return build_master_command_envelope(
        channel="vk",
        external_account_id=_ACCOUNT,
        external_message_id=msg_id,
        text=text,
        occurred_at=_NOW,
    )


@pytest.mark.asyncio
async def test_migration_master_command_pendings(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        exists = await session.scalar(
            text("SELECT to_regclass('public.master_command_pendings') IS NOT NULL")
        )
        assert exists is True
        partial = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'uq_master_command_pendings_active_identity'"
            )
        )
        assert partial is not None
        assert "UNIQUE" in partial.upper()
        purpose = await session.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_ephemeral_pii_values_allowed_purpose'"
            )
        )
        assert purpose is not None
        assert "MASTER_BOOKING_CLIENT_WRITE" in purpose


@pytest.mark.asyncio
async def test_unbound_master_no_s2s(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = _ScriptedClient()
    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(session, master_client=client)
        result = await flow.handle(_env("выходной завтра", "msg-unbound"))
    assert result.outcome is MasterCommandFlowOutcome.BINDING_REQUIRED
    assert client.close_day_calls == []


@pytest.mark.asyncio
async def test_confirm_cancel_expire_and_da_without_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    client = _ScriptedClient()

    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(session, master_client=client, clock=lambda: _NOW)
        preview = await flow.handle(_env("выходной завтра", "msg-day-1"))
        assert preview.outcome is MasterCommandFlowOutcome.CONFIRMATION_REQUIRED
        assert preview.preview is not None
        assert preview.preview.date_key == "2026-08-11"
        assert _MASTER not in repr(preview)

        da = await flow.handle(_env("да", "msg-confirm-1"))
        assert da.outcome is MasterCommandFlowOutcome.SUCCESS
        assert len(client.close_day_calls) == 1
        key1 = client.close_day_calls[0]["idempotency_key"]

    # "да" without pending executes nothing
    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(session, master_client=client, clock=lambda: _NOW)
        orphan = await flow.handle(_env("да", "msg-orphan-da"))
        assert orphan.outcome is MasterCommandFlowOutcome.MANUAL_HELP
        assert len(client.close_day_calls) == 1

    # Cancel path
    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(session, master_client=client, clock=lambda: _NOW)
        await flow.handle(_env("выходной 12.08", "msg-day-2"))
        cancelled = await flow.handle(_env("отмена", "msg-cancel-1"))
        assert cancelled.outcome is MasterCommandFlowOutcome.CANCELLED
        assert len(client.close_day_calls) == 1

    # Expired confirm
    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(session, master_client=client, clock=lambda: _NOW)
        await flow.handle(_env("выходной 13.08", "msg-day-3"))
    later = _NOW + timedelta(hours=1)
    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(session, master_client=client, clock=lambda: later)
        expired = await flow.handle(_env("да", "msg-confirm-expired"))
        assert expired.outcome is MasterCommandFlowOutcome.MANUAL_HELP
        assert len(client.close_day_calls) == 1

    # Duplicate inbound does not re-mutate
    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(session, master_client=client, clock=lambda: _NOW)
        dup = await flow.handle(_env("выходной завтра", "msg-day-1"))
        assert dup.outcome in {
            MasterCommandFlowOutcome.CONFIRMATION_REQUIRED,
            MasterCommandFlowOutcome.SUCCESS,
            MasterCommandFlowOutcome.DUPLICATE_IGNORED,
        }
        # First inbound was confirmation; after success the mirror may differ —
        # critical invariant: no second S2S call.
        assert len(client.close_day_calls) == 1
        assert client.close_day_calls[0]["idempotency_key"] == key1


@pytest.mark.asyncio
async def test_second_pending_conflicts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    client = _ScriptedClient()
    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(session, master_client=client, clock=lambda: _NOW)
        first = await flow.handle(_env("выходной завтра", "msg-a"))
        assert first.outcome is MasterCommandFlowOutcome.CONFIRMATION_REQUIRED
        second = await flow.handle(_env("закрыть интервал 10.08 с 14:00 до 15:00", "msg-b"))
        assert second.outcome is MasterCommandFlowOutcome.CONFLICT
        assert client.close_day_calls == []


@pytest.mark.asyncio
async def test_schedule_read_no_confirm(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    client = _ScriptedClient()
    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(session, master_client=client, clock=lambda: _NOW)
        result = await flow.handle(_env("расписание", "msg-sched"))
    assert result.outcome is MasterCommandFlowOutcome.SUCCESS
    assert client.read_schedule_calls == 1
    assert client.close_day_calls == []


@pytest.mark.asyncio
async def test_concurrent_confirm_single_mutation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    client = _ScriptedClient()
    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(session, master_client=client, clock=lambda: _NOW)
        preview = await flow.handle(_env("выходной завтра", "msg-race-cmd"))
        assert preview.outcome is MasterCommandFlowOutcome.CONFIRMATION_REQUIRED

    async def _confirm(msg_id: str) -> MasterCommandFlowOutcome:
        async with session_scope(session_factory) as session:
            flow = MasterCommandFlowService(
                session, master_client=client, clock=lambda: _NOW
            )
            result = await flow.handle(_env("да", msg_id))
            return result.outcome

    outcomes = await asyncio.gather(
        _confirm("msg-race-c1"),
        _confirm("msg-race-c2"),
        _confirm("msg-race-c3"),
    )
    success = [o for o in outcomes if o is MasterCommandFlowOutcome.SUCCESS]
    assert len(success) == 1
    assert len(client.close_day_calls) == 1


@pytest.mark.asyncio
async def test_timeout_keeps_stable_idempotency_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    client = _ScriptedClient(
        close_day_results=[
            MasterCommandHttpError("TIMEOUT"),
            MasterMutationRemoteSuccess(
                idempotent_replay=False, resource_kind="block"
            ),
        ]
    )
    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(session, master_client=client, clock=lambda: _NOW)
        await flow.handle(_env("выходной завтра", "msg-to-cmd"))
        first = await flow.handle(_env("да", "msg-to-c1"))
        assert first.outcome is MasterCommandFlowOutcome.UNAVAILABLE
        assert len(client.close_day_calls) == 1
        key = client.close_day_calls[0]["idempotency_key"]
        second = await flow.handle(_env("да", "msg-to-c2"))
        assert second.outcome is MasterCommandFlowOutcome.SUCCESS
        assert len(client.close_day_calls) == 2
        assert client.close_day_calls[1]["idempotency_key"] == key


@pytest.mark.asyncio
async def test_cross_channel_isolation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = MasterChannelBindingService(session)
        await svc.bind(channel="vk", external_account_id=_ACCOUNT, master_id=_MASTER)
        await svc.bind(
            channel="max",
            external_account_id=_ACCOUNT,
            master_id=_MASTER,
        )
    client = _ScriptedClient()
    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(session, master_client=client, clock=lambda: _NOW)
        vk = await flow.handle(_env("выходной завтра", "msg-vk"))
        assert vk.outcome is MasterCommandFlowOutcome.CONFIRMATION_REQUIRED
        max_env = build_master_command_envelope(
            channel="max",
            external_account_id=_ACCOUNT,
            external_message_id="msg-max",
            text="выходной завтра",
            occurred_at=_NOW,
        )
        mx = await flow.handle(max_env)
        assert mx.outcome is MasterCommandFlowOutcome.CONFIRMATION_REQUIRED


@pytest.mark.asyncio
async def test_no_pii_in_pending_payload(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    client = _ScriptedClient()
    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(session, master_client=client, clock=lambda: _NOW)
        result = await flow.handle(
            _env("закрыть интервал 10.08 с 14:00 до 15:00", "msg-pii")
        )
        assert result.outcome is MasterCommandFlowOutcome.CONFIRMATION_REQUIRED
        row = await session.scalar(
            text(
                "SELECT safe_payload::text, phone_ref_token, name_ref_token, "
                "master_id FROM master_command_pendings "
                "WHERE inbound_message_id = 'msg-pii'"
            )
        )
        assert row is not None
        payload_text, phone_ref, name_ref, master_id = row
        assert "+7" not in payload_text
        assert "7999" not in payload_text
        assert phone_ref is None
        assert name_ref is None
        # master_id stored but must not leak via flow result
        assert master_id == _MASTER
        assert _MASTER not in repr(result)
        assert "14:00" in payload_text


async def _active_booking_row(
    session: AsyncSession,
) -> Any:
    return await session.scalar(
        text(
            "SELECT id, state, idempotency_key, phone_ref_token, name_ref_token, "
            "pii_conversation_id, command_version, command_kind, "
            "execution_lease_expires_at, result_code "
            "FROM master_command_pendings "
            "WHERE external_account_id = :acc AND command_kind = 'CREATE_BOOKING' "
            "AND state IN ('AWAITING_CONFIRMATION', 'AWAITING_CLARIFICATION', "
            "'EXECUTING') "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"acc": _ACCOUNT},
    )


async def _pii_still_readable(
    store: EphemeralPiiStore,
    *,
    phone_token: str,
    name_token: str,
    conversation_id: uuid.UUID,
) -> bool:
    try:
        phone = await store.read_plaintext(
            EphemeralPiiReference.parse(phone_token),
            conversation_id=conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.MASTER_BOOKING_CLIENT_WRITE,
        )
        name = await store.read_plaintext(
            EphemeralPiiReference.parse(name_token),
            conversation_id=conversation_id,
            kind=EphemeralPiiKind.CLIENT_NAME,
            purpose=EphemeralPiiPurpose.MASTER_BOOKING_CLIENT_WRITE,
        )
    except Exception:
        return False
    return phone == "+79991234567" and name == "Иван"


@pytest.mark.asyncio
async def test_create_booking_success_reads_pii_nondestructively_ttl_orphan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    pii = _pii_store(session_factory)
    client = _ScriptedClient()

    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(
            session, master_client=client, pii_store=pii, clock=lambda: _NOW
        )
        preview = await flow.handle(_env(_BOOKING_TEXT, "msg-cb-ok"))
        assert preview.outcome is MasterCommandFlowOutcome.CONFIRMATION_REQUIRED
        row = await _active_booking_row(session)
        assert row is not None
        _id, state, key, phone_tok, name_tok, conv, version, kind, *_rest = row
        assert state == "AWAITING_CONFIRMATION"
        assert kind == "CREATE_BOOKING"
        assert key is not None
        assert phone_tok and name_tok and conv
        assert await _pii_still_readable(
            pii,
            phone_token=phone_tok,
            name_token=name_tok,
            conversation_id=uuid.UUID(str(conv)),
        )
        # Non-destructive: second read still works before confirm.
        assert await _pii_still_readable(
            pii,
            phone_token=phone_tok,
            name_token=name_tok,
            conversation_id=uuid.UUID(str(conv)),
        )

        ok = await flow.handle(_env("да", "msg-cb-ok-da"))
        assert ok.outcome is MasterCommandFlowOutcome.SUCCESS
        assert len(client.create_booking_calls) == 1
        assert client.create_booking_calls[0]["idempotency_key"] == key
        assert client.create_booking_calls[0]["phone"] == "+79991234567"
        assert client.create_booking_calls[0]["client_name"] == "Иван"
        assert "+79991234567" not in repr(ok)
        assert "Иван" not in repr(ok)

    # No sync delete: ciphertext remains until TTL/maintenance (bounded orphan).
    assert await _pii_still_readable(
        pii,
        phone_token=phone_tok,
        name_token=name_tok,
        conversation_id=uuid.UUID(str(conv)),
    )
    async with session_factory() as session:
        terminal = await session.scalar(
            text(
                "SELECT state, phone_ref_token, name_ref_token, command_kind "
                "FROM master_command_pendings WHERE id = :id"
            ),
            {"id": _id},
        )
        assert terminal[0] == "SUCCEEDED"
        # Terminal row clears executable refs in the same UoW; not re-executed.
        assert terminal[1] is None
        assert terminal[2] is None
        mirror = await session.scalar(
            text(
                "SELECT command_kind, idempotency_key FROM master_command_pendings "
                "WHERE inbound_message_id = 'msg-cb-ok-da'"
            )
        )
        assert mirror is not None
        assert mirror[0] == "CREATE_BOOKING"
        assert mirror[1] == key


@pytest.mark.asyncio
async def test_create_booking_timeout_and_in_progress_keep_pii_and_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    pii = _pii_store(session_factory)
    client = _ScriptedClient(
        create_booking_results=[
            MasterCommandHttpError("TIMEOUT"),
            MasterCommandHttpError("IDEMPOTENCY_IN_PROGRESS"),
            MasterMutationRemoteSuccess(
                idempotent_replay=True, resource_kind="booking"
            ),
        ]
    )

    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(
            session, master_client=client, pii_store=pii, clock=lambda: _NOW
        )
        await flow.handle(_env(_BOOKING_TEXT, "msg-cb-to"))
        row = await _active_booking_row(session)
        assert row is not None
        key = row[2]
        phone_tok, name_tok, conv = row[3], row[4], row[5]

        first = await flow.handle(_env("да", "msg-cb-to-1"))
        assert first.outcome is MasterCommandFlowOutcome.UNAVAILABLE
        assert first.result_code == "TIMEOUT"
        row = await _active_booking_row(session)
        assert row is not None
        assert row[1] == "AWAITING_CONFIRMATION"
        assert row[2] == key
        assert row[3] == phone_tok
        assert row[4] == name_tok
        assert await _pii_still_readable(
            pii,
            phone_token=phone_tok,
            name_token=name_tok,
            conversation_id=uuid.UUID(str(conv)),
        )

        second = await flow.handle(_env("да", "msg-cb-to-2"))
        assert second.outcome is MasterCommandFlowOutcome.UNAVAILABLE
        assert second.result_code == "IDEMPOTENCY_IN_PROGRESS"
        assert await _pii_still_readable(
            pii,
            phone_token=phone_tok,
            name_token=name_tok,
            conversation_id=uuid.UUID(str(conv)),
        )

        third = await flow.handle(_env("да", "msg-cb-to-3"))
        assert third.outcome is MasterCommandFlowOutcome.SUCCESS
        assert len(client.create_booking_calls) == 3
        assert {c["idempotency_key"] for c in client.create_booking_calls} == {key}


@pytest.mark.asyncio
async def test_create_booking_crash_before_remote_keeps_pii(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    pii = _pii_store(session_factory)
    client = _ScriptedClient(
        create_booking_results=[
            RuntimeError("simulated crash after PII read"),
            MasterMutationRemoteSuccess(
                idempotent_replay=False, resource_kind="booking"
            ),
        ]
    )

    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(
            session, master_client=client, pii_store=pii, clock=lambda: _NOW
        )
        await flow.handle(_env(_BOOKING_TEXT, "msg-cb-crash-pre"))
        row = await _active_booking_row(session)
        assert row is not None
        key, phone_tok, name_tok, conv = row[2], row[3], row[4], row[5]

        first = await flow.handle(_env("да", "msg-cb-crash-pre-1"))
        assert first.outcome is MasterCommandFlowOutcome.UNAVAILABLE
        assert first.result_code == "INTERNAL_RETRYABLE"
        assert await _pii_still_readable(
            pii,
            phone_token=phone_tok,
            name_token=name_tok,
            conversation_id=uuid.UUID(str(conv)),
        )
        row = await _active_booking_row(session)
        assert row is not None
        assert row[1] == "AWAITING_CONFIRMATION"
        assert row[2] == key

        second = await flow.handle(_env("да", "msg-cb-crash-pre-2"))
        assert second.outcome is MasterCommandFlowOutcome.SUCCESS
        assert client.create_booking_calls[-1]["idempotency_key"] == key


@pytest.mark.asyncio
async def test_create_booking_expired_executing_reclaim_same_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Crash after remote / before local complete: expired EXECUTING → reclaim."""

    await _bind(session_factory)
    pii = _pii_store(session_factory)
    client = _ScriptedClient()

    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(
            session, master_client=client, pii_store=pii, clock=lambda: _NOW
        )
        await flow.handle(_env(_BOOKING_TEXT, "msg-cb-reclaim"))
        row = await _active_booking_row(session)
        assert row is not None
        row_id, _state, key, phone_tok, name_tok, conv, version, *_ = row
        lease_token = uuid.uuid4()
        # Simulate crash leaving EXECUTING with expired lease (remote may have won).
        await session.execute(
            text(
                "UPDATE master_command_pendings SET state = 'EXECUTING', "
                "execution_lease_token = :tok, "
                "execution_lease_expires_at = :exp, "
                "confirmation_expires_at = NULL, "
                "updated_at = :now "
                "WHERE id = :id"
            ),
            {
                "tok": lease_token,
                "exp": _NOW - timedelta(seconds=1),
                "now": _NOW,
                "id": row_id,
            },
        )
        await session.flush()

    later = _NOW + timedelta(seconds=EXECUTION_LEASE_SECONDS + 5)
    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(
            session, master_client=client, pii_store=pii, clock=lambda: later
        )
        assert await _pii_still_readable(
            pii,
            phone_token=phone_tok,
            name_token=name_tok,
            conversation_id=uuid.UUID(str(conv)),
        )
        result = await flow.handle(_env("да", "msg-cb-reclaim-da"))
        assert result.outcome is MasterCommandFlowOutcome.SUCCESS
        assert len(client.create_booking_calls) == 1
        assert client.create_booking_calls[0]["idempotency_key"] == key
        assert result.command_version == version


@pytest.mark.asyncio
async def test_create_booking_concurrent_confirm_single_mutation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    pii = _pii_store(session_factory)
    client = _ScriptedClient()

    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(
            session, master_client=client, pii_store=pii, clock=lambda: _NOW
        )
        preview = await flow.handle(_env(_BOOKING_TEXT, "msg-cb-race"))
        assert preview.outcome is MasterCommandFlowOutcome.CONFIRMATION_REQUIRED
        row = await _active_booking_row(session)
        assert row is not None
        key = row[2]

    async def _confirm(msg_id: str) -> MasterCommandFlowOutcome:
        async with session_scope(session_factory) as session:
            flow = MasterCommandFlowService(
                session, master_client=client, pii_store=pii, clock=lambda: _NOW
            )
            result = await flow.handle(_env("да", msg_id))
            return result.outcome

    outcomes = await asyncio.gather(
        _confirm("msg-cb-race-1"),
        _confirm("msg-cb-race-2"),
        _confirm("msg-cb-race-3"),
    )
    success = [o for o in outcomes if o is MasterCommandFlowOutcome.SUCCESS]
    assert len(success) == 1
    assert len(client.create_booking_calls) == 1
    assert client.create_booking_calls[0]["idempotency_key"] == key


@pytest.mark.asyncio
async def test_create_booking_concurrent_reclaim_one_executor(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    pii = _pii_store(session_factory)
    client = _ScriptedClient()

    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(
            session, master_client=client, pii_store=pii, clock=lambda: _NOW
        )
        await flow.handle(_env(_BOOKING_TEXT, "msg-cb-creclaim"))
        row = await _active_booking_row(session)
        assert row is not None
        row_id, _, key, *_ = row
        await session.execute(
            text(
                "UPDATE master_command_pendings SET state = 'EXECUTING', "
                "execution_lease_token = :tok, "
                "execution_lease_expires_at = :exp, "
                "confirmation_expires_at = NULL, updated_at = :now WHERE id = :id"
            ),
            {
                "tok": uuid.uuid4(),
                "exp": _NOW - timedelta(seconds=1),
                "now": _NOW,
                "id": row_id,
            },
        )
        await session.flush()

    later = _NOW + timedelta(seconds=5)

    async def _reclaim(msg_id: str) -> MasterCommandFlowOutcome:
        async with session_scope(session_factory) as session:
            flow = MasterCommandFlowService(
                session, master_client=client, pii_store=pii, clock=lambda: later
            )
            return (await flow.handle(_env("да", msg_id))).outcome

    outcomes = await asyncio.gather(
        _reclaim("msg-cb-creclaim-1"),
        _reclaim("msg-cb-creclaim-2"),
        _reclaim("msg-cb-creclaim-3"),
    )
    success = [o for o in outcomes if o is MasterCommandFlowOutcome.SUCCESS]
    assert len(success) == 1
    assert len(client.create_booking_calls) == 1
    assert client.create_booking_calls[0]["idempotency_key"] == key


@pytest.mark.asyncio
async def test_create_booking_repo_concurrent_reclaim_cas(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    pii = _pii_store(session_factory)
    client = _ScriptedClient()

    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(
            session, master_client=client, pii_store=pii, clock=lambda: _NOW
        )
        await flow.handle(_env(_BOOKING_TEXT, "msg-cb-cas"))
        row = await pending_repo.lock_active_by_identity(
            session,
            channel="vk",
            connection_scope="default",
            external_account_id=_ACCOUNT,
        )
        assert row is not None
        key = row.idempotency_key
        version = row.command_version
        # Force expired EXECUTING (crash window after remote / before local complete).
        await session.execute(
            text(
                "UPDATE master_command_pendings SET state = 'EXECUTING', "
                "execution_lease_token = :tok, "
                "execution_lease_expires_at = :exp WHERE id = :id"
            ),
            {
                "tok": uuid.uuid4(),
                "exp": _NOW - timedelta(seconds=1),
                "id": row.id,
            },
        )
        await session.flush()

    winners = {"count": 0}

    async def _try_reclaim() -> bool:
        async with session_scope(session_factory) as session:
            active = await pending_repo.lock_active_by_identity(
                session,
                channel="vk",
                connection_scope="default",
                external_account_id=_ACCOUNT,
            )
            assert active is not None
            ok = await pending_repo.reclaim_expired_execution(
                session,
                row=active,
                lease_token=uuid.uuid4(),
                lease_expires_at=_NOW + timedelta(seconds=60),
                expected_version=version,
                now=_NOW,
            )
            if ok:
                winners["count"] += 1
            return ok

    results = await asyncio.gather(_try_reclaim(), _try_reclaim(), _try_reclaim())
    assert sum(1 for r in results if r) == 1
    assert winners["count"] == 1
    async with session_factory() as session:
        state_key = await session.scalar(
            text(
                "SELECT state, idempotency_key FROM master_command_pendings "
                "WHERE inbound_message_id = 'msg-cb-cas'"
            )
        )
        assert state_key[0] == "EXECUTING"
        assert state_key[1] == key


@pytest.mark.asyncio
async def test_create_booking_definitive_failure_and_cancel_terminal_not_executable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    pii = _pii_store(session_factory)
    client = _ScriptedClient(
        create_booking_results=[
            MasterCommandHttpError("SLOT_NO_LONGER_AVAILABLE"),
        ]
    )

    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(
            session, master_client=client, pii_store=pii, clock=lambda: _NOW
        )
        await flow.handle(_env(_BOOKING_TEXT, "msg-cb-fail"))
        row = await _active_booking_row(session)
        assert row is not None
        phone_tok, name_tok, conv = row[3], row[4], row[5]
        failed = await flow.handle(_env("да", "msg-cb-fail-da"))
        assert failed.outcome is MasterCommandFlowOutcome.CONFLICT
        assert failed.result_code == "SLOT_NO_LONGER_AVAILABLE"

    # TTL orphan: ciphertext retained; terminal FAILED is not re-executed.
    assert await _pii_still_readable(
        pii,
        phone_token=phone_tok,
        name_token=name_tok,
        conversation_id=uuid.UUID(str(conv)),
    )
    async with session_factory() as session:
        state = await session.scalar(
            text(
                "SELECT state FROM master_command_pendings "
                "WHERE inbound_message_id = 'msg-cb-fail'"
            )
        )
        assert state == "FAILED"

    client2 = _ScriptedClient()
    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(
            session, master_client=client2, pii_store=pii, clock=lambda: _NOW
        )
        await flow.handle(_env(_BOOKING_TEXT, "msg-cb-cancel"))
        row = await _active_booking_row(session)
        assert row is not None
        phone_tok, name_tok, conv = row[3], row[4], row[5]
        cancelled = await flow.handle(_env("отмена", "msg-cb-cancel-x"))
        assert cancelled.outcome is MasterCommandFlowOutcome.CANCELLED
        # Cancelled is terminal: further «да» does not mutate.
        orphan = await flow.handle(_env("да", "msg-cb-cancel-da"))
        assert orphan.outcome is MasterCommandFlowOutcome.MANUAL_HELP
        assert client2.create_booking_calls == []
    assert await _pii_still_readable(
        pii,
        phone_token=phone_tok,
        name_token=name_tok,
        conversation_id=uuid.UUID(str(conv)),
    )


@pytest.mark.asyncio
async def test_create_booking_expiry_terminal_keeps_ttl_pii(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    pii = _pii_store(session_factory)
    client = _ScriptedClient()

    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(
            session, master_client=client, pii_store=pii, clock=lambda: _NOW
        )
        await flow.handle(_env(_BOOKING_TEXT, "msg-cb-exp"))
        row = await _active_booking_row(session)
        assert row is not None
        phone_tok, name_tok, conv = row[3], row[4], row[5]

    later = _NOW + timedelta(hours=1)
    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(
            session, master_client=client, pii_store=pii, clock=lambda: later
        )
        expired = await flow.handle(_env("да", "msg-cb-exp-da"))
        assert expired.outcome is MasterCommandFlowOutcome.MANUAL_HELP
        assert client.create_booking_calls == []

    assert await _pii_still_readable(
        pii,
        phone_token=phone_tok,
        name_token=name_tok,
        conversation_id=uuid.UUID(str(conv)),
    )
    async with session_factory() as session:
        state = await session.scalar(
            text(
                "SELECT state FROM master_command_pendings "
                "WHERE inbound_message_id = 'msg-cb-exp'"
            )
        )
        assert state == "EXPIRED"


@pytest.mark.asyncio
async def test_create_booking_live_lease_blocks_second_executor(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    pii = _pii_store(session_factory)
    client = _ScriptedClient()

    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(
            session, master_client=client, pii_store=pii, clock=lambda: _NOW
        )
        await flow.handle(_env(_BOOKING_TEXT, "msg-cb-lease"))
        row = await _active_booking_row(session)
        assert row is not None
        await session.execute(
            text(
                "UPDATE master_command_pendings SET state = 'EXECUTING', "
                "execution_lease_token = :tok, "
                "execution_lease_expires_at = :exp, "
                "confirmation_expires_at = :cexp, updated_at = :now WHERE id = :id"
            ),
            {
                "tok": uuid.uuid4(),
                "exp": _NOW + timedelta(seconds=30),
                "cexp": _NOW + timedelta(minutes=10),
                "now": _NOW,
                "id": row[0],
            },
        )
        await session.flush()

        blocked = await flow.handle(_env("да", "msg-cb-lease-da"))
        assert blocked.outcome is MasterCommandFlowOutcome.CONFLICT
        assert client.create_booking_calls == []


@pytest.mark.asyncio
async def test_create_booking_outer_rollback_after_success_keeps_pii_and_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """B1: no separate committed PII delete before durable terminal commit.

    Remote SUCCESS + flushed terminal, then outer UoW rollback must leave
    recoverable pending + readable PII + the same idempotency key.
    """

    await _bind(session_factory)
    pii = _pii_store(session_factory)
    client = _ScriptedClient(
        create_booking_results=[
            MasterMutationRemoteSuccess(
                idempotent_replay=False, resource_kind="booking"
            ),
            MasterMutationRemoteSuccess(
                idempotent_replay=True, resource_kind="booking"
            ),
        ]
    )

    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(
            session, master_client=client, pii_store=pii, clock=lambda: _NOW
        )
        await flow.handle(_env(_BOOKING_TEXT, "msg-cb-rb"))
        row = await _active_booking_row(session)
        assert row is not None
        key, phone_tok, name_tok, conv = row[2], row[3], row[4], row[5]

    with pytest.raises(RuntimeError, match="forced outer rollback"):
        async with session_factory() as session:
            async with session.begin():
                flow = MasterCommandFlowService(
                    session, master_client=client, pii_store=pii, clock=lambda: _NOW
                )
                ok = await flow.handle(_env("да", "msg-cb-rb-da1"))
                assert ok.outcome is MasterCommandFlowOutcome.SUCCESS
                assert len(client.create_booking_calls) == 1
                assert client.create_booking_calls[0]["idempotency_key"] == key
                # Force crash/rollback of caller UoW after terminal flush.
                raise RuntimeError("forced outer rollback after SUCCESS flush")

    # Pending restored to confirmable; PII ciphertext still present.
    async with session_factory() as session:
        row = await _active_booking_row(session)
        assert row is not None
        assert row[1] == "AWAITING_CONFIRMATION"
        assert row[2] == key
        assert row[3] == phone_tok
        assert row[4] == name_tok
    assert await _pii_still_readable(
        pii,
        phone_token=phone_tok,
        name_token=name_tok,
        conversation_id=uuid.UUID(str(conv)),
    )

    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(
            session, master_client=client, pii_store=pii, clock=lambda: _NOW
        )
        # Explicit subsequent confirm (no auto-loop); same key.
        retry = await flow.handle(_env("да", "msg-cb-rb-da2"))
        assert retry.outcome is MasterCommandFlowOutcome.SUCCESS
        assert len(client.create_booking_calls) == 2
        assert client.create_booking_calls[1]["idempotency_key"] == key


@pytest.mark.asyncio
async def test_create_booking_response_invalid_retryable_keeps_pii_and_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _bind(session_factory)
    pii = _pii_store(session_factory)
    client = _ScriptedClient(
        create_booking_results=[
            MasterCommandHttpError("RESPONSE_INVALID"),
            MasterMutationRemoteSuccess(
                idempotent_replay=True, resource_kind="booking"
            ),
        ]
    )

    async with session_scope(session_factory) as session:
        flow = MasterCommandFlowService(
            session, master_client=client, pii_store=pii, clock=lambda: _NOW
        )
        await flow.handle(_env(_BOOKING_TEXT, "msg-cb-ri"))
        row = await _active_booking_row(session)
        assert row is not None
        key, phone_tok, name_tok, conv = row[2], row[3], row[4], row[5]

        first = await flow.handle(_env("да", "msg-cb-ri-1"))
        assert first.outcome is MasterCommandFlowOutcome.UNAVAILABLE
        assert first.result_code == "RESPONSE_INVALID"
        row = await _active_booking_row(session)
        assert row is not None
        assert row[1] == "AWAITING_CONFIRMATION"
        assert row[2] == key
        assert await _pii_still_readable(
            pii,
            phone_token=phone_tok,
            name_token=name_tok,
            conversation_id=uuid.UUID(str(conv)),
        )

        # No auto-retry: only a new explicit confirm continues.
        second = await flow.handle(_env("да", "msg-cb-ri-2"))
        assert second.outcome is MasterCommandFlowOutcome.SUCCESS
        assert len(client.create_booking_calls) == 2
        assert {c["idempotency_key"] for c in client.create_booking_calls} == {key}
