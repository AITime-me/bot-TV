"""Durable CAPTURE-ONLY rows for native amoCRM outgoing_message webhooks.

No FSM, no ingress worker consumer, no PII / message text columns.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.pii_gateway import (
    orm_local_column,
    repr_orm_fingerprint,
    repr_orm_literal,
)
from app.db.base import Base


class AmocrmNativeOutgoingCapture(Base):
    __tablename__ = "amocrm_native_outgoing_captures"
    __table_args__ = (
        UniqueConstraint(
            "amocrm_message_id",
            name="uq_amocrm_native_outgoing_captures_message_id",
        ),
        CheckConstraint(
            "char_length(amocrm_message_id) BETWEEN 1 AND 128",
            name="ck_amocrm_native_outgoing_captures_message_id_len",
        ),
        CheckConstraint(
            "char_length(chat_id) BETWEEN 1 AND 128",
            name="ck_amocrm_native_outgoing_captures_chat_id_len",
        ),
        CheckConstraint(
            "talk_id > 0",
            name="ck_amocrm_native_outgoing_captures_talk_id_positive",
        ),
        CheckConstraint(
            "contact_id > 0",
            name="ck_amocrm_native_outgoing_captures_contact_id_positive",
        ),
        CheckConstraint(
            "source_id IS NULL OR source_id > 0",
            name="ck_amocrm_native_outgoing_captures_source_id_positive",
        ),
        CheckConstraint(
            "char_length(origin) BETWEEN 1 AND 64",
            name="ck_amocrm_native_outgoing_captures_origin_len",
        ),
        CheckConstraint(
            "type = 'outgoing'",
            name="ck_amocrm_native_outgoing_captures_type",
        ),
        CheckConstraint(
            "message_type = 'text'",
            name="ck_amocrm_native_outgoing_captures_message_type",
        ),
        Index(
            "ix_amocrm_native_outgoing_captures_talk_received",
            "talk_id",
            "received_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    amocrm_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    talk_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    contact_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    origin: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    author_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    author_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    author_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recipient_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recipient_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("statement_timestamp()"),
    )
    # Explicit empty marker column forbidden for body text — do not add text.

    def __repr__(self) -> str:
        return (
            "AmocrmNativeOutgoingCapture("
            f"id={self.id!r}, "
            f"amocrm_message_id={repr_orm_fingerprint(orm_local_column(self, 'amocrm_message_id'), purpose='amocrm_message_id')}, "
            f"talk_id={repr_orm_literal(orm_local_column(self, 'talk_id'))}, "
            f"chat_id={repr_orm_fingerprint(orm_local_column(self, 'chat_id'), purpose='amocrm_chat_id')}, "
            f"contact_id={repr_orm_literal(orm_local_column(self, 'contact_id'))}, "
            f"origin={repr_orm_literal(orm_local_column(self, 'origin'))}, "
            f"author_type={repr_orm_literal(orm_local_column(self, 'author_type'))}, "
            f"type={repr_orm_literal(orm_local_column(self, 'type'))}, "
            f"message_type={repr_orm_literal(orm_local_column(self, 'message_type'))})"
        )
