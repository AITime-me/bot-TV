"""Durable master ↔ channel-account bindings (CURSOR-27).

Maps (channel, connection_scope, external_account_id) → online-zapis-tv masterId.
No plaintext PII fields. No FK to online-zapis-tv. No live channel adapters.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.pii_gateway import orm_local_column, repr_orm_fingerprint
from app.db.base import Base

_CHANNEL_SQL = "'synthetic', 'vk', 'max'"
_STATUS_SQL = "'ACTIVE', 'REVOKED'"
_MASTER_ID_RE = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class MasterChannelBinding(Base):
    """One durable bind row. History kept via REVOKED rows; ≤1 ACTIVE per identity."""

    __tablename__ = "master_channel_bindings"
    __table_args__ = (
        CheckConstraint(
            f"channel IN ({_CHANNEL_SQL})",
            name="ck_master_channel_bindings_channel",
        ),
        CheckConstraint(
            f"status IN ({_STATUS_SQL})",
            name="ck_master_channel_bindings_status",
        ),
        CheckConstraint(
            "char_length(connection_scope) BETWEEN 1 AND 128",
            name="ck_master_channel_bindings_connection_scope_len",
        ),
        CheckConstraint(
            "char_length(external_account_id) BETWEEN 1 AND 128",
            name="ck_master_channel_bindings_external_account_id_len",
        ),
        # Printable ASCII excluding space/DEL — locale-independent, == ^[\x21-\x7E]+$
        CheckConstraint(
            "connection_scope ~ '^[!-~]+$'",
            name="ck_master_channel_bindings_connection_scope_printable_ascii",
        ),
        CheckConstraint(
            "external_account_id ~ '^[!-~]+$'",
            name="ck_master_channel_bindings_external_account_id_printable_ascii",
        ),
        CheckConstraint(
            f"master_id ~ '{_MASTER_ID_RE}'",
            name="ck_master_channel_bindings_master_id",
        ),
        CheckConstraint(
            "(status = 'ACTIVE' AND revoked_at IS NULL) OR "
            "(status = 'REVOKED' AND revoked_at IS NOT NULL)",
            name="ck_master_channel_bindings_status_revoked_at",
        ),
        Index(
            "uq_master_channel_bindings_active_identity",
            "channel",
            "connection_scope",
            "external_account_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index(
            "ix_master_channel_bindings_master_id",
            "master_id",
        ),
        Index(
            "ix_master_channel_bindings_identity",
            "channel",
            "connection_scope",
            "external_account_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    connection_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    master_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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
            "MasterChannelBinding("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='binding_id')}, "
            f"channel={orm_local_column(self, 'channel')!r}, "
            "connection_scope=<redacted>, "
            "external_account_id=<redacted>, "
            "master_id=<redacted>, "
            f"status={orm_local_column(self, 'status')!r})"
        )
