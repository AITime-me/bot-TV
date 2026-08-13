"""BOT-CLOSED-TEST-01A: PostgreSQL integration for closed-test HTTP surface."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import BotMode, Settings
from app.core.mode_contract import is_live_booking_s2s_read_allowed
from app.core.outbound_policy import OutboundAction, is_automatic_outbound_allowed
from app.db.session import session_scope
from app.main import create_app
from app.models.inbox import InboxMessage
from app.models.ingress import IngressEvent, IngressStatus
from app.models.outbox import DeliveryStatus, DestinationType, OutboxMessage
from app.models.reply_plan import ReplyPlan, ReplyPlanStatus
from app.services.ingress import IngressWorker
from app.services.outbound_arbiter import OutboundArbiter
from app.services.outbound_reply_text import OutboundReplyTextError
from app.services.reply_outbound import OutboundWorker, ReplyPlanWorker
from app.services.synthetic_outbound import SyntheticOutboundAdapter
from tests.foundation_test_db import SecretDatabaseUrl
from tests.pg_harness import truncate_foundation_tables

_TOKEN = "closed-test-token-" + ("a" * 16)
_HEADER = {"X-Bot-Closed-Test-Token": _TOKEN}


@pytest_asyncio.fixture(autouse=True)
async def closed_test_row_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


@contextmanager
def _app_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_url: str,
) -> Iterator[TestClient]:
    """Sync TestClient with lifespan on one anyio portal for the whole ``with``.

    Without the context manager, repeated requests against an asyncpg-backed app
    reopen/close portals across pytest-asyncio loops and raise
    ``Event loop is closed`` / ``Future attached to a different loop``.
    """

    monkeypatch.setenv("BOT_CLOSED_TEST_ENABLED", "true")
    monkeypatch.setenv("BOT_CLOSED_TEST_TOKEN", _TOKEN)
    settings = Settings.from_env(
        {
            "BOT_MODE": "OFF",
            "EMERGENCY_LOCK": "true",
            "DATABASE_URL": database_url,
        }
    )
    assert is_live_booking_s2s_read_allowed(settings) is False
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.mark.asyncio
async def test_post_persists_durable_ingress_and_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    pg_database_url: SecretDatabaseUrl,
) -> None:
    body = {
        "session_id": "closed-sess-1",
        "request_id": "closed-req-1",
        "text": "closed-test-message-one",
    }
    with _app_client(monkeypatch, database_url=pg_database_url.reveal()) as client:
        first = client.post("/internal/closed-test/events", json=body, headers=_HEADER)
        assert first.status_code == 202
        payload = first.json()
        assert payload["accepted"] is True
        assert payload["duplicate"] is False
        assert payload["status"] == IngressStatus.RECEIVED.value
        assert "closed-test-message-one" not in first.text
        assert "text" not in payload
        event_id = payload["event_id"]

        second = client.post("/internal/closed-test/events", json=body, headers=_HEADER)
        assert second.status_code == 202
        dup = second.json()
        assert dup["duplicate"] is True
        assert dup["event_id"] == event_id
        assert dup["correlation_id"] == payload["correlation_id"]

    async with session_factory() as session:
        async with session.begin():
            count = await session.scalar(select(func.count()).select_from(IngressEvent))
            assert count == 1
            row = await session.get(IngressEvent, event_id)
            assert row is not None
            assert row.channel == "synthetic"
            assert row.external_event_id == "closed-req-1"
            assert row.external_conversation_id == "closed-sess-1"


@pytest.mark.asyncio
async def test_get_unknown_404_and_received_pending(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    pg_database_url: SecretDatabaseUrl,
) -> None:
    with _app_client(monkeypatch, database_url=pg_database_url.reveal()) as client:
        missing = client.get(
            f"/internal/closed-test/events/{uuid4()}",
            headers=_HEADER,
        )
        assert missing.status_code == 404

        post = client.post(
            "/internal/closed-test/events",
            json={
                "session_id": "closed-sess-pending",
                "request_id": "closed-req-pending",
                "text": "pending-only",
            },
            headers=_HEADER,
        )
        event_id = post.json()["event_id"]
        status = client.get(
            f"/internal/closed-test/events/{event_id}",
            headers=_HEADER,
        )
        assert status.status_code == 200
        body = status.json()
        assert body["ingress"]["status"] == IngressStatus.RECEIVED.value
        assert body["ingress"]["channel"] == "synthetic"
        assert body["inbound"] is None
        assert body["reply_plan"] is None
        assert body["outbound"] is None
        assert body["synthetic_result"] is None
        assert "pending-only" not in status.text

    # GET is read-only: ingress row unchanged.
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(IngressEvent, event_id)
            assert row is not None
            assert row.status == IngressStatus.RECEIVED.value
            assert row.lease_token is None


@pytest.mark.asyncio
async def test_closed_test_e2e_plain_request_fails_closed_without_delivered_outbound(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    pg_database_url: SecretDatabaseUrl,
) -> None:
    """HTTP closed-test plain text is unrenderable → no fabricated DELIVERED body.

    Closed-test HTTP has no booking field. BOT-REPLY-DURABLE-01 fails closed
    rather than manufacturing outbound text from client echo/token. Successful
    durable-text delivery is covered by ``test_outbound_reply_text_pg``.
    """

    with _app_client(monkeypatch, database_url=pg_database_url.reveal()) as client:
        post = client.post(
            "/internal/closed-test/events",
            json={
                "session_id": "closed-sess-e2e",
                "request_id": "closed-req-e2e",
                "text": "e2e-closed-test-text",
            },
            headers=_HEADER,
        )
        assert post.status_code == 202
        event_id = post.json()["event_id"]

        ingress_worker = IngressWorker(session_factory, worker_id="closed-ingress")
        claim = await ingress_worker.claim_one()
        assert claim is not None
        assert str(claim.event_id) == event_id
        processed = await ingress_worker.process_claimed(claim)
        assert processed.status == IngressStatus.PROCESSED.value
        assert processed.inbox_id is not None

        async with session_scope(session_factory) as session:
            inbox = await session.get(InboxMessage, processed.inbox_id)
            assert inbox is not None
            plans = (
                await session.scalars(
                    select(ReplyPlan).where(
                        ReplyPlan.conversation_id == inbox.conversation_id
                    )
                )
            ).all()
            plan = next(
                p for p in plans if p.payload_json.get("inbox_id") == str(inbox.id)
            )
            plan_id = plan.id
            due = plan.not_before + timedelta(seconds=1)
            assert "booking" not in plan.payload_json

        plan_worker = ReplyPlanWorker(session_factory, worker_id="closed-plan")
        plan_claim = await plan_worker.claim_one(now=due)
        assert plan_claim is not None
        with pytest.raises(OutboundReplyTextError):
            await plan_worker.dispatch_claimed(plan_claim)

        sink = SyntheticOutboundAdapter()
        outbound_worker = OutboundWorker(
            session_factory,
            worker_id="closed-out",
            arbiter=OutboundArbiter(session_factory, sink=sink),
        )
        assert await outbound_worker.claim_one(now=due) is None
        assert sink.calls == []

        # Duplicate request does not create a second business path.
        dup = client.post(
            "/internal/closed-test/events",
            json={
                "session_id": "closed-sess-e2e",
                "request_id": "closed-req-e2e",
                "text": "e2e-closed-test-text",
            },
            headers=_HEADER,
        )
        assert dup.json()["duplicate"] is True
        assert dup.json()["event_id"] == event_id

        async with session_factory() as session:
            async with session.begin():
                assert (
                    await session.scalar(select(func.count()).select_from(IngressEvent))
                    == 1
                )
                assert (
                    await session.scalar(select(func.count()).select_from(InboxMessage))
                    == 1
                )
                plan_row = await session.get(ReplyPlan, plan_id)
                assert plan_row is not None
                assert plan_row.status in {
                    ReplyPlanStatus.FAILED.value,
                    ReplyPlanStatus.DEAD.value,
                }
                synth_count = await session.scalar(
                    select(func.count())
                    .select_from(OutboxMessage)
                    .where(
                        OutboxMessage.destination_type
                        == DestinationType.SYNTHETIC_OUTBOUND.value
                    )
                )
                assert synth_count == 0
                delivered = await session.scalar(
                    select(func.count())
                    .select_from(OutboxMessage)
                    .where(
                        OutboxMessage.delivery_status
                        == DeliveryStatus.DELIVERED.value
                    )
                )
                assert delivered == 0
                destinations = (
                    await session.scalars(select(OutboxMessage.destination_type))
                ).all()
                assert set(destinations) <= {DestinationType.INTERNAL_DRAFT.value}

        status = client.get(
            f"/internal/closed-test/events/{event_id}",
            headers=_HEADER,
        )
        assert status.status_code == 200
        body = status.json()
        assert body["ingress"]["status"] == IngressStatus.PROCESSED.value
        assert body["inbound"] is not None
        assert body["reply_plan"] is not None
        assert body["reply_plan"]["status"] in {
            ReplyPlanStatus.FAILED.value,
            ReplyPlanStatus.DEAD.value,
        }
        assert body["outbound"] is None
        assert body["synthetic_result"] is None
        assert "e2e-closed-test-text" not in status.text
        assert _TOKEN not in status.text

    assert is_automatic_outbound_allowed(Settings(), OutboundAction.SEND_MESSAGE) is False
    assert is_live_booking_s2s_read_allowed(
        Settings(bot_mode=BotMode.OFF, emergency_lock=True)
    ) is False


@pytest.mark.asyncio
async def test_get_does_not_mutate_or_claim(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    pg_database_url: SecretDatabaseUrl,
) -> None:
    with _app_client(monkeypatch, database_url=pg_database_url.reveal()) as client:
        post = client.post(
            "/internal/closed-test/events",
            json={
                "session_id": "closed-sess-ro",
                "request_id": "closed-req-ro",
                "text": "read-only-get",
            },
            headers=_HEADER,
        )
        event_id = post.json()["event_id"]

        before_status = None
        before_lease = None
        async with session_factory() as session:
            async with session.begin():
                row = await session.get(IngressEvent, event_id)
                assert row is not None
                before_status = row.status
                before_lease = row.lease_token
                before_updated = row.updated_at

        for _ in range(3):
            response = client.get(
                f"/internal/closed-test/events/{event_id}",
                headers=_HEADER,
            )
            assert response.status_code == 200

        async with session_factory() as session:
            async with session.begin():
                row = await session.get(IngressEvent, event_id)
                assert row is not None
                assert row.status == before_status
                assert row.lease_token == before_lease
                assert row.updated_at == before_updated
                # Force a no-op update check: claim still available to worker.
                claimable = await session.scalar(
                    select(func.count())
                    .select_from(IngressEvent)
                    .where(IngressEvent.status == IngressStatus.RECEIVED.value)
                )
                assert claimable == 1


@pytest.mark.asyncio
async def test_correct_token_post_accepted_with_real_db(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    pg_database_url: SecretDatabaseUrl,
) -> None:
    with _app_client(monkeypatch, database_url=pg_database_url.reveal()) as client:
        response = client.post(
            "/internal/closed-test/events",
            json={
                "session_id": "closed-sess-auth",
                "request_id": "closed-req-auth",
                "text": "auth-ok",
            },
            headers=_HEADER,
        )
        assert response.status_code == 202
        assert response.json()["accepted"] is True
        assert "auth-ok" not in response.text
        assert _TOKEN not in response.text
        assert _TOKEN not in repr(response.json())


@pytest.mark.asyncio
async def test_duplicate_request_id_different_session_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    pg_database_url: SecretDatabaseUrl,
) -> None:
    with _app_client(monkeypatch, database_url=pg_database_url.reveal()) as client:
        first = client.post(
            "/internal/closed-test/events",
            json={
                "session_id": "closed-sess-a",
                "request_id": "closed-req-collision",
                "text": "same-text-body",
            },
            headers=_HEADER,
        )
        assert first.status_code == 202
        event_id = first.json()["event_id"]

        conflict = client.post(
            "/internal/closed-test/events",
            json={
                "session_id": "closed-sess-b",
                "request_id": "closed-req-collision",
                "text": "same-text-body",
            },
            headers=_HEADER,
        )
        assert conflict.status_code == 409
        assert conflict.json() == {"detail": "IDEMPOTENCY_CONFLICT"}
        assert "closed-sess-a" not in conflict.text
        assert "closed-sess-b" not in conflict.text
        assert "same-text-body" not in conflict.text

    async with session_factory() as session:
        async with session.begin():
            assert await session.scalar(select(func.count()).select_from(IngressEvent)) == 1
            row = await session.get(IngressEvent, event_id)
            assert row is not None
            assert row.external_conversation_id == "closed-sess-a"
            assert await session.scalar(select(func.count()).select_from(InboxMessage)) == 0


@pytest.mark.asyncio
async def test_duplicate_request_id_different_text_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    pg_database_url: SecretDatabaseUrl,
) -> None:
    with _app_client(monkeypatch, database_url=pg_database_url.reveal()) as client:
        first = client.post(
            "/internal/closed-test/events",
            json={
                "session_id": "closed-sess-text",
                "request_id": "closed-req-text-collision",
                "text": "original-payload-text",
            },
            headers=_HEADER,
        )
        assert first.status_code == 202
        event_id = first.json()["event_id"]

        conflict = client.post(
            "/internal/closed-test/events",
            json={
                "session_id": "closed-sess-text",
                "request_id": "closed-req-text-collision",
                "text": "mutated-payload-text",
            },
            headers=_HEADER,
        )
        assert conflict.status_code == 409
        assert conflict.json() == {"detail": "IDEMPOTENCY_CONFLICT"}
        assert "original-payload-text" not in conflict.text
        assert "mutated-payload-text" not in conflict.text

    async with session_factory() as session:
        async with session.begin():
            assert await session.scalar(select(func.count()).select_from(IngressEvent)) == 1
            row = await session.get(IngressEvent, event_id)
            assert row is not None
            assert row.envelope_json.get("text") == "original-payload-text"
            assert await session.scalar(select(func.count()).select_from(InboxMessage)) == 0
            assert await session.scalar(select(func.count()).select_from(ReplyPlan)) == 0
            assert await session.scalar(select(func.count()).select_from(OutboxMessage)) == 0
