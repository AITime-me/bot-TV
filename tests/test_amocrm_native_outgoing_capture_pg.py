"""PostgreSQL: native outgoing CAPTURE-ONLY webhook persistence."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.amocrm_native_outgoing_capture_webhook import (
    amocrm_native_outgoing_capture_path,
)
from app.config import Settings
from app.db.session import session_scope
from app.main import create_app
from app.models.amocrm_native_outgoing_capture import AmocrmNativeOutgoingCapture
from app.models.conversation import Conversation
from app.models.ingress import IngressEvent
from app.models.manager_message import ManagerMessage
from tests.foundation_test_db import SecretDatabaseUrl
from tests.pg_harness import truncate_foundation_tables

_TOKEN = "p" * 32
_TALK = "1894"
_CHAT = "1af271b6-19b9-4ae5-9b1d-da96f1ca2072"
_CONTACT = "28592745"
_ORIGIN = "vk"
_SOURCE = "19666978"
_PATH = amocrm_native_outgoing_capture_path(_TOKEN)


def _form(**fields: str) -> bytes:
    return urlencode(fields).encode("utf-8")


def _target_body(*, message_id: str = "pg-msg-1", **overrides: str) -> bytes:
    fields = {
        "account[id]": "321321",
        "outgoing_message[add][0][id]": message_id,
        "outgoing_message[add][0][chat_id]": _CHAT,
        "outgoing_message[add][0][talk_id]": _TALK,
        "outgoing_message[add][0][contact_id]": _CONTACT,
        "outgoing_message[add][0][text]": "PG_SECRET_TEXT_MUST_NOT_LAND",
        "outgoing_message[add][0][created_at]": "1725530000",
        "outgoing_message[add][0][message_type]": "text",
        "outgoing_message[add][0][type]": "outgoing",
        "outgoing_message[add][0][origin]": _ORIGIN,
        "outgoing_message[add][0][source_id]": _SOURCE,
        "outgoing_message[add][0][author][id]": "author-pg-1",
        "outgoing_message[add][0][author][type]": "internal",
        "outgoing_message[add][0][author][user_id]": "777",
        "outgoing_message[add][0][author][name]": "Hidden Name",
        "outgoing_message[add][0][recipient][id]": "recipient-pg-1",
        "outgoing_message[add][0][recipient][type]": "external",
        "outgoing_message[add][0][recipient][name]": "Hidden Client",
    }
    fields.update(overrides)
    return _form(**fields)


@contextmanager
def _app_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_url: str,
) -> Iterator[TestClient]:
    monkeypatch.setenv("AMOCRM_NATIVE_OUTGOING_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("AMOCRM_NATIVE_OUTGOING_CAPTURE_PATH_TOKEN", _TOKEN)
    monkeypatch.setenv("AMOCRM_NATIVE_OUTGOING_CAPTURE_TALK_ID", _TALK)
    monkeypatch.setenv("AMOCRM_NATIVE_OUTGOING_CAPTURE_CHAT_ID", _CHAT)
    monkeypatch.setenv("AMOCRM_NATIVE_OUTGOING_CAPTURE_CONTACT_ID", _CONTACT)
    monkeypatch.setenv("AMOCRM_NATIVE_OUTGOING_CAPTURE_ORIGIN", _ORIGIN)
    monkeypatch.setenv("AMOCRM_NATIVE_OUTGOING_CAPTURE_SOURCE_ID", _SOURCE)
    settings = Settings.from_env(
        {
            "BOT_MODE": "OFF",
            "EMERGENCY_LOCK": "true",
            "DATABASE_URL": database_url,
        }
    )
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.mark.asyncio
async def test_capture_persists_sanitized_row_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    pg_database_url: SecretDatabaseUrl,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await truncate_foundation_tables(session_factory)

    with _app_client(monkeypatch, database_url=pg_database_url.reveal()) as client:
        first = client.post(
            _PATH,
            content=_target_body(),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "X-Amocrm-Requestid": "pg-req-1",
            },
        )
        second = client.post(
            _PATH,
            content=_target_body(),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "X-Amocrm-Requestid": "pg-req-2",
            },
        )
        wrong = client.post(
            _PATH,
            content=_target_body(
                message_id="pg-msg-other",
                **{"outgoing_message[add][0][talk_id]": "9999"},
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert wrong.status_code == 200

    async with session_scope(session_factory) as session:
        rows = list(await session.scalars(select(AmocrmNativeOutgoingCapture)))
        assert len(rows) == 1
        row = rows[0]
        assert row.amocrm_message_id == "pg-msg-1"
        assert row.talk_id == 1894
        assert row.chat_id == _CHAT
        assert row.contact_id == 28592745
        assert row.origin == "vk"
        assert row.source_id == 19666978
        assert row.author_type == "internal"
        assert row.author_user_id == "777"
        assert row.author_id == "author-pg-1"
        assert row.recipient_id == "recipient-pg-1"
        assert row.recipient_type == "external"
        assert row.type == "outgoing"
        assert row.message_type == "text"
        assert row.request_id == "pg-req-1"
        assert row.account_id == "321321"

        # Model + live PG catalog: no PII/text columns exist.
        model_cols = set(AmocrmNativeOutgoingCapture.__table__.columns.keys())
        assert "text" not in model_cols
        assert "body_text" not in model_cols
        assert "name" not in model_cols
        catalog_cols = set(
            await session.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'amocrm_native_outgoing_captures'"
                )
            )
        )
        assert "amocrm_message_id" in catalog_cols
        assert "text" not in catalog_cols
        assert "body_text" not in catalog_cols
        assert "name" not in catalog_cols

        check_names = set(
            await session.scalars(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = "
                    "'public.amocrm_native_outgoing_captures'::regclass "
                    "AND contype = 'c'"
                )
            )
        )
        assert "ck_amocrm_native_outgoing_captures_type" in check_names
        assert "ck_amocrm_native_outgoing_captures_message_type" in check_names

        # No FSM / ingress side effects from capture path.
        assert await session.scalar(select(func.count()).select_from(IngressEvent)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(ManagerMessage))
            == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(Conversation)) == 0
        )

        # Raw PII must not appear in any varchar column of the capture row.
        dumped = await session.execute(
            text(
                "SELECT amocrm_message_id::text, chat_id, origin, author_id, "
                "author_type, author_user_id, recipient_id, recipient_type, "
                "type, message_type, account_id, request_id "
                "FROM amocrm_native_outgoing_captures"
            )
        )
        joined = " ".join(str(v) for v in dumped.one())
        assert "PG_SECRET_TEXT_MUST_NOT_LAND" not in joined
        assert "Hidden Name" not in joined
        assert "Hidden Client" not in joined


@pytest.mark.asyncio
async def test_capture_check_constraints_reject_non_proof_types(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from uuid import uuid4

    await truncate_foundation_tables(session_factory)
    base_sql = (
        "INSERT INTO amocrm_native_outgoing_captures ("
        "id, amocrm_message_id, talk_id, chat_id, contact_id, origin, "
        "type, message_type"
        ") VALUES ("
        ":id, :mid, 1894, :chat, 28592745, 'vk', "
        ":type, :message_type"
        ")"
    )
    async with session_factory() as session:
        with pytest.raises((IntegrityError, DBAPIError)):
            async with session.begin():
                await session.execute(
                    text(base_sql),
                    {
                        "id": uuid4(),
                        "mid": "ck-bad-type",
                        "chat": _CHAT,
                        "type": "incoming",
                        "message_type": "text",
                    },
                )
                await session.flush()
    async with session_factory() as session:
        with pytest.raises((IntegrityError, DBAPIError)):
            async with session.begin():
                await session.execute(
                    text(base_sql),
                    {
                        "id": uuid4(),
                        "mid": "ck-bad-msgtype",
                        "chat": _CHAT,
                        "type": "outgoing",
                        "message_type": "picture",
                    },
                )
                await session.flush()
    async with session_scope(session_factory) as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(AmocrmNativeOutgoingCapture)
            )
            == 0
        )