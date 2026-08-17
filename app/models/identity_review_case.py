"""Durable identity review cases (IR-1).

Stores fixed reason codes only — never phone/email/name/raw webhook payloads.
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

from app.core.identity_glue import (
    IDENTITY_REVIEW_REASON_CODES,
    IdentityReviewCaseStatus,
    IdentityReviewReasonCode,
)
from app.core.pii_gateway import orm_local_column, repr_orm_fingerprint
from app.db.base import Base

_STATUS_SQL = "'OPEN', 'RESOLVED'"
_REASON_SQL = (
    "'AMBIGUOUS_RESOLVE', 'CONFLICTING_CANONICAL', 'CANONICAL_NOT_ACTIVE'"
)


class IdentityReviewCase(Base):
    """Manual-review queue for conversation↔canonical attachment conflicts."""

    __tablename__ = "identity_review_cases"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_STATUS_SQL})",
            name="ck_identity_review_cases_status",
        ),
        CheckConstraint(
            f"reason_code IN ({_REASON_SQL})",
            name="ck_identity_review_cases_reason_code",
        ),
        CheckConstraint(
            "("
            "status = 'OPEN' AND resolved_canonical_identity_id IS NULL "
            "AND resolved_at IS NULL"
            ") OR ("
            "status = 'RESOLVED' AND resolved_canonical_identity_id IS NOT NULL "
            "AND resolved_at IS NOT NULL"
            ")",
            name="ck_identity_review_cases_resolved_state",
        ),
        Index(
            "ix_identity_review_cases_conversation_id",
            "conversation_id",
        ),
        Index(
            "ix_identity_review_cases_status",
            "status",
        ),
        Index(
            "uq_identity_review_cases_open_conversation_reason",
            "conversation_id",
            "reason_code",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "conversations.id",
            name="fk_identity_review_cases_conversation",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    proposed_canonical_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "canonical_identities.id",
            name="fk_identity_review_cases_proposed_canonical",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    resolved_canonical_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "canonical_identities.id",
            name="fk_identity_review_cases_resolved_canonical",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    def __repr__(self) -> str:
        return (
            "IdentityReviewCase("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='review_case_id')}, "
            f"conversation_id={repr_orm_fingerprint(orm_local_column(self, 'conversation_id'), purpose='conversation_id')}, "
            f"reason_code={orm_local_column(self, 'reason_code')!r}, "
            f"status={orm_local_column(self, 'status')!r}, "
            "proposed_canonical_identity_id=<redacted>, "
            "resolved_canonical_identity_id=<redacted>)"
        )


assert IDENTITY_REVIEW_REASON_CODES == frozenset(
    code.value for code in IdentityReviewReasonCode
)
assert IdentityReviewCaseStatus.OPEN.value == "OPEN"
assert IdentityReviewCaseStatus.RESOLVED.value == "RESOLVED"
