"""QA-only Yandex shadow draft storage (AI-DIALOGUE-02 persistence).

One row per inbound inbox message. Never feeds ReplyPlan / outbox / CRM /
booking / client delivery. generated_text is durable for manual QA read only —
never appears in __repr__ or runtime logs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.pii_gateway import (
    orm_local_column,
    repr_orm_fingerprint,
    repr_orm_literal,
)
from app.core.shadow_draft_types import ShadowDraftDisposition, ShadowDraftReasonCode
from app.db.base import Base

_DISPOSITION_SQL = ", ".join(
    f"'{item.value}'" for item in ShadowDraftDisposition
)
_REASON_CODE_SQL = ", ".join(
    f"'{item.value}'" for item in ShadowDraftReasonCode
)


class YandexShadowDraft(Base):
    """Persisted shadow draft for QA. Not an outbound / CRM artifact."""

    __tablename__ = "yandex_shadow_drafts"
    __table_args__ = (
        UniqueConstraint(
            "inbox_message_id",
            name="uq_yandex_shadow_drafts_inbox_message_id",
        ),
        CheckConstraint(
            f"disposition IN ({_DISPOSITION_SQL})",
            name="ck_yandex_shadow_drafts_disposition",
        ),
        CheckConstraint(
            f"reason_code IN ({_REASON_CODE_SQL})",
            name="ck_yandex_shadow_drafts_reason_code",
        ),
        CheckConstraint(
            "jsonb_typeof(provenance_json) = 'object'",
            name="ck_yandex_shadow_drafts_provenance_object",
        ),
        CheckConstraint(
            "jsonb_typeof(generation_metadata_json) = 'object'",
            name="ck_yandex_shadow_drafts_metadata_object",
        ),
        Index(
            "ix_yandex_shadow_drafts_conversation_created",
            "conversation_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    inbox_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    handoff_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    generated_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    generation_metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        text = orm_local_column(self, "generated_text")
        text_len = len(text) if type(text) is str else 0
        return (
            "YandexShadowDraft("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='shadow_draft_id')}, "
            f"inbox_message_id={repr_orm_fingerprint(orm_local_column(self, 'inbox_message_id'), purpose='inbox_message_id')}, "
            f"conversation_id={repr_orm_fingerprint(orm_local_column(self, 'conversation_id'), purpose='conversation_id')}, "
            f"disposition={repr_orm_literal(orm_local_column(self, 'disposition'))}, "
            f"reason_code={repr_orm_literal(orm_local_column(self, 'reason_code'))}, "
            f"handoff_required={repr_orm_literal(orm_local_column(self, 'handoff_required'))}, "
            f"text_len={text_len!r}, "
            "generated_text=<redacted>, "
            "provenance_json=<redacted>, "
            "generation_metadata_json=<redacted>)"
        )
