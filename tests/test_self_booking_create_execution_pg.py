"""PostgreSQL tests for SELF-BOOKING-COMMAND-02 execution path."""

from __future__ import annotations

import base64
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.booking_create_http import BookingCreateHttpError
from app.core.booking_create_remote import BookingCreateRemoteSuccess
from app.core.ephemeral_pii_keys import EnvEphemeralPiiKeyProvider
from app.core.ephemeral_pii_types import (
    EphemeralPiiKind,
    EphemeralPiiReference,
    EphemeralPiiTtlPolicy,
)
from app.core.identity_resolution import CanonicalIdentityStatus
from app.core.self_booking_create_types import (
    SelfBookingCreateAdmitOutcome,
    SelfBookingCreateExecutionOutcome,
    SelfBookingCreatePendingState,
)
from app.db.session import session_scope
from app.models.canonical_identity import CanonicalIdentity
from app.models.conversation import Channel, Conversation
from app.repositories import conversations as conversation_repo
from app.repositories import self_booking_create_pendings as pending_repo
from app.services.booking_flow import BookingFlowService
from app.services.client_ref_resolution import ClientRefResolverService
from app.services.ephemeral_pii_store import EphemeralPiiStore
from app.services.self_booking_create_execution import (
    SelfBookingCreateExecutionService,
    _SELF_BOOKING_PII_PURPOSE,
)
from app.services.self_booking_create_pending import SelfBookingCreatePendingService
from tests.pg_harness import truncate_foundation_tables

_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_BOOKING = "33333333-3333-4333-8333-333333333333"
_SLOT = f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.1000"
_STARTS = "2026-08-20T10:00:00+05:00"
_KEY = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_KEY2 = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
_PHONE = "+79001234567"
_NAME = "Иван Тестов"
_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _as_uuid(value: object) -> uuid.UUID:
    """Builtin uuid.UUID for EphemeralPiiStore exact-type checks (ORM/asyncpg)."""

    if type(value) is uuid.UUID:
        return value
    return uuid.UUID(str(value))


@pytest_asyncio.fixture(autouse=True)
async def self_booking_exec_cleanup(
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


class RecordingCreateClient:
    def __init__(
        self,
        *,
        result: BookingCreateRemoteSuccess | None = None,
        error: BaseException | None = None,
        errors: list[BaseException] | None = None,
        results: list[BookingCreateRemoteSuccess] | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.errors = list(errors or [])
        self.results = list(results or [])
        self.calls: list[dict[str, Any]] = []

    def create_booking(self, **kwargs: Any) -> BookingCreateRemoteSuccess:
        self.calls.append(dict(kwargs))
        if self.errors:
            raise self.errors.pop(0)
        if self.results:
            return self.results.pop(0)
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("result not configured")
        return self.result


async def _seed_conversation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    with_canonical: bool = True,
) -> Conversation:
    async with session_scope(session_factory) as session:
        conversation, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.SYNTHETIC,
            external_conversation_id=f"sbc-exec-{uuid.uuid4().hex[:12]}",
        )
        if with_canonical:
            canonical_id = uuid.uuid4()
            now = func.statement_timestamp()
            session.add(
                CanonicalIdentity(
                    id=canonical_id,
                    status=CanonicalIdentityStatus.ACTIVE.value,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            conversation.canonical_identity_id = canonical_id
            await session.flush()
        await session.refresh(conversation)
        return conversation


async def _store_pii(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    conversation_id: object,
) -> tuple[str, str]:
    cid = _as_uuid(conversation_id)
    store = _pii_store(session_factory)
    phone_h = await store.store(
        _PHONE,
        conversation_id=cid,
        kind=EphemeralPiiKind.PHONE,
        purpose=_SELF_BOOKING_PII_PURPOSE,
    )
    name_h = await store.store(
        _NAME,
        conversation_id=cid,
        kind=EphemeralPiiKind.CLIENT_NAME,
        purpose=_SELF_BOOKING_PII_PURPOSE,
    )
    return phone_h.reference.to_token(), name_h.reference.to_token()


async def _admit(
    session: AsyncSession,
    conversation: Conversation,
    *,
    phone_ref: str,
    name_ref: str,
    idempotency_key: str = _KEY,
    confirm_id: str | None = None,
    max_attempts: int = 3,
) -> uuid.UUID:
    svc = SelfBookingCreatePendingService(session, clock=lambda: _NOW)
    admitted = await svc.admit_confirmed(
        conversation_id=conversation.id,
        channel="synthetic",
        confirm_external_message_id=confirm_id or f"confirm-{uuid.uuid4().hex[:10]}",
        slot_id=_SLOT,
        starts_at=_STARTS,
        fence_context_version=conversation.context_version,
        fence_manager_epoch=conversation.manager_epoch,
        fence_event_seq_hwm=conversation.current_event_seq,
        personal_data_consent=True,
        offer_acknowledgement=True,
        phone_ref_token=phone_ref,
        name_ref_token=name_ref,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
    )
    assert admitted.outcome is SelfBookingCreateAdmitOutcome.ADMITTED
    assert admitted.pending_id is not None
    return admitted.pending_id


def _exec_svc(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    create: RecordingCreateClient,
) -> SelfBookingCreateExecutionService:
    return SelfBookingCreateExecutionService(
        session,
        pending_service=SelfBookingCreatePendingService(session, clock=lambda: _NOW),
        booking_flow=BookingFlowService(None, booking_create_client=create),
        client_ref_resolver=ClientRefResolverService(session),
        pii_store=_pii_store(session_factory),
        clock=lambda: _NOW,
    )


@pytest.mark.asyncio
async def test_success_create_and_terminal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    phone_ref, name_ref = await _store_pii(
        session_factory, conversation_id=conversation.id
    )
    create = RecordingCreateClient(
        result=BookingCreateRemoteSuccess(
            booking_id=_BOOKING,
            slot_id=_SLOT,
            starts_at=_STARTS,
            idempotent_replay=False,
        )
    )

    async with session_scope(session_factory) as session:
        pending_id = await _admit(
            session, conversation, phone_ref=phone_ref, name_ref=name_ref
        )
        result = await _exec_svc(session, session_factory, create).execute(
            pending_id=pending_id
        )
        assert result.outcome is SelfBookingCreateExecutionOutcome.SUCCEEDED
        assert result.idempotency_key == _KEY
        assert result.booking_id == _BOOKING
        row = await pending_repo.get_by_id(session, pending_id=pending_id)
        assert row is not None
        assert row.state == SelfBookingCreatePendingState.SUCCEEDED.value
        assert row.idempotency_key == _KEY

    assert len(create.calls) == 1
    assert create.calls[0]["idempotency_key"] == _KEY
    assert create.calls[0]["client_ref"] == str(conversation.canonical_identity_id)
    assert create.calls[0]["phone"] == _PHONE
    assert create.calls[0]["client_name"] == _NAME


@pytest.mark.asyncio
async def test_client_ref_fail_closed_zero_create(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory, with_canonical=False)
    phone_ref, name_ref = await _store_pii(
        session_factory, conversation_id=conversation.id
    )
    create = RecordingCreateClient(
        result=BookingCreateRemoteSuccess(
            booking_id=_BOOKING,
            slot_id=_SLOT,
            starts_at=_STARTS,
            idempotent_replay=False,
        )
    )

    async with session_scope(session_factory) as session:
        pending_id = await _admit(
            session, conversation, phone_ref=phone_ref, name_ref=name_ref
        )
        result = await _exec_svc(session, session_factory, create).execute(
            pending_id=pending_id
        )
        assert result.outcome is SelfBookingCreateExecutionOutcome.FAILED
        assert result.result_code == "CLIENT_REF_NOT_FOUND"
        row = await pending_repo.get_by_id(session, pending_id=pending_id)
        assert row is not None
        assert row.state == SelfBookingCreatePendingState.FAILED.value

    assert create.calls == []


@pytest.mark.asyncio
async def test_pii_unavailable_zero_create(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    phone_ref = EphemeralPiiReference.generate().to_token()
    name_ref = EphemeralPiiReference.generate().to_token()
    create = RecordingCreateClient(
        result=BookingCreateRemoteSuccess(
            booking_id=_BOOKING,
            slot_id=_SLOT,
            starts_at=_STARTS,
            idempotent_replay=False,
        )
    )

    async with session_scope(session_factory) as session:
        pending_id = await _admit(
            session, conversation, phone_ref=phone_ref, name_ref=name_ref
        )
        result = await _exec_svc(session, session_factory, create).execute(
            pending_id=pending_id
        )
        assert result.outcome is SelfBookingCreateExecutionOutcome.FAILED
        assert result.result_code == "PII_UNAVAILABLE"
        row = await pending_repo.get_by_id(session, pending_id=pending_id)
        assert row is not None
        assert row.state == SelfBookingCreatePendingState.FAILED.value

    assert create.calls == []


@pytest.mark.asyncio
async def test_stale_fence_zero_create(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    phone_ref, name_ref = await _store_pii(
        session_factory, conversation_id=conversation.id
    )
    create = RecordingCreateClient(
        result=BookingCreateRemoteSuccess(
            booking_id=_BOOKING,
            slot_id=_SLOT,
            starts_at=_STARTS,
            idempotent_replay=False,
        )
    )

    async with session_scope(session_factory) as session:
        pending_id = await _admit(
            session, conversation, phone_ref=phone_ref, name_ref=name_ref
        )
        locked = await conversation_repo.get_by_id_for_update(
            session, conversation_id=conversation.id
        )
        assert locked is not None
        locked.context_version = conversation.context_version + 1
        await session.flush()

        result = await _exec_svc(session, session_factory, create).execute(
            pending_id=pending_id
        )
        assert result.outcome is SelfBookingCreateExecutionOutcome.CANCELLED
        row = await pending_repo.get_by_id(session, pending_id=pending_id)
        assert row is not None
        assert row.state == SelfBookingCreatePendingState.CANCELLED.value

    assert create.calls == []


@pytest.mark.asyncio
async def test_duplicate_reclaim_same_idempotency_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    phone_ref, name_ref = await _store_pii(
        session_factory, conversation_id=conversation.id
    )
    create = RecordingCreateClient(
        errors=[BookingCreateHttpError("TIMEOUT")],
        results=[
            BookingCreateRemoteSuccess(
                booking_id=_BOOKING,
                slot_id=_SLOT,
                starts_at=_STARTS,
                idempotent_replay=True,
            )
        ],
    )

    async with session_scope(session_factory) as session:
        pending_id = await _admit(
            session, conversation, phone_ref=phone_ref, name_ref=name_ref
        )
        svc = _exec_svc(session, session_factory, create)
        first = await svc.execute(pending_id=pending_id)
        assert first.outcome is SelfBookingCreateExecutionOutcome.RETRY_SCHEDULED
        assert first.idempotency_key == _KEY

        second = await svc.execute(pending_id=pending_id)
        assert second.outcome is SelfBookingCreateExecutionOutcome.SUCCEEDED
        assert second.idempotency_key == _KEY

        row = await pending_repo.get_by_id(session, pending_id=pending_id)
        assert row is not None
        assert row.state == SelfBookingCreatePendingState.SUCCEEDED.value
        assert row.idempotency_key == _KEY

    assert len(create.calls) == 2
    assert create.calls[0]["idempotency_key"] == _KEY
    assert create.calls[1]["idempotency_key"] == _KEY


@pytest.mark.asyncio
async def test_ambiguous_create_keeps_action_identity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    phone_ref, name_ref = await _store_pii(
        session_factory, conversation_id=conversation.id
    )
    create = RecordingCreateClient(
        error=BookingCreateHttpError("IDEMPOTENCY_IN_PROGRESS"),
    )

    async with session_scope(session_factory) as session:
        pending_id = await _admit(
            session,
            conversation,
            phone_ref=phone_ref,
            name_ref=name_ref,
            idempotency_key=_KEY2,
        )
        result = await _exec_svc(session, session_factory, create).execute(
            pending_id=pending_id
        )
        assert result.outcome is SelfBookingCreateExecutionOutcome.RETRY_SCHEDULED
        assert result.result_code == "IDEMPOTENCY_IN_PROGRESS"
        row = await pending_repo.get_by_id(session, pending_id=pending_id)
        assert row is not None
        assert row.state == SelfBookingCreatePendingState.READY.value
        assert row.idempotency_key == _KEY2
        assert row.phone_ref_token == phone_ref
        assert row.name_ref_token == name_ref

    assert len(create.calls) == 1
    assert create.calls[0]["idempotency_key"] == _KEY2


@pytest.mark.asyncio
async def test_terminal_success_pii_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    phone_ref, name_ref = await _store_pii(
        session_factory, conversation_id=conversation.id
    )
    store = _pii_store(session_factory)
    create = RecordingCreateClient(
        result=BookingCreateRemoteSuccess(
            booking_id=_BOOKING,
            slot_id=_SLOT,
            starts_at=_STARTS,
            idempotent_replay=False,
        )
    )

    async with session_scope(session_factory) as session:
        pending_id = await _admit(
            session, conversation, phone_ref=phone_ref, name_ref=name_ref
        )
        assert (
            await store.read_plaintext(
                EphemeralPiiReference.parse(phone_ref),
                conversation_id=_as_uuid(conversation.id),
                kind=EphemeralPiiKind.PHONE,
                purpose=_SELF_BOOKING_PII_PURPOSE,
            )
            == _PHONE
        )
        result = await _exec_svc(session, session_factory, create).execute(
            pending_id=pending_id
        )
        assert result.outcome is SelfBookingCreateExecutionOutcome.SUCCEEDED

    with pytest.raises(Exception):
        await store.read_plaintext(
            EphemeralPiiReference.parse(phone_ref),
            conversation_id=_as_uuid(conversation.id),
            kind=EphemeralPiiKind.PHONE,
            purpose=_SELF_BOOKING_PII_PURPOSE,
        )
    with pytest.raises(Exception):
        await store.read_plaintext(
            EphemeralPiiReference.parse(name_ref),
            conversation_id=_as_uuid(conversation.id),
            kind=EphemeralPiiKind.CLIENT_NAME,
            purpose=_SELF_BOOKING_PII_PURPOSE,
        )


@pytest.mark.asyncio
async def test_stale_lease_cannot_finalize(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    phone_ref, name_ref = await _store_pii(
        session_factory, conversation_id=conversation.id
    )

    async with session_scope(session_factory) as session:
        pending_id = await _admit(
            session, conversation, phone_ref=phone_ref, name_ref=name_ref
        )
        pending_svc = SelfBookingCreatePendingService(session, clock=lambda: _NOW)
        claimed = await pending_svc.claim_for_execution(
            pending_id=pending_id,
            lease_token=uuid.uuid4(),
        )
        assert claimed is not None
        live_lease = claimed.execution_lease_token
        assert live_lease is not None

        stale = uuid.uuid4()
        ok = await pending_repo.mark_terminal(
            session,
            row=claimed,
            state=SelfBookingCreatePendingState.SUCCEEDED,
            result_code="OK",
            result_outcome=SelfBookingCreatePendingState.SUCCEEDED.value,
            now=_NOW,
            lease_token=stale,
        )
        assert ok is False
        row = await pending_repo.get_by_id(session, pending_id=pending_id)
        assert row is not None
        assert row.state == SelfBookingCreatePendingState.EXECUTING.value
        assert row.execution_lease_token == live_lease

        ok = await pending_repo.mark_terminal(
            session,
            row=claimed,
            state=SelfBookingCreatePendingState.SUCCEEDED,
            result_code="OK",
            result_outcome=SelfBookingCreatePendingState.SUCCEEDED.value,
            now=_NOW,
            lease_token=live_lease,
        )
        assert ok is True
        row = await pending_repo.get_by_id(session, pending_id=pending_id)
        assert row is not None
        assert row.state == SelfBookingCreatePendingState.SUCCEEDED.value
