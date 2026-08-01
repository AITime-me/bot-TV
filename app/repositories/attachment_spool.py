"""Repository for attachment spool metadata. No crypto, plaintext, or tokens."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pii_gateway import repr_orm_fingerprint
from app.models.attachment_spool import AttachmentSpoolObject


def _db_uuid(value: object) -> uuid.UUID:
    if type(value) is uuid.UUID:
        return value
    return uuid.UUID(str(value))


@dataclass(frozen=True, slots=True, repr=False)
class AttachmentSpoolRow:
    """Internal metadata projection. Never exposed outside service."""

    id: uuid.UUID
    object_id: uuid.UUID
    conversation_id: uuid.UUID
    kind: str
    purpose: str
    detected_mime: str
    plaintext_size: int
    ciphertext_size: int
    ciphertext_sha256: bytes
    nonce: bytes
    key_id: str
    crypto_version: int
    state: str
    reference_digest: bytes
    expires_at: datetime | None = None
    lease_token_digest: bytes | None = None
    leased_at: datetime | None = None
    lease_expires_at: datetime | None = None

    def __repr__(self) -> str:
        return (
            "AttachmentSpoolRow("
            f"id={repr_orm_fingerprint(self.id, purpose='attachment_id')}, "
            f"object_id={repr_orm_fingerprint(self.object_id, purpose='attachment_object_id')}, "
            f"conversation_id={repr_orm_fingerprint(self.conversation_id, purpose='conversation_id')}, "
            f"kind={self.kind!r}, "
            f"purpose={self.purpose!r}, "
            f"detected_mime={self.detected_mime!r}, "
            f"state={self.state!r}, "
            f"plaintext_size={self.plaintext_size!r}, "
            f"ciphertext_size={self.ciphertext_size!r}, "
            "reference_digest=<redacted>, "
            "ciphertext_sha256=<redacted>, "
            "nonce=<redacted>, "
            "key_id=<redacted>, "
            "lease_token_digest=<redacted>)"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


def _to_row(row: AttachmentSpoolObject) -> AttachmentSpoolRow:
    return AttachmentSpoolRow(
        id=_db_uuid(row.id),
        object_id=_db_uuid(row.object_id),
        conversation_id=_db_uuid(row.conversation_id),
        kind=row.kind,
        purpose=row.purpose,
        detected_mime=row.detected_mime,
        plaintext_size=int(row.plaintext_size),
        ciphertext_size=int(row.ciphertext_size),
        ciphertext_sha256=row.ciphertext_sha256,
        nonce=row.nonce,
        key_id=row.key_id,
        crypto_version=int(row.crypto_version),
        state=row.state,
        reference_digest=row.reference_digest,
        expires_at=row.expires_at,
        lease_token_digest=row.lease_token_digest,
        leased_at=row.leased_at,
        lease_expires_at=row.lease_expires_at,
    )


async def fetch_statement_timestamp(session: AsyncSession) -> datetime:
    """Fresh PostgreSQL statement_timestamp() for eligibility decisions."""
    value = await session.scalar(select(func.statement_timestamp()))
    if type(value) is not datetime:
        raise RuntimeError("statement_timestamp unavailable")
    return value


async def insert_writing(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
    reference_digest: bytes,
    object_id: uuid.UUID,
    conversation_id: uuid.UUID,
    kind: str,
    purpose: str,
    detected_mime: str,
    plaintext_size: int,
    ciphertext_size: int,
    ciphertext_sha256: bytes,
    nonce: bytes,
    key_id: str,
    crypto_version: int,
    ttl_seconds: int,
) -> bool:
    """Insert WRITING metadata. Returns True when inserted, False on digest collision."""
    stmt = (
        insert(AttachmentSpoolObject)
        .values(
            id=row_id,
            reference_digest=reference_digest,
            object_id=object_id,
            conversation_id=conversation_id,
            kind=kind,
            purpose=purpose,
            detected_mime=detected_mime,
            plaintext_size=plaintext_size,
            ciphertext_size=ciphertext_size,
            ciphertext_sha256=ciphertext_sha256,
            nonce=nonce,
            key_id=key_id,
            crypto_version=crypto_version,
            state="WRITING",
            created_at=func.statement_timestamp(),
            updated_at=func.statement_timestamp(),
            expires_at=func.statement_timestamp()
            + func.make_interval(0, 0, 0, 0, 0, 0, ttl_seconds),
        )
        .on_conflict_do_nothing(
            index_elements=[AttachmentSpoolObject.reference_digest],
        )
        .returning(AttachmentSpoolObject.id)
    )
    inserted_id = await session.scalar(stmt)
    return inserted_id is not None


async def select_for_update_by_id(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
) -> AttachmentSpoolRow | None:
    stmt = (
        select(AttachmentSpoolObject)
        .where(AttachmentSpoolObject.id == row_id)
        .with_for_update()
    )
    row = await session.scalar(stmt)
    if row is None:
        return None
    return _to_row(row)


async def mark_stored(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
) -> bool:
    result = await session.execute(
        update(AttachmentSpoolObject)
        .where(
            AttachmentSpoolObject.id == row_id,
            AttachmentSpoolObject.state == "WRITING",
        )
        .values(
            state="STORED",
            updated_at=func.statement_timestamp(),
        )
        .returning(AttachmentSpoolObject.id)
    )
    return result.scalar_one_or_none() is not None


async def delete_by_id(session: AsyncSession, *, row_id: uuid.UUID) -> None:
    await session.execute(
        delete(AttachmentSpoolObject).where(AttachmentSpoolObject.id == row_id)
    )
    await session.flush()


async def delete_by_object_id(session: AsyncSession, *, object_id: uuid.UUID) -> bool:
    result = await session.execute(
        delete(AttachmentSpoolObject)
        .where(AttachmentSpoolObject.object_id == object_id)
        .returning(AttachmentSpoolObject.id)
    )
    await session.flush()
    return result.scalar_one_or_none() is not None


async def exists_by_object_id(
    session: AsyncSession,
    *,
    object_id: uuid.UUID,
) -> bool:
    found = await session.scalar(
        select(AttachmentSpoolObject.id).where(
            AttachmentSpoolObject.object_id == object_id
        )
    )
    return found is not None


async def select_stale_writing_for_reconcile(
    session: AsyncSession,
    *,
    grace_seconds: int,
    limit: int,
) -> list[AttachmentSpoolRow]:
    """Lock stale WRITING rows. Post-lock grace rechecked by caller."""
    cutoff = func.statement_timestamp() - func.make_interval(
        0, 0, 0, 0, 0, 0, grace_seconds
    )
    stmt = (
        select(AttachmentSpoolObject)
        .where(
            AttachmentSpoolObject.state == "WRITING",
            AttachmentSpoolObject.updated_at < cutoff,
        )
        .order_by(AttachmentSpoolObject.updated_at, AttachmentSpoolObject.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = (await session.scalars(stmt)).all()
    return [_to_row(row) for row in rows]


async def row_still_stale_writing(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
    grace_seconds: int,
) -> bool:
    """Fresh post-lock grace check with a new statement_timestamp()."""
    cutoff = func.statement_timestamp() - func.make_interval(
        0, 0, 0, 0, 0, 0, grace_seconds
    )
    found = await session.scalar(
        select(AttachmentSpoolObject.id).where(
            AttachmentSpoolObject.id == row_id,
            AttachmentSpoolObject.state == "WRITING",
            AttachmentSpoolObject.updated_at < cutoff,
        )
    )
    return found is not None


async def select_stored_missing_file_candidates(
    session: AsyncSession,
    *,
    limit: int,
) -> list[AttachmentSpoolRow]:
    """Lock STORED rows for unrecoverable-file reconciliation (bounded)."""
    stmt = (
        select(AttachmentSpoolObject)
        .where(AttachmentSpoolObject.state == "STORED")
        .order_by(AttachmentSpoolObject.updated_at, AttachmentSpoolObject.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = (await session.scalars(stmt)).all()
    return [_to_row(row) for row in rows]


async def select_for_update_by_reference_digest(
    session: AsyncSession,
    *,
    reference_digest: bytes,
) -> AttachmentSpoolRow | None:
    stmt = (
        select(AttachmentSpoolObject)
        .where(AttachmentSpoolObject.reference_digest == reference_digest)
        .with_for_update()
    )
    row = await session.scalar(stmt)
    if row is None:
        return None
    return _to_row(row)


async def select_for_update_by_lease_digest(
    session: AsyncSession,
    *,
    lease_token_digest: bytes,
) -> AttachmentSpoolRow | None:
    stmt = (
        select(AttachmentSpoolObject)
        .where(AttachmentSpoolObject.lease_token_digest == lease_token_digest)
        .with_for_update()
    )
    row = await session.scalar(stmt)
    if row is None:
        return None
    return _to_row(row)


async def select_expired_leased_for_reclaim(
    session: AsyncSession,
    *,
    limit: int,
) -> list[AttachmentSpoolRow]:
    stmt = (
        select(AttachmentSpoolObject)
        .where(
            AttachmentSpoolObject.state == "LEASED",
            AttachmentSpoolObject.lease_expires_at <= func.statement_timestamp(),
        )
        .order_by(
            AttachmentSpoolObject.lease_expires_at,
            AttachmentSpoolObject.id,
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = (await session.scalars(stmt)).all()
    return [_to_row(row) for row in rows]


async def clear_lease_to_stored(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
) -> bool:
    result = await session.execute(
        update(AttachmentSpoolObject)
        .where(
            AttachmentSpoolObject.id == row_id,
            AttachmentSpoolObject.state == "LEASED",
        )
        .values(
            state="STORED",
            lease_token_digest=None,
            leased_at=None,
            lease_expires_at=None,
            updated_at=func.statement_timestamp(),
        )
        .returning(AttachmentSpoolObject.id)
    )
    return result.scalar_one_or_none() is not None


async def apply_lease(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
    lease_token_digest: bytes,
    lease_ttl_seconds: int,
) -> AttachmentSpoolRow | None:
    result = await session.execute(
        update(AttachmentSpoolObject)
        .where(
            AttachmentSpoolObject.id == row_id,
            AttachmentSpoolObject.state == "STORED",
        )
        .values(
            state="LEASED",
            lease_token_digest=lease_token_digest,
            leased_at=func.statement_timestamp(),
            lease_expires_at=func.statement_timestamp()
            + func.make_interval(0, 0, 0, 0, 0, 0, lease_ttl_seconds),
            updated_at=func.statement_timestamp(),
        )
        .returning(AttachmentSpoolObject)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return _to_row(row)


async def transition_leased_to_delete_pending(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
    lease_token_digest: bytes,
) -> AttachmentSpoolRow | None:
    """Conditionally transition LEASED → DELETE_PENDING. Lease active at UPDATE time."""
    result = await session.execute(
        update(AttachmentSpoolObject)
        .where(
            AttachmentSpoolObject.id == row_id,
            AttachmentSpoolObject.state == "LEASED",
            AttachmentSpoolObject.lease_token_digest == lease_token_digest,
            AttachmentSpoolObject.lease_expires_at.is_not(None),
            AttachmentSpoolObject.lease_expires_at > func.statement_timestamp(),
        )
        .values(
            state="DELETE_PENDING",
            updated_at=func.statement_timestamp(),
        )
        .returning(AttachmentSpoolObject)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return _to_row(row)


async def select_delete_pending_for_finalize(
    session: AsyncSession,
    *,
    limit: int,
) -> list[AttachmentSpoolRow]:
    stmt = (
        select(AttachmentSpoolObject)
        .where(AttachmentSpoolObject.state == "DELETE_PENDING")
        .order_by(AttachmentSpoolObject.updated_at, AttachmentSpoolObject.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = (await session.scalars(stmt)).all()
    return [_to_row(row) for row in rows]


async def select_expired_for_purge(
    session: AsyncSession,
    *,
    limit: int,
) -> list[AttachmentSpoolRow]:
    """Lock expired STORED or dual-expired LEASED candidates for purge."""
    stmt = (
        select(AttachmentSpoolObject)
        .where(
            or_(
                and_(
                    AttachmentSpoolObject.state == "STORED",
                    AttachmentSpoolObject.expires_at
                    <= func.statement_timestamp(),
                ),
                and_(
                    AttachmentSpoolObject.state == "LEASED",
                    AttachmentSpoolObject.expires_at
                    <= func.statement_timestamp(),
                    AttachmentSpoolObject.lease_expires_at.is_not(None),
                    AttachmentSpoolObject.lease_expires_at
                    <= func.statement_timestamp(),
                ),
            )
        )
        .order_by(
            AttachmentSpoolObject.expires_at,
            AttachmentSpoolObject.id,
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = (await session.scalars(stmt)).all()
    return [_to_row(row) for row in rows]


async def transition_expired_stored_to_delete_pending(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
) -> AttachmentSpoolRow | None:
    """Conditionally transition expired STORED → DELETE_PENDING."""
    result = await session.execute(
        update(AttachmentSpoolObject)
        .where(
            AttachmentSpoolObject.id == row_id,
            AttachmentSpoolObject.state == "STORED",
            AttachmentSpoolObject.expires_at <= func.statement_timestamp(),
        )
        .values(
            state="DELETE_PENDING",
            updated_at=func.statement_timestamp(),
        )
        .returning(AttachmentSpoolObject)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return _to_row(row)


async def transition_expired_leased_to_delete_pending(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
) -> AttachmentSpoolRow | None:
    """Conditionally transition dual-expired LEASED → DELETE_PENDING; clear lease."""
    result = await session.execute(
        update(AttachmentSpoolObject)
        .where(
            AttachmentSpoolObject.id == row_id,
            AttachmentSpoolObject.state == "LEASED",
            AttachmentSpoolObject.expires_at <= func.statement_timestamp(),
            AttachmentSpoolObject.lease_expires_at.is_not(None),
            AttachmentSpoolObject.lease_expires_at
            <= func.statement_timestamp(),
        )
        .values(
            state="DELETE_PENDING",
            updated_at=func.statement_timestamp(),
            lease_token_digest=None,
            leased_at=None,
            lease_expires_at=None,
        )
        .returning(AttachmentSpoolObject)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return _to_row(row)
