"""Closed types for encrypted ephemeral PII foundation (Stage 2A).

No storage, AI recovery, or integration adapters live here.
"""

from __future__ import annotations

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
    }
)

CRYPTO_VERSION_V1: Final[int] = 1
KEY_SIZE_BYTES: Final[int] = 32
NONCE_SIZE_BYTES: Final[int] = 12
# AES-GCM ciphertext always includes a 16-byte authentication tag.
MIN_CIPHERTEXT_BYTES: Final[int] = 16
# Hard limit for one ephemeral plaintext value (UTF-8 bytes). Phones are tiny;
# keep headroom without allowing large free-text blobs.
MAX_PLAINTEXT_BYTES: Final[int] = 256


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
