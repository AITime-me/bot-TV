"""Repository for encrypted ephemeral PII rows. No crypto, plaintext, or tokens."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pii_gateway import repr_orm_fingerprint
from app.models.ephemeral_pii import EphemeralPiiValue


def _db_uuid(value: object) -> uuid.UUID:
    """Normalize ORM/driver UUID values to exact stdlib UUID."""
    if type(value) is uuid.UUID:
        return value
    return uuid.UUID(str(value))


@dataclass(frozen=True, slots=True, repr=False)
class EphemeralPiiLockedRow:
    """Internal locked row projection. Never exposed outside service."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    pii_kind: str
    allowed_purpose: str
    ciphertext: bytes
    nonce: bytes
    key_id: str
    crypto_version: int

    def __repr__(self) -> str:
        return (
            "EphemeralPiiLockedRow("
            f"id={repr_orm_fingerprint(self.id, purpose='ephemeral_pii_id')}, "
            f"conversation_id={repr_orm_fingerprint(self.conversation_id, purpose='conversation_id')}, "
            f"pii_kind={self.pii_kind!r}, "
            f"allowed_purpose={self.allowed_purpose!r}, "
            f"crypto_version={self.crypto_version!r}, "
            "ciphertext=<redacted>, "
            "nonce=<redacted>, "
            "key_id=<redacted>)"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


async def insert_if_reference_available(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
    reference_digest: bytes,
    conversation_id: uuid.UUID,
    pii_kind: str,
    allowed_purpose: str,
    ciphertext: bytes,
    nonce: bytes,
    key_id: str,
    crypto_version: int,
    ttl_seconds: int,
) -> bool:
    """Insert ciphertext row. Returns True when inserted, False on digest collision."""
    stmt = (
        insert(EphemeralPiiValue)
        .values(
            id=row_id,
            reference_digest=reference_digest,
            conversation_id=conversation_id,
            pii_kind=pii_kind,
            allowed_purpose=allowed_purpose,
            ciphertext=ciphertext,
            nonce=nonce,
            key_id=key_id,
            crypto_version=crypto_version,
            created_at=func.statement_timestamp(),
            expires_at=func.statement_timestamp()
            + func.make_interval(0, 0, 0, 0, 0, 0, ttl_seconds),
        )
        .on_conflict_do_nothing(
            index_elements=[EphemeralPiiValue.reference_digest],
        )
        .returning(EphemeralPiiValue.id)
    )
    inserted_id = await session.scalar(stmt)
    return inserted_id is not None


async def _row_still_unexpired_after_lock(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
) -> bool:
    """Post-lock expiry check with a fresh ``statement_timestamp()``."""
    still_valid = await session.scalar(
        select(EphemeralPiiValue.id).where(
            EphemeralPiiValue.id == row_id,
            EphemeralPiiValue.expires_at > func.statement_timestamp(),
        )
    )
    return still_valid is not None


async def select_for_consume(
    session: AsyncSession,
    *,
    reference_digest: bytes,
) -> EphemeralPiiLockedRow | None:
    """Lock a row by digest for consume/delete. Does not commit."""
    stmt = (
        select(EphemeralPiiValue)
        .where(
            EphemeralPiiValue.reference_digest == reference_digest,
            EphemeralPiiValue.expires_at > func.statement_timestamp(),
        )
        .with_for_update()
    )
    row = await session.scalar(stmt)
    if row is None:
        return None
    row_id = _db_uuid(row.id)
    if not await _row_still_unexpired_after_lock(session, row_id=row_id):
        return None
    return EphemeralPiiLockedRow(
        id=row_id,
        conversation_id=_db_uuid(row.conversation_id),
        pii_kind=row.pii_kind,
        allowed_purpose=row.allowed_purpose,
        ciphertext=row.ciphertext,
        nonce=row.nonce,
        key_id=row.key_id,
        crypto_version=row.crypto_version,
    )


async def select_for_read(
    session: AsyncSession,
    *,
    reference_digest: bytes,
) -> EphemeralPiiLockedRow | None:
    """Load an unexpired row for non-destructive decrypt. Does not delete."""
    stmt = select(EphemeralPiiValue).where(
        EphemeralPiiValue.reference_digest == reference_digest,
        EphemeralPiiValue.expires_at > func.statement_timestamp(),
    )
    row = await session.scalar(stmt)
    if row is None:
        return None
    return EphemeralPiiLockedRow(
        id=_db_uuid(row.id),
        conversation_id=_db_uuid(row.conversation_id),
        pii_kind=row.pii_kind,
        allowed_purpose=row.allowed_purpose,
        ciphertext=row.ciphertext,
        nonce=row.nonce,
        key_id=row.key_id,
        crypto_version=row.crypto_version,
    )


async def delete_locked_row(session: AsyncSession, *, row_id: uuid.UUID) -> None:
    """Delete a row already locked in the current transaction."""
    await session.execute(
        delete(EphemeralPiiValue).where(EphemeralPiiValue.id == row_id)
    )
    await session.flush()


async def purge_expired_batch(session: AsyncSession, *, limit: int) -> int:
    """Delete expired rows in bounded batches. Does not commit."""
    result = await session.execute(
        text(
            """
            WITH due AS (
                SELECT id
                FROM ephemeral_pii_values
                WHERE expires_at <= statement_timestamp()
                ORDER BY expires_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
            )
            DELETE FROM ephemeral_pii_values AS target
            USING due
            WHERE target.id = due.id
            RETURNING target.id
            """
        ),
        {"limit": limit},
    )
    return len(result.fetchall())
