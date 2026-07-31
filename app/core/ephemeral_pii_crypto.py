"""AES-256-GCM encrypt/decrypt for ephemeral PII values.

No storage, no AI recovery path, no key enumeration, no Fernet, no custom crypto.
"""

from __future__ import annotations

import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.ephemeral_pii_keys import EphemeralPiiKeyProvider
from app.core.ephemeral_pii_types import (
    CRYPTO_VERSION_V1,
    KEY_SIZE_BYTES,
    MAX_PLAINTEXT_BYTES,
    MIN_CIPHERTEXT_BYTES,
    NONCE_SIZE_BYTES,
    EphemeralPiiAad,
    EphemeralPiiCiphertext,
    EphemeralPiiError,
)


def _aad_bytes(aad: object) -> bytes:
    if type(aad) is not EphemeralPiiAad:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    try:
        return aad.to_bytes()
    except EphemeralPiiError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None


def _require_key_bytes(key: object) -> bytes:
    if type(key) is not bytes or len(key) != KEY_SIZE_BYTES:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    return key


def _decode_utf8_plaintext(data: bytes) -> str:
    """Decode authenticated plaintext. Invalid UTF-8 is access denial."""
    if type(data) is not bytes:
        raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED") from None
    try:
        return data.decode("utf-8")
    except Exception:
        raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED") from None


def _require_encrypt_nonce(nonce: object) -> bytes:
    """Post-condition for nonce generators used by encrypt_text."""
    if type(nonce) is not bytes or len(nonce) != NONCE_SIZE_BYTES:
        raise EphemeralPiiError("EPHEMERAL_PII_ENCRYPT_FAILED") from None
    return nonce


def encrypt_text(
    value: object,
    *,
    aad: EphemeralPiiAad,
    key_provider: EphemeralPiiKeyProvider,
) -> EphemeralPiiCiphertext:
    """Encrypt a strict ``str`` plaintext under AES-256-GCM with required AAD."""
    if type(value) is not str:
        raise EphemeralPiiError("EPHEMERAL_PII_VALUE_INVALID") from None
    if value == "":
        raise EphemeralPiiError("EPHEMERAL_PII_VALUE_INVALID") from None
    try:
        plaintext = value.encode("utf-8")
    except Exception:
        raise EphemeralPiiError("EPHEMERAL_PII_VALUE_INVALID") from None
    if len(plaintext) > MAX_PLAINTEXT_BYTES:
        raise EphemeralPiiError("EPHEMERAL_PII_VALUE_INVALID") from None

    aad_bytes = _aad_bytes(aad)
    if aad.crypto_version != CRYPTO_VERSION_V1:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None

    try:
        active_key_id = key_provider.active_key_id()
        if active_key_id != aad.key_id:
            raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
        key = _require_key_bytes(key_provider.get_key(active_key_id))
    except EphemeralPiiError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise EphemeralPiiError("EPHEMERAL_PII_KEY_UNAVAILABLE") from None

    try:
        nonce = secrets.token_bytes(NONCE_SIZE_BYTES)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise EphemeralPiiError("EPHEMERAL_PII_ENCRYPT_FAILED") from None

    nonce = _require_encrypt_nonce(nonce)

    try:
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, aad_bytes)
    except EphemeralPiiError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise EphemeralPiiError("EPHEMERAL_PII_ENCRYPT_FAILED") from None

    try:
        return EphemeralPiiCiphertext(
            ciphertext=ciphertext,
            nonce=nonce,
            key_id=active_key_id,
            crypto_version=CRYPTO_VERSION_V1,
        )
    except EphemeralPiiError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise EphemeralPiiError("EPHEMERAL_PII_ENCRYPT_FAILED") from None


def decrypt_text(
    encrypted: object,
    *,
    aad: EphemeralPiiAad,
    key_provider: EphemeralPiiKeyProvider,
) -> str:
    """Decrypt and authenticate. Crypto failures unify to ACCESS_DENIED."""
    if type(encrypted) is not EphemeralPiiCiphertext:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    aad_bytes = _aad_bytes(aad)

    if encrypted.crypto_version != CRYPTO_VERSION_V1:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    if aad.crypto_version != encrypted.crypto_version:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    if aad.key_id != encrypted.key_id:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    # Defense in depth: DTO invariants already enforce these shapes.
    if type(encrypted.nonce) is not bytes or len(encrypted.nonce) != NONCE_SIZE_BYTES:
        raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED") from None
    if (
        type(encrypted.ciphertext) is not bytes
        or len(encrypted.ciphertext) < MIN_CIPHERTEXT_BYTES
    ):
        raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED") from None

    # Unify all recorded-key fetch failures so decrypt cannot oracle
    # missing vs malformed vs wrong-length key material.
    try:
        raw_key = key_provider.get_key(encrypted.key_id)
    except EphemeralPiiError:
        raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED") from None
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED") from None

    if type(raw_key) is not bytes or len(raw_key) != KEY_SIZE_BYTES:
        raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED") from None
    key = raw_key

    try:
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(encrypted.nonce, encrypted.ciphertext, aad_bytes)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED") from None

    return _decode_utf8_plaintext(plaintext)
