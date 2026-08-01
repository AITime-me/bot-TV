"""Encrypted attachment spool store Stage 1A1.

DB-authoritative WRITING → STORED lifecycle. No delivery lease/read/ack API.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import attachment_fs as attachment_fs
from app.core.attachment_crypto import encrypt_bytes
from app.core.attachment_keys import AttachmentKeyProvider
from app.core.attachment_mime import detect_attachment_mime
from app.core.attachment_types import (
    CRYPTO_VERSION_V1,
    MAX_RECONCILE_BATCH,
    MAX_REFERENCE_COLLISION_RETRIES,
    AttachmentAad,
    AttachmentError,
    AttachmentHandle,
    AttachmentKind,
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
