"""Durable last-verified control-plane publication snapshots (local cache only)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_KIND_SQL = "'SETTINGS', 'KNOWLEDGE'"


class ControlPlaneSnapshot(Base):
    """One row per publication kind. online-zapis remains Source of Truth."""

    __tablename__ = "control_plane_snapshots"
    __table_args__ = (
        CheckConstraint(
            f"kind IN ({_KIND_SQL})",
            name="ck_control_plane_snapshots_kind",
        ),
        CheckConstraint(
            "schema_version >= 1",
            name="ck_control_plane_snapshots_schema_version",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_control_plane_snapshots_version",
        ),
        CheckConstraint(
            "char_length(publication_id) BETWEEN 1 AND 64",
            name="ck_control_plane_snapshots_publication_id_len",
        ),
        CheckConstraint(
            "char_length(checksum) = 64",
            name="ck_control_plane_snapshots_checksum_len",
        ),
        CheckConstraint(
            "checksum ~ '^[0-9a-f]{64}$'",
            name="ck_control_plane_snapshots_checksum_hex",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_control_plane_snapshots_payload_object",
        ),
        Index(
            "ix_control_plane_snapshots_verified_at",
            "verified_at",
        ),
    )

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    publication_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    usable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    last_error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"ControlPlaneSnapshot(kind={self.kind!r}, "
            f"publication_id={self.publication_id!r}, "
            f"version={self.version!r}, "
            f"usable={self.usable!r}, "
            f"last_error_code={self.last_error_code!r})"
        )
