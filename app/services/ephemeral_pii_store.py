"""Encrypted ephemeral PII store. Service-owned transactions; no AI recovery."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ephemeral_pii_crypto import decrypt_text, encrypt_text
from app.core.ephemeral_pii_keys import EphemeralPiiKeyProvider
from app.core.ephemeral_pii_types import (
    CRYPTO_VERSION_V1,
    MAX_PURGE_BATCH,
    MAX_REFERENCE_COLLISION_RETRIES,
    EphemeralPiiAad,
    EphemeralPiiCiphertext,
    EphemeralPiiError,
    EphemeralPiiHandle,
    EphemeralPiiKind,
    EphemeralPiiPurpose,
    EphemeralPiiReference,
    EphemeralPiiTtlPolicy,
)
from app.db.session import session_scope
from app.repositories import ephemeral_pii as ephemeral_pii_repo

_REFERENCE_FACTORY: Final[
    Callable[[], EphemeralPiiReference]
] = EphemeralPiiReference.generate


class EphemeralPiiStore:
    """Store/consume encrypted ephemeral PII values. No mutable decrypted cache."""

    __slots__ = (
        "_key_provider",
        "_reference_factory",
        "_session_factory",
        "_ttl_policy",
    )

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        key_provider: EphemeralPiiKeyProvider,
        ttl_policy: EphemeralPiiTtlPolicy,
        reference_factory: Callable[[], EphemeralPiiReference] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._key_provider = key_provider
        self._ttl_policy = ttl_policy
        self._reference_factory = (
            _REFERENCE_FACTORY if reference_factory is None else reference_factory
        )

    async def store(
        self,
        plaintext: str,
        *,
        conversation_id: UUID,
        kind: EphemeralPiiKind,
        purpose: EphemeralPiiPurpose,
    ) -> EphemeralPiiHandle:
        _require_plaintext(plaintext)
        _require_conversation_id(conversation_id)
        _require_kind(kind)
        _require_purpose(purpose)

        active = self._key_provider.get_active_key()
        record_id = uuid.uuid4()
        aad = EphemeralPiiAad(
            crypto_version=CRYPTO_VERSION_V1,
            record_id=record_id,
            key_id=active.key_id,
            kind=kind,
            conversation_id=conversation_id,
            purpose=purpose,
        )
        encrypted = encrypt_text(
            plaintext,
            aad=aad,
            key_provider=self._key_provider,
            active_key=active,
        )

        handle: EphemeralPiiHandle | None = None
        try:
            async with session_scope(self._session_factory) as session:
                for _ in range(MAX_REFERENCE_COLLISION_RETRIES):
                    reference = self._reference_factory()
                    digest = reference.digest()
                    inserted = await ephemeral_pii_repo.insert_if_reference_available(
                        session,
                        row_id=record_id,
                        reference_digest=digest,
                        conversation_id=conversation_id,
                        pii_kind=kind.value,
                        allowed_purpose=purpose.value,
                        ciphertext=encrypted.ciphertext,
                        nonce=encrypted.nonce,
                        key_id=encrypted.key_id,
                        crypto_version=encrypted.crypto_version,
                        ttl_seconds=self._ttl_policy.ttl_seconds,
                    )
                    if inserted:
                        handle = EphemeralPiiHandle(
                            reference=reference,
                            kind=kind,
                            purpose=purpose,
                        )
                        break
        except EphemeralPiiError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise EphemeralPiiError("EPHEMERAL_PII_STORE_FAILED") from None

        if handle is None:
            raise EphemeralPiiError("EPHEMERAL_PII_STORE_FAILED") from None
        return handle

    async def consume_once(
        self,
        reference: EphemeralPiiReference,
        *,
        conversation_id: UUID,
        kind: EphemeralPiiKind,
        purpose: EphemeralPiiPurpose,
    ) -> str:
        _require_reference(reference)
        _require_conversation_id(conversation_id)
        _require_kind(kind)
        _require_purpose(purpose)

        digest = reference.digest()
        plaintext_result: str | None = None
        try:
            async with session_scope(self._session_factory) as session:
                row = await ephemeral_pii_repo.select_for_consume(
                    session,
                    reference_digest=digest,
                )
                if row is None:
                    raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED") from None
                if not _bindings_match(row, conversation_id, kind, purpose):
                    raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED") from None
                if row.crypto_version != CRYPTO_VERSION_V1:
                    raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED") from None

                aad = EphemeralPiiAad(
                    crypto_version=row.crypto_version,
                    record_id=row.id,
                    key_id=row.key_id,
                    kind=kind,
                    conversation_id=conversation_id,
                    purpose=purpose,
                )
                encrypted = EphemeralPiiCiphertext(
                    ciphertext=row.ciphertext,
                    nonce=row.nonce,
                    key_id=row.key_id,
                    crypto_version=row.crypto_version,
                )
                try:
                    plaintext = decrypt_text(
                        encrypted,
                        aad=aad,
                        key_provider=self._key_provider,
                    )
                except EphemeralPiiError as exc:
                    if exc.code == "EPHEMERAL_PII_ACCESS_DENIED":
                        raise
                    raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED") from None

                await ephemeral_pii_repo.delete_locked_row(session, row_id=row.id)
                plaintext_result = plaintext
        except EphemeralPiiError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise EphemeralPiiError("EPHEMERAL_PII_STORE_FAILED") from None

        if plaintext_result is None:
            raise EphemeralPiiError("EPHEMERAL_PII_STORE_FAILED") from None
        return plaintext_result

    async def delete(
        self,
        reference: EphemeralPiiReference,
        *,
        conversation_id: UUID,
        kind: EphemeralPiiKind,
        purpose: EphemeralPiiPurpose,
    ) -> None:
        _require_reference(reference)
        _require_conversation_id(conversation_id)
        _require_kind(kind)
        _require_purpose(purpose)

        digest = reference.digest()
        try:
            async with session_scope(self._session_factory) as session:
                row = await ephemeral_pii_repo.select_for_consume(
                    session,
                    reference_digest=digest,
                )
                if row is None:
                    raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED") from None
                if not _bindings_match(row, conversation_id, kind, purpose):
                    raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED") from None
                if row.crypto_version != CRYPTO_VERSION_V1:
                    raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED") from None
                await ephemeral_pii_repo.delete_locked_row(session, row_id=row.id)
        except EphemeralPiiError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise EphemeralPiiError("EPHEMERAL_PII_STORE_FAILED") from None

    async def purge_expired(self, *, limit: int) -> int:
        _require_purge_limit(limit)
        deleted_count = 0
        try:
            async with session_scope(self._session_factory) as session:
                deleted_count = await ephemeral_pii_repo.purge_expired_batch(
                    session,
                    limit=limit,
                )
        except EphemeralPiiError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise EphemeralPiiError("EPHEMERAL_PII_PURGE_FAILED") from None
        return deleted_count


def _require_plaintext(value: object) -> str:
    if type(value) is not str or value == "":
        raise EphemeralPiiError("EPHEMERAL_PII_VALUE_INVALID") from None
    return value


def _require_conversation_id(value: object) -> UUID:
    if type(value) is not UUID:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    return value


def _require_kind(value: object) -> EphemeralPiiKind:
    if type(value) is not EphemeralPiiKind:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    return value


def _require_purpose(value: object) -> EphemeralPiiPurpose:
    if type(value) is not EphemeralPiiPurpose:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    return value


def _require_reference(value: object) -> EphemeralPiiReference:
    if type(value) is not EphemeralPiiReference:
        raise EphemeralPiiError("EPHEMERAL_PII_REFERENCE_INVALID") from None
    return value


def _require_purge_limit(value: object) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise EphemeralPiiError("EPHEMERAL_PII_POLICY_INVALID") from None
    if not 1 <= value <= MAX_PURGE_BATCH:
        raise EphemeralPiiError("EPHEMERAL_PII_POLICY_INVALID") from None
    return value


def _bindings_match(
    row: ephemeral_pii_repo.EphemeralPiiLockedRow,
    conversation_id: UUID,
    kind: EphemeralPiiKind,
    purpose: EphemeralPiiPurpose,
) -> bool:
    return (
        row.conversation_id == conversation_id
        and row.pii_kind == kind.value
        and row.allowed_purpose == purpose.value
    )


