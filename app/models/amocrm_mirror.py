from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

MIRROR_KEY_MAX_LENGTH = 160
MIRROR_PAYLOAD_SCHEMA = "amocrm.mirror.job.v1"
DEFAULT_MIRROR_MAX_ATTEMPTS = 5


class AmoCrmMirrorJobType(str, enum.Enum):
    """bot-TV domain events queued for mirroring — not amoCRM entities.

    Job types stay in bot-TV vocabulary. AMO-01B2 converges required CRM
    entity state (one TECHNICAL_DEAL) in the adapter — not via new job types.
    """

    CLIENT_MESSAGE_RECEIVED_META = "CLIENT_MESSAGE_RECEIVED_META"
    REPLY_PLAN_STATE_CHANGED = "REPLY_PLAN_STATE_CHANGED"
    MANAGER_TAKEOVER = "MANAGER_TAKEOVER"
    OUTBOUND_DELIVERED_META = "OUTBOUND_DELIVERED_META"


class AmoCrmMirrorSubjectKind(str, enum.Enum):
    """Internal bot-TV subject a job refers to."""

    CONVERSATION = "CONVERSATION"
    INBOX_MESSAGE = "INBOX_MESSAGE"
    REPLY_PLAN = "REPLY_PLAN"
    OUTBOX_MESSAGE = "OUTBOX_MESSAGE"


class AmoCrmMirrorStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    # MIRRORED: required amoCRM entity state for this mirror job converged successfully.
    # Not "message content copied to CRM".
    MIRRORED = "MIRRORED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    DEAD = "DEAD"


class AmoCrmMirrorSkipReason(str, enum.Enum):
    """Terminal, non-error refusals to mirror an outdated event."""

    MANAGER_TAKEOVER = "MANAGER_TAKEOVER"
    STALE_CONTEXT = "STALE_CONTEXT"
    SUBJECT_STATE_CHANGED = "SUBJECT_STATE_CHANGED"


# PROCESSING→PROCESSING is lease reclaim (same status, new fencing token) and is
# handled outside this map, exactly as for ingress and reply plans.
AMOCRM_MIRROR_TRANSITIONS: dict[AmoCrmMirrorStatus, frozenset[AmoCrmMirrorStatus]] = {
    AmoCrmMirrorStatus.PENDING: frozenset({AmoCrmMirrorStatus.PROCESSING}),
    AmoCrmMirrorStatus.PROCESSING: frozenset(
        {
            AmoCrmMirrorStatus.MIRRORED,
            AmoCrmMirrorStatus.SKIPPED,
            AmoCrmMirrorStatus.FAILED,
            AmoCrmMirrorStatus.DEAD,
        }
    ),
    AmoCrmMirrorStatus.FAILED: frozenset(
        {
            AmoCrmMirrorStatus.PROCESSING,
            AmoCrmMirrorStatus.DEAD,
        }
    ),
    AmoCrmMirrorStatus.MIRRORED: frozenset(),
    AmoCrmMirrorStatus.SKIPPED: frozenset(),
    AmoCrmMirrorStatus.DEAD: frozenset(),
}

TERMINAL_AMOCRM_MIRROR_STATUSES = frozenset(
    {
        AmoCrmMirrorStatus.MIRRORED,
        AmoCrmMirrorStatus.SKIPPED,
        AmoCrmMirrorStatus.DEAD,
    }
)


def amocrm_mirror_transition_allowed(
    current: AmoCrmMirrorStatus | str,
    target: AmoCrmMirrorStatus | str,
) -> bool:
    current_status = (
        current
        if isinstance(current, AmoCrmMirrorStatus)
        else AmoCrmMirrorStatus(current)
    )
    target_status = (
        target if isinstance(target, AmoCrmMirrorStatus) else AmoCrmMirrorStatus(target)
    )
    return target_status in AMOCRM_MIRROR_TRANSITIONS[current_status]


class MirrorPayloadViolation(RuntimeError):
    """Raised when a mirror payload is not whitelist-clean."""


ALLOWED_MIRROR_PAYLOAD_KEYS = frozenset(
    {
        "schema",
        "job_type",
        "subject_kind",
        "subject_id",
        "conversation_id",
        "context_version",
        "subject_status",
    }
)

# Client text, contacts and provider identifiers must never reach amoCRM
# payloads. The only client text in bot-TV lives in inbox/outbox payloads.
FORBIDDEN_MIRROR_PAYLOAD_KEYS = frozenset(
    {
        "text",
        "draft_text",
        "phone",
        "email",
        "client_name",
        "external_conversation_id",
        "external_message_id",
        "external_event_id",
        "envelope_json",
        "payload_json",
    }
)


def assert_mirror_payload_is_safe(payload: Mapping[str, Any]) -> None:
    """Fail closed on any key or value shape outside the whitelist."""
    keys = set(payload)
    forbidden = keys & FORBIDDEN_MIRROR_PAYLOAD_KEYS
    if forbidden:
        raise MirrorPayloadViolation("MIRROR_PAYLOAD_FORBIDDEN_KEY")
    if not keys <= ALLOWED_MIRROR_PAYLOAD_KEYS:
        raise MirrorPayloadViolation("MIRROR_PAYLOAD_KEY_NOT_ALLOWED")
    if payload.get("schema") != MIRROR_PAYLOAD_SCHEMA:
        raise MirrorPayloadViolation("MIRROR_PAYLOAD_SCHEMA_MISMATCH")
    for value in payload.values():
        # Flat scalars only: nested containers could smuggle client data.
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise MirrorPayloadViolation("MIRROR_PAYLOAD_VALUE_NOT_ALLOWED")


def safe_mirror_payload(
    *,
    job_type: AmoCrmMirrorJobType,
    subject_kind: AmoCrmMirrorSubjectKind,
    subject_id: uuid.UUID,
    conversation_id: uuid.UUID,
    context_version: int | None = None,
    subject_status: str | None = None,
) -> dict[str, Any]:
    """Build the only payload shape a mirror job may carry.

    conversation_id is internal subject identity for revalidation, never an
    amoCRM entity reference.
    """
    payload: dict[str, Any] = {
        "schema": MIRROR_PAYLOAD_SCHEMA,
        "job_type": job_type.value,
        "subject_kind": subject_kind.value,
        "subject_id": str(subject_id),
        "conversation_id": str(conversation_id),
    }
    if context_version is not None:
        payload["context_version"] = int(context_version)
    if subject_status is not None:
        payload["subject_status"] = str(subject_status)
    assert_mirror_payload_is_safe(payload)
    return payload


def client_message_mirror_key(inbox_id: uuid.UUID) -> str:
    return f"client-message-meta:{inbox_id}"


def reply_plan_state_mirror_key(plan_id: uuid.UUID, status: str) -> str:
    return f"reply-plan-state:{plan_id}:{status}"


def manager_takeover_mirror_key(conversation_id: uuid.UUID) -> str:
    return f"manager-takeover:{conversation_id}"


def outbound_delivered_mirror_key(outbound_id: uuid.UUID) -> str:
    return f"outbound-delivered:{outbound_id}"


class AmoCrmMirrorJob(Base):
    """Transactional outbox of bot-TV → amoCRM domain events (CURSOR-09).

    Enqueued inside the domain transaction that produced the event and drained
    by a leased worker. ``MIRRORED`` means required amoCRM entity state for
    this job converged successfully, not that message text was copied to CRM.
    Payload stays metadata-only. __repr__ intentionally omits payload contents.
    """

    __tablename__ = "amocrm_mirror_jobs"
    __table_args__ = (
        UniqueConstraint("mirror_key", name="uq_amocrm_mirror_key"),
        CheckConstraint(
            "job_type IN ('CLIENT_MESSAGE_RECEIVED_META', "
            "'REPLY_PLAN_STATE_CHANGED', 'MANAGER_TAKEOVER', "
            "'OUTBOUND_DELIVERED_META')",
            name="ck_amocrm_mirror_job_type",
        ),
        CheckConstraint(
            "subject_kind IN ('CONVERSATION', 'INBOX_MESSAGE', 'REPLY_PLAN', "
            "'OUTBOX_MESSAGE')",
            name="ck_amocrm_mirror_subject_kind",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'MIRRORED', 'SKIPPED', "
            "'FAILED', 'DEAD')",
            name="ck_amocrm_mirror_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_amocrm_mirror_attempt_count_nonnegative",
        ),
        CheckConstraint(
            "max_attempts > 0",
            name="ck_amocrm_mirror_max_attempts_positive",
        ),
        CheckConstraint(
            "lease_version >= 0",
            name="ck_amocrm_mirror_lease_version_nonnegative",
        ),
        CheckConstraint(
            "context_version IS NULL OR context_version >= 0",
            name="ck_amocrm_mirror_context_version_nonnegative",
        ),
        Index(
            "ix_amocrm_mirror_jobs_status_next_attempt_at",
            "status",
            "next_attempt_at",
        ),
        Index("ix_amocrm_mirror_jobs_lease_until", "lease_until"),
        Index("ix_amocrm_mirror_jobs_conversation_id", "conversation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    job_type: Mapped[str] = mapped_column(String(48), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    context_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mirror_key: Mapped[str] = mapped_column(
        String(MIRROR_KEY_MAX_LENGTH),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=AmoCrmMirrorStatus.PENDING.value,
        server_default=text("'PENDING'"),
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_MIRROR_MAX_ATTEMPTS,
        server_default=text(str(DEFAULT_MIRROR_MAX_ATTEMPTS)),
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
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"AmoCrmMirrorJob(id={self.id!r}, job_type={self.job_type!r}, "
            f"subject_kind={self.subject_kind!r}, subject_id={self.subject_id!r}, "
            f"conversation_id={self.conversation_id!r}, "
            f"context_version={self.context_version!r}, status={self.status!r}, "
            f"attempt_count={self.attempt_count!r}, "
            f"lease_version={self.lease_version!r}, "
            f"skip_reason={self.skip_reason!r}, payload=<redacted>)"
        )
