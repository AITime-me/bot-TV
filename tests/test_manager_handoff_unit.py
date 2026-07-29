from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.services.dialog_context import (
    MAX_DIALOG_CONTEXT_CHARS,
    MAX_DIALOG_CONTEXT_MESSAGES,
    DialogMessage,
    trim_dialog_messages,
)
from app.services.manager_messages import (
    ManagerEventClassification,
    apply_manager_message_in_session,
    classify_manager_sequence,
)
from app.schemas.manager_message import SyntheticManagerMessageEvent

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("hwm", "sequence", "expected"),
    [
        (None, None, ManagerEventClassification.QUARANTINED),
        (None, 0, ManagerEventClassification.CHRONOLOGICALLY_NEW),
        (10, 11, ManagerEventClassification.CHRONOLOGICALLY_NEW),
        (10, 10, ManagerEventClassification.STALE),
        (10, 9, ManagerEventClassification.STALE),
    ],
)
def test_manager_sequence_classification_is_total_and_deterministic(
    hwm: int | None,
    sequence: int | None,
    expected: ManagerEventClassification,
) -> None:
    assert (
        classify_manager_sequence(
            current_hwm=hwm,
            provider_sequence=sequence,
        )
        is expected
    )


def test_manager_event_requires_safe_shape_and_redacts_text() -> None:
    event = SyntheticManagerMessageEvent(
        external_conversation_id="conv-1",
        external_message_id="manager-1",
        provider_sequence=7,
        provider_occurred_at=datetime(2026, 7, 29, 8, 0),
        text="Записала Вас на пятницу в 15:00",
    )
    assert event.provider_occurred_at_utc() == datetime(
        2026,
        7,
        29,
        8,
        0,
        tzinfo=timezone.utc,
    )
    assert "Записала" not in repr(event)
    assert event.redacted_view()["text"] == "<redacted>"

    with pytest.raises(ValidationError):
        SyntheticManagerMessageEvent(
            external_conversation_id="conv-1",
            external_message_id="manager-2",
            provider_sequence=-1,
            text="invalid",
        )
    with pytest.raises(ValidationError):
        SyntheticManagerMessageEvent(
            external_conversation_id="conv-1",
            external_message_id="manager-2",
            provider_sequence=9_223_372_036_854_775_808,
            text="too large for PostgreSQL bigint",
        )
    with pytest.raises(ValidationError):
        SyntheticManagerMessageEvent(
            external_conversation_id="conv-1",
            external_message_id="manager-2",
            provider_sequence=8,
            text="valid",
            token="must-not-pass",  # type: ignore[call-arg]
        )


def test_missing_provider_sequence_is_preservable_for_quarantine() -> None:
    event = SyntheticManagerMessageEvent(
        external_conversation_id="conv-1",
        external_message_id="manager-without-order",
        text="audit only",
    )
    assert event.provider_sequence is None
    assert (
        classify_manager_sequence(
            current_hwm=5,
            provider_sequence=event.provider_sequence,
        )
        is ManagerEventClassification.QUARANTINED
    )


def test_dialog_context_keeps_newest_contiguous_suffix_in_dialog_order() -> None:
    newest_first = [
        DialogMessage(conversation_event_seq=5, author="client", text="c" * 5),
        DialogMessage(conversation_event_seq=4, author="manager", text="m" * 4),
        DialogMessage(conversation_event_seq=3, author="client", text="x" * 8),
    ]
    trimmed = trim_dialog_messages(
        newest_first,
        max_messages=3,
        max_chars=10,
    )
    assert [message.conversation_event_seq for message in trimmed] == [4, 5]
    assert sum(len(message.text) for message in trimmed) == 9
    assert "c" * 5 not in repr(trimmed[-1])
    assert "text=<redacted>" in repr(trimmed[-1])


def test_dialog_context_limits_are_explicit_and_bounded() -> None:
    assert MAX_DIALOG_CONTEXT_MESSAGES == 40
    assert MAX_DIALOG_CONTEXT_CHARS == 12_000
    with pytest.raises(ValueError):
        trim_dialog_messages([], max_messages=0)
    with pytest.raises(ValueError):
        trim_dialog_messages([], max_chars=0)


def test_manager_ordering_and_handoff_scheduling_never_read_host_clock() -> None:
    paths = (
        _REPO_ROOT / "app" / "services" / "manager_messages.py",
        _REPO_ROOT / "app" / "repositories" / "manager_messages.py",
        _REPO_ROOT / "app" / "repositories" / "conversations.py",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "datetime.now(" not in source
        assert "utcnow(" not in source
    classifier_source = inspect.getsource(classify_manager_sequence)
    assert "provider_occurred_at" not in classifier_source
    assert "received_at" not in classifier_source


def test_dialog_context_reads_only_canonical_text_tables() -> None:
    source = (
        _REPO_ROOT / "app" / "services" / "dialog_context.py"
    ).read_text(encoding="utf-8")
    assert "FROM inbox_messages" in source
    assert "FROM manager_messages" in source
    assert "status = 'APPLIED'" in source
    assert "outbox_messages" not in source
    assert "reply_plans" not in source


@pytest.mark.asyncio
async def test_chronologically_new_manager_event_applies_all_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation = MagicMock()
    conversation.id = uuid.uuid4()
    conversation.status = "OPEN"
    conversation.manager_sequence_hwm = None
    conversation.current_event_seq = 0
    conversation.context_version = 0
    conversation.manager_epoch = 0
    message = MagicMock()
    message.id = uuid.uuid4()
    message.conversation_id = conversation.id
    message.status = "QUARANTINED"
    moment = datetime(2026, 7, 29, 8, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "app.services.manager_messages.conversation_repo.get_or_create",
        AsyncMock(return_value=(conversation, False)),
    )
    monkeypatch.setattr(
        "app.services.manager_messages.conversation_repo.lock_for_update",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.manager_messages.manager_message_repo."
        "insert_quarantined_if_absent",
        AsyncMock(return_value=(message, True)),
    )

    async def _allocate(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        conversation.current_event_seq = 1
        return conversation

    async def _mark_applied(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        message.status = "APPLIED"
        message.conversation_event_seq = 1
        return message

    async def _apply_manager(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        conversation.context_version = 1
        conversation.manager_epoch = 1
        conversation.manager_sequence_hwm = 10
        return conversation, True

    monkeypatch.setattr(
        "app.services.manager_messages.conversation_repo.allocate_next_event_seq",
        AsyncMock(side_effect=_allocate),
    )
    monkeypatch.setattr(
        "app.services.manager_messages.manager_message_repo.mark_applied",
        AsyncMock(side_effect=_mark_applied),
    )
    monkeypatch.setattr(
        "app.services.manager_messages.db_statement_now",
        AsyncMock(return_value=moment),
    )
    apply_fsm = AsyncMock(side_effect=_apply_manager)
    monkeypatch.setattr(
        "app.services.manager_messages.conversation_repo."
        "apply_chronologically_new_manager_message",
        apply_fsm,
    )
    cancel_plans = AsyncMock(return_value=2)
    cancel_outbound = AsyncMock(return_value=1)
    mirror = AsyncMock()
    monkeypatch.setattr(
        "app.services.manager_messages.reply_plan_repo."
        "cancel_open_plans_for_takeover",
        cancel_plans,
    )
    monkeypatch.setattr(
        "app.services.manager_messages.outbound_repo."
        "cancel_unadmitted_for_manager_message",
        cancel_outbound,
    )
    monkeypatch.setattr(
        "app.services.manager_messages.enqueue_manager_takeover",
        mirror,
    )

    result = await apply_manager_message_in_session(
        AsyncMock(),
        event=SyntheticManagerMessageEvent(
            external_conversation_id="conv-1",
            external_message_id="manager-10",
            provider_sequence=10,
            text="manager text",
        ),
    )

    assert result.status == "APPLIED"
    assert result.entered_from_bot is True
    assert result.cancelled_plans == 2
    assert result.cancelled_outbound == 1
    assert result.context_version == 1
    assert result.manager_epoch == 1
    assert result.event_seq_hwm == 1
    apply_fsm.assert_awaited_once()
    cancel_plans.assert_awaited_once()
    cancel_outbound.assert_awaited_once()
    mirror.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_manager_event_stops_before_fsm_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation = MagicMock()
    conversation.id = uuid.uuid4()
    conversation.status = "HANDOFF"
    conversation.manager_sequence_hwm = 20
    conversation.current_event_seq = 7
    conversation.context_version = 7
    conversation.manager_epoch = 3
    message = MagicMock()
    message.id = uuid.uuid4()
    message.conversation_id = conversation.id
    message.status = "QUARANTINED"

    monkeypatch.setattr(
        "app.services.manager_messages.conversation_repo.get_or_create",
        AsyncMock(return_value=(conversation, False)),
    )
    monkeypatch.setattr(
        "app.services.manager_messages.conversation_repo.lock_for_update",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.manager_messages.manager_message_repo."
        "insert_quarantined_if_absent",
        AsyncMock(return_value=(message, True)),
    )

    async def _mark_stale(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        message.status = "STALE"
        return message

    monkeypatch.setattr(
        "app.services.manager_messages.manager_message_repo.mark_stale",
        AsyncMock(side_effect=_mark_stale),
    )
    allocate = AsyncMock()
    apply_fsm = AsyncMock()
    cancel_plans = AsyncMock()
    cancel_outbound = AsyncMock()
    monkeypatch.setattr(
        "app.services.manager_messages.conversation_repo.allocate_next_event_seq",
        allocate,
    )
    monkeypatch.setattr(
        "app.services.manager_messages.conversation_repo."
        "apply_chronologically_new_manager_message",
        apply_fsm,
    )
    monkeypatch.setattr(
        "app.services.manager_messages.reply_plan_repo."
        "cancel_open_plans_for_takeover",
        cancel_plans,
    )
    monkeypatch.setattr(
        "app.services.manager_messages.outbound_repo."
        "cancel_unadmitted_for_manager_message",
        cancel_outbound,
    )

    result = await apply_manager_message_in_session(
        AsyncMock(),
        event=SyntheticManagerMessageEvent(
            external_conversation_id="conv-1",
            external_message_id="manager-19",
            provider_sequence=19,
            text="stale text",
        ),
    )

    assert result.status == "STALE"
    assert result.fsm_changed is False
    assert result.context_version == 7
    assert result.manager_epoch == 3
    assert result.event_seq_hwm == 7
    allocate.assert_not_awaited()
    apply_fsm.assert_not_awaited()
    cancel_plans.assert_not_awaited()
    cancel_outbound.assert_not_awaited()
