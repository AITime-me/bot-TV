"""PostgreSQL proofs for QA Yandex shadow draft persistence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.shadow_draft_types import (
    ShadowDraftDisposition,
    ShadowDraftProvenanceSummary,
    ShadowDraftReasonCode,
    ShadowDraftReply,
)
from app.db.session import session_scope
from app.models.conversation import Channel
from app.models.inbox import InboxMessage
from app.models.yandex_shadow_draft import YandexShadowDraft
from app.repositories import conversations as conversation_repo
from app.repositories import messages as message_repo
from app.repositories import yandex_shadow_drafts as shadow_draft_repo
from tests.pg_harness import truncate_foundation_tables

_SECRET_TEXT = "SECRET_SHADOW_QA_TEXT_ROUNDTRIP"


@pytest_asyncio.fixture(autouse=True)
async def cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


async def _seed_conversation_inbox(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[object, InboxMessage]:
    async with session_scope(session_factory) as session:
        conversation, _ = await conversation_repo.get_or_create(
            session,
            channel=Channel.SYNTHETIC,
            external_conversation_id=f"shadow-qa-{uuid4()}",
        )
        conversation = await conversation_repo.lock_for_update(
            session,
            conversation_id=conversation.id,
        )
        conversation = await conversation_repo.allocate_next_event_seq(
            session,
            conversation=conversation,
        )
        inbox, created = await message_repo.insert_inbox_if_absent(
            session,
            conversation_id=conversation.id,
            channel=Channel.SYNTHETIC,
            external_message_id=f"msg-{uuid4()}",
            conversation_event_seq=conversation.current_event_seq,
            payload_json={"text": "client asks about booking"},
            received_at=datetime.now(timezone.utc),
        )
        assert created is True
        return conversation, inbox


def _reply(*, text: str | None, disposition: ShadowDraftDisposition) -> ShadowDraftReply:
    return ShadowDraftReply(
        text=text,
        disposition=disposition,
        handoff_required=disposition
        in {ShadowDraftDisposition.HANDOFF, ShadowDraftDisposition.PROVIDER_ERROR},
        reason_code=(
            ShadowDraftReasonCode.OK
            if disposition is ShadowDraftDisposition.REPLY
            else ShadowDraftReasonCode.EMERGENCY_LOCK
            if disposition is ShadowDraftDisposition.DENIED
            else ShadowDraftReasonCode.PROVIDER_TIMEOUT
            if disposition is ShadowDraftDisposition.PROVIDER_ERROR
            else ShadowDraftReasonCode.OK
        ),
        provenance=ShadowDraftProvenanceSummary(
            settings_publication_id="pub-s",
            settings_checksum="a" * 64,
            knowledge_publication_id="pub-k",
            knowledge_checksum="b" * 64,
            selected_knowledge_keys=("k1",),
            live_facts_service_count=1,
            live_facts_master_count=2,
            history_turn_count=3,
        ),
        generation_metadata={
            "provider": "yandex",
            "shadow": True,
            "text_len": len(text) if text else 0,
            "model_configured": True,
            "provider_transport_called": disposition
            is not ShadowDraftDisposition.DENIED,
        },
    )


@pytest.mark.asyncio
async def test_insert_round_trip_and_lookup_by_inbox(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation, inbox = await _seed_conversation_inbox(session_factory)
    reply = _reply(text=_SECRET_TEXT, disposition=ShadowDraftDisposition.REPLY)

    async with session_scope(session_factory) as session:
        inserted = await shadow_draft_repo.insert_if_absent(
            session,
            row_id=uuid4(),
            inbox_message_id=inbox.id,
            conversation_id=conversation.id,
            disposition=reply.disposition.value,
            reason_code=reply.reason_code.value,
            handoff_required=reply.handoff_required,
            generated_text=reply.text,
            provenance_json=reply.provenance.as_dict(),
            generation_metadata_json=dict(reply.generation_metadata),
        )
        assert inserted is not None
        assert inserted.generated_text == _SECRET_TEXT
        assert _SECRET_TEXT not in repr(inserted)

    async with session_factory() as session:
        row = await shadow_draft_repo.get_by_inbox_message_id(
            session,
            inbox_message_id=inbox.id,
        )
        assert row is not None
        assert row.generated_text == _SECRET_TEXT
        assert row.disposition == ShadowDraftDisposition.REPLY.value
        assert row.reason_code == ShadowDraftReasonCode.OK.value
        assert row.handoff_required is False
        assert row.provenance_json["settingsPublicationId"] == "pub-s"
        assert row.generation_metadata_json["provider"] == "yandex"
        assert _SECRET_TEXT not in repr(row)

        latest = await shadow_draft_repo.get_latest_for_conversation(
            session,
            conversation_id=conversation.id,
        )
        assert latest is not None
        assert latest.id == row.id


@pytest.mark.asyncio
async def test_insert_same_inbox_is_idempotent_first_write_wins(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation, inbox = await _seed_conversation_inbox(session_factory)
    first_id = uuid4()

    async with session_scope(session_factory) as session:
        first = await shadow_draft_repo.insert_if_absent(
            session,
            row_id=first_id,
            inbox_message_id=inbox.id,
            conversation_id=conversation.id,
            disposition=ShadowDraftDisposition.REPLY.value,
            reason_code=ShadowDraftReasonCode.OK.value,
            handoff_required=False,
            generated_text="FIRST_DRAFT_WINS",
            provenance_json={},
            generation_metadata_json={"provider": "yandex", "shadow": True},
        )
        assert first is not None

    async with session_scope(session_factory) as session:
        second = await shadow_draft_repo.insert_if_absent(
            session,
            row_id=uuid4(),
            inbox_message_id=inbox.id,
            conversation_id=conversation.id,
            disposition=ShadowDraftDisposition.HANDOFF.value,
            reason_code=ShadowDraftReasonCode.OK.value,
            handoff_required=True,
            generated_text="SECOND_MUST_NOT_OVERWRITE",
            provenance_json={},
            generation_metadata_json={"provider": "yandex", "shadow": True},
        )
        assert second is None

    async with session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(YandexShadowDraft)
        )
        assert count == 1
        row = await shadow_draft_repo.get_by_inbox_message_id(
            session,
            inbox_message_id=inbox.id,
        )
        assert row is not None
        assert row.id == first_id
        assert row.generated_text == "FIRST_DRAFT_WINS"
        assert row.disposition == ShadowDraftDisposition.REPLY.value


@pytest.mark.asyncio
async def test_fk_rejects_unknown_inbox(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation, _inbox = await _seed_conversation_inbox(session_factory)
    with pytest.raises(IntegrityError):
        async with session_scope(session_factory) as session:
            await shadow_draft_repo.insert_if_absent(
                session,
                row_id=uuid4(),
                inbox_message_id=uuid4(),
                conversation_id=conversation.id,
                disposition=ShadowDraftDisposition.DENIED.value,
                reason_code=ShadowDraftReasonCode.EMERGENCY_LOCK.value,
                handoff_required=True,
                generated_text=None,
                provenance_json={},
                generation_metadata_json={"provider": "yandex", "shadow": True},
            )


@pytest.mark.asyncio
async def test_disposition_check_rejects_invalid(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation, inbox = await _seed_conversation_inbox(session_factory)
    with pytest.raises(IntegrityError):
        async with session_scope(session_factory) as session:
            await session.execute(
                text(
                    "INSERT INTO yandex_shadow_drafts ("
                    "id, inbox_message_id, conversation_id, disposition, "
                    "reason_code, handoff_required, generated_text, "
                    "provenance_json, generation_metadata_json"
                    ") VALUES ("
                    ":id, :inbox, :conv, 'NOT_A_DISPOSITION', 'OK', false, NULL, "
                    "'{}'::jsonb, '{}'::jsonb)"
                ),
                {
                    "id": str(uuid4()),
                    "inbox": str(inbox.id),
                    "conv": str(conversation.id),
                },
            )
