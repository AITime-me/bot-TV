"""Encrypted ephemeral PII store. Service-owned transactions; no AI recovery."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ephemeral_pii_crypto import decrypt_text, encrypt_text
from app.core.ephemeral_pii_keys import (
    EnvEphemeralPiiKeyProvider,
    EphemeralPiiKeyProvider,
)
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

# Align ciphertext TTL with master confirmation window (15m); not imported from
# master_command_types to keep the PII store free of command-flow coupling.
_DEFAULT_STORE_TTL_SECONDS: Final[int] = 15 * 60

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

        handle: EphemeralPiiHandle | None = None
        try:
            async with session_scope(self._session_factory) as session:
                handle = await self._store_one_in_session(
                    session,
                    plaintext,
                    conversation_id=conversation_id,
                    kind=kind,
                    purpose=purpose,
                )
        except EphemeralPiiError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise EphemeralPiiError("EPHEMERAL_PII_STORE_FAILED") from None

        if handle is None:
            raise EphemeralPiiError("EPHEMERAL_PII_STORE_FAILED") from None
        return handle

    async def store_booking_phone_write_pair(
        self,
        session: AsyncSession,
        phone: str,
        client_name: str,
        *,
        conversation_id: UUID,
    ) -> tuple[EphemeralPiiHandle, EphemeralPiiHandle]:
        """Encrypt+insert PHONE and CLIENT_NAME in the caller UoW (no commit).

        Purpose is fixed to BOOKING_PHONE_WRITE. Both rows flush in ``session``;
        caller must commit or roll back atomically with any admission map row.
        """

        _require_plaintext(phone)
        _require_plaintext(client_name)
        _require_conversation_id(conversation_id)
        if session is None:
            raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None

        purpose = EphemeralPiiPurpose.BOOKING_PHONE_WRITE
        try:
            phone_handle = await self._store_one_in_session(
                session,
                phone,
                conversation_id=conversation_id,
                kind=EphemeralPiiKind.PHONE,
                purpose=purpose,
            )
            name_handle = await self._store_one_in_session(
                session,
                client_name,
                conversation_id=conversation_id,
                kind=EphemeralPiiKind.CLIENT_NAME,
                purpose=purpose,
            )
        except EphemeralPiiError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise EphemeralPiiError("EPHEMERAL_PII_STORE_FAILED") from None
        if phone_handle is None or name_handle is None:
            raise EphemeralPiiError("EPHEMERAL_PII_STORE_FAILED") from None
        return phone_handle, name_handle

    async def booking_phone_write_pair_alive(
        self,
        session: AsyncSession,
        *,
        phone_ref_token: str,
        name_ref_token: str,
        conversation_id: UUID,
    ) -> bool:
        """True iff both unexpired ciphertext rows bind to conversation+purpose."""

        _require_conversation_id(conversation_id)
        purpose = EphemeralPiiPurpose.BOOKING_PHONE_WRITE
        try:
            phone_ref = EphemeralPiiReference.parse(phone_ref_token)
            name_ref = EphemeralPiiReference.parse(name_ref_token)
        except EphemeralPiiError:
            return False
        try:
            phone_row = await ephemeral_pii_repo.select_for_read(
                session,
                reference_digest=phone_ref.digest(),
            )
            name_row = await ephemeral_pii_repo.select_for_read(
                session,
                reference_digest=name_ref.digest(),
            )
        except EphemeralPiiError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise EphemeralPiiError("EPHEMERAL_PII_STORE_FAILED") from None
        if phone_row is None or name_row is None:
            return False
        return (
            _bindings_match(
                phone_row,
                conversation_id,
                EphemeralPiiKind.PHONE,
                purpose,
            )
            and _bindings_match(
                name_row,
                conversation_id,
                EphemeralPiiKind.CLIENT_NAME,
                purpose,
            )
        )

    async def _store_one_in_session(
        self,
        session: AsyncSession,
        plaintext: str,
        *,
        conversation_id: UUID,
        kind: EphemeralPiiKind,
        purpose: EphemeralPiiPurpose,
    ) -> EphemeralPiiHandle | None:
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
                return EphemeralPiiHandle(
                    reference=reference,
                    kind=kind,
                    purpose=purpose,
                )
        return None

    async def read_plaintext(
        self,
        reference: EphemeralPiiReference,
        *,
        conversation_id: UUID,
        kind: EphemeralPiiKind,
        purpose: EphemeralPiiPurpose,
    ) -> str:
        """Purpose-bound decrypt without destroying ciphertext (retry-safe)."""

        _require_reference(reference)
        _require_conversation_id(conversation_id)
        _require_kind(kind)
        _require_purpose(purpose)

        digest = reference.digest()
        try:
            async with session_scope(self._session_factory) as session:
                row = await ephemeral_pii_repo.select_for_read(
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
                    return decrypt_text(
                        encrypted,
                        aad=aad,
                        key_provider=self._key_provider,
                    )
                except EphemeralPiiError as exc:
                    if exc.code == "EPHEMERAL_PII_ACCESS_DENIED":
                        raise
                    raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED") from None
        except EphemeralPiiError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise EphemeralPiiError("EPHEMERAL_PII_STORE_FAILED") from None

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


_PII_ACTIVE_KEY_ENV: Final[str] = "EPHEMERAL_PII_ACTIVE_KEY_ID"
_PII_KEY_PREFIX: Final[str] = "EPHEMERAL_PII_KEY_"


def build_ephemeral_pii_store_from_env(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    environ: Mapping[str, str] | None = None,
    ttl_seconds: int = _DEFAULT_STORE_TTL_SECONDS,
) -> EphemeralPiiStore | None:
    """Compose the production EphemeralPiiStore from env, or None if unset.

    Fully unset → ``None`` (CREATE_BOOKING stays unavailable without inventing
    keys). Any partial/invalid ``EPHEMERAL_PII_*`` presence fails closed via
    ``EphemeralPiiError`` — same EnvEphemeralPiiKeyProvider semantics.
    """

    if session_factory is None:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    source = os.environ if environ is None else environ
    try:
        active_raw = source.get(_PII_ACTIVE_KEY_ENV)
        key_present = any(name.startswith(_PII_KEY_PREFIX) for name in source)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None

    if (active_raw is None or active_raw == "") and not key_present:
        return None

    provider = EnvEphemeralPiiKeyProvider(source)
    # Eager validate when any EPHEMERAL_PII_* is present (fail closed incomplete).
    provider.get_active_key()
    return EphemeralPiiStore(
        session_factory=session_factory,
        key_provider=provider,
        ttl_policy=EphemeralPiiTtlPolicy(ttl_seconds),
    )


