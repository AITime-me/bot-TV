"""Conversation-scoped amoCRM CRM entity links (AMO-01B2).

CONTACT | TECHNICAL_DEAL.
ACTIVE | REVOKED | RESERVED | RECONCILE_REQUIRED.
Create fencing via lease + create_submitted_at. No chat create. No booking SoT.
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
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.pii_gateway import orm_local_column, repr_orm_fingerprint
from app.db.base import Base


class AmocrmEntityKind(str, enum.Enum):
    CONTACT = "CONTACT"
    TECHNICAL_DEAL = "TECHNICAL_DEAL"


class AmocrmEntityLinkStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    RESERVED = "RESERVED"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"


_OPEN_STATUSES_SQL = (
    "'ACTIVE', 'RESERVED', 'RECONCILE_REQUIRED'"
)


class AmocrmEntityLink(Base):
    __tablename__ = "amocrm_entity_links"
    __table_args__ = (
        CheckConstraint(
            "entity_kind IN ('CONTACT', 'TECHNICAL_DEAL')",
            name="ck_amocrm_entity_links_entity_kind",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED', 'RESERVED', 'RECONCILE_REQUIRED')",
            name="ck_amocrm_entity_links_status",
        ),
        CheckConstraint(
            "("
            "status IN ('RESERVED', 'RECONCILE_REQUIRED') "
            "AND (external_id IS NULL OR char_length(external_id) >= 1)"
            ") OR ("
            "status IN ('ACTIVE', 'REVOKED') "
            "AND external_id IS NOT NULL AND char_length(external_id) >= 1"
            ")",
            name="ck_amocrm_entity_links_external_id_state",
        ),
        CheckConstraint(
            "lease_version >= 0",
            name="ck_amocrm_entity_links_lease_version_nonnegative",
        ),
        Index("ix_amocrm_entity_links_conversation_id", "conversation_id"),
        Index("ix_amocrm_entity_links_status", "status"),
        Index("ix_amocrm_entity_links_lease_until", "lease_until"),
        Index(
            "uq_amocrm_entity_links_open_conversation_kind",
            "conversation_id",
            "entity_kind",
            unique=True,
            postgresql_where=text(f"status IN ({_OPEN_STATUSES_SQL})"),
        ),
        Index(
            "uq_amocrm_entity_links_active_kind_external",
            "entity_kind",
            "external_id",
            unique=True,
            postgresql_where=text(
                "status = 'ACTIVE' AND external_id IS NOT NULL"
            ),
        ),
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
            name="fk_amocrm_entity_links_conversation_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    lease_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    create_submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("statement_timestamp()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("statement_timestamp()"),
    )

    def __repr__(self) -> str:
        return (
            "AmocrmEntityLink("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='entity_link_id')}, "
            f"conversation_id={repr_orm_fingerprint(orm_local_column(self, 'conversation_id'), purpose='conversation_id')}, "
            f"entity_kind={orm_local_column(self, 'entity_kind')!r}, "
            f"external_id={repr_orm_fingerprint(orm_local_column(self, 'external_id'), purpose='amocrm_external_id')}, "
            f"status={orm_local_column(self, 'status')!r}, "
            f"lease_version={orm_local_column(self, 'lease_version')!r})"
        )
