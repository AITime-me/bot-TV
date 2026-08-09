"""Encrypted ephemeral PII value storage. Ciphertext only — no plaintext."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, SmallInteger, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import BYTEA, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.pii_gateway import orm_local_column, repr_orm_fingerprint
from app.db.base import Base

_ALLOWED_PURPOSES = (
    "BOOKING_PHONE_WRITE",
    "APPROVED_STAFF_ALERT_PHONE",
    "AMOCRM_CONTACT_SYNC",
    "MASTER_BOOKING_CLIENT_WRITE",
)
_PURPOSE_SQL = ", ".join(f"'{value}'" for value in _ALLOWED_PURPOSES)


class EphemeralPiiValue(Base):
    """Durable encrypted ephemeral PII row. No relationships; no plaintext."""

    __tablename__ = "ephemeral_pii_values"
    __table_args__ = (
        UniqueConstraint(
            "reference_digest",
            name="uq_ephemeral_pii_values_reference_digest",
        ),
        CheckConstraint(
            "octet_length(reference_digest) = 32",
            name="ck_ephemeral_pii_values_reference_digest_len",
        ),
        CheckConstraint(
            "octet_length(nonce) = 12",
            name="ck_ephemeral_pii_values_nonce_len",
        ),
        CheckConstraint(
            "octet_length(ciphertext) >= 16",
            name="ck_ephemeral_pii_values_ciphertext_len",
        ),
        CheckConstraint(
            "crypto_version = 1",
            name="ck_ephemeral_pii_values_crypto_version",
        ),
        CheckConstraint(
            "pii_kind IN ('PHONE', 'CLIENT_NAME')",
            name="ck_ephemeral_pii_values_pii_kind",
        ),
        CheckConstraint(
            f"allowed_purpose IN ({_PURPOSE_SQL})",
            name="ck_ephemeral_pii_values_allowed_purpose",
        ),
        CheckConstraint(
            "key_id ~ '^[A-Z0-9_]{1,64}$'",
            name="ck_ephemeral_pii_values_key_id",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_ephemeral_pii_values_expires_after_created",
        ),
        Index("ix_ephemeral_pii_values_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    reference_digest: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    pii_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    allowed_purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    nonce: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    crypto_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            "EphemeralPiiValue("
            f"id={repr_orm_fingerprint(orm_local_column(self, 'id'), purpose='ephemeral_pii_id')}, "
            f"conversation_id={repr_orm_fingerprint(orm_local_column(self, 'conversation_id'), purpose='conversation_id')}, "
            f"pii_kind={orm_local_column(self, 'pii_kind')!r}, "
            f"allowed_purpose={orm_local_column(self, 'allowed_purpose')!r}, "
            "reference_digest=<redacted>, "
            "ciphertext=<redacted>, "
            "nonce=<redacted>, "
            "key_id=<redacted>)"
        )
