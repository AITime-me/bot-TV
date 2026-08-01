from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, UniqueConstraint, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.base import Base
from app.db.session import session_scope
from app.models.conversation import Conversation
from app.models.inbox import InboxMessage
from app.models.outbox import DeliveryStatus, DestinationType, OutboxMessage
from app.repositories import messages as message_repo
from app.schemas.inbound import SyntheticInboundEvent
from app.services.inbound import InboundService
from app.services.takeover import apply_manager_takeover_in_session
from tests.foundation_test_db import (
    SecretDatabaseUrl,
    assert_safe_test_database_url,
    run_alembic_command_async,
)
from tests.pg_harness import assert_postgres_reachable, truncate_foundation_tables

import app.models  # noqa: F401 — register metadata

# Re-export for unit tests that probe reachability helpers.
__all__ = ["assert_postgres_reachable"]

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_FOUNDATION_TABLES = (
    "outbox_messages",
    "inbox_messages",
    "conversations",
    "ingress_events",
    "reply_plans",
    "amocrm_mirror_jobs",
    "manager_messages",
    "worker_heartbeats",
    "conversation_ops_events",
    "ephemeral_pii_values",
    "attachment_spool_objects",
)

# PostgreSQL rewrites `col IN (...)` into `= 'x'::text` or `= ANY (ARRAY[...])`
# and adds casts/parentheses, so the applied CHECK is verified by column plus
# allowed literal set instead of by raw SQL text.
_EXPECTED_CHECKS: dict[str, tuple[str, frozenset[str]]] = {
    "ck_conversations_channel": ("channel", frozenset({"synthetic"})),
    "ck_conversations_status": (
        "status",
        frozenset({"OPEN", "HANDOFF", "CLOSED"}),
    ),
    "ck_conversations_ownership": ("ownership", frozenset({"BOT", "MANAGER"})),
    "ck_conversations_context_version_nonnegative": ("context_version", frozenset()),
    "ck_conversations_handoff_state": (
        "handoff_state",
        frozenset({"BOT_ACTIVE", "HUMAN_ACTIVE", "HUMAN_PAUSE"}),
    ),
    "ck_conversations_manager_epoch_nonnegative": ("manager_epoch", frozenset()),
    "ck_conversations_current_event_seq_nonnegative": (
        "current_event_seq",
        frozenset(),
    ),
    "ck_conversations_manager_sequence_hwm_nonnegative": (
        "manager_sequence_hwm",
        frozenset(),
    ),
    "ck_conversations_handoff_consistency": (
        "handoff_state",
        frozenset(
            {
                "CLOSED",
                "BOT",
                "BOT_ACTIVE",
                "OPEN",
                "HANDOFF",
                "MANAGER",
                "HUMAN_ACTIVE",
                "HUMAN_PAUSE",
            }
        ),
    ),
    "ck_conversations_handoff_quarantine_consistency": (
        "handoff_quarantined_at",
        frozenset(),
    ),
    "ck_conversations_handoff_quarantine_reason": (
        "handoff_quarantine_reason",
        frozenset(
            {
                "HANDOFF_DEFERRED_PLAN_MISSING",
                "HANDOFF_DEFERRED_PLAN_TYPE",
                "HANDOFF_DEFERRED_PLAN_NOT_OPEN",
                "HANDOFF_DEFERRED_PLAN_CONTEXT",
                "HANDOFF_DEFERRED_PLAN_MANAGER_EPOCH",
                "HANDOFF_DEFERRED_PLAN_EVENT_SEQ",
                "HANDOFF_DEFERRED_PLAN_DEADLINE",
                "HANDOFF_DEFERRED_PLAN_MARKER",
                "HANDOFF_EXPIRY_UNSUPPORTED_STATE",
            }
        ),
    ),
    "ck_conversations_handoff_quarantine_clear_path": (
        "handoff_quarantine_clear_path",
        frozenset({"MANAGER_MESSAGE_APPLIED"}),
    ),
    "ck_conversation_ops_events_event_type": (
        "event_type",
        frozenset(
            {
                "HANDOFF_EXPIRY_QUARANTINED",
                "HANDOFF_QUARANTINE_CLEARED",
            }
        ),
    ),
    "ck_conversation_ops_events_reason_code": (
        "reason_code",
        frozenset(
            {
                "HANDOFF_DEFERRED_PLAN_MISSING",
                "HANDOFF_DEFERRED_PLAN_TYPE",
                "HANDOFF_DEFERRED_PLAN_NOT_OPEN",
                "HANDOFF_DEFERRED_PLAN_CONTEXT",
                "HANDOFF_DEFERRED_PLAN_MANAGER_EPOCH",
                "HANDOFF_DEFERRED_PLAN_EVENT_SEQ",
                "HANDOFF_DEFERRED_PLAN_DEADLINE",
                "HANDOFF_DEFERRED_PLAN_MARKER",
                "HANDOFF_EXPIRY_UNSUPPORTED_STATE",
            }
        ),
    ),
    "ck_conversation_ops_events_clear_path": (
        "clear_path",
        frozenset(
            {
                "HANDOFF_EXPIRY_QUARANTINED",
                "HANDOFF_QUARANTINE_CLEARED",
                "MANAGER_MESSAGE_APPLIED",
            }
        ),
    ),
    "ck_conversation_ops_events_manager_epoch_nonnegative": (
        "manager_epoch",
        frozenset(),
    ),
    "ck_conversation_ops_events_context_version_nonnegative": (
        "context_version",
        frozenset(),
    ),
    "ck_inbox_channel": ("channel", frozenset({"synthetic"})),
    "ck_inbox_direction": ("direction", frozenset({"INBOUND"})),
    "ck_inbox_message_type": ("message_type", frozenset({"TEXT"})),
    "ck_inbox_processing_status": (
        "processing_status",
        frozenset({"RECEIVED", "PROCESSING", "PROCESSED", "FAILED"}),
    ),
    "ck_inbox_conversation_event_seq_positive": (
        "conversation_event_seq",
        frozenset(),
    ),
    "ck_outbox_destination_type": (
        "destination_type",
        frozenset({"INTERNAL_DRAFT", "SYNTHETIC_OUTBOUND"}),
    ),
    "ck_outbox_delivery_status": (
        "delivery_status",
        frozenset(
            {
                "PENDING",
                "PROCESSING",
                "ADMITTED",
                "DELIVERED",
                "FAILED",
                "DEAD",
                "CANCELLED",
            }
        ),
    ),
    "ck_outbox_attempt_count_nonnegative": ("attempt_count", frozenset()),
    "ck_outbox_max_attempts_positive": ("max_attempts", frozenset()),
    "ck_outbox_lease_version_nonnegative": ("lease_version", frozenset()),
    "ck_outbox_manager_epoch_nonnegative": ("manager_epoch", frozenset()),
    "ck_outbox_event_seq_hwm_nonnegative": ("event_seq_hwm", frozenset()),
    "ck_outbox_admitted_destination": (
        "admitted_at",
        frozenset(
            {
                "SYNTHETIC_OUTBOUND",
                "ADMITTED",
                "DELIVERED",
                "DEAD",
            }
        ),
    ),
    "ck_outbox_admitted_state": (
        "delivery_status",
        frozenset({"ADMITTED", "SYNTHETIC_OUTBOUND"}),
    ),
    "ck_outbox_delivered_after_admission": (
        "admitted_at",
        frozenset({"SYNTHETIC_OUTBOUND", "DELIVERED"}),
    ),
    "ck_outbox_lease_complete": ("lease_token", frozenset()),
    "ck_outbox_unleased_states": (
        "lease_token",
        frozenset({"PENDING", "FAILED", "DELIVERED", "DEAD", "CANCELLED"}),
    ),
    "ck_outbox_processing_lease": (
        "lease_token",
        frozenset({"PROCESSING"}),
    ),
    "ck_ingress_channel": ("channel", frozenset({"synthetic"})),
    "ck_ingress_event_type": ("event_type", frozenset({"SYNTHETIC_MESSAGE"})),
    "ck_ingress_status": (
        "status",
        frozenset({"RECEIVED", "PROCESSING", "PROCESSED", "FAILED", "DEAD"}),
    ),
    "ck_ingress_attempt_count_nonnegative": ("attempt_count", frozenset()),
    "ck_ingress_max_attempts_positive": ("max_attempts", frozenset()),
    "ck_ingress_lease_version_nonnegative": ("lease_version", frozenset()),
    "ck_reply_plans_plan_type": (
        "plan_type",
        frozenset({"CLIENT_REPLY", "SERVICE_SIGNAL"}),
    ),
    "ck_reply_plans_status": (
        "status",
        frozenset(
            {
                "PENDING",
                "READY",
                "PROCESSING",
                "DISPATCHED",
                "CANCELLED",
                "SUPERSEDED",
                "FAILED",
                "DEAD",
            }
        ),
    ),
    "ck_reply_plans_delay_nonnegative": ("bot_response_delay_ms", frozenset()),
    "ck_reply_plans_attempt_count_nonnegative": ("attempt_count", frozenset()),
    "ck_reply_plans_max_attempts_positive": ("max_attempts", frozenset()),
    "ck_reply_plans_lease_version_nonnegative": ("lease_version", frozenset()),
    "ck_reply_plans_context_version_nonnegative": ("context_version", frozenset()),
    "ck_reply_plans_manager_epoch_nonnegative": ("manager_epoch", frozenset()),
    "ck_reply_plans_event_seq_hwm_nonnegative": ("event_seq_hwm", frozenset()),
    "ck_amocrm_mirror_job_type": (
        "job_type",
        frozenset(
            {
                "CLIENT_MESSAGE_RECEIVED_META",
                "REPLY_PLAN_STATE_CHANGED",
                "MANAGER_TAKEOVER",
                "OUTBOUND_DELIVERED_META",
            }
        ),
    ),
    "ck_amocrm_mirror_subject_kind": (
        "subject_kind",
        frozenset(
            {
                "CONVERSATION",
                "INBOX_MESSAGE",
                "REPLY_PLAN",
                "OUTBOX_MESSAGE",
            }
        ),
    ),
    "ck_amocrm_mirror_status": (
        "status",
        frozenset(
            {
                "PENDING",
                "PROCESSING",
                "MIRRORED",
                "SKIPPED",
                "FAILED",
                "DEAD",
            }
        ),
    ),
    "ck_amocrm_mirror_attempt_count_nonnegative": ("attempt_count", frozenset()),
    "ck_amocrm_mirror_max_attempts_positive": ("max_attempts", frozenset()),
    "ck_amocrm_mirror_lease_version_nonnegative": ("lease_version", frozenset()),
    "ck_amocrm_mirror_context_version_nonnegative": ("context_version", frozenset()),
    "ck_manager_messages_channel": ("channel", frozenset({"synthetic"})),
    "ck_manager_messages_status": (
        "status",
        frozenset({"APPLIED", "STALE", "QUARANTINED"}),
    ),
    "ck_manager_messages_provider_sequence_nonnegative": (
        "provider_sequence",
        frozenset(),
    ),
    "ck_manager_messages_event_seq_positive": (
        "conversation_event_seq",
        frozenset(),
    ),
    "ck_manager_messages_body_length": ("body_text", frozenset()),
    "ck_manager_messages_classification": (
        "status",
        frozenset({"APPLIED", "STALE", "QUARANTINED"}),
    ),
    "ck_worker_heartbeats_loop_name": (
        "loop_name",
        frozenset(
            {
                "ingress",
                "handoff_expiry",
                "reply_plan",
                "outbound",
                "amocrm_mirror",
            }
        ),
    ),
    "ck_worker_heartbeats_consecutive_failures_nonnegative": (
        "consecutive_failures",
        frozenset(),
    ),
    "ck_worker_heartbeats_failure_consistency": (
        "consecutive_failures",
        frozenset(),
    ),
}

_EXPECTED_UNIQUES: dict[str, tuple[str, ...]] = {
    "uq_conversations_channel_external_id": (
        "channel",
        "external_conversation_id",
    ),
    "uq_inbox_channel_external_message_id": (
        "channel",
        "external_message_id",
    ),
    "uq_outbox_source_inbox_destination": (
        "source_inbox_id",
        "destination_type",
    ),
    "uq_outbox_idempotency_key": ("idempotency_key",),
    "uq_outbox_reply_plan_destination": (
        "reply_plan_id",
        "destination_type",
    ),
    "uq_ingress_channel_external_event_id": (
        "channel",
        "external_event_id",
    ),
    "uq_reply_plans_conversation_context_version": (
        "conversation_id",
        "context_version",
    ),
    "uq_amocrm_mirror_key": ("mirror_key",),
    "uq_inbox_conversation_event_seq": (
        "conversation_id",
        "conversation_event_seq",
    ),
    "uq_manager_messages_channel_external_message_id": (
        "channel",
        "external_message_id",
    ),
    "uq_manager_messages_conversation_event_seq": (
        "conversation_id",
        "conversation_event_seq",
    ),
    "uq_ephemeral_pii_values_reference_digest": ("reference_digest",),
    "uq_attachment_spool_objects_reference_digest": ("reference_digest",),
    "uq_attachment_spool_objects_object_id": ("object_id",),
}

_EXPECTED_INDEXES: dict[str, tuple[str, ...]] = {
    "ix_inbox_messages_conversation_id": ("conversation_id",),
    "ix_outbox_messages_conversation_id": ("conversation_id",),
    "ix_outbox_messages_source_inbox_id": ("source_inbox_id",),
    "ix_outbox_messages_reply_plan_id": ("reply_plan_id",),
    "ix_outbox_messages_status_not_before": ("delivery_status", "not_before"),
    "ix_outbox_messages_lease_until": ("lease_until",),
    "ix_ingress_events_status_created_at": ("status", "created_at"),
    "ix_ingress_events_next_attempt_at": ("next_attempt_at",),
    "ix_ingress_events_lease_until": ("lease_until",),
    "ix_ingress_events_correlation_id": ("correlation_id",),
    "ix_reply_plans_status_not_before": ("status", "not_before"),
    "ix_reply_plans_lease_until": ("lease_until",),
    "ix_reply_plans_conversation_id": ("conversation_id",),
    "ix_amocrm_mirror_jobs_status_next_attempt_at": ("status", "next_attempt_at"),
    "ix_amocrm_mirror_jobs_lease_until": ("lease_until",),
    "ix_amocrm_mirror_jobs_conversation_id": ("conversation_id",),
    "ix_manager_messages_conversation_provider_sequence": (
        "conversation_id",
        "provider_sequence",
    ),
    "ix_manager_messages_conversation_event_seq": (
        "conversation_id",
        "conversation_event_seq",
    ),
    "ix_worker_heartbeats_last_succeeded_at": ("last_succeeded_at",),
    "ix_conversation_ops_events_conversation_created": (
        "conversation_id",
        "created_at",
    ),
    "ix_ephemeral_pii_values_expires_at": ("expires_at",),
    "ix_attachment_spool_objects_expires_at": ("expires_at",),
    "ix_attachment_spool_objects_state_updated_at": ("state", "updated_at"),
}


def _compact_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip()).replace('"', "")


async def _table_names(session: AsyncSession) -> set[str]:
    rows = await session.scalars(
        text(
            "SELECT tablename FROM pg_catalog.pg_tables "
            "WHERE schemaname = 'public'"
        )
    )
    return set(rows.all())


def _check_literals(definition: str) -> frozenset[str]:
    """Return the quoted literals allowed by a CHECK definition."""
    return frozenset(
        literal.replace("''", "'")
        for literal in re.findall(r"'((?:[^']|'')*)'", definition)
    )


def _assert_check_semantics(name: str, definition: str) -> None:
    column, allowed = _EXPECTED_CHECKS[name]
    assert re.search(rf"\b{re.escape(column)}\b", definition), (
        f"{name} does not constrain column {column}: {definition}"
    )
    actual = _check_literals(definition)
    assert actual == allowed, (
        f"{name} allows {sorted(actual)}, expected {sorted(allowed)}"
    )
    if name.endswith("_nonnegative"):
        assert ">=" in definition or ">" in definition
    if name.endswith("_positive"):
        assert ">" in definition


async def _assert_foundation_schema(session: AsyncSession) -> None:
    """Verify applied Alembic schema against expected names/semantics."""
    names = await _table_names(session)
    assert set(_FOUNDATION_TABLES) <= names

    check_rows = (
        await session.execute(
            text(
                "SELECT c.conname, pg_get_constraintdef(c.oid) "
                "FROM pg_catalog.pg_constraint c "
                "JOIN pg_catalog.pg_class t ON t.oid = c.conrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = 'public' AND c.contype = 'c'"
            )
        )
    ).all()
    checks = {row[0]: _compact_sql(row[1]) for row in check_rows}
    for name in _EXPECTED_CHECKS:
        assert name in checks, f"missing CHECK {name}"
        _assert_check_semantics(name, checks[name])

    unique_rows = (
        await session.execute(
            text(
                "SELECT c.conname, "
                "array_agg(a.attname ORDER BY u.ord) "
                "FROM pg_catalog.pg_constraint c "
                "JOIN pg_catalog.pg_class t ON t.oid = c.conrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace "
                "JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS u(attnum, ord) "
                "ON true "
                "JOIN pg_catalog.pg_attribute a "
                "ON a.attrelid = t.oid AND a.attnum = u.attnum "
                "WHERE n.nspname = 'public' AND c.contype = 'u' "
                "GROUP BY c.conname"
            )
        )
    ).all()
    uniques = {row[0]: tuple(row[1]) for row in unique_rows}
    for name, cols in _EXPECTED_UNIQUES.items():
        assert name in uniques, f"missing UNIQUE {name}"
        assert uniques[name] == cols

    index_rows = (
        await session.execute(
            text(
                "SELECT i.relname, "
                "array_agg(a.attname ORDER BY u.ord) "
                "FROM pg_catalog.pg_class t "
                "JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace "
                "JOIN pg_catalog.pg_index ix ON ix.indrelid = t.oid "
                "JOIN pg_catalog.pg_class i ON i.oid = ix.indexrelid "
                "JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS u(attnum, ord) "
                "ON true "
                "JOIN pg_catalog.pg_attribute a "
                "ON a.attrelid = t.oid AND a.attnum = u.attnum "
                "WHERE n.nspname = 'public' AND NOT ix.indisprimary "
                "GROUP BY i.relname"
            )
        )
    ).all()
    indexes = {row[0]: tuple(row[1]) for row in index_rows}
    for name, cols in _EXPECTED_INDEXES.items():
        assert name in indexes, f"missing INDEX {name}"
        assert indexes[name] == cols

    fk_rows = (
        await session.execute(
            text(
                "SELECT t.relname, c.conname, pg_get_constraintdef(c.oid) "
                "FROM pg_catalog.pg_constraint c "
                "JOIN pg_catalog.pg_class t ON t.oid = c.conrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = 'public' AND c.contype = 'f'"
            )
        )
    ).all()
    fk_defs = " | ".join(row[2].upper() for row in fk_rows)
    assert "ON DELETE CASCADE" in fk_defs
    assert "ON DELETE SET NULL" in fk_defs
    ops_fk_defs = [
        row[2].upper()
        for row in fk_rows
        if row[0] == "conversation_ops_events"
    ]
    assert ops_fk_defs, "missing conversation_ops_events foreign key"
    assert all("ON DELETE RESTRICT" in definition for definition in ops_fk_defs)
    assert all("ON DELETE CASCADE" not in definition for definition in ops_fk_defs)

    # No applied CHECK may admit a SENT delivery status.
    for name, definition in checks.items():
        assert "SENT" not in _check_literals(definition), f"{name} admits SENT"


async def _assert_no_foundation_tables(session: AsyncSession) -> None:
    names = await _table_names(session)
    assert not (set(_FOUNDATION_TABLES) & names)
    leftover_types = await session.scalar(
        text(
            "SELECT count(*) FROM pg_catalog.pg_type t "
            "JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace "
            "WHERE n.nspname = 'public' AND t.typtype = 'e'"
        )
    )
    assert leftover_types == 0


async def _assert_autogenerate_empty(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:

        def _compare(sync_conn):  # type: ignore[no-untyped-def]
            context = MigrationContext.configure(sync_conn)
            return compare_metadata(context, Base.metadata)

        diffs = await connection.run_sync(_compare)
    assert diffs == [], f"unexpected autogenerate drift: {diffs!r}"


@pytest_asyncio.fixture(autouse=True)
async def foundation_row_cleanup(
    request: pytest.FixtureRequest,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Truncate foundation tables before and after each data-using PG test.

    session_factory is a declared parameter on purpose: pytest-asyncio resolves
    async fixtures through asyncio.Runner.run, which raises RuntimeError when
    called from the already running loop of this fixture, so it must never be
    pulled in dynamically from the request object.
    """
    if request.node.get_closest_marker("no_foundation_row_cleanup"):
        yield
        return
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


@pytest_asyncio.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        async with session.begin():
            yield session


@pytest.mark.asyncio
async def test_create_conversation_and_inbox(db_session: AsyncSession) -> None:
    service = InboundService(db_session)
    result = await service.accept(
        SyntheticInboundEvent(
            external_conversation_id="synth-conv-a",
            external_message_id="synth-msg-1",
            text="foundation hello",
        )
    )

    assert result.created_conversation is True
    assert result.created_inbox is True
    assert result.created_outbox is True
    assert result.duplicate is False
    assert result.outbox.destination_type == DestinationType.INTERNAL_DRAFT.value
    assert result.outbox.delivery_status == DeliveryStatus.PENDING.value
    assert result.automatic_reply_allowed is True


@pytest.mark.asyncio
async def test_unique_conversation_identity(db_session: AsyncSession) -> None:
    service = InboundService(db_session)
    first = await service.accept(
        SyntheticInboundEvent(
            external_conversation_id="synth-conv-b",
            external_message_id="synth-msg-b1",
            text="first",
        )
    )
    second = await service.accept(
        SyntheticInboundEvent(
            external_conversation_id="synth-conv-b",
            external_message_id="synth-msg-b2",
            text="second",
        )
    )
    assert first.conversation.id == second.conversation.id
    assert second.created_conversation is False
    count = await db_session.scalar(select(func.count()).select_from(Conversation))
    assert count == 1


@pytest.mark.asyncio
async def test_sequential_accept_one_conversation_inbox_outbox(
    db_session: AsyncSession,
) -> None:
    service = InboundService(db_session)
    event = SyntheticInboundEvent(
        external_conversation_id="synth-conv-c",
        external_message_id="synth-msg-dup",
        text="once",
    )
    first = await service.accept(event)
    second = await service.accept(event)

    assert first.duplicate is False
    assert second.duplicate is True
    assert first.conversation.id == second.conversation.id
    assert first.inbox.id == second.inbox.id
    assert first.outbox.id == second.outbox.id
    assert second.created_outbox is False

    assert await db_session.scalar(select(func.count()).select_from(InboxMessage)) == 1
    assert await db_session.scalar(select(func.count()).select_from(OutboxMessage)) == 1


@pytest.mark.asyncio
async def test_parallel_accept_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event = SyntheticInboundEvent(
        external_conversation_id="synth-conv-parallel",
        external_message_id="synth-msg-parallel",
        text="race",
    )

    async def _accept_once() -> tuple:
        async with session_factory() as session:
            async with session.begin():
                result = await InboundService(session).accept(event)
                return (
                    result.conversation.id,
                    result.inbox.id,
                    result.outbox.id,
                )

    first_ids, second_ids = await asyncio.gather(_accept_once(), _accept_once())
    assert first_ids == second_ids

    async with session_factory() as session:
        async with session.begin():
            assert (
                await session.scalar(select(func.count()).select_from(Conversation))
                == 1
            )
            assert (
                await session.scalar(select(func.count()).select_from(InboxMessage))
                == 1
            )
            assert (
                await session.scalar(select(func.count()).select_from(OutboxMessage))
                == 1
            )


@pytest.mark.asyncio
async def test_db_rejects_second_internal_draft(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        async with session.begin():
            result = await InboundService(session).accept(
                SyntheticInboundEvent(
                    external_conversation_id="synth-conv-second-draft",
                    external_message_id="synth-msg-second-draft",
                    text="draft",
                )
            )
            conversation_id = result.conversation.id
            inbox_id = result.inbox.id

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                session.add(
                    OutboxMessage(
                        conversation_id=conversation_id,
                        source_inbox_id=inbox_id,
                        destination_type=DestinationType.INTERNAL_DRAFT.value,
                        payload_json={"schema": "internal.draft.v1", "forced": True},
                        delivery_status=DeliveryStatus.PENDING.value,
                    )
                )
                await session.flush()

    async with session_factory() as session:
        async with session.begin():
            assert (
                await session.scalar(select(func.count()).select_from(OutboxMessage))
                == 1
            )


@pytest.mark.asyncio
async def test_db_rejects_sent_and_unknown_channel(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        async with session.begin():
            result = await InboundService(session).accept(
                SyntheticInboundEvent(
                    external_conversation_id="synth-conv-check",
                    external_message_id="synth-msg-check",
                    text="checks",
                )
            )
            outbox_id = result.outbox.id

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE outbox_messages SET delivery_status = 'SENT' "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(outbox_id)},
                )

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO conversations "
                        "(id, channel, external_conversation_id, status) "
                        "VALUES (CAST(:id AS uuid), 'vk', 'bad-channel', 'OPEN')"
                    ),
                    {"id": str(uuid4())},
                )


@pytest.mark.asyncio
async def test_rollback_between_inbox_and_outbox(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Partial accept must roll back via session_scope before row cleanup."""
    event = SyntheticInboundEvent(
        external_conversation_id="synth-conv-rollback",
        external_message_id="synth-msg-rollback",
        text="partial",
    )
    original = message_repo.create_internal_draft_outbox

    async def _boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("forced_outbox_failure")

    message_repo.create_internal_draft_outbox = _boom  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="forced_outbox_failure"):
            async with session_scope(session_factory) as session:
                await InboundService(session).accept(event)
    finally:
        message_repo.create_internal_draft_outbox = original  # type: ignore[assignment]

    # Assert emptiness before fixture post-cleanup TRUNCATE.
    async with session_factory() as session:
        async with session.begin():
            assert (
                await session.scalar(select(func.count()).select_from(Conversation))
                == 0
            )
            assert (
                await session.scalar(select(func.count()).select_from(InboxMessage))
                == 0
            )
            assert (
                await session.scalar(select(func.count()).select_from(OutboxMessage))
                == 0
            )


@pytest.mark.asyncio
async def test_accept_is_atomic_on_success(db_session: AsyncSession) -> None:
    result = await InboundService(db_session).accept(
        SyntheticInboundEvent(
            external_conversation_id="synth-conv-d",
            external_message_id="synth-msg-d1",
            text="atomic",
        )
    )
    assert await db_session.get(Conversation, result.conversation.id) is not None
    assert await db_session.get(InboxMessage, result.inbox.id) is not None
    assert await db_session.get(OutboxMessage, result.outbox.id) is not None


@pytest.mark.asyncio
async def test_takeover_blocks_auto_reply_flag(db_session: AsyncSession) -> None:
    service = InboundService(db_session)
    first = await service.accept(
        SyntheticInboundEvent(
            external_conversation_id="synth-conv-e",
            external_message_id="synth-msg-e1",
            text="before takeover",
        )
    )
    await apply_manager_takeover_in_session(
        db_session,
        conversation_id=first.conversation.id,
        now=datetime.now(timezone.utc),
    )

    second = await service.accept(
        SyntheticInboundEvent(
            external_conversation_id="synth-conv-e",
            external_message_id="synth-msg-e2",
            text="after takeover",
        )
    )
    assert second.automatic_reply_allowed is False
    assert second.outbox.delivery_status == DeliveryStatus.PENDING.value


@pytest.mark.asyncio
async def test_no_sent_status_persisted(db_session: AsyncSession) -> None:
    await InboundService(db_session).accept(
        SyntheticInboundEvent(
            external_conversation_id="synth-conv-f",
            external_message_id="synth-msg-f1",
            text="draft only",
        )
    )
    statuses = list(await db_session.scalars(select(OutboxMessage.delivery_status)))
    assert statuses
    assert set(statuses) <= {
        DeliveryStatus.PENDING.value,
        DeliveryStatus.PROCESSING.value,
        DeliveryStatus.DELIVERED.value,
        DeliveryStatus.FAILED.value,
        DeliveryStatus.DEAD.value,
        DeliveryStatus.CANCELLED.value,
    }
    assert "SENT" not in set(statuses)


@pytest.mark.asyncio
async def test_session_isolation_is_read_committed(
    db_session: AsyncSession,
) -> None:
    # current_setting is a plain function call: no utility statement is prepared.
    level = await db_session.scalar(
        text("SELECT current_setting('transaction_isolation')")
    )
    assert level is not None
    assert str(level).replace("-", " ").lower() == "read committed"


@pytest.mark.asyncio
async def test_applied_schema_matches_metadata_and_autogenerate(
    pg_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await _assert_foundation_schema(session)
    await _assert_autogenerate_empty(pg_engine)

    model_checks = {
        c.name: str(c.sqltext).strip()
        for table in Base.metadata.tables.values()
        for c in table.constraints
        if isinstance(c, CheckConstraint) and c.name
    }
    model_uniques = {
        c.name: tuple(col.name for col in c.columns)
        for table in Base.metadata.tables.values()
        for c in table.constraints
        if isinstance(c, UniqueConstraint) and c.name
    }
    assert set(_EXPECTED_CHECKS) <= set(model_checks)
    for name, cols in _EXPECTED_UNIQUES.items():
        assert model_uniques[name] == cols


@pytest.mark.asyncio
@pytest.mark.no_foundation_row_cleanup
async def test_alembic_upgrade_downgrade_upgrade(
    pg_database_url: SecretDatabaseUrl,
    pg_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Apply the foundation migration cycle on the safe test database.

    Leaves the database at Alembic head (migration-created schema).
    Never restores schema via metadata.create_all.
    """
    assert_safe_test_database_url(pg_database_url)
    # Drop pooled connections before DDL from another thread.
    await pg_engine.dispose()

    try:
        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="downgrade",
            revision="base",
            database_url=pg_database_url,
        )
        async with session_factory() as session:
            async with session.begin():
                await _assert_no_foundation_tables(session)

        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="upgrade",
            revision="head",
            database_url=pg_database_url,
        )
        async with session_factory() as session:
            async with session.begin():
                await _assert_foundation_schema(session)
        await _assert_autogenerate_empty(pg_engine)

        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="downgrade",
            revision="base",
            database_url=pg_database_url,
        )
        async with session_factory() as session:
            async with session.begin():
                await _assert_no_foundation_tables(session)

        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="upgrade",
            revision="head",
            database_url=pg_database_url,
        )
        async with session_factory() as session:
            async with session.begin():
                await _assert_foundation_schema(session)
        await _assert_autogenerate_empty(pg_engine)
    finally:
        # Always leave subsequent tests on migration head.
        await pg_engine.dispose()
        await run_alembic_command_async(
            alembic_ini=_ALEMBIC_INI,
            command_name="upgrade",
            revision="head",
            database_url=pg_database_url,
        )
        await pg_engine.dispose()
