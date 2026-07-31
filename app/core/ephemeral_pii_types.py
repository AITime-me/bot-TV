"""Closed types for encrypted ephemeral PII foundation (Stage 2A).

No storage, AI recovery, or integration adapters live here.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

_ALLOWED_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "EPHEMERAL_PII_CONFIG_INVALID",
        "EPHEMERAL_PII_KEY_UNAVAILABLE",
        "EPHEMERAL_PII_VALUE_INVALID",
        "EPHEMERAL_PII_ENCRYPT_FAILED",
        "EPHEMERAL_PII_ACCESS_DENIED",
        "EPHEMERAL_PII_REFERENCE_INVALID",
        "EPHEMERAL_PII_STORE_FAILED",
        "EPHEMERAL_PII_PURGE_FAILED",
        "EPHEMERAL_PII_POLICY_INVALID",
    }
)

CRYPTO_VERSION_V1: Final[int] = 1
KEY_SIZE_BYTES: Final[int] = 32
NONCE_SIZE_BYTES: Final[int] = 12
REFERENCE_RAW_BYTES: Final[int] = 32
REFERENCE_DIGEST_BYTES: Final[int] = 32
REFERENCE_TOKEN_LENGTH: Final[int] = 44
# AES-GCM ciphertext always includes a 16-byte authentication tag.
MIN_CIPHERTEXT_BYTES: Final[int] = 16
# Hard limit for one ephemeral plaintext value (UTF-8 bytes). Phones are tiny;
# keep headroom without allowing large free-text blobs.
MAX_PLAINTEXT_BYTES: Final[int] = 256
MAX_TTL_SECONDS: Final[int] = 86400
MAX_PURGE_BATCH: Final[int] = 1000
MAX_REFERENCE_COLLISION_RETRIES: Final[int] = 3

_REFERENCE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}=$")


class EphemeralPiiError(RuntimeError):
    """Fail-closed ephemeral PII boundary error. Message is a fixed code only."""

    def __init__(self, code: object) -> None:
        if type(code) is not str or code not in _ALLOWED_ERROR_CODES:
            super().__init__("EPHEMERAL_PII_CONFIG_INVALID")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "EPHEMERAL_PII_CONFIG_INVALID"


class EphemeralPiiKind(StrEnum):
    PHONE = "PHONE"


class EphemeralPiiPurpose(StrEnum):
    BOOKING_PHONE_WRITE = "BOOKING_PHONE_WRITE"
    APPROVED_STAFF_ALERT_PHONE = "APPROVED_STAFF_ALERT_PHONE"
    AMOCRM_CONTACT_SYNC = "AMOCRM_CONTACT_SYNC"


def _require_exact_uuid(value: object) -> UUID:
    if type(value) is not UUID:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    return value


def _require_exact_kind(value: object) -> EphemeralPiiKind:
    if type(value) is not EphemeralPiiKind:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    return value


def _require_exact_purpose(value: object) -> EphemeralPiiPurpose:
    if type(value) is not EphemeralPiiPurpose:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    return value


def _require_crypto_version(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 1:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    return value


def _require_key_id_token(value: object) -> str:
    # Deferred to keys module pattern: ASCII [A-Z0-9_]{1,64} only.
    if type(value) is not str:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    if not value or len(value) > 64:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    for char in value:
        o = ord(char)
        if not (
            (48 <= o <= 57)  # 0-9
            or (65 <= o <= 90)  # A-Z
            or char == "_"
        ):
            raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    return value


@dataclass(frozen=True, slots=True, repr=False)
class EphemeralPiiAad:
    """Associated data binding ciphertext to record and purpose context."""

    crypto_version: int
    record_id: UUID
    key_id: str
    kind: EphemeralPiiKind
    conversation_id: UUID
    purpose: EphemeralPiiPurpose

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "crypto_version", _require_crypto_version(self.crypto_version)
        )
        object.__setattr__(self, "record_id", _require_exact_uuid(self.record_id))
        object.__setattr__(self, "key_id", _require_key_id_token(self.key_id))
        object.__setattr__(self, "kind", _require_exact_kind(self.kind))
        object.__setattr__(
            self, "conversation_id", _require_exact_uuid(self.conversation_id)
        )
        object.__setattr__(self, "purpose", _require_exact_purpose(self.purpose))

    def to_bytes(self) -> bytes:
        """Deterministic UTF-8 AAD. Fixed field order; no JSON; no user text."""
        # UUID str() is canonical 8-4-4-4-12 lowercase hex form.
        parts = (
            "epii-aad-v1",
            f"crypto_version={self.crypto_version:d}",
            f"record_id={self.record_id}",
            f"key_id={self.key_id}",
            f"kind={self.kind.value}",
            f"conversation_id={self.conversation_id}",
            f"purpose={self.purpose.value}",
        )
        return "\n".join(parts).encode("utf-8")

    def __repr__(self) -> str:
        return (
            "EphemeralPiiAad("
            f"crypto_version={self.crypto_version!r}, "
            "record_id=<redacted>, "
            "key_id=<redacted>, "
            f"kind={self.kind.value!r}, "
            "conversation_id=<redacted>, "
            f"purpose={self.purpose.value!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


@dataclass(frozen=True, slots=True, repr=False)
class EphemeralPiiCiphertext:
    """Opaque AEAD envelope. Never embeds plaintext."""

    ciphertext: bytes
    nonce: bytes
    key_id: str
    crypto_version: int

    def __post_init__(self) -> None:
        if type(self.ciphertext) is not bytes:
            raise EphemeralPiiError("EPHEMERAL_PII_VALUE_INVALID") from None
        if len(self.ciphertext) < MIN_CIPHERTEXT_BYTES:
            raise EphemeralPiiError("EPHEMERAL_PII_VALUE_INVALID") from None
        if type(self.nonce) is not bytes:
            raise EphemeralPiiError("EPHEMERAL_PII_VALUE_INVALID") from None
        if len(self.nonce) != NONCE_SIZE_BYTES:
            raise EphemeralPiiError("EPHEMERAL_PII_VALUE_INVALID") from None
        object.__setattr__(self, "key_id", _require_key_id_token(self.key_id))
        object.__setattr__(
            self, "crypto_version", _require_crypto_version(self.crypto_version)
        )

    def __repr__(self) -> str:
        return (
            "EphemeralPiiCiphertext("
            f"crypto_version={self.crypto_version!r}, "
            f"nonce_len={len(self.nonce)}, "
            f"ciphertext_len={len(self.ciphertext)}, "
            "key_id=<redacted>)"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


def _canonical_reference_token(raw: bytes) -> str:
    if type(raw) is not bytes or len(raw) != REFERENCE_RAW_BYTES:
        raise EphemeralPiiError("EPHEMERAL_PII_REFERENCE_INVALID") from None
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_reference_token(token: str) -> bytes:
    if _REFERENCE_TOKEN_RE.fullmatch(token) is None:
        raise EphemeralPiiError("EPHEMERAL_PII_REFERENCE_INVALID") from None
    try:
        decoded = base64.urlsafe_b64decode(token)
    except Exception:
        raise EphemeralPiiError("EPHEMERAL_PII_REFERENCE_INVALID") from None
    if type(decoded) is not bytes or len(decoded) != REFERENCE_RAW_BYTES:
        raise EphemeralPiiError("EPHEMERAL_PII_REFERENCE_INVALID") from None
    if _canonical_reference_token(decoded) != token:
        raise EphemeralPiiError("EPHEMERAL_PII_REFERENCE_INVALID") from None
    return decoded


class EphemeralPiiReference:
    """Opaque 256-bit reference. Raw bytes never appear in repr or PostgreSQL."""

    __slots__ = ("_raw",)

    def __init__(self, raw: bytes) -> None:
        if type(raw) is not bytes or len(raw) != REFERENCE_RAW_BYTES:
            raise EphemeralPiiError("EPHEMERAL_PII_REFERENCE_INVALID") from None
        object.__setattr__(self, "_raw", raw)

    @classmethod
    def generate(cls) -> EphemeralPiiReference:
        try:
            raw = secrets.token_bytes(REFERENCE_RAW_BYTES)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise EphemeralPiiError("EPHEMERAL_PII_REFERENCE_INVALID") from None
        return cls(raw)

    @classmethod
    def parse(cls, value: object) -> EphemeralPiiReference:
        if type(value) is not str:
            raise EphemeralPiiError("EPHEMERAL_PII_REFERENCE_INVALID") from None
        return cls(_decode_reference_token(value))

    def to_token(self) -> str:
        return _canonical_reference_token(self._raw)

    def digest(self) -> bytes:
        return hashlib.sha256(self._raw).digest()

    def __repr__(self) -> str:
        return "EphemeralPiiReference(<redacted>)"

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()

    def __eq__(self, other: object) -> bool:
        return type(other) is EphemeralPiiReference and self._raw == other._raw

    def __hash__(self) -> int:
        return hash(self._raw)


@dataclass(frozen=True, slots=True, repr=False)
class EphemeralPiiHandle:
    """Caller-facing store result. No database ids or ciphertext."""

    reference: EphemeralPiiReference
    kind: EphemeralPiiKind
    purpose: EphemeralPiiPurpose

    def __post_init__(self) -> None:
        if type(self.reference) is not EphemeralPiiReference:
            raise EphemeralPiiError("EPHEMERAL_PII_REFERENCE_INVALID") from None
        object.__setattr__(self, "kind", _require_exact_kind(self.kind))
        object.__setattr__(self, "purpose", _require_exact_purpose(self.purpose))

    def __repr__(self) -> str:
        return (
            "EphemeralPiiHandle("
            "reference=<redacted>, "
            f"kind={self.kind.value!r}, "
            f"purpose={self.purpose.value!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


@dataclass(frozen=True, slots=True, repr=False)
class EphemeralPiiTtlPolicy:
    """Trusted server-side TTL. Not accepted from store callers."""

    ttl_seconds: int

    def __post_init__(self) -> None:
        if type(self.ttl_seconds) is not int or isinstance(self.ttl_seconds, bool):
            raise EphemeralPiiError("EPHEMERAL_PII_POLICY_INVALID") from None
        if not 1 <= self.ttl_seconds <= MAX_TTL_SECONDS:
            raise EphemeralPiiError("EPHEMERAL_PII_POLICY_INVALID") from None

    def __repr__(self) -> str:
        return "EphemeralPiiTtlPolicy(<redacted>)"

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()
