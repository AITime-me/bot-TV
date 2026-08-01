"""Closed types for temporary encrypted attachment spool (Stage 1A1/1A2A).

No channel adapters, delivery leases, AI recovery, or worker wiring.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final
from uuid import UUID

_ALLOWED_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "ATTACHMENT_POLICY_INVALID",
        "ATTACHMENT_REFERENCE_INVALID",
        "ATTACHMENT_LEASE_TOKEN_INVALID",
        "ATTACHMENT_VALUE_INVALID",
        "ATTACHMENT_TOO_LARGE",
        "ATTACHMENT_MIME_DENIED",
        "ATTACHMENT_CONFIG_INVALID",
        "ATTACHMENT_KEY_UNAVAILABLE",
        "ATTACHMENT_ENCRYPT_FAILED",
        "ATTACHMENT_ACCESS_DENIED",
        "ATTACHMENT_STORE_FAILED",
        "ATTACHMENT_FILESYSTEM_FAILED",
        "ATTACHMENT_RECONCILE_FAILED",
    }
)

CRYPTO_VERSION_V1: Final[int] = 1
KEY_SIZE_BYTES: Final[int] = 32
NONCE_SIZE_BYTES: Final[int] = 12
AES_GCM_TAG_BYTES: Final[int] = 16
REFERENCE_RAW_BYTES: Final[int] = 32
REFERENCE_DIGEST_BYTES: Final[int] = 32
REFERENCE_TOKEN_LENGTH: Final[int] = 44
SHA256_DIGEST_BYTES: Final[int] = 32
MAX_PLAINTEXT_BYTES: Final[int] = 5 * 1024 * 1024
MAX_TTL_SECONDS: Final[int] = 86400
WRITING_GRACE_SECONDS: Final[int] = 600
MAX_RECONCILE_BATCH: Final[int] = 1000
MAX_REFERENCE_COLLISION_RETRIES: Final[int] = 3
LEASE_TOKEN_RAW_BYTES: Final[int] = 32
LEASE_TOKEN_LENGTH: Final[int] = 44
LEASE_TTL_SECONDS: Final[int] = 300
MAX_LEASE_TOKEN_COLLISION_RETRIES: Final[int] = 3
MAX_LEASE_RECLAIM_BATCH: Final[int] = 1000
MAX_PURGE_BATCH: Final[int] = 1000

_REFERENCE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}=$")
_LEASE_TOKEN_RE = _REFERENCE_TOKEN_RE


class AttachmentError(RuntimeError):
    """Fail-closed attachment spool error. Message is a fixed code only."""

    def __init__(self, code: object) -> None:
        if type(code) is not str or code not in _ALLOWED_ERROR_CODES:
            super().__init__("ATTACHMENT_CONFIG_INVALID")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "ATTACHMENT_CONFIG_INVALID"


class AttachmentKind(StrEnum):
    IMAGE = "IMAGE"


class AttachmentPurpose(StrEnum):
    INBOUND_ATTACHMENT_RELAY = "INBOUND_ATTACHMENT_RELAY"
    OUTBOUND_ATTACHMENT_DELIVERY = "OUTBOUND_ATTACHMENT_DELIVERY"


class AttachmentMime(StrEnum):
    IMAGE_JPEG = "image/jpeg"
    IMAGE_PNG = "image/png"


class AttachmentState(StrEnum):
    WRITING = "WRITING"
    STORED = "STORED"
    LEASED = "LEASED"
    DELETE_PENDING = "DELETE_PENDING"


class CiphertextInspectStatus(StrEnum):
    """Structured ciphertext filesystem inspection outcome.

    Distinguishes confirmed missing/mismatch from unsafe and transient IO.
    """

    MISSING = "MISSING"
    VALID = "VALID"
    MISMATCH = "MISMATCH"
    UNSAFE = "UNSAFE"
    IO_UNAVAILABLE = "IO_UNAVAILABLE"


class CiphertextUnlinkStatus(StrEnum):
    """Outcome of a safe regular-file unlink attempt."""

    REMOVED = "REMOVED"
    ALREADY_MISSING = "ALREADY_MISSING"
    UNSAFE = "UNSAFE"
    IO_UNAVAILABLE = "IO_UNAVAILABLE"


def _require_exact_uuid(value: object) -> UUID:
    if type(value) is not UUID:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    return value


def _require_exact_kind(value: object) -> AttachmentKind:
    if type(value) is not AttachmentKind:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    return value


def _require_exact_purpose(value: object) -> AttachmentPurpose:
    if type(value) is not AttachmentPurpose:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    return value


def _require_exact_mime(value: object) -> AttachmentMime:
    if type(value) is not AttachmentMime:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    return value


def _require_crypto_version(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 1:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    return value


def _require_key_id_token(value: object) -> str:
    if type(value) is not str:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    if not value or len(value) > 64:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    for char in value:
        o = ord(char)
        if not (
            (48 <= o <= 57)
            or (65 <= o <= 90)
            or char == "_"
        ):
            raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    return value


def _require_positive_size(value: object, *, max_bytes: int) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise AttachmentError("ATTACHMENT_VALUE_INVALID") from None
    if value <= 0:
        raise AttachmentError("ATTACHMENT_VALUE_INVALID") from None
    if value > max_bytes:
        raise AttachmentError("ATTACHMENT_TOO_LARGE") from None
    return value


@dataclass(frozen=True, slots=True, repr=False)
class AttachmentAad:
    """Associated data binding ciphertext to object and purpose context."""

    crypto_version: int
    record_id: UUID
    object_id: UUID
    key_id: str
    kind: AttachmentKind
    conversation_id: UUID
    purpose: AttachmentPurpose
    mime: AttachmentMime
    plaintext_size: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "crypto_version", _require_crypto_version(self.crypto_version)
        )
        object.__setattr__(self, "record_id", _require_exact_uuid(self.record_id))
        object.__setattr__(self, "object_id", _require_exact_uuid(self.object_id))
        object.__setattr__(self, "key_id", _require_key_id_token(self.key_id))
        object.__setattr__(self, "kind", _require_exact_kind(self.kind))
        object.__setattr__(
            self, "conversation_id", _require_exact_uuid(self.conversation_id)
        )
        object.__setattr__(self, "purpose", _require_exact_purpose(self.purpose))
        object.__setattr__(self, "mime", _require_exact_mime(self.mime))
        object.__setattr__(
            self,
            "plaintext_size",
            _require_positive_size(self.plaintext_size, max_bytes=MAX_PLAINTEXT_BYTES),
        )

    def to_bytes(self) -> bytes:
        parts = (
            "attachment-aad-v1",
            f"crypto_version={self.crypto_version:d}",
            f"record_id={self.record_id}",
            f"object_id={self.object_id}",
            f"key_id={self.key_id}",
            f"kind={self.kind.value}",
            f"conversation_id={self.conversation_id}",
            f"purpose={self.purpose.value}",
            f"mime={self.mime.value}",
            f"plaintext_size={self.plaintext_size:d}",
        )
        return "\n".join(parts).encode("utf-8")

    def __repr__(self) -> str:
        return (
            "AttachmentAad("
            f"crypto_version={self.crypto_version!r}, "
            "record_id=<redacted>, "
            "object_id=<redacted>, "
            "key_id=<redacted>, "
            f"kind={self.kind.value!r}, "
            "conversation_id=<redacted>, "
            f"purpose={self.purpose.value!r}, "
            f"mime={self.mime.value!r}, "
            f"plaintext_size={self.plaintext_size!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


@dataclass(frozen=True, slots=True, repr=False)
class AttachmentCiphertext:
    """Opaque AEAD envelope for attachment bytes. Never embeds plaintext."""

    ciphertext: bytes
    nonce: bytes
    key_id: str
    crypto_version: int
    ciphertext_sha256: bytes

    def __post_init__(self) -> None:
        if type(self.ciphertext) is not bytes:
            raise AttachmentError("ATTACHMENT_VALUE_INVALID") from None
        if len(self.ciphertext) < AES_GCM_TAG_BYTES:
            raise AttachmentError("ATTACHMENT_VALUE_INVALID") from None
        if type(self.nonce) is not bytes:
            raise AttachmentError("ATTACHMENT_VALUE_INVALID") from None
        if len(self.nonce) != NONCE_SIZE_BYTES:
            raise AttachmentError("ATTACHMENT_VALUE_INVALID") from None
        if type(self.ciphertext_sha256) is not bytes:
            raise AttachmentError("ATTACHMENT_VALUE_INVALID") from None
        if len(self.ciphertext_sha256) != SHA256_DIGEST_BYTES:
            raise AttachmentError("ATTACHMENT_VALUE_INVALID") from None
        object.__setattr__(self, "key_id", _require_key_id_token(self.key_id))
        object.__setattr__(
            self, "crypto_version", _require_crypto_version(self.crypto_version)
        )

    def __repr__(self) -> str:
        return (
            "AttachmentCiphertext("
            f"crypto_version={self.crypto_version!r}, "
            f"nonce_len={len(self.nonce)}, "
            f"ciphertext_len={len(self.ciphertext)}, "
            "ciphertext_sha256=<redacted>, "
            "key_id=<redacted>)"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


def _canonical_reference_token(raw: bytes) -> str:
    if type(raw) is not bytes or len(raw) != REFERENCE_RAW_BYTES:
        raise AttachmentError("ATTACHMENT_REFERENCE_INVALID") from None
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_reference_token(token: str) -> bytes:
    if _REFERENCE_TOKEN_RE.fullmatch(token) is None:
        raise AttachmentError("ATTACHMENT_REFERENCE_INVALID") from None
    try:
        decoded = base64.urlsafe_b64decode(token)
    except Exception:
        raise AttachmentError("ATTACHMENT_REFERENCE_INVALID") from None
    if type(decoded) is not bytes or len(decoded) != REFERENCE_RAW_BYTES:
        raise AttachmentError("ATTACHMENT_REFERENCE_INVALID") from None
    if _canonical_reference_token(decoded) != token:
        raise AttachmentError("ATTACHMENT_REFERENCE_INVALID") from None
    return decoded


class AttachmentReference:
    """Opaque 256-bit reference. Raw bytes never appear in repr or PostgreSQL."""

    __slots__ = ("_raw",)

    def __init__(self, raw: bytes) -> None:
        if type(raw) is not bytes or len(raw) != REFERENCE_RAW_BYTES:
            raise AttachmentError("ATTACHMENT_REFERENCE_INVALID") from None
        object.__setattr__(self, "_raw", raw)

    @classmethod
    def generate(cls) -> AttachmentReference:
        try:
            raw = secrets.token_bytes(REFERENCE_RAW_BYTES)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise AttachmentError("ATTACHMENT_REFERENCE_INVALID") from None
        return cls(raw)

    @classmethod
    def parse(cls, value: object) -> AttachmentReference:
        if type(value) is not str:
            raise AttachmentError("ATTACHMENT_REFERENCE_INVALID") from None
        return cls(_decode_reference_token(value))

    def to_token(self) -> str:
        return _canonical_reference_token(self._raw)

    def digest(self) -> bytes:
        return hashlib.sha256(self._raw).digest()

    def __repr__(self) -> str:
        return "AttachmentReference(<redacted>)"

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()

    def __eq__(self, other: object) -> bool:
        return type(other) is AttachmentReference and self._raw == other._raw

    def __hash__(self) -> int:
        return hash(self._raw)


def _canonical_lease_token(raw: bytes) -> str:
    if type(raw) is not bytes or len(raw) != LEASE_TOKEN_RAW_BYTES:
        raise AttachmentError("ATTACHMENT_LEASE_TOKEN_INVALID") from None
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_lease_token(token: str) -> bytes:
    if _LEASE_TOKEN_RE.fullmatch(token) is None:
        raise AttachmentError("ATTACHMENT_LEASE_TOKEN_INVALID") from None
    try:
        decoded = base64.urlsafe_b64decode(token)
    except Exception:
        raise AttachmentError("ATTACHMENT_LEASE_TOKEN_INVALID") from None
    if type(decoded) is not bytes or len(decoded) != LEASE_TOKEN_RAW_BYTES:
        raise AttachmentError("ATTACHMENT_LEASE_TOKEN_INVALID") from None
    if _canonical_lease_token(decoded) != token:
        raise AttachmentError("ATTACHMENT_LEASE_TOKEN_INVALID") from None
    return decoded


class AttachmentLeaseToken:
    """Opaque 256-bit lease credential. Raw bytes never appear in repr or PostgreSQL."""

    __slots__ = ("_raw",)

    def __init__(self, raw: bytes) -> None:
        if type(raw) is not bytes or len(raw) != LEASE_TOKEN_RAW_BYTES:
            raise AttachmentError("ATTACHMENT_LEASE_TOKEN_INVALID") from None
        object.__setattr__(self, "_raw", raw)

    @classmethod
    def generate(cls) -> AttachmentLeaseToken:
        try:
            raw = secrets.token_bytes(LEASE_TOKEN_RAW_BYTES)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise AttachmentError("ATTACHMENT_LEASE_TOKEN_INVALID") from None
        return cls(raw)

    @classmethod
    def parse(cls, value: object) -> AttachmentLeaseToken:
        if type(value) is not str:
            raise AttachmentError("ATTACHMENT_LEASE_TOKEN_INVALID") from None
        return cls(_decode_lease_token(value))

    def to_token(self) -> str:
        return _canonical_lease_token(self._raw)

    def digest(self) -> bytes:
        return hashlib.sha256(self._raw).digest()

    def __repr__(self) -> str:
        return "AttachmentLeaseToken(<redacted>)"

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()

    def __eq__(self, other: object) -> bool:
        return type(other) is AttachmentLeaseToken and self._raw == other._raw

    def __hash__(self) -> int:
        return hash(self._raw)


@dataclass(frozen=True, slots=True, repr=False)
class AttachmentLeaseHandle:
    """Lease grant returned by acquire. Contains only token and lease expiry."""

    token: AttachmentLeaseToken
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        if type(self.token) is not AttachmentLeaseToken:
            raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
        if type(self.lease_expires_at) is not datetime:
            raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
        if self.lease_expires_at.tzinfo is None:
            raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None

    def __repr__(self) -> str:
        return (
            "AttachmentLeaseHandle("
            "token=<redacted>, "
            "lease_expires_at=<redacted>)"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


@dataclass(frozen=True, slots=True, repr=False)
class AttachmentLeaseReclaimResult:
    """Count-only expired-lease reclaim outcome."""

    reclaimed: int
    skipped: int

    def __post_init__(self) -> None:
        for name in ("reclaimed", "skipped"):
            value = getattr(self, name)
            if type(value) is not int or isinstance(value, bool) or value < 0:
                raise AttachmentError("ATTACHMENT_RECONCILE_FAILED") from None

    def __repr__(self) -> str:
        return (
            "AttachmentLeaseReclaimResult("
            f"reclaimed={self.reclaimed}, "
            f"skipped={self.skipped})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


@dataclass(frozen=True, slots=True, repr=False)
class AttachmentPlaintext:
    """Decrypted attachment bytes with server-detected MIME only."""

    data: bytes
    mime: AttachmentMime

    def __post_init__(self) -> None:
        if type(self.data) is not bytes or self.data == b"":
            raise AttachmentError("ATTACHMENT_VALUE_INVALID") from None
        object.__setattr__(self, "mime", _require_exact_mime(self.mime))

    def __repr__(self) -> str:
        return f"AttachmentPlaintext(mime={self.mime.value!r}, data=<redacted>)"

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


@dataclass(frozen=True, slots=True, repr=False)
class AttachmentHandle:
    """Caller-facing store result. No database ids or ciphertext."""

    reference: AttachmentReference
    kind: AttachmentKind
    purpose: AttachmentPurpose
    mime: AttachmentMime
    plaintext_size: int

    def __post_init__(self) -> None:
        if type(self.reference) is not AttachmentReference:
            raise AttachmentError("ATTACHMENT_REFERENCE_INVALID") from None
        object.__setattr__(self, "kind", _require_exact_kind(self.kind))
        object.__setattr__(self, "purpose", _require_exact_purpose(self.purpose))
        object.__setattr__(self, "mime", _require_exact_mime(self.mime))
        object.__setattr__(
            self,
            "plaintext_size",
            _require_positive_size(self.plaintext_size, max_bytes=MAX_PLAINTEXT_BYTES),
        )

    def __repr__(self) -> str:
        return (
            "AttachmentHandle("
            "reference=<redacted>, "
            f"kind={self.kind.value!r}, "
            f"purpose={self.purpose.value!r}, "
            f"mime={self.mime.value!r}, "
            f"plaintext_size={self.plaintext_size!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


@dataclass(frozen=True, slots=True, repr=False)
class AttachmentSpoolPolicy:
    """Trusted server-side spool policy. Not accepted from store callers."""

    spool_root: Path
    ttl_seconds: int

    def __post_init__(self) -> None:
        if type(self.ttl_seconds) is not int or isinstance(self.ttl_seconds, bool):
            raise AttachmentError("ATTACHMENT_POLICY_INVALID") from None
        if not 1 <= self.ttl_seconds <= MAX_TTL_SECONDS:
            raise AttachmentError("ATTACHMENT_POLICY_INVALID") from None
        # Path is abstract; concrete values are PosixPath/WindowsPath.
        if not isinstance(self.spool_root, Path):
            raise AttachmentError("ATTACHMENT_POLICY_INVALID") from None
        root = self.spool_root
        if not root.is_absolute():
            raise AttachmentError("ATTACHMENT_POLICY_INVALID") from None
        try:
            if root.is_symlink():
                raise AttachmentError("ATTACHMENT_POLICY_INVALID") from None
            if root.exists():
                if not root.is_dir():
                    raise AttachmentError("ATTACHMENT_POLICY_INVALID") from None
                if os.path.islink(root):
                    raise AttachmentError("ATTACHMENT_POLICY_INVALID") from None
        except AttachmentError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise AttachmentError("ATTACHMENT_POLICY_INVALID") from None
        object.__setattr__(self, "spool_root", root)

    @property
    def max_plaintext_bytes(self) -> int:
        return MAX_PLAINTEXT_BYTES

    @property
    def writing_grace_seconds(self) -> int:
        return WRITING_GRACE_SECONDS

    def __repr__(self) -> str:
        return "AttachmentSpoolPolicy(<redacted>)"

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


@dataclass(frozen=True, slots=True, repr=False)
class AttachmentReconcileResult:
    """Count-only reconciliation outcome. No paths or identifiers."""

    promoted_to_stored: int
    deleted_writing_rows: int
    deleted_orphan_temps: int
    deleted_orphan_finals: int
    deleted_unrecoverable_stored: int
    deleted_delete_pending: int
    unsafe_skipped: int
    io_unavailable_skipped: int

    def __post_init__(self) -> None:
        for name in (
            "promoted_to_stored",
            "deleted_writing_rows",
            "deleted_orphan_temps",
            "deleted_orphan_finals",
            "deleted_unrecoverable_stored",
            "deleted_delete_pending",
            "unsafe_skipped",
            "io_unavailable_skipped",
        ):
            value = getattr(self, name)
            if type(value) is not int or isinstance(value, bool) or value < 0:
                raise AttachmentError("ATTACHMENT_RECONCILE_FAILED") from None

    def __repr__(self) -> str:
        return (
            "AttachmentReconcileResult("
            f"promoted_to_stored={self.promoted_to_stored}, "
            f"deleted_writing_rows={self.deleted_writing_rows}, "
            f"deleted_orphan_temps={self.deleted_orphan_temps}, "
            f"deleted_orphan_finals={self.deleted_orphan_finals}, "
            f"deleted_unrecoverable_stored={self.deleted_unrecoverable_stored}, "
            f"deleted_delete_pending={self.deleted_delete_pending}, "
            f"unsafe_skipped={self.unsafe_skipped}, "
            f"io_unavailable_skipped={self.io_unavailable_skipped})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


@dataclass(frozen=True, slots=True, repr=False)
class AttachmentPurgeResult:
    """Count-only expiry purge outcome. No paths or identifiers."""

    transitioned_stored: int
    transitioned_leased: int
    deleted: int
    unsafe_skipped: int
    io_unavailable_skipped: int
    skipped: int

    def __post_init__(self) -> None:
        for name in (
            "transitioned_stored",
            "transitioned_leased",
            "deleted",
            "unsafe_skipped",
            "io_unavailable_skipped",
            "skipped",
        ):
            value = getattr(self, name)
            if type(value) is not int or isinstance(value, bool) or value < 0:
                raise AttachmentError("ATTACHMENT_RECONCILE_FAILED") from None

    def __repr__(self) -> str:
        return (
            "AttachmentPurgeResult("
            f"transitioned_stored={self.transitioned_stored}, "
            f"transitioned_leased={self.transitioned_leased}, "
            f"deleted={self.deleted}, "
            f"unsafe_skipped={self.unsafe_skipped}, "
            f"io_unavailable_skipped={self.io_unavailable_skipped}, "
            f"skipped={self.skipped})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()
