"""Encrypted attachment spool metadata. Ciphertext lives on filesystem only."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import BYTEA, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.pii_gateway import orm_local_column, repr_orm_fingerprint
from app.db.base import Base

_ALLOWED_PURPOSES = (
    "INBOUND_ATTACHMENT_RELAY",
    "OUTBOUND_ATTACHMENT_DELIVERY",
)
_PURPOSE_SQL = ", ".join(f"'{value}'" for value in _ALLOWED_PURPOSES)
_ALLOWED_MIMES = ("image/jpeg", "image/png")
_MIME_SQL = ", ".join(f"'{value}'" for value in _ALLOWED_MIMES)
_MAX_PLAINTEXT = 5 * 1024 * 1024
_GCM_TAG = 16


class AttachmentSpoolObject(Base):
    """Durable attachment metadata. No plaintext, raw reference, or file bytes."""

    __tablename__ = "attachment_spool_objects"
    __table_args__ = (
        UniqueConstraint(
            "reference_digest",
            name="uq_attachment_spool_objects_reference_digest",
        ),
        UniqueConstraint(
            "object_id",
            name="uq_attachment_spool_objects_object_id",
        ),
        CheckConstraint(
            "octet_length(reference_digest) = 32",
            name="ck_attachment_spool_objects_reference_digest_len",
        ),
        CheckConstraint(
            "octet_length(ciphertext_sha256) = 32",
            name="ck_attachment_spool_objects_ciphertext_sha256_len",
        ),
        CheckConstraint(
            "octet_length(nonce) = 12",
            name="ck_attachment_spool_objects_nonce_len",
        ),
        CheckConstraint(
            "crypto_version = 1",
            name="ck_attachment_spool_objects_crypto_version",
        ),
        CheckConstraint(
            "kind = 'IMAGE'",
            name="ck_attachment_spool_objects_kind",
        ),
        CheckConstraint(
            f"purpose IN ({_PURPOSE_SQL})",
            name="ck_attachment_spool_objects_purpose",
        ),
        CheckConstraint(
            f"detected_mime IN ({_MIME_SQL})",
            name="ck_attachment_spool_objects_detected_mime",
        ),
        CheckConstraint(
            "state IN ('WRITING', 'STORED')",
            name="ck_attachment_spool_objects_state",
        ),
        CheckConstraint(
            f"plaintext_size > 0 AND plaintext_size <= {_MAX_PLAINTEXT}",
            name="ck_attachment_spool_objects_plaintext_size",
        ),
        CheckConstraint(
            f"ciphertext_size = plaintext_size + {_GCM_TAG}",
            name="ck_attachment_spool_objects_ciphertext_size",
        ),
        CheckConstraint(
            "key_id ~ '^[A-Z0-9_]{1,64}$'",
            name="ck_attachment_spool_objects_key_id",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_attachment_spool_objects_expires_after_created",
        ),
        Index("ix_attachment_spool_objects_expires_at", "expires_at"),
        Index(
            "ix_attachment_spool_objects_state_updated_at",
            "state",
            "updated_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    reference_digest: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_mime: Mapped[str] = mapped_column(String(64), nullable=False)
    plaintext_size: Mapped[int] = mapped_column(Integer, nullable=False)
    ciphertext_size: Mapped[int] = mapped_column(Integer, nullable=False)
    ciphertext_sha256: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    nonce: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    crypto_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            "AttachmentSpoolObject("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='attachment_id')}, "
            f"object_id={repr_orm_fingerprint(orm_local_column(self, 'object_id'), purpose='attachment_object_id')}, "
            f"conversation_id={repr_orm_fingerprint(orm_local_column(self, 'conversation_id'), purpose='conversation_id')}, "
            f"kind={orm_local_column(self, 'kind')!r}, "
            f"purpose={orm_local_column(self, 'purpose')!r}, "
            f"detected_mime={orm_local_column(self, 'detected_mime')!r}, "
            f"state={orm_local_column(self, 'state')!r}, "
            "reference_digest=<redacted>, "
            "ciphertext_sha256=<redacted>, "
            "nonce=<redacted>, "
            "key_id=<redacted>)"
        )
