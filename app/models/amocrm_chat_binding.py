"""Durable conversation ↔ amoCRM chat binding (AMO-01A).

Maps amocrm_chat_id → existing conversation for deterministic manager routing.
No contact/lead/deal create. Unknown/revoked → fail closed at resolve time.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.pii_gateway import orm_local_column, repr_orm_fingerprint
from app.db.base import Base


class AmocrmChatBindingStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class AmocrmChatBinding(Base):
    """One chat binding row. ACTIVE required for manager ingress routing."""

    __tablename__ = "amocrm_chat_bindings"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            name="uq_amocrm_chat_bindings_conversation_id",
        ),
        UniqueConstraint(
            "amocrm_chat_id",
            name="uq_amocrm_chat_bindings_amocrm_chat_id",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')",
            name="ck_amocrm_chat_bindings_status",
        ),
        CheckConstraint(
            "char_length(amocrm_chat_id) >= 1",
            name="ck_amocrm_chat_bindings_chat_id_nonempty",
        ),
        Index("ix_amocrm_chat_bindings_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "conversations.id",
            name="fk_amocrm_chat_bindings_conversation_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    amocrm_chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("statement_timestamp()"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("statement_timestamp()"),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            "AmocrmChatBinding("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='binding_id')}, "
            f"conversation_id={repr_orm_fingerprint(orm_local_column(self, 'conversation_id'), purpose='conversation_id')}, "
            f"amocrm_chat_id={repr_orm_fingerprint(orm_local_column(self, 'amocrm_chat_id'), purpose='amocrm_chat_id')}, "
            f"status={orm_local_column(self, 'status')!r})"
        )
