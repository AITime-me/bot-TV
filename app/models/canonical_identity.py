"""Canonical client identity graph (CURSOR-30).

``canonical_identities`` holds the stable bot-TV UUID. ``external_identity_links``
maps provider/scope/kind/external_id → canonical identity. No live CRM I/O.
No plaintext name columns. Phone/email stored only as normalized opaque ids.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.pii_gateway import orm_local_column, repr_orm_fingerprint
from app.db.base import Base

_CANONICAL_STATUS_SQL = "'ACTIVE', 'ARCHIVED'"
_LINK_STATUS_SQL = "'ACTIVE', 'REVOKED'"
_CONFIDENCE_SQL = "'CONFIRMED', 'SECONDARY'"
_ENTITY_KIND_SQL = (
    "'CHANNEL_ACCOUNT', 'PHONE', 'EMAIL', 'ONLINE_ZAPIS_CLIENT', "
    "'AMOCRM_CONTACT', 'AMOCRM_BUYER_CARD', 'AMOCRM_TECHNICAL_DEAL', "
    "'AMOCRM_DEAL'"
)


class CanonicalIdentity(Base):
    """Stable internal client identity. Not a name, phone, or amo id."""

    __tablename__ = "canonical_identities"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_CANONICAL_STATUS_SQL})",
            name="ck_canonical_identities_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            "CanonicalIdentity("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='canonical_id')}, "
            f"status={orm_local_column(self, 'status')!r})"
        )


class ExternalIdentityLink(Base):
    """One durable external identity/entity link. History via REVOKED rows."""

    __tablename__ = "external_identity_links"
    __table_args__ = (
        CheckConstraint(
            f"entity_kind IN ({_ENTITY_KIND_SQL})",
            name="ck_external_identity_links_entity_kind",
        ),
        CheckConstraint(
            f"status IN ({_LINK_STATUS_SQL})",
            name="ck_external_identity_links_status",
        ),
        CheckConstraint(
            f"confidence IN ({_CONFIDENCE_SQL})",
            name="ck_external_identity_links_confidence",
        ),
        CheckConstraint(
            "char_length(provider) BETWEEN 1 AND 64",
            name="ck_external_identity_links_provider_len",
        ),
        CheckConstraint(
            "char_length(connection_scope) BETWEEN 1 AND 128",
            name="ck_external_identity_links_connection_scope_len",
        ),
        CheckConstraint(
            "char_length(external_id) BETWEEN 1 AND 256",
            name="ck_external_identity_links_external_id_len",
        ),
        CheckConstraint(
            "char_length(source) BETWEEN 1 AND 64",
            name="ck_external_identity_links_source_len",
        ),
        CheckConstraint(
            "provider ~ '^[!-~]+$'",
            name="ck_external_identity_links_provider_printable_ascii",
        ),
        CheckConstraint(
            "connection_scope ~ '^[!-~]+$'",
            name="ck_external_identity_links_connection_scope_printable_ascii",
        ),
        CheckConstraint(
            "external_id ~ '^[!-~]+$'",
            name="ck_external_identity_links_external_id_printable_ascii",
        ),
        CheckConstraint(
            "source ~ '^[!-~]+$'",
            name="ck_external_identity_links_source_printable_ascii",
        ),
        CheckConstraint(
            "(status = 'ACTIVE' AND revoked_at IS NULL) OR "
            "(status = 'REVOKED' AND revoked_at IS NOT NULL)",
            name="ck_external_identity_links_status_revoked_at",
        ),
        Index(
            "uq_external_identity_links_active_key",
            "provider",
            "connection_scope",
            "entity_kind",
            "external_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        # One ACTIVE amoCRM Lead id: business Deal XOR technical/chat Lead.
        # Buyer Card (Customer) is a separate namespace and is not included.
        Index(
            "uq_external_identity_links_active_amocrm_deal_role",
            "provider",
            "connection_scope",
            "external_id",
            unique=True,
            postgresql_where=text(
                "status = 'ACTIVE' AND entity_kind IN "
                "('AMOCRM_DEAL', 'AMOCRM_TECHNICAL_DEAL')"
            ),
        ),
        Index(
            "ix_external_identity_links_canonical",
            "canonical_identity_id",
        ),
        Index(
            "ix_external_identity_links_lookup",
            "provider",
            "connection_scope",
            "entity_kind",
            "external_id",
        ),
        Index(
            "ix_external_identity_links_kind_external",
            "entity_kind",
            "external_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    canonical_identity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "canonical_identities.id",
            name="fk_external_identity_links_canonical",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    connection_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            "ExternalIdentityLink("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='link_id')}, "
            "canonical_identity_id=<redacted>, "
            f"provider={orm_local_column(self, 'provider')!r}, "
            "connection_scope=<redacted>, "
            f"entity_kind={orm_local_column(self, 'entity_kind')!r}, "
            "external_id=<redacted>, "
            f"status={orm_local_column(self, 'status')!r}, "
            f"confidence={orm_local_column(self, 'confidence')!r})"
        )
