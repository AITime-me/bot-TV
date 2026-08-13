"""Durable encrypted amoCRM CRM OAuth token row (AMO-01B2).

Ciphertext only. Chat HMAC secrets never stored here.
"""

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
    text,
)
from sqlalchemy.dialects.postgresql import BYTEA, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AmocrmCrmOauthToken(Base):
    __tablename__ = "amocrm_crm_oauth_tokens"
    __table_args__ = (
        UniqueConstraint(
            "connection_scope",
            name="uq_amocrm_crm_oauth_tokens_connection_scope",
        ),
        CheckConstraint(
            "char_length(connection_scope) BETWEEN 1 AND 64",
            name="ck_amocrm_crm_oauth_tokens_scope_len",
        ),
        CheckConstraint(
            "crypto_version = 1",
            name="ck_amocrm_crm_oauth_tokens_crypto_version",
        ),
        CheckConstraint(
            "octet_length(access_nonce) = 12",
            name="ck_amocrm_crm_oauth_tokens_access_nonce_len",
        ),
        CheckConstraint(
            "octet_length(refresh_nonce) = 12",
            name="ck_amocrm_crm_oauth_tokens_refresh_nonce_len",
        ),
        CheckConstraint(
            "octet_length(access_ciphertext) >= 16",
            name="ck_amocrm_crm_oauth_tokens_access_ct_len",
        ),
        CheckConstraint(
            "octet_length(refresh_ciphertext) >= 16",
            name="ck_amocrm_crm_oauth_tokens_refresh_ct_len",
        ),
        CheckConstraint(
            "key_id ~ '^[A-Z0-9_]{1,64}$'",
            name="ck_amocrm_crm_oauth_tokens_key_id",
        ),
        CheckConstraint(
            "lease_version >= 0",
            name="ck_amocrm_crm_oauth_tokens_lease_version_nonnegative",
        ),
        Index("ix_amocrm_crm_oauth_tokens_lease_until", "lease_until"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    connection_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    crypto_version: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("1"),
    )
    access_nonce: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    access_ciphertext: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    refresh_nonce: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    refresh_ciphertext: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    access_expires_at: Mapped[datetime | None] = mapped_column(
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
        server_default=text("0"),
    )
    lease_until: Mapped[datetime | None] = mapped_column(
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
            "AmocrmCrmOauthToken("
            f"id={self.id!r}, "
            "connection_scope=<redacted>, key_id=<redacted>, "
            "access=<redacted>, refresh=<redacted>, "
            f"lease_version={self.lease_version!r})"
        )
