"""PostgreSQL tests for atomic PII admission (SELF-BOOKING-COMMAND-03H)."""

from __future__ import annotations

import asyncio
import base64
import secrets
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ephemeral_pii_keys import EnvEphemeralPiiKeyProvider
from app.core.ephemeral_pii_types import (
    EphemeralPiiKind,
    EphemeralPiiPurpose,
    EphemeralPiiReference,
    EphemeralPiiTtlPolicy,
)
from app.core.pii_admission_mac_keys import EnvPiiAdmissionMacKeyProvider
from app.core.self_booking_pii_admission_types import PiiAdmissionError
from app.db.session import session_scope
from app.models.conversation import Channel, Conversation
from app.models.ephemeral_pii import EphemeralPiiValue
from app.models.inbox import InboxMessage
from app.models.ingress import IngressEvent
from app.models.outbox import OutboxMessage
from app.models.self_booking_pii_admission import SelfBookingPiiAdmission
from app.repositories import conversations as conversation_repo
from app.services.ephemeral_pii_store import EphemeralPiiStore
from app.services.self_booking_pii_admission import SelfBookingPiiAdmissionService
from tests.pg_harness import truncate_foundation_tables

_PHONE = "+79001234567"
_PHONE_ALT = "+79007654321"
_NAME = "Test Client"
_NAME_ALT = "Other Client"
_PII_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
_MAC_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
_MAC_KEY2_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
_TTL_SECONDS = 900


def _as_uuid(value: object) -> uuid.UUID:
    """Builtin uuid.UUID for strict PII admission / store exact-type checks."""

    if type(value) is uuid.UUID:
        return value
    return uuid.UUID(str(value))


@pytest_asyncio.fixture(autouse=True)
async def pii_admission_row_cleanup(
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


def _pii_store(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ttl_seconds: int = _TTL_SECONDS,
) -> EphemeralPiiStore:
    return EphemeralPiiStore(
        session_factory=session_factory,
        key_provider=EnvEphemeralPiiKeyProvider(
            {
                "EPHEMERAL_PII_ACTIVE_KEY_ID": "TESTK1",
                "EPHEMERAL_PII_KEY_TESTK1": _PII_KEY_B64,
            }
        ),
        ttl_policy=EphemeralPiiTtlPolicy(ttl_seconds),
    )


def _mac_env_k1() -> dict[str, str]:
    return {
        "PII_ADMISSION_MAC_ACTIVE_KEY_ID": "MACK1",
        "PII_ADMISSION_MAC_KEY_MACK1": _MAC_KEY_B64,
    }


def _mac_env_rotated_keep_k1() -> dict[str, str]:
    return {
        "PII_ADMISSION_MAC_ACTIVE_KEY_ID": "MACK2",
        "PII_ADMISSION_MAC_KEY_MACK1": _MAC_KEY_B64,
        "PII_ADMISSION_MAC_KEY_MACK2": _MAC_KEY2_B64,
    }


def _mac_env_rotated_drop_k1() -> dict[str, str]:
    return {
        "PII_ADMISSION_MAC_ACTIVE_KEY_ID": "MACK2",
        "PII_ADMISSION_MAC_KEY_MACK2": _MAC_KEY2_B64,
    }


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ttl_seconds: int = _TTL_SECONDS,
    mac_environ: dict[str, str] | None = None,
) -> SelfBookingPiiAdmissionService:
    return SelfBookingPiiAdmissionService(
        session_factory=session_factory,
        pii_store=_pii_store(session_factory, ttl_seconds=ttl_seconds),
        mac_key_provider=EnvPiiAdmissionMacKeyProvider(
            mac_environ if mac_environ is not None else _mac_env_k1()
        ),
    )


async def _seed_conversation(
    session_factory: async_sessionmaker[AsyncSession],
) -> Conversation:
    async with session_scope(session_factory) as session:
        conversation, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.SYNTHETIC,
            external_conversation_id=f"pii-adm-{uuid.uuid4().hex[:12]}",
        )
        await session.refresh(conversation)
        return conversation


@pytest.mark.asyncio
async def test_migration_creates_pii_admission_table(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        exists = await session.scalar(
            text(
                "SELECT to_regclass('public.self_booking_pii_admissions') "
                "IS NOT NULL"
            )
        )
    assert exists is True


@pytest.mark.asyncio
async def test_first_admission_stores_pair_and_map(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    service = _service(session_factory)
    request_id = f"req-{uuid.uuid4().hex[:12]}"

    result = await service.admit(
        conversation_id=_as_uuid(conversation.id),
        request_id=request_id,
        phone=_PHONE,
        client_name=_NAME,
    )

    assert result.reused is False
    assert result.conversation_id == _as_uuid(conversation.id)
    assert result.request_id == request_id
    phone_ref = EphemeralPiiReference.parse(result.phone_ref_token)
    name_ref = EphemeralPiiReference.parse(result.name_ref_token)

    async with session_factory() as session:
        map_row = await session.scalar(
            select(SelfBookingPiiAdmission).where(
                SelfBookingPiiAdmission.conversation_id == conversation.id,
                SelfBookingPiiAdmission.request_id == request_id,
            )
        )
        assert map_row is not None
        assert map_row.phone_ref_token == result.phone_ref_token
        assert map_row.name_ref_token == result.name_ref_token
        assert len(map_row.content_mac) == 32
        assert map_row.mac_key_id == "MACK1"
        assert _PHONE not in repr(map_row)
        assert _NAME not in repr(map_row)

        pii_count = await session.scalar(select(func.count()).select_from(EphemeralPiiValue))
        assert pii_count == 2
        phone_row = await session.scalar(
            select(EphemeralPiiValue).where(
                EphemeralPiiValue.reference_digest == phone_ref.digest()
            )
        )
        name_row = await session.scalar(
            select(EphemeralPiiValue).where(
                EphemeralPiiValue.reference_digest == name_ref.digest()
            )
        )
        assert phone_row is not None
        assert name_row is not None
        assert phone_row.pii_kind == EphemeralPiiKind.PHONE.value
        assert name_row.pii_kind == EphemeralPiiKind.CLIENT_NAME.value
        assert phone_row.allowed_purpose == EphemeralPiiPurpose.BOOKING_PHONE_WRITE.value
        assert name_row.allowed_purpose == EphemeralPiiPurpose.BOOKING_PHONE_WRITE.value
        assert _PHONE.encode() not in phone_row.ciphertext
        assert _NAME.encode() not in name_row.ciphertext

        inbox_n = await session.scalar(select(func.count()).select_from(InboxMessage))
        outbox_n = await session.scalar(select(func.count()).select_from(OutboxMessage))
        ingress_n = await session.scalar(select(func.count()).select_from(IngressEvent))
        assert inbox_n == 0
        assert outbox_n == 0
        assert ingress_n == 0

    plaintext = await _pii_store(session_factory).read_plaintext(
        phone_ref,
        conversation_id=_as_uuid(conversation.id),
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    assert plaintext == _PHONE


@pytest.mark.asyncio
async def test_exact_replay_returns_same_refs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    service = _service(session_factory)
    request_id = f"req-{uuid.uuid4().hex[:12]}"

    first = await service.admit(
        conversation_id=_as_uuid(conversation.id),
        request_id=request_id,
        phone="8 (900) 123-45-67",
        client_name="  Test   Client ",
    )
    second = await service.admit(
        conversation_id=_as_uuid(conversation.id),
        request_id=request_id,
        phone=_PHONE,
        client_name=_NAME,
    )

    assert second.reused is True
    assert second.phone_ref_token == first.phone_ref_token
    assert second.name_ref_token == first.name_ref_token

    async with session_factory() as session:
        pii_count = await session.scalar(select(func.count()).select_from(EphemeralPiiValue))
        map_count = await session.scalar(
            select(func.count()).select_from(SelfBookingPiiAdmission)
        )
        assert pii_count == 2
        assert map_count == 1


@pytest.mark.asyncio
async def test_conflicting_replay_fail_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    service = _service(session_factory)
    request_id = f"req-{uuid.uuid4().hex[:12]}"

    await service.admit(
        conversation_id=_as_uuid(conversation.id),
        request_id=request_id,
        phone=_PHONE,
        client_name=_NAME,
    )
    with pytest.raises(PiiAdmissionError) as exc_info:
        await service.admit(
            conversation_id=_as_uuid(conversation.id),
            request_id=request_id,
            phone=_PHONE_ALT,
            client_name=_NAME_ALT,
        )
    assert exc_info.value.code == "PII_ADMISSION_CONFLICT"

    async with session_factory() as session:
        pii_count = await session.scalar(select(func.count()).select_from(EphemeralPiiValue))
        assert pii_count == 2


@pytest.mark.asyncio
async def test_transaction_rollback_leaves_no_partial_state(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation = await _seed_conversation(session_factory)
    service = _service(session_factory)
    request_id = f"req-{uuid.uuid4().hex[:12]}"

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic map insert failure")

    monkeypatch.setattr(
        "app.services.self_booking_pii_admission.admission_repo.insert_if_absent",
        _boom,
    )
    with pytest.raises(PiiAdmissionError) as exc_info:
        await service.admit(
            conversation_id=_as_uuid(conversation.id),
            request_id=request_id,
            phone=_PHONE,
            client_name=_NAME,
        )
    assert exc_info.value.code == "PII_ADMISSION_STORE_FAILED"

    async with session_factory() as session:
        pii_count = await session.scalar(select(func.count()).select_from(EphemeralPiiValue))
        map_count = await session.scalar(
            select(func.count()).select_from(SelfBookingPiiAdmission)
        )
        assert pii_count == 0
        assert map_count == 0


@pytest.mark.asyncio
async def test_concurrent_duplicate_one_winner_same_refs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    service = _service(session_factory)
    request_id = f"req-{uuid.uuid4().hex[:12]}"

    results = await asyncio.gather(
        service.admit(
            conversation_id=_as_uuid(conversation.id),
            request_id=request_id,
            phone=_PHONE,
            client_name=_NAME,
        ),
        service.admit(
            conversation_id=_as_uuid(conversation.id),
            request_id=request_id,
            phone=_PHONE,
            client_name=_NAME,
        ),
    )
    tokens = {(r.phone_ref_token, r.name_ref_token) for r in results}
    assert len(tokens) == 1
    assert sum(1 for r in results if r.reused) >= 1

    async with session_factory() as session:
        pii_count = await session.scalar(select(func.count()).select_from(EphemeralPiiValue))
        map_count = await session.scalar(
            select(func.count()).select_from(SelfBookingPiiAdmission)
        )
        assert pii_count == 2
        assert map_count == 1


@pytest.mark.asyncio
async def test_expired_ciphertext_fail_closed_no_reissue(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    service = _service(session_factory)
    request_id = f"req-{uuid.uuid4().hex[:12]}"

    first = await service.admit(
        conversation_id=_as_uuid(conversation.id),
        request_id=request_id,
        phone=_PHONE,
        client_name=_NAME,
    )

    async with session_factory() as session:
        async with session.begin():
            # Preserve expires_at > created_at CHECK while making rows past TTL.
            await session.execute(
                text(
                    """
                    UPDATE ephemeral_pii_values
                    SET
                        created_at = statement_timestamp() - interval '2 hours',
                        expires_at = statement_timestamp() - interval '1 second'
                    """
                )
            )

    with pytest.raises(PiiAdmissionError) as exc_info:
        await service.admit(
            conversation_id=_as_uuid(conversation.id),
            request_id=request_id,
            phone=_PHONE,
            client_name=_NAME,
        )
    assert exc_info.value.code == "PII_ADMISSION_EXPIRED"

    async with session_factory() as session:
        map_row = await session.scalar(
            select(SelfBookingPiiAdmission).where(
                SelfBookingPiiAdmission.request_id == request_id
            )
        )
        assert map_row is not None
        assert map_row.phone_ref_token == first.phone_ref_token
        assert map_row.name_ref_token == first.name_ref_token
        # Still exactly the original pair rows (expired), no replacement mint.
        pii_count = await session.scalar(select(func.count()).select_from(EphemeralPiiValue))
        assert pii_count == 2


@pytest.mark.asyncio
async def test_missing_ciphertext_fail_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    service = _service(session_factory)
    request_id = f"req-{uuid.uuid4().hex[:12]}"

    await service.admit(
        conversation_id=_as_uuid(conversation.id),
        request_id=request_id,
        phone=_PHONE,
        client_name=_NAME,
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM ephemeral_pii_values"))

    with pytest.raises(PiiAdmissionError) as exc_info:
        await service.admit(
            conversation_id=_as_uuid(conversation.id),
            request_id=request_id,
            phone=_PHONE,
            client_name=_NAME,
        )
    assert exc_info.value.code == "PII_ADMISSION_EXPIRED"


@pytest.mark.asyncio
async def test_mac_key_rotation_replay_conflict_and_new_request(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_conversation(session_factory)
    old_request = f"req-old-{uuid.uuid4().hex[:10]}"
    new_request = f"req-new-{uuid.uuid4().hex[:10]}"

    service_k1 = _service(session_factory, mac_environ=_mac_env_k1())
    first = await service_k1.admit(
        conversation_id=_as_uuid(conversation.id),
        request_id=old_request,
        phone=_PHONE,
        client_name=_NAME,
    )

    service_k2 = _service(
        session_factory, mac_environ=_mac_env_rotated_keep_k1()
    )
    replay = await service_k2.admit(
        conversation_id=_as_uuid(conversation.id),
        request_id=old_request,
        phone=_PHONE,
        client_name=_NAME,
    )
    assert replay.reused is True
    assert replay.phone_ref_token == first.phone_ref_token
    assert replay.name_ref_token == first.name_ref_token

    with pytest.raises(PiiAdmissionError) as conflict:
        await service_k2.admit(
            conversation_id=_as_uuid(conversation.id),
            request_id=old_request,
            phone=_PHONE_ALT,
            client_name=_NAME_ALT,
        )
    assert conflict.value.code == "PII_ADMISSION_CONFLICT"

    created = await service_k2.admit(
        conversation_id=_as_uuid(conversation.id),
        request_id=new_request,
        phone=_PHONE,
        client_name=_NAME,
    )
    assert created.reused is False
    assert created.phone_ref_token != first.phone_ref_token

    async with session_factory() as session:
        old_row = await session.scalar(
            select(SelfBookingPiiAdmission).where(
                SelfBookingPiiAdmission.request_id == old_request
            )
        )
        new_row = await session.scalar(
            select(SelfBookingPiiAdmission).where(
                SelfBookingPiiAdmission.request_id == new_request
            )
        )
        assert old_row is not None and old_row.mac_key_id == "MACK1"
        assert new_row is not None and new_row.mac_key_id == "MACK2"
        pii_count = await session.scalar(
            select(func.count()).select_from(EphemeralPiiValue)
        )
        assert pii_count == 4

    service_dropped = _service(
        session_factory, mac_environ=_mac_env_rotated_drop_k1()
    )
    with pytest.raises(PiiAdmissionError) as missing:
        await service_dropped.admit(
            conversation_id=_as_uuid(conversation.id),
            request_id=old_request,
            phone=_PHONE,
            client_name=_NAME,
        )
    assert missing.value.code == "PII_ADMISSION_CONFLICT"

    async with session_factory() as session:
        pii_count = await session.scalar(
            select(func.count()).select_from(EphemeralPiiValue)
        )
        map_count = await session.scalar(
            select(func.count()).select_from(SelfBookingPiiAdmission)
        )
        assert pii_count == 4
        assert map_count == 2
        old_row = await session.scalar(
            select(SelfBookingPiiAdmission).where(
                SelfBookingPiiAdmission.request_id == old_request
            )
        )
        assert old_row is not None
        assert old_row.phone_ref_token == first.phone_ref_token
        assert old_row.mac_key_id == "MACK1"
