from __future__ import annotations

from pathlib import Path

from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from app.models.conversation import Conversation, HandoffState
from app.models.conversation_ops_event import ConversationOpsEvent
from app.models.inbox import InboxMessage
from app.models.manager_message import ManagerMessage, ManagerMessageStatus
from app.models.outbox import (
    DeliveryStatus,
    OUTBOUND_TRANSITIONS,
    outbound_transition_allowed,
)
from app.models.reply_plan import ReplyPlan


def _named_checks(model: type) -> dict[str, str]:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name
    }


def _named_uniques(model: type) -> dict[str, tuple[str, ...]]:
    return {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint) and constraint.name
    }


def test_handoff_schema_has_strict_state_and_fencing_fields() -> None:
    assert {state.value for state in HandoffState} == {
        "BOT_ACTIVE",
        "HUMAN_ACTIVE",
        "HUMAN_PAUSE",
    }
    conversation_columns = Conversation.__table__.columns
    assert conversation_columns.handoff_state.nullable is False
    assert conversation_columns.manager_epoch.nullable is False
    assert conversation_columns.current_event_seq.nullable is False
    assert conversation_columns.manager_sequence_hwm.nullable is True
    assert conversation_columns.handoff_deadline_at.nullable is True
    assert conversation_columns.human_pause_anchor_at.nullable is True
    checks = _named_checks(Conversation)
    assert "ck_conversations_handoff_consistency" in checks
    assert "HUMAN_ACTIVE" in checks["ck_conversations_handoff_consistency"]
    assert "HUMAN_PAUSE" in checks["ck_conversations_handoff_consistency"]
    due_index = next(
        index
        for index in Conversation.__table__.indexes
        if isinstance(index, Index) and index.name == "ix_conversations_handoff_due"
    )
    assert tuple(column.name for column in due_index.columns) == (
        "handoff_deadline_at",
    )
    predicate = str(due_index.dialect_options["postgresql"]["where"])
    assert "HUMAN_ACTIVE" in predicate
    assert "HUMAN_PAUSE" in predicate
    assert "handoff_quarantined_at IS NULL" in predicate
    assert "handoff_quarantine_cleared_at IS NOT NULL" in predicate
    assert "ck_conversations_handoff_quarantine_consistency" in checks
    assert "ck_conversations_handoff_quarantine_reason" in checks
    assert "ck_conversations_handoff_quarantine_clear_path" in checks
    assert conversation_columns.handoff_quarantined_at.nullable is True
    assert conversation_columns.handoff_quarantine_reason.nullable is True
    assert conversation_columns.handoff_quarantine_cleared_at.nullable is True
    assert conversation_columns.handoff_quarantine_clear_path.nullable is True

    assert ReplyPlan.__table__.columns.manager_epoch.nullable is False
    assert ReplyPlan.__table__.columns.event_seq_hwm.nullable is False


def test_client_and_manager_messages_have_dialog_event_order() -> None:
    inbox_columns = InboxMessage.__table__.columns
    assert inbox_columns.conversation_event_seq.nullable is False
    assert _named_uniques(InboxMessage)["uq_inbox_conversation_event_seq"] == (
        "conversation_id",
        "conversation_event_seq",
    )

    manager_columns = ManagerMessage.__table__.columns
    assert manager_columns.provider_sequence.nullable is True
    assert manager_columns.conversation_event_seq.nullable is True
    assert "payload_json" not in manager_columns
    assert _named_uniques(ManagerMessage)[
        "uq_manager_messages_channel_external_message_id"
    ] == ("channel", "external_message_id")
    assert _named_uniques(ManagerMessage)[
        "uq_manager_messages_conversation_event_seq"
    ] == ("conversation_id", "conversation_event_seq")


def test_manager_message_classification_and_repr_are_fail_closed() -> None:
    assert {status.value for status in ManagerMessageStatus} == {
        "APPLIED",
        "STALE",
        "QUARANTINED",
    }
    checks = _named_checks(ManagerMessage)
    classification = checks["ck_manager_messages_classification"]
    assert "provider_sequence IS NOT NULL" in classification
    assert "conversation_event_seq IS NOT NULL" in classification
    assert "conversation_event_seq IS NULL" in classification

    message = ManagerMessage(
        channel="synthetic",
        external_message_id="manager-message-1",
        body_text="Записала Вас на пятницу в 15:00",
        status=ManagerMessageStatus.QUARANTINED.value,
    )
    rendered = repr(message)
    assert "Записала" not in rendered
    assert "body_text=<redacted>" in rendered


def test_conversation_ops_events_are_append_only_and_payload_free() -> None:
    columns = {column.name for column in ConversationOpsEvent.__table__.columns}
    assert columns == {
        "id",
        "conversation_id",
        "created_at",
        "event_type",
        "reason_code",
        "clear_path",
        "manager_epoch",
        "context_version",
    }
    forbidden = {
        "body",
        "body_text",
        "text",
        "phone",
        "email",
        "payload",
        "payload_json",
        "draft_text",
    }
    assert columns.isdisjoint(forbidden)
    checks = _named_checks(ConversationOpsEvent)
    assert "ck_conversation_ops_events_event_type" in checks
    assert "ck_conversation_ops_events_reason_code" in checks
    assert "ck_conversation_ops_events_clear_path" in checks

    fk = next(iter(ConversationOpsEvent.__table__.c.conversation_id.foreign_keys))
    assert fk.ondelete == "RESTRICT"

    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "20260729_13_handoff_quarantine.py"
    ).read_text(encoding="utf-8")
    table_start = migration.index('op.create_table(\n        "conversation_ops_events"')
    table_end = migration.index("op.create_index(", table_start)
    table_block = migration[table_start:table_end]
    assert 'ondelete="RESTRICT"' in table_block
    assert "CASCADE" not in table_block
    assert "вЂ" not in (
        Path(__file__).resolve().parents[1]
        / "app"
        / "models"
        / "conversation_ops_event.py"
    ).read_text(encoding="utf-8")


def test_admitted_is_explicit_durable_outbound_state() -> None:
    assert DeliveryStatus.ADMITTED in OUTBOUND_TRANSITIONS[DeliveryStatus.PROCESSING]
    assert outbound_transition_allowed(
        DeliveryStatus.PROCESSING,
        DeliveryStatus.ADMITTED,
    )
    assert outbound_transition_allowed(
        DeliveryStatus.ADMITTED,
        DeliveryStatus.DELIVERED,
    )
    assert not outbound_transition_allowed(
        DeliveryStatus.ADMITTED,
        DeliveryStatus.CANCELLED,
    )
