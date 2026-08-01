"""Encrypted attachment spool store Stage 1A1/1A2A/1A2B1.

DB-authoritative WRITING → STORED lifecycle, lease acquire/release/reclaim,
and secure lease-gated read/decrypt with second revalidation.
No delivery ack/purge API.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import attachment_fs as attachment_fs
from app.core.attachment_crypto import decrypt_bytes, encrypt_bytes
from app.core.attachment_keys import AttachmentKeyProvider
from app.core.attachment_mime import detect_attachment_mime
from app.core.attachment_types import (
    CRYPTO_VERSION_V1,
    LEASE_TTL_SECONDS,
    MAX_LEASE_RECLAIM_BATCH,
    MAX_LEASE_TOKEN_COLLISION_RETRIES,
    MAX_RECONCILE_BATCH,
    MAX_REFERENCE_COLLISION_RETRIES,
    AttachmentAad,
    AttachmentCiphertext,
    AttachmentError,
    AttachmentHandle,
    AttachmentKind,
    AttachmentLeaseHandle,
    AttachmentLeaseReclaimResult,
    AttachmentLeaseToken,
    AttachmentMime,
    AttachmentPlaintext,
    AttachmentPurpose,
    AttachmentReconcileResult,
    AttachmentReference,
    AttachmentSpoolPolicy,
    CiphertextInspectStatus,
    CiphertextUnlinkStatus,
)
from app.db.session import session_scope
from app.repositories import attachment_spool as spool_repo

_REFERENCE_FACTORY: Final[
    Callable[[], AttachmentReference]
] = AttachmentReference.generate


class AttachmentSpoolStore:
    """Store encrypted attachment ciphertext with DB-authoritative registration."""

    __slots__ = (
        "_key_provider",
        "_policy",
        "_reference_factory",
        "_session_factory",
    )

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        key_provider: AttachmentKeyProvider,
        policy: AttachmentSpoolPolicy,
        reference_factory: Callable[[], AttachmentReference] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._key_provider = key_provider
        self._policy = policy
        self._reference_factory = (
            _REFERENCE_FACTORY if reference_factory is None else reference_factory
        )

    async def store(
        self,
        plaintext: object,
        *,
        conversation_id: UUID,
        kind: AttachmentKind,
        purpose: AttachmentPurpose,
    ) -> AttachmentHandle:
        data = _require_plaintext_bytes(plaintext)
        _require_conversation_id(conversation_id)
        _require_kind(kind)
        _require_purpose(purpose)
        mime = detect_attachment_mime(data)

        active = self._key_provider.get_active_key()

        for _ in range(MAX_REFERENCE_COLLISION_RETRIES):
            record_id = uuid.uuid4()
            object_id = uuid.uuid4()
            reference = self._reference_factory()
            digest = reference.digest()
            aad = AttachmentAad(
                crypto_version=CRYPTO_VERSION_V1,
                record_id=record_id,
                object_id=object_id,
                key_id=active.key_id,
                kind=kind,
                conversation_id=conversation_id,
                purpose=purpose,
                mime=mime,
                plaintext_size=len(data),
            )
            try:
                encrypted = encrypt_bytes(
                    data,
                    aad=aad,
                    key_provider=self._key_provider,
                    active_key=active,
                )
            except AttachmentError:
                raise
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                raise AttachmentError("ATTACHMENT_ENCRYPT_FAILED") from None

            writing_ok = False
            try:
                async with session_scope(self._session_factory) as session:
                    inserted = await spool_repo.insert_writing(
                        session,
                        row_id=record_id,
                        reference_digest=digest,
                        object_id=object_id,
                        conversation_id=conversation_id,
                        kind=kind.value,
                        purpose=purpose.value,
                        detected_mime=mime.value,
                        plaintext_size=len(data),
                        ciphertext_size=len(encrypted.ciphertext),
                        ciphertext_sha256=encrypted.ciphertext_sha256,
                        nonce=encrypted.nonce,
                        key_id=encrypted.key_id,
                        crypto_version=encrypted.crypto_version,
                        ttl_seconds=self._policy.ttl_seconds,
                    )
                    writing_ok = inserted
            except AttachmentError:
                raise
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                raise AttachmentError("ATTACHMENT_STORE_FAILED") from None

            if not writing_ok:
                continue

            # Filesystem only after durable WRITING registration.
            try:
                attachment_fs.write_ciphertext_atomic(
                    self._policy.spool_root,
                    object_id,
                    encrypted.ciphertext,
                    expected_sha256=encrypted.ciphertext_sha256,
                )
            except AttachmentError as exc:
                await self._best_effort_delete_writing(record_id, object_id)
                if exc.code == "ATTACHMENT_FILESYSTEM_FAILED":
                    raise
                raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None
            except (KeyboardInterrupt, SystemExit):
                await self._best_effort_delete_writing(record_id, object_id)
                raise
            except Exception:
                await self._best_effort_delete_writing(record_id, object_id)
                raise AttachmentError("ATTACHMENT_FILESYSTEM_FAILED") from None

            try:
                async with session_scope(self._session_factory) as session:
                    row = await spool_repo.select_for_update_by_id(
                        session, row_id=record_id
                    )
                    if row is None or row.state != "WRITING":
                        raise AttachmentError("ATTACHMENT_STORE_FAILED") from None
                    if (
                        row.object_id != object_id
                        or row.conversation_id != conversation_id
                        or row.kind != kind.value
                        or row.purpose != purpose.value
                        or row.detected_mime != mime.value
                        or row.plaintext_size != len(data)
                        or row.ciphertext_size != len(encrypted.ciphertext)
                        or row.ciphertext_sha256 != encrypted.ciphertext_sha256
                        or row.nonce != encrypted.nonce
                        or row.key_id != encrypted.key_id
                        or row.crypto_version != encrypted.crypto_version
                        or row.reference_digest != digest
                    ):
                        raise AttachmentError("ATTACHMENT_STORE_FAILED") from None
                    attachment_fs.verify_ciphertext_file(
                        self._policy.spool_root,
                        object_id,
                        expected_size=row.ciphertext_size,
                        expected_sha256=row.ciphertext_sha256,
                        final=True,
                    )
                    marked = await spool_repo.mark_stored(session, row_id=record_id)
                    if not marked:
                        raise AttachmentError("ATTACHMENT_STORE_FAILED") from None
            except AttachmentError:
                # Leave WRITING + final for reconciliation promote/abandon.
                raise
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                raise AttachmentError("ATTACHMENT_STORE_FAILED") from None

            return AttachmentHandle(
                reference=reference,
                kind=kind,
                purpose=purpose,
                mime=mime,
                plaintext_size=len(data),
            )

        raise AttachmentError("ATTACHMENT_STORE_FAILED") from None

    async def acquire(self, reference: AttachmentReference) -> AttachmentLeaseHandle:
        if type(reference) is not AttachmentReference:
            raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
        reference_digest = reference.digest()
        pending_handle: AttachmentLeaseHandle | None = None
        try:
            async with session_scope(self._session_factory) as session:
                row = await spool_repo.select_for_update_by_reference_digest(
                    session,
                    reference_digest=reference_digest,
                )
                if row is None:
                    raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
                now = await spool_repo.fetch_statement_timestamp(session)
                decision = _acquire_decision(row, now)
                if decision == "deny":
                    raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
                if decision == "reclaim":
                    cleared = await spool_repo.clear_lease_to_stored(
                        session, row_id=row.id
                    )
                    if not cleared:
                        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
                    row = await spool_repo.select_for_update_by_id(
                        session, row_id=row.id
                    )
                    if row is None or row.state != "STORED":
                        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
                    now = await spool_repo.fetch_statement_timestamp(session)
                    if not _stored_object_active(row, now):
                        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None

                lease_row: spool_repo.AttachmentSpoolRow | None = None
                token: AttachmentLeaseToken | None = None
                for _ in range(MAX_LEASE_TOKEN_COLLISION_RETRIES):
                    token = AttachmentLeaseToken.generate()
                    lease_digest = token.digest()
                    try:
                        async with session.begin_nested():
                            lease_row = await spool_repo.apply_lease(
                                session,
                                row_id=row.id,
                                lease_token_digest=lease_digest,
                                lease_ttl_seconds=LEASE_TTL_SECONDS,
                            )
                            if lease_row is None:
                                raise AttachmentError(
                                    "ATTACHMENT_ACCESS_DENIED"
                                ) from None
                            await session.flush()
                    except IntegrityError as exc:
                        if _is_lease_digest_unique_collision(exc):
                            lease_row = None
                            continue
                        raise AttachmentError("ATTACHMENT_STORE_FAILED") from None
                    break
                else:
                    raise AttachmentError("ATTACHMENT_STORE_FAILED") from None

                if (
                    lease_row is None
                    or token is None
                    or lease_row.lease_expires_at is None
                ):
                    raise AttachmentError("ATTACHMENT_STORE_FAILED") from None
                pending_handle = AttachmentLeaseHandle(
                    token=token,
                    lease_expires_at=lease_row.lease_expires_at,
                )
        except AttachmentError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise AttachmentError("ATTACHMENT_STORE_FAILED") from None
        if pending_handle is None:
            raise AttachmentError("ATTACHMENT_STORE_FAILED") from None
        return pending_handle

    async def read(self, lease_token: AttachmentLeaseToken) -> AttachmentPlaintext:
        if type(lease_token) is not AttachmentLeaseToken:
            raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
        lease_digest = lease_token.digest()
        snapshot: _ReadCryptoSnapshot | None = None
        try:
            async with session_scope(self._session_factory) as session:
                row = await spool_repo.select_for_update_by_lease_digest(
                    session,
                    lease_token_digest=lease_digest,
                )
                if row is None:
                    raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
                now = await spool_repo.fetch_statement_timestamp(session)
                if not _lease_read_authorized(row, lease_digest, now):
                    raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
                snapshot = _ReadCryptoSnapshot.from_row(row, lease_digest)
        except AttachmentError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise AttachmentError("ATTACHMENT_STORE_FAILED") from None
        if snapshot is None:
            raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None

        plaintext: bytes | None = None
        try:
            ciphertext_bytes = attachment_fs.read_ciphertext_verified(
                self._policy.spool_root,
                snapshot.object_id,
                expected_size=snapshot.ciphertext_size,
                expected_sha256=snapshot.ciphertext_sha256,
            )
            encrypted = AttachmentCiphertext(
                ciphertext=ciphertext_bytes,
                nonce=snapshot.nonce,
                key_id=snapshot.key_id,
                crypto_version=snapshot.crypto_version,
                ciphertext_sha256=snapshot.ciphertext_sha256,
            )
            aad = AttachmentAad(
                crypto_version=snapshot.crypto_version,
                record_id=snapshot.row_id,
                object_id=snapshot.object_id,
                key_id=snapshot.key_id,
                kind=snapshot.kind,
                conversation_id=snapshot.conversation_id,
                purpose=snapshot.purpose,
                mime=snapshot.mime,
                plaintext_size=snapshot.plaintext_size,
            )
            try:
                plaintext = decrypt_bytes(
                    encrypted,
                    aad=aad,
                    key_provider=self._key_provider,
                )
            except AttachmentError:
                raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
        except AttachmentError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise AttachmentError("ATTACHMENT_STORE_FAILED") from None

        try:
            async with session_scope(self._session_factory) as session:
                row = await spool_repo.select_for_update_by_id(
                    session, row_id=snapshot.row_id
                )
                if row is None:
                    raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
                now = await spool_repo.fetch_statement_timestamp(session)
                if not snapshot.matches_locked_row(row, lease_digest=lease_digest, now=now):
                    raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
        except AttachmentError:
            plaintext = None
            raise
        except (KeyboardInterrupt, SystemExit):
            plaintext = None
            raise
        except Exception:
            plaintext = None
            raise AttachmentError("ATTACHMENT_STORE_FAILED") from None

        if plaintext is None:
            raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
        return AttachmentPlaintext(data=plaintext, mime=snapshot.mime)

    async def release(self, token: AttachmentLeaseToken) -> None:
        if type(token) is not AttachmentLeaseToken:
            raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
        lease_digest = token.digest()
        try:
            async with session_scope(self._session_factory) as session:
                row = await spool_repo.select_for_update_by_lease_digest(
                    session,
                    lease_token_digest=lease_digest,
                )
                if row is None:
                    raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
                now = await spool_repo.fetch_statement_timestamp(session)
                if not _release_allowed(row, lease_digest, now):
                    raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
                cleared = await spool_repo.clear_lease_to_stored(
                    session, row_id=row.id
                )
                if not cleared:
                    raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
        except AttachmentError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise AttachmentError("ATTACHMENT_STORE_FAILED") from None

    async def reclaim_expired_leases(
        self, *, limit: int
    ) -> AttachmentLeaseReclaimResult:
        _require_reclaim_limit(limit)
        reclaimed = 0
        skipped = 0
        try:
            async with session_scope(self._session_factory) as session:
                rows = await spool_repo.select_expired_leased_for_reclaim(
                    session, limit=limit
                )
                for row in rows:
                    now = await spool_repo.fetch_statement_timestamp(session)
                    if row.state != "LEASED":
                        skipped += 1
                        continue
                    if (
                        row.lease_expires_at is None
                        or row.lease_expires_at > now
                    ):
                        skipped += 1
                        continue
                    cleared = await spool_repo.clear_lease_to_stored(
                        session, row_id=row.id
                    )
                    if cleared:
                        reclaimed += 1
                    else:
                        skipped += 1
        except AttachmentError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise AttachmentError("ATTACHMENT_RECONCILE_FAILED") from None
        return AttachmentLeaseReclaimResult(reclaimed=reclaimed, skipped=skipped)

    async def reconcile(self, *, limit: int) -> AttachmentReconcileResult:
        _require_reconcile_limit(limit)
        promoted = 0
        deleted_writing = 0
        deleted_orphan_temps = 0
        deleted_orphan_finals = 0
        deleted_unrecoverable = 0
        unsafe_skipped = 0
        io_unavailable_skipped = 0
        try:
            async with session_scope(self._session_factory) as session:
                candidates = await spool_repo.select_stale_writing_for_reconcile(
                    session,
                    grace_seconds=self._policy.writing_grace_seconds,
                    limit=limit,
                )
                for row in candidates:
                    if not await spool_repo.row_still_stale_writing(
                        session,
                        row_id=row.id,
                        grace_seconds=self._policy.writing_grace_seconds,
                    ):
                        continue
                    outcome = await self._reconcile_stale_writing_row(session, row)
                    if outcome == "promoted":
                        promoted += 1
                    elif outcome == "deleted":
                        deleted_writing += 1
                    elif outcome == "unsafe":
                        unsafe_skipped += 1
                    elif outcome == "io":
                        io_unavailable_skipped += 1

            orphan_temps, orphan_finals, unsafe_fs, io_fs = (
                await self._reconcile_orphan_files(limit=limit)
            )
            deleted_orphan_temps += orphan_temps
            deleted_orphan_finals += orphan_finals
            unsafe_skipped += unsafe_fs
            io_unavailable_skipped += io_fs
            stored_deleted, stored_unsafe, stored_io = (
                await self._reconcile_unrecoverable_stored(limit=limit)
            )
            deleted_unrecoverable += stored_deleted
            unsafe_skipped += stored_unsafe
            io_unavailable_skipped += stored_io
        except AttachmentError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise AttachmentError("ATTACHMENT_RECONCILE_FAILED") from None

        return AttachmentReconcileResult(
            promoted_to_stored=promoted,
            deleted_writing_rows=deleted_writing,
            deleted_orphan_temps=deleted_orphan_temps,
            deleted_orphan_finals=deleted_orphan_finals,
            deleted_unrecoverable_stored=deleted_unrecoverable,
            unsafe_skipped=unsafe_skipped,
            io_unavailable_skipped=io_unavailable_skipped,
        )

    async def _reconcile_stale_writing_row(
        self, session: AsyncSession, row: spool_repo.AttachmentSpoolRow
    ) -> str:
        root = self._policy.spool_root
        final_status = attachment_fs.inspect_ciphertext_file(
            root,
            row.object_id,
            expected_size=row.ciphertext_size,
            expected_sha256=row.ciphertext_sha256,
            final=True,
        )
        if final_status is CiphertextInspectStatus.VALID:
            marked = await spool_repo.mark_stored(session, row_id=row.id)
            if marked:
                attachment_fs.safe_unlink_object_file(
                    root, row.object_id, final=False
                )
                return "promoted"
            return "noop"

        if final_status is CiphertextInspectStatus.UNSAFE:
            return "unsafe"
        if final_status is CiphertextInspectStatus.IO_UNAVAILABLE:
            return "io"

        if final_status is CiphertextInspectStatus.MISMATCH:
            return await self._delete_writing_after_file_cleanup(
                session, row_id=row.id, object_id=row.object_id, unlink_final=True
            )

        # MISSING final
        temp_status = attachment_fs.probe_object_file(
            root, row.object_id, final=False
        )
        if temp_status is CiphertextInspectStatus.UNSAFE:
            return "unsafe"
        if temp_status is CiphertextInspectStatus.IO_UNAVAILABLE:
            return "io"
        if temp_status is CiphertextInspectStatus.MISSING:
            await spool_repo.delete_by_id(session, row_id=row.id)
            return "deleted"
        return await self._delete_writing_after_file_cleanup(
            session, row_id=row.id, object_id=row.object_id, unlink_final=False
        )

    async def _delete_writing_after_file_cleanup(
        self,
        session: AsyncSession,
        *,
        row_id: UUID,
        object_id: UUID,
        unlink_final: bool,
    ) -> str:
        root = self._policy.spool_root
        if unlink_final:
            final_unlink = attachment_fs.safe_unlink_object_file(
                root, object_id, final=True
            )
            if final_unlink is CiphertextUnlinkStatus.UNSAFE:
                return "unsafe"
            if final_unlink is CiphertextUnlinkStatus.IO_UNAVAILABLE:
                return "io"
            if not attachment_fs.unlink_succeeded(final_unlink):
                return "io"
        temp_unlink = attachment_fs.safe_unlink_object_file(
            root, object_id, final=False
        )
        if temp_unlink is CiphertextUnlinkStatus.UNSAFE:
            return "unsafe"
        if temp_unlink is CiphertextUnlinkStatus.IO_UNAVAILABLE:
            return "io"
        if not attachment_fs.unlink_succeeded(temp_unlink):
            return "io"
        await spool_repo.delete_by_id(session, row_id=row_id)
        return "deleted"

    async def _best_effort_delete_writing(
        self, row_id: UUID, object_id: UUID
    ) -> None:
        try:
            attachment_fs.unlink_object_files(self._policy.spool_root, object_id)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            pass
        try:
            async with session_scope(self._session_factory) as session:
                await spool_repo.delete_by_id(session, row_id=row_id)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            pass

    async def _reconcile_orphan_files(
        self, *, limit: int
    ) -> tuple[int, int, int, int]:
        deleted_temps = 0
        deleted_finals = 0
        unsafe = 0
        io_skipped = 0
        grace = float(self._policy.writing_grace_seconds)
        inspected = 0
        root = self._policy.spool_root
        deleted_orphans: set[tuple[bytes, str]] = set()
        for shard in attachment_fs.iter_shard_dirs(root):
            try:
                for entry in __import__("os").scandir(shard):
                    if inspected >= limit:
                        return deleted_temps, deleted_finals, unsafe, io_skipped
                    inspected += 1
                    try:
                        is_symlink = entry.is_symlink()
                        is_file = entry.is_file(follow_symlinks=False)
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except Exception:
                        io_skipped += 1
                        continue
                    if is_symlink:
                        unsafe += 1
                        continue
                    if not is_file:
                        continue
                    parsed = attachment_fs.parse_object_filename(entry.name)
                    if parsed is None:
                        # Unknown/invalid name consumes budget; never open/delete.
                        continue
                    object_id, suffix = parsed
                    if not attachment_fs.orphan_entry_is_canonical(
                        shard, object_id, entry.name, suffix=suffix
                    ):
                        unsafe += 1
                        continue
                    path = Path(entry.path)
                    age = attachment_fs.file_mtime_age_seconds(path)
                    if age is None:
                        io_or_unsafe = attachment_fs.probe_object_file(
                            root, object_id, final=(suffix == ".bin")
                        )
                        if io_or_unsafe is CiphertextInspectStatus.UNSAFE:
                            unsafe += 1
                        elif io_or_unsafe is CiphertextInspectStatus.IO_UNAVAILABLE:
                            io_skipped += 1
                        continue
                    if age < grace:
                        continue
                    async with session_scope(self._session_factory) as session:
                        exists = await spool_repo.exists_by_object_id(
                            session, object_id=object_id
                        )
                    if exists:
                        continue
                    dedupe_key = (object_id.bytes, suffix)
                    if dedupe_key in deleted_orphans:
                        continue
                    unlink_status = attachment_fs.safe_unlink_object_file(
                        root, object_id, final=(suffix == ".bin")
                    )
                    if unlink_status is CiphertextUnlinkStatus.UNSAFE:
                        unsafe += 1
                        continue
                    if unlink_status is CiphertextUnlinkStatus.IO_UNAVAILABLE:
                        io_skipped += 1
                        continue
                    if unlink_status is not CiphertextUnlinkStatus.REMOVED:
                        if unlink_status is CiphertextUnlinkStatus.ALREADY_MISSING:
                            unsafe += 1
                        else:
                            io_skipped += 1
                        continue
                    deleted_orphans.add(dedupe_key)
                    if suffix == ".tmp":
                        deleted_temps += 1
                    else:
                        deleted_finals += 1
            except AttachmentError:
                raise
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                raise AttachmentError("ATTACHMENT_RECONCILE_FAILED") from None
        return deleted_temps, deleted_finals, unsafe, io_skipped

    async def _reconcile_unrecoverable_stored(
        self, *, limit: int
    ) -> tuple[int, int, int]:
        deleted = 0
        unsafe = 0
        io_skipped = 0
        root = self._policy.spool_root
        async with session_scope(self._session_factory) as session:
            rows = await spool_repo.select_stored_missing_file_candidates(
                session, limit=limit
            )
            for row in rows:
                status = attachment_fs.inspect_ciphertext_file(
                    root,
                    row.object_id,
                    expected_size=row.ciphertext_size,
                    expected_sha256=row.ciphertext_sha256,
                    final=True,
                )
                if status is CiphertextInspectStatus.VALID:
                    continue
                if status is CiphertextInspectStatus.UNSAFE:
                    unsafe += 1
                    continue
                if status is CiphertextInspectStatus.IO_UNAVAILABLE:
                    io_skipped += 1
                    continue
                if status is CiphertextInspectStatus.MISSING:
                    await spool_repo.delete_by_id(session, row_id=row.id)
                    deleted += 1
                    continue
                unlink_status = attachment_fs.safe_unlink_object_file(
                    root, row.object_id, final=True
                )
                if unlink_status is CiphertextUnlinkStatus.UNSAFE:
                    unsafe += 1
                    continue
                if unlink_status is CiphertextUnlinkStatus.IO_UNAVAILABLE:
                    io_skipped += 1
                    continue
                if attachment_fs.unlink_succeeded(unlink_status):
                    await spool_repo.delete_by_id(session, row_id=row.id)
                    deleted += 1
                else:
                    io_skipped += 1
        return deleted, unsafe, io_skipped


def _require_plaintext_bytes(value: object) -> bytes:
    if type(value) is not bytes:
        raise AttachmentError("ATTACHMENT_VALUE_INVALID") from None
    if value == b"":
        raise AttachmentError("ATTACHMENT_VALUE_INVALID") from None
    from app.core.attachment_types import MAX_PLAINTEXT_BYTES

    if len(value) > MAX_PLAINTEXT_BYTES:
        raise AttachmentError("ATTACHMENT_TOO_LARGE") from None
    return value


def _require_conversation_id(value: object) -> UUID:
    if type(value) is not UUID:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    return value


def _require_kind(value: object) -> AttachmentKind:
    if type(value) is not AttachmentKind:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    return value


def _require_purpose(value: object) -> AttachmentPurpose:
    if type(value) is not AttachmentPurpose:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    return value


def _require_reconcile_limit(value: object) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise AttachmentError("ATTACHMENT_POLICY_INVALID") from None
    if not 1 <= value <= MAX_RECONCILE_BATCH:
        raise AttachmentError("ATTACHMENT_POLICY_INVALID") from None
    return value


def _require_reclaim_limit(value: object) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise AttachmentError("ATTACHMENT_POLICY_INVALID") from None
    if not 1 <= value <= MAX_LEASE_RECLAIM_BATCH:
        raise AttachmentError("ATTACHMENT_POLICY_INVALID") from None
    return value


def _stored_object_active(
    row: spool_repo.AttachmentSpoolRow, now: object
) -> bool:
    if row.expires_at is None:
        return False
    return row.expires_at > now  # type: ignore[operator]


def _acquire_decision(
    row: spool_repo.AttachmentSpoolRow, now: object
) -> str:
    if row.state in ("WRITING", "DELETE_PENDING"):
        return "deny"
    if row.expires_at is None or row.expires_at <= now:  # type: ignore[operator]
        return "deny"
    if row.state == "STORED":
        return "eligible"
    if row.state == "LEASED":
        if row.lease_expires_at is None:
            return "deny"
        if row.lease_expires_at > now:  # type: ignore[operator]
            return "deny"
        return "reclaim"
    return "deny"


def _release_allowed(
    row: spool_repo.AttachmentSpoolRow,
    lease_digest: bytes,
    now: object,
) -> bool:
    if row.state != "LEASED":
        return False
    if row.lease_token_digest != lease_digest:
        return False
    if row.lease_expires_at is None or row.lease_expires_at <= now:  # type: ignore[operator]
        return False
    return True


def _lease_read_authorized(
    row: spool_repo.AttachmentSpoolRow,
    lease_digest: bytes,
    now: object,
) -> bool:
    if row.state != "LEASED":
        return False
    if row.lease_token_digest != lease_digest:
        return False
    if row.lease_expires_at is None or row.lease_expires_at <= now:  # type: ignore[operator]
        return False
    return True


@dataclass(frozen=True, slots=True, repr=False)
class _ReadCryptoSnapshot:
    row_id: UUID
    object_id: UUID
    conversation_id: UUID
    kind: AttachmentKind
    purpose: AttachmentPurpose
    mime: AttachmentMime
    plaintext_size: int
    ciphertext_size: int
    ciphertext_sha256: bytes
    nonce: bytes
    key_id: str
    crypto_version: int
    lease_token_digest: bytes
    lease_expires_at: datetime

    @classmethod
    def from_row(
        cls,
        row: spool_repo.AttachmentSpoolRow,
        lease_digest: bytes,
    ) -> _ReadCryptoSnapshot:
        if row.lease_token_digest != lease_digest:
            raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
        if row.lease_expires_at is None:
            raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
        kind = _row_kind(row.kind)
        purpose = _row_purpose(row.purpose)
        mime = _row_mime(row.detected_mime)
        return cls(
            row_id=row.id,
            object_id=row.object_id,
            conversation_id=row.conversation_id,
            kind=kind,
            purpose=purpose,
            mime=mime,
            plaintext_size=row.plaintext_size,
            ciphertext_size=row.ciphertext_size,
            ciphertext_sha256=row.ciphertext_sha256,
            nonce=row.nonce,
            key_id=row.key_id,
            crypto_version=row.crypto_version,
            lease_token_digest=lease_digest,
            lease_expires_at=row.lease_expires_at,
        )

    def matches_locked_row(
        self,
        row: spool_repo.AttachmentSpoolRow,
        *,
        lease_digest: bytes,
        now: object,
    ) -> bool:
        if not _lease_read_authorized(row, lease_digest, now):
            return False
        if row.id != self.row_id:
            return False
        if row.object_id != self.object_id:
            return False
        if row.conversation_id != self.conversation_id:
            return False
        if row.kind != self.kind.value:
            return False
        if row.purpose != self.purpose.value:
            return False
        if row.detected_mime != self.mime.value:
            return False
        if row.plaintext_size != self.plaintext_size:
            return False
        if row.ciphertext_size != self.ciphertext_size:
            return False
        if row.ciphertext_sha256 != self.ciphertext_sha256:
            return False
        if row.nonce != self.nonce:
            return False
        if row.key_id != self.key_id:
            return False
        if row.crypto_version != self.crypto_version:
            return False
        if row.lease_token_digest != self.lease_token_digest:
            return False
        if row.lease_expires_at != self.lease_expires_at:
            return False
        return True

    def __repr__(self) -> str:
        return "_ReadCryptoSnapshot(<redacted>)"

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


def _row_kind(value: str) -> AttachmentKind:
    if type(value) is not str:
        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
    try:
        kind = AttachmentKind(value)
    except ValueError:
        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
    return _require_kind(kind)


def _row_purpose(value: str) -> AttachmentPurpose:
    if type(value) is not str:
        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
    try:
        purpose = AttachmentPurpose(value)
    except ValueError:
        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
    return _require_purpose(purpose)


def _row_mime(value: str) -> AttachmentMime:
    if type(value) is not str:
        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
    try:
        mime = AttachmentMime(value)
    except ValueError:
        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
    if type(mime) is not AttachmentMime:
        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
    return mime


_LEASE_DIGEST_UNIQUE_CONSTRAINT: Final[str] = (
    "uq_attachment_spool_objects_lease_token_digest"
)
_PG_UNIQUE_VIOLATION_SQLSTATE: Final[str] = "23505"
_PG_EXCEPTION_CHAIN_LIMIT: Final[int] = 4


def _normalize_sqlstate(value: object) -> str | None:
    if type(value) is not str or value == "":
        return None
    return value


def _normalize_constraint_name(value: object) -> str | None:
    if type(value) is not str or value == "":
        return None
    return value


def _structured_pg_violation_fields(exc: object | None) -> tuple[str | None, str | None]:
    """Extract SQLSTATE and constraint name from a bounded exception chain."""
    sqlstate: str | None = None
    constraint_name: str | None = None
    current = exc
    for _ in range(_PG_EXCEPTION_CHAIN_LIMIT):
        if current is None:
            break
        if sqlstate is None:
            sqlstate = _normalize_sqlstate(getattr(current, "sqlstate", None))
        if sqlstate is None:
            sqlstate = _normalize_sqlstate(getattr(current, "pgcode", None))
        if constraint_name is None:
            constraint_name = _normalize_constraint_name(
                getattr(current, "constraint_name", None)
            )
        if constraint_name is None:
            diag = getattr(current, "diag", None)
            if diag is not None:
                constraint_name = _normalize_constraint_name(
                    getattr(diag, "constraint_name", None)
                )
        if sqlstate is not None and constraint_name is not None:
            break
        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        if cause is not None:
            current = cause
            continue
        current = context
    return sqlstate, constraint_name


def _is_lease_digest_unique_collision(exc: IntegrityError) -> bool:
    sqlstate, constraint_name = _structured_pg_violation_fields(
        getattr(exc, "orig", None)
    )
    if sqlstate != _PG_UNIQUE_VIOLATION_SQLSTATE:
        return False
    if constraint_name != _LEASE_DIGEST_UNIQUE_CONSTRAINT:
        return False
    return True
