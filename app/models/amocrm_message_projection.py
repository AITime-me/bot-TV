"""Durable amoCRM Chat message projection queue + ledger (AMO-01B1).

Stores no message text. Text is loaded at claim-time from inbox
(CLIENT_INBOUND / B1a) or ``outbox_messages.payload_json.text``
(BOT_OUTBOUND / B1b). Projection is not a client-delivery path.
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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

INTEGRATION_MSGID_MAX_LENGTH = 40
DEFAULT_PROJECTION_MAX_ATTEMPTS = 5


class AmocrmProjectionSourceKind(str, enum.Enum):
    CLIENT_INBOUND = "CLIENT_INBOUND"
    BOT_OUTBOUND = "BOT_OUTBOUND"


class AmocrmProjectionStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROJECTED = "PROJECTED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    DEAD = "DEAD"


class AmocrmProjectionSkipReason(str, enum.Enum):
    BINDING_UNKNOWN = "BINDING_UNKNOWN"
    BINDING_REVOKED = "BINDING_REVOKED"
    BINDING_INTEGRATION_CONVERSATION_MISSING = (
        "BINDING_INTEGRATION_CONVERSATION_MISSING"
    )
    SOURCE_MISSING = "SOURCE_MISSING"
    SOURCE_EMPTY_TEXT = "SOURCE_EMPTY_TEXT"
    SOURCE_NOT_DELIVERED = "SOURCE_NOT_DELIVERED"
    EGRESS_DISABLED = "EGRESS_DISABLED"


AMOCRM_PROJECTION_TRANSITIONS: dict[
    AmocrmProjectionStatus, frozenset[AmocrmProjectionStatus]
] = {
    AmocrmProjectionStatus.PENDING: frozenset({AmocrmProjectionStatus.PROCESSING}),
    AmocrmProjectionStatus.PROCESSING: frozenset(
        {
            AmocrmProjectionStatus.PROJECTED,
            AmocrmProjectionStatus.SKIPPED,
            AmocrmProjectionStatus.FAILED,
            AmocrmProjectionStatus.DEAD,
        }
    ),
    AmocrmProjectionStatus.FAILED: frozenset(
        {
            AmocrmProjectionStatus.PROCESSING,
            AmocrmProjectionStatus.DEAD,
        }
    ),
    AmocrmProjectionStatus.PROJECTED: frozenset(),
    AmocrmProjectionStatus.SKIPPED: frozenset(),
    AmocrmProjectionStatus.DEAD: frozenset(),
}


def amocrm_projection_transition_allowed(
    current: AmocrmProjectionStatus | str,
    target: AmocrmProjectionStatus | str,
) -> bool:
    current_status = (
        current
        if isinstance(current, AmocrmProjectionStatus)
        else AmocrmProjectionStatus(current)
    )
    target_status = (
        target
        if isinstance(target, AmocrmProjectionStatus)
        else AmocrmProjectionStatus(target)
    )
    return target_status in AMOCRM_PROJECTION_TRANSITIONS[current_status]


def integration_msgid_for_source(
    *,
    source_kind: AmocrmProjectionSourceKind | str,
    source_id: uuid.UUID,
) -> str:
    """Deterministic integration msgid (<=40 ASCII)."""

    kind = (
        source_kind
        if isinstance(source_kind, AmocrmProjectionSourceKind)
        else AmocrmProjectionSourceKind(source_kind)
    )
    prefix = "c" if kind is AmocrmProjectionSourceKind.CLIENT_INBOUND else "b"
    msgid = f"{prefix}{source_id.hex}"
    if len(msgid) > INTEGRATION_MSGID_MAX_LENGTH:
        raise ValueError("INTEGRATION_MSGID_TOO_LONG")
    return msgid


def integration_conversation_id_for(conversation_id: uuid.UUID) -> str:
    """Deprecated helper — egress must use binding.integration_conversation_id."""

    return conversation_id.hex


class AmocrmMessageProjection(Base):
    """Queue row + projection ledger for Chat egress."""

    __tablename__ = "amocrm_message_projections"
    __table_args__ = (
        UniqueConstraint(
            "source_kind",
            "source_id",
            name="uq_amocrm_message_projections_source",
        ),
        UniqueConstraint(
            "integration_msgid",
            name="uq_amocrm_message_projections_integration_msgid",
        ),
        UniqueConstraint(
            "amocrm_message_id",
            name="uq_amocrm_message_projections_amocrm_message_id",
        ),
        CheckConstraint(
            "source_kind IN ('CLIENT_INBOUND', 'BOT_OUTBOUND')",
            name="ck_amocrm_message_projections_source_kind",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'PROJECTED', 'SKIPPED', "
            "'FAILED', 'DEAD')",
            name="ck_amocrm_message_projections_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_amocrm_message_projections_attempt_count_nonnegative",
        ),
        CheckConstraint(
            "max_attempts > 0",
            name="ck_amocrm_message_projections_max_attempts_positive",
        ),
        CheckConstraint(
            "lease_version >= 0",
            name="ck_amocrm_message_projections_lease_version_nonnegative",
        ),
        CheckConstraint(
            "integration_msgid ~ '^[cb][0-9a-f]{32}$'",
            name="ck_amocrm_message_projections_integration_msgid_format",
        ),
        CheckConstraint(
            "(status = 'PROJECTED' AND amocrm_message_id IS NOT NULL) OR "
            "(status <> 'PROJECTED')",
            name="ck_amocrm_message_projections_projected_has_amo_id",
        ),
        Index(
            "ix_amocrm_message_projections_status_next_attempt_at",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_amocrm_message_projections_lease_until",
            "lease_until",
        ),
        Index(
            "ix_amocrm_message_projections_conversation_id",
            "conversation_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    integration_msgid: Mapped[str] = mapped_column(String(40), nullable=False)
    amocrm_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=AmocrmProjectionStatus.PENDING.value,
        server_default=text("'PENDING'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_PROJECTION_MAX_ATTEMPTS,
        server_default=text("5"),
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    lease_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
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
            "AmocrmMessageProjection("
            f"id={self.id!r}, "
            f"source_kind={self.source_kind!r}, "
            f"status={self.status!r}, "
            f"attempt_count={self.attempt_count!r}, "
            "integration_msgid=<redacted>, amocrm_message_id=<redacted>)"
        )
