"""PostgreSQL tests for closed-test PII admission HTTP boundary (03I)."""

from __future__ import annotations

import base64
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.session import session_scope
from app.main import create_app
from app.models.conversation import Channel
from app.models.ephemeral_pii import EphemeralPiiValue
from app.models.inbox import InboxMessage
from app.models.ingress import IngressEvent
from app.models.outbox import OutboxMessage
from app.models.self_booking_pii_admission import SelfBookingPiiAdmission
from app.repositories import conversations as conversation_repo
from tests.foundation_test_db import SecretDatabaseUrl
from tests.pg_harness import truncate_foundation_tables

_TOKEN = "closed-test-pii-token-" + ("b" * 12)
_HEADER = {"X-Bot-Closed-Test-Token": _TOKEN}
_PATH = "/internal/closed-test/pii-admissions"
_PHONE = "+79001234567"
_PHONE_ALT = "+79007654321"
_NAME = "Test Client"
_NAME_ALT = "Other Client"
_PII_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
_MAC_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


@pytest_asyncio.fixture(autouse=True)
async def closed_test_pii_row_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


def _enable_pii_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_CLOSED_TEST_ENABLED", "true")
    monkeypatch.setenv("BOT_CLOSED_TEST_TOKEN", _TOKEN)
    monkeypatch.setenv("EPHEMERAL_PII_ACTIVE_KEY_ID", "TESTK1")
    monkeypatch.setenv("EPHEMERAL_PII_KEY_TESTK1", _PII_KEY_B64)
    monkeypatch.setenv("PII_ADMISSION_MAC_ACTIVE_KEY_ID", "MACK1")
    monkeypatch.setenv("PII_ADMISSION_MAC_KEY_MACK1", _MAC_KEY_B64)


@contextmanager
def _app_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_url: str,
) -> Iterator[TestClient]:
    _enable_pii_env(monkeypatch)
    settings = Settings.from_env(
        {
            "BOT_MODE": "OFF",
            "EMERGENCY_LOCK": "true",
            "DATABASE_URL": database_url,
        }
    )
    with TestClient(create_app(settings)) as client:
        yield client


async def _seed_conversation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
) -> uuid.UUID:
    async with session_scope(session_factory) as session:
        conversation, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.SYNTHETIC,
            external_conversation_id=session_id,
        )
        await session.refresh(conversation)
        cid = conversation.id
        return cid if type(cid) is uuid.UUID else uuid.UUID(str(cid))


@pytest.mark.asyncio
async def test_success_and_exact_replay(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    pg_database_url: SecretDatabaseUrl,
) -> None:
    session_id = f"sess-{uuid.uuid4().hex[:10]}"
    request_id = f"req-{uuid.uuid4().hex[:10]}"
    await _seed_conversation(session_factory, session_id=session_id)
    body = {
        "session_id": session_id,
        "request_id": request_id,
        "client_name": _NAME,
        "phone": _PHONE,
    }

    with _app_client(monkeypatch, database_url=pg_database_url.reveal()) as client:
        first = client.post(_PATH, json=body, headers=_HEADER)
        assert first.status_code == 200
        first_json = first.json()
        assert first_json == {
            "accepted": True,
            "reused": False,
            "session_id": session_id,
            "request_id": request_id,
            "status": "ADMITTED",
        }
        assert "phone_ref" not in first.text
        assert "name_ref" not in first.text
        assert _PHONE not in first.text
        assert _NAME not in first.text

        second = client.post(_PATH, json=body, headers=_HEADER)
        assert second.status_code == 200
        assert second.json()["reused"] is True
        assert second.json()["status"] == "REUSED"

    async with session_factory() as session:
        pii_n = await session.scalar(select(func.count()).select_from(EphemeralPiiValue))
        map_n = await session.scalar(
            select(func.count()).select_from(SelfBookingPiiAdmission)
        )
        inbox_n = await session.scalar(select(func.count()).select_from(InboxMessage))
        outbox_n = await session.scalar(select(func.count()).select_from(OutboxMessage))
        ingress_n = await session.scalar(select(func.count()).select_from(IngressEvent))
        assert pii_n == 2
        assert map_n == 1
        assert inbox_n == 0
        assert outbox_n == 0
        assert ingress_n == 0


@pytest.mark.asyncio
async def test_conflicting_body_conflict(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    pg_database_url: SecretDatabaseUrl,
) -> None:
    session_id = f"sess-{uuid.uuid4().hex[:10]}"
    request_id = f"req-{uuid.uuid4().hex[:10]}"
    await _seed_conversation(session_factory, session_id=session_id)

    with _app_client(monkeypatch, database_url=pg_database_url.reveal()) as client:
        ok = client.post(
            _PATH,
            json={
                "session_id": session_id,
                "request_id": request_id,
                "client_name": _NAME,
                "phone": _PHONE,
            },
            headers=_HEADER,
        )
        assert ok.status_code == 200
        conflict = client.post(
            _PATH,
            json={
                "session_id": session_id,
                "request_id": request_id,
                "client_name": _NAME_ALT,
                "phone": _PHONE_ALT,
            },
            headers=_HEADER,
        )
        assert conflict.status_code == 409
        assert conflict.json() == {"detail": "PII_ADMISSION_CONFLICT"}
        assert _PHONE_ALT not in conflict.text
        assert _NAME_ALT not in conflict.text

    async with session_factory() as session:
        pii_n = await session.scalar(select(func.count()).select_from(EphemeralPiiValue))
        assert pii_n == 2


@pytest.mark.asyncio
async def test_unknown_conversation_zero_store(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    pg_database_url: SecretDatabaseUrl,
) -> None:
    with _app_client(monkeypatch, database_url=pg_database_url.reveal()) as client:
        response = client.post(
            _PATH,
            json={
                "session_id": f"missing-{uuid.uuid4().hex[:8]}",
                "request_id": f"req-{uuid.uuid4().hex[:8]}",
                "client_name": _NAME,
                "phone": _PHONE,
            },
            headers=_HEADER,
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "CONVERSATION_NOT_FOUND"}

    async with session_factory() as session:
        pii_n = await session.scalar(select(func.count()).select_from(EphemeralPiiValue))
        map_n = await session.scalar(
            select(func.count()).select_from(SelfBookingPiiAdmission)
        )
        assert pii_n == 0
        assert map_n == 0


@pytest.mark.asyncio
async def test_expired_refs_refresh_required(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    pg_database_url: SecretDatabaseUrl,
) -> None:
    session_id = f"sess-{uuid.uuid4().hex[:10]}"
    request_id = f"req-{uuid.uuid4().hex[:10]}"
    await _seed_conversation(session_factory, session_id=session_id)
    body = {
        "session_id": session_id,
        "request_id": request_id,
        "client_name": _NAME,
        "phone": _PHONE,
    }

    with _app_client(monkeypatch, database_url=pg_database_url.reveal()) as client:
        first = client.post(_PATH, json=body, headers=_HEADER)
        assert first.status_code == 200

        async with session_factory() as session:
            async with session.begin():
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

        expired = client.post(_PATH, json=body, headers=_HEADER)
        assert expired.status_code == 409
        assert expired.json() == {"detail": "REFRESH_REQUIRED"}
        assert _PHONE not in expired.text

    async with session_factory() as session:
        map_n = await session.scalar(
            select(func.count()).select_from(SelfBookingPiiAdmission)
        )
        pii_n = await session.scalar(select(func.count()).select_from(EphemeralPiiValue))
        assert map_n == 1
        assert pii_n == 2
