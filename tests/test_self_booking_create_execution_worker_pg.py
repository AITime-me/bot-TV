"""PostgreSQL tests for SELF-BOOKING-COMMAND-03L execution worker."""

from __future__ import annotations

import asyncio
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
from app.services.ephemeral_pii_store import EphemeralPiiStore
from app.services.self_booking_create_execution import _SELF_BOOKING_PII_PURPOSE
from app.services.self_booking_create_execution_worker import (
    SelfBookingCreateExecutionWorker,
)
from app.services.self_booking_create_pending import SelfBookingCreatePendingService
from app.services.takeover import apply_manager_takeover_in_session
from tests.pg_harness import truncate_foundation_tables

_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"
_BOOKING = "33333333-3333-4333-8333-333333333333"
_SLOT = f"bs1.{_SERVICE}.{_MASTER}.2026-08-20.1000"
_STARTS = "2026-08-20T10:00:00+05:00"
_KEY = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_PHONE = "+79001234567"
_NAME = "Test Client"
_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _as_uuid(value: object) -> uuid.UUID:
    if type(value) is uuid.UUID:
        return value
    return uuid.UUID(str(value))


@pytest_asyncio.fixture(autouse=True)
async def exec_worker_cleanup(
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
    ) -> None:
        self.result = result
        self.error = error
        self.errors = list(errors or [])
        self.calls: list[dict[str, Any]] = []

    def create_booking(self, **kwargs: Any) -> BookingCreateRemoteSuccess:
        self.calls.append(dict(kwargs))
        if self.errors:
            raise self.errors.pop(0)
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("result not configured")
        return self.result


def _worker(
    session_factory: async_sessionmaker[AsyncSession],
    create: RecordingCreateClient,
) -> SelfBookingCreateExecutionWorker:
    return SelfBookingCreateExecutionWorker(
        session_factory,
        booking_flow=BookingFlowService(None, booking_create_client=create),
        pii_store=_pii_store(session_factory),
    )


async def _seed_conversation(
    session_factory: async_sessionmaker[AsyncSession],
) -> Conversation:
    async with session_scope(session_factory) as session:
        conversation, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.SYNTHETIC,
            external_conversation_id=f"sbc-w-{uuid.uuid4().hex[:12]}",
        )
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


async def _admit_ready(
    session_factory: async_sessionmaker[AsyncSession],
    conversation: Conversation,
    *,
    phone_ref: str,
    name_ref: str,
    idempotency_key: str = _KEY,
) -> uuid.UUID:
    async with session_scope(session_factory) as session:
        svc = SelfBookingCreatePendingService(session, clock=lambda: _NOW)
        admitted = await svc.admit_confirmed(
            conversation_id=conversation.id,
            channel="synthetic",
            confirm_external_message_id=f"confirm-{uuid.uuid4().hex[:10]}",
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
            max_attempts=3,
        )
        assert admitted.outcome is SelfBookingCreateAdmitOutcome.ADMITTED
        assert admitted.pending_id is not None
        return admitted.pending_id


@pytest.mark.asyncio
async def test_worker_claims_ready_and_succeeds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    phone_ref, name_ref = await _store_pii(
        session_factory, conversation_id=conversation.id
    )
    pending_id = await _admit_ready(
        session_factory, conversation, phone_ref=phone_ref, name_ref=name_ref
    )
    create = RecordingCreateClient(
        result=BookingCreateRemoteSuccess(
            booking_id=_BOOKING,
            slot_id=_SLOT,
            starts_at=_STARTS,
            idempotent_replay=False,
        )
    )
    worker = _worker(session_factory, create)

    claimed = await worker.claim_one()
    assert claimed == pending_id
    result = await worker.process_one(claimed)
    assert result.outcome is SelfBookingCreateExecutionOutcome.SUCCEEDED
    assert result.idempotency_key == _KEY
    assert len(create.calls) == 1
    assert create.calls[0]["idempotency_key"] == _KEY

    async with session_scope(session_factory) as session:
        row = await pending_repo.get_by_id(session, pending_id=pending_id)
        assert row is not None
        assert row.state == SelfBookingCreatePendingState.SUCCEEDED.value


@pytest.mark.asyncio
async def test_retry_keeps_same_idempotency_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    phone_ref, name_ref = await _store_pii(
        session_factory, conversation_id=conversation.id
    )
    pending_id = await _admit_ready(
        session_factory, conversation, phone_ref=phone_ref, name_ref=name_ref
    )
    create = RecordingCreateClient(
        errors=[BookingCreateHttpError("TIMEOUT")],
        result=BookingCreateRemoteSuccess(
            booking_id=_BOOKING,
            slot_id=_SLOT,
            starts_at=_STARTS,
            idempotent_replay=False,
        ),
    )
    worker = _worker(session_factory, create)

    first_id = await worker.claim_one()
    assert first_id == pending_id
    first = await worker.process_one(first_id)
    assert first.outcome is SelfBookingCreateExecutionOutcome.RETRY_SCHEDULED
    assert first.idempotency_key == _KEY

    second_id = await worker.claim_one()
    assert second_id == pending_id
    second = await worker.process_one(second_id)
    assert second.outcome is SelfBookingCreateExecutionOutcome.SUCCEEDED
    assert second.idempotency_key == _KEY
    assert create.calls[0]["idempotency_key"] == _KEY
    assert create.calls[1]["idempotency_key"] == _KEY


@pytest.mark.asyncio
async def test_stale_fence_zero_create(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    phone_ref, name_ref = await _store_pii(
        session_factory, conversation_id=conversation.id
    )
    pending_id = await _admit_ready(
        session_factory, conversation, phone_ref=phone_ref, name_ref=name_ref
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
        locked = await conversation_repo.get_by_id_for_update(
            session, conversation_id=conversation.id
        )
        assert locked is not None
        await conversation_repo.bump_context_for_new_message(
            session, conversation=locked, activity_at=_NOW
        )

    worker = _worker(session_factory, create)
    claimed = await worker.claim_one()
    assert claimed == pending_id
    result = await worker.process_one(claimed)
    assert result.outcome is SelfBookingCreateExecutionOutcome.CANCELLED
    assert create.calls == []


@pytest.mark.asyncio
async def test_takeover_zero_create(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    phone_ref, name_ref = await _store_pii(
        session_factory, conversation_id=conversation.id
    )
    pending_id = await _admit_ready(
        session_factory, conversation, phone_ref=phone_ref, name_ref=name_ref
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
        _, _, changed = await apply_manager_takeover_in_session(
            session, conversation_id=conversation.id, now=_NOW
        )
        assert changed is True

    worker = _worker(session_factory, create)
    claimed = await worker.claim_one()
    assert claimed == pending_id
    result = await worker.process_one(claimed)
    assert result.outcome is SelfBookingCreateExecutionOutcome.CANCELLED
    assert create.calls == []


@pytest.mark.asyncio
async def test_duplicate_claim_one_execution(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    phone_ref, name_ref = await _store_pii(
        session_factory, conversation_id=conversation.id
    )
    pending_id = await _admit_ready(
        session_factory, conversation, phone_ref=phone_ref, name_ref=name_ref
    )
    create = RecordingCreateClient(
        result=BookingCreateRemoteSuccess(
            booking_id=_BOOKING,
            slot_id=_SLOT,
            starts_at=_STARTS,
            idempotent_replay=False,
        )
    )
    worker_a = _worker(session_factory, create)
    worker_b = _worker(session_factory, create)

    async def _run(worker: SelfBookingCreateExecutionWorker) -> str:
        claimed = await worker.claim_one()
        if claimed is None:
            return "empty"
        outcome = await worker.process_one(claimed)
        return outcome.outcome.value

    results = await asyncio.gather(_run(worker_a), _run(worker_b))
    assert "SUCCEEDED" in results
    assert results.count("SUCCEEDED") == 1
    assert len(create.calls) == 1

    async with session_scope(session_factory) as session:
        row = await pending_repo.get_by_id(session, pending_id=pending_id)
        assert row is not None
        assert row.state == SelfBookingCreatePendingState.SUCCEEDED.value


@pytest.mark.asyncio
async def test_no_plaintext_in_worker_result(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    phone_ref, name_ref = await _store_pii(
        session_factory, conversation_id=conversation.id
    )
    await _admit_ready(
        session_factory, conversation, phone_ref=phone_ref, name_ref=name_ref
    )
    create = RecordingCreateClient(
        result=BookingCreateRemoteSuccess(
            booking_id=_BOOKING,
            slot_id=_SLOT,
            starts_at=_STARTS,
            idempotent_replay=False,
        )
    )
    worker = _worker(session_factory, create)
    claimed = await worker.claim_one()
    assert claimed is not None
    result = await worker.process_one(claimed)
    rendered = repr(result)
    assert _PHONE not in rendered
    assert _NAME not in rendered
    assert _KEY not in rendered
