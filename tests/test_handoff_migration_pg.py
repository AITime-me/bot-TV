from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tests.foundation_test_db import (
    SecretDatabaseUrl,
    run_alembic_command_async,
)
from tests.pg_harness import truncate_foundation_tables

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_PRE_HANDOFF_REVISION = "20260728_10_attempt_exhaustion"


@pytest.mark.asyncio
async def test_handoff_backfill_and_schema_downgrade(
    pg_database_url: SecretDatabaseUrl,
    pg_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Backfill every legacy ownership/status class and fence stale sends."""
    ids = {
        "bot_open": uuid.uuid4(),
        "bot_handoff": uuid.uuid4(),
        "manager_open": uuid.uuid4(),
        "manager_handoff": uuid.uuid4(),
        "closed": uuid.uuid4(),
    }
    reply_plan_id = uuid.uuid4()
    outbound_id = uuid.uuid4()

    await truncate_foundation_tables(session_factory)
    await pg_engine.dispose()
    await run_alembic_command_async(
        alembic_ini=_ALEMBIC_INI,
        command_name="downgrade",
        revision=_PRE_HANDOFF_REVISION,
        database_url=pg_database_url,
    )

    try:
        async with session_factory() as session:
            async with session.begin():
                for label, ownership, status in (
                    ("bot_open", "BOT", "OPEN"),
                    ("bot_handoff", "BOT", "HANDOFF"),
                    ("manager_open", "MANAGER", "OPEN"),
                    ("manager_handoff", "MANAGER", "HANDOFF"),
                    ("closed", "MANAGER", "CLOSED"),
                ):
                    await session.execute(
                        text(
                            """
                            INSERT INTO conversations (
                                id, channel, external_conversation_id, status,
                                ownership, context_version, manager_takeover_at
                            ) VALUES (
                                :id, 'synthetic', :external_id, :status,
                                :ownership, :context_version, NULL
                            )
                            """
                        ),
                        {
                            "id": ids[label],
                            "external_id": f"legacy-{label}",
                            "status": status,
                            "ownership": ownership,
                            "context_version": 7 if label == "bot_open" else 0,
                        },
                    )

                # Insert in reverse chronological order. Backfill must use
                # received_at, created_at, id rather than insertion order.
                for external_id, received_at in (
                    (
                        "legacy-late",
                        datetime(2026, 7, 28, 12, 2, tzinfo=timezone.utc),
                    ),
                    (
                        "legacy-early",
                        datetime(2026, 7, 28, 12, 1, tzinfo=timezone.utc),
                    ),
                ):
                    await session.execute(
                        text(
                            """
                            INSERT INTO inbox_messages (
                                id, conversation_id, channel,
                                external_message_id, direction, message_type,
                                payload_json, received_at, processing_status
                            ) VALUES (
                                :id, :conversation_id, 'synthetic',
                                :external_message_id, 'INBOUND', 'TEXT',
                                '{"text":"redacted-test"}'::jsonb,
                                :received_at, 'PROCESSED'
                            )
                            """
                        ),
                        {
                            "id": uuid.uuid4(),
                            "conversation_id": ids["bot_open"],
                            "external_message_id": external_id,
                            "received_at": received_at,
                        },
                    )

                await session.execute(
                    text(
                        """
                        INSERT INTO reply_plans (
                            id, conversation_id, context_version, plan_type,
                            status, not_before, payload_json, lease_owner,
                            lease_token, lease_version, lease_until,
                            attempt_count, max_attempts, correlation_id
                        ) VALUES (
                            :id, :conversation_id, 0, 'CLIENT_REPLY',
                            'PROCESSING', now(), '{}'::jsonb, 'legacy-worker',
                            :lease_token, 1, now() + interval '5 minutes',
                            1, 5, :correlation_id
                        )
                        """
                    ),
                    {
                        "id": reply_plan_id,
                        "conversation_id": ids["manager_handoff"],
                        "lease_token": uuid.uuid4(),
                        "correlation_id": uuid.uuid4(),
                    },
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO outbox_messages (
                            id, conversation_id, reply_plan_id,
                            idempotency_key, context_version,
                            destination_type, payload_json, delivery_status,
                            not_before, attempt_count, max_attempts,
                            lease_owner, lease_token, lease_version,
                            lease_until, correlation_id
                        ) VALUES (
                            :id, :conversation_id, :reply_plan_id,
                            :idempotency_key, 0,
                            'SYNTHETIC_OUTBOUND', '{}'::jsonb, 'PROCESSING',
                            now(), 1, 5,
                            'legacy-worker', :lease_token, 1,
                            now() + interval '5 minutes', :correlation_id
                        )
                        """
                    ),
                    {
                        "id": outbound_id,
                        "conversation_id": ids["manager_handoff"],
                        "reply_plan_id": reply_plan_id,
                        "idempotency_key": f"legacy-{outbound_id}",
                        "lease_token": uuid.uuid4(),
                        "correlation_id": uuid.uuid4(),
                    },
                )

        await pg_engine.dispose()
        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="upgrade",
            revision="head",
            database_url=pg_database_url,
        )

        async with session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT
                            external_conversation_id, ownership, status,
                            handoff_state, manager_epoch, context_version,
                            current_event_seq,
                            handoff_deadline_at = 'infinity'::timestamptz
                                AS infinite_deadline,
                            manager_takeover_at IS NOT NULL AS has_takeover,
                            active_reply_plan_id
                        FROM conversations
                        ORDER BY external_conversation_id
                        """
                    )
                )
            ).mappings()
            conversations = {
                row["external_conversation_id"]: row for row in rows
            }

            assert conversations["legacy-bot_open"]["ownership"] == "BOT"
            assert conversations["legacy-bot_open"]["status"] == "OPEN"
            assert conversations["legacy-bot_open"]["handoff_state"] == "BOT_ACTIVE"
            assert conversations["legacy-bot_open"]["manager_epoch"] == 0
            assert conversations["legacy-bot_open"]["context_version"] == 7
            assert conversations["legacy-bot_open"]["current_event_seq"] == 2

            for key in ("legacy-bot_handoff", "legacy-manager_open"):
                assert conversations[key]["ownership"] == "BOT"
                assert conversations[key]["status"] == "OPEN"
                assert conversations[key]["handoff_state"] == "BOT_ACTIVE"
                assert conversations[key]["manager_epoch"] == 0

            human = conversations["legacy-manager_handoff"]
            assert human["ownership"] == "MANAGER"
            assert human["status"] == "HANDOFF"
            assert human["handoff_state"] == "HUMAN_ACTIVE"
            assert human["manager_epoch"] == 1
            assert human["infinite_deadline"] is True
            assert human["has_takeover"] is True
            assert human["active_reply_plan_id"] is None

            closed = conversations["legacy-closed"]
            assert closed["ownership"] == "BOT"
            assert closed["status"] == "CLOSED"
            assert closed["handoff_state"] == "BOT_ACTIVE"
            assert closed["manager_epoch"] == 0

            ordered_messages = (
                await session.execute(
                    text(
                        """
                        SELECT external_message_id, conversation_event_seq
                        FROM inbox_messages
                        WHERE conversation_id = :conversation_id
                        ORDER BY conversation_event_seq
                        """
                    ),
                    {"conversation_id": ids["bot_open"]},
                )
            ).all()
            assert ordered_messages == [
                ("legacy-early", 1),
                ("legacy-late", 2),
            ]

            plan = (
                await session.execute(
                    text(
                        """
                        SELECT status, cancel_reason, lease_owner, lease_token,
                               lease_until, manager_epoch, event_seq_hwm
                        FROM reply_plans
                        WHERE id = :id
                        """
                    ),
                    {"id": reply_plan_id},
                )
            ).mappings().one()
            assert plan["status"] == "CANCELLED"
            assert plan["cancel_reason"] == "HANDOFF_SCHEMA_MIGRATION"
            assert plan["lease_owner"] is None
            assert plan["lease_token"] is None
            assert plan["lease_until"] is None
            assert plan["manager_epoch"] == 1
            assert plan["event_seq_hwm"] == 0

            outbound = (
                await session.execute(
                    text(
                        """
                        SELECT delivery_status, lease_owner, lease_token,
                               lease_until, manager_epoch, event_seq_hwm,
                               admitted_at
                        FROM outbox_messages
                        WHERE id = :id
                        """
                    ),
                    {"id": outbound_id},
                )
            ).mappings().one()
            assert outbound["delivery_status"] == "CANCELLED"
            assert outbound["lease_owner"] is None
            assert outbound["lease_token"] is None
            assert outbound["lease_until"] is None
            assert outbound["manager_epoch"] == 1
            assert outbound["event_seq_hwm"] == 0
            assert outbound["admitted_at"] is None

        await pg_engine.dispose()
        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="downgrade",
            revision=_PRE_HANDOFF_REVISION,
            database_url=pg_database_url,
        )
        async with session_factory() as session:
            manager_table = await session.scalar(
                text("SELECT to_regclass('public.manager_messages')")
            )
            event_seq_column = await session.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'inbox_messages'
                      AND column_name = 'conversation_event_seq'
                    """
                )
            )
            normalized_legacy_state = (
                await session.execute(
                    text(
                        """
                        SELECT ownership, status
                        FROM conversations
                        WHERE id = :id
                        """
                    ),
                    {"id": ids["bot_handoff"]},
                )
            ).one()
            fenced_plan_status = await session.scalar(
                text("SELECT status FROM reply_plans WHERE id = :id"),
                {"id": reply_plan_id},
            )
            assert manager_table is None
            assert event_seq_column == 0
            # Schema downgrade is intentionally not a data restore.
            assert normalized_legacy_state == ("BOT", "OPEN")
            assert fenced_plan_status == "CANCELLED"
    finally:
        await pg_engine.dispose()
        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="upgrade",
            revision="head",
            database_url=pg_database_url,
        )
        await truncate_foundation_tables(session_factory)
        await pg_engine.dispose()
