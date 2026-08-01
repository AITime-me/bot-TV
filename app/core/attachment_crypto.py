"""AES-256-GCM encrypt/decrypt for attachment spool bytes.

No filesystem storage, no delivery API, no AI recovery path.
"""

from __future__ import annotations

import hashlib
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.attachment_keys import ActiveAttachmentKey, AttachmentKeyProvider
from app.core.attachment_types import (
    AES_GCM_TAG_BYTES,
    CRYPTO_VERSION_V1,
    KEY_SIZE_BYTES,
    MAX_PLAINTEXT_BYTES,
    NONCE_SIZE_BYTES,
    AttachmentAad,
    AttachmentCiphertext,
    AttachmentError,
)


def _aad_bytes(aad: object) -> bytes:
    if type(aad) is not AttachmentAad:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    try:
        return aad.to_bytes()
    except AttachmentError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None


def _require_key_bytes(key: object) -> bytes:
    if type(key) is not bytes or len(key) != KEY_SIZE_BYTES:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    return key


def _require_encrypt_nonce(nonce: object) -> bytes:
    if type(nonce) is not bytes or len(nonce) != NONCE_SIZE_BYTES:
        raise AttachmentError("ATTACHMENT_ENCRYPT_FAILED") from None
    return nonce


def encrypt_bytes(
    value: object,
    *,
    aad: AttachmentAad,
    key_provider: AttachmentKeyProvider,
    active_key: ActiveAttachmentKey | None = None,
) -> AttachmentCiphertext:
    """Encrypt exact ``bytes`` plaintext under AES-256-GCM with required AAD."""
    if type(value) is not bytes:
        raise AttachmentError("ATTACHMENT_VALUE_INVALID") from None
    if value == b"":
        raise AttachmentError("ATTACHMENT_VALUE_INVALID") from None
    if len(value) > MAX_PLAINTEXT_BYTES:
        raise AttachmentError("ATTACHMENT_TOO_LARGE") from None
    if aad.plaintext_size != len(value):
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None

    aad_bytes = _aad_bytes(aad)
    if aad.crypto_version != CRYPTO_VERSION_V1:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None

    if active_key is not None:
        if type(active_key) is not ActiveAttachmentKey:
            raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
        if active_key.key_id != aad.key_id:
            raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
        key = _require_key_bytes(active_key.key)
        active_key_id = active_key.key_id
    else:
        try:
            active_key_id = key_provider.active_key_id()
            if active_key_id != aad.key_id:
                raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
            key = _require_key_bytes(key_provider.get_key(active_key_id))
        except AttachmentError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise AttachmentError("ATTACHMENT_KEY_UNAVAILABLE") from None

    try:
        nonce = secrets.token_bytes(NONCE_SIZE_BYTES)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise AttachmentError("ATTACHMENT_ENCRYPT_FAILED") from None

    nonce = _require_encrypt_nonce(nonce)

    try:
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, value, aad_bytes)
    except AttachmentError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise AttachmentError("ATTACHMENT_ENCRYPT_FAILED") from None

    if len(ciphertext) != len(value) + AES_GCM_TAG_BYTES:
        raise AttachmentError("ATTACHMENT_ENCRYPT_FAILED") from None

    digest = hashlib.sha256(ciphertext).digest()
    try:
        return AttachmentCiphertext(
            ciphertext=ciphertext,
            nonce=nonce,
            key_id=active_key_id,
            crypto_version=CRYPTO_VERSION_V1,
            ciphertext_sha256=digest,
        )
    except AttachmentError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise AttachmentError("ATTACHMENT_ENCRYPT_FAILED") from None


def decrypt_bytes(
    encrypted: object,
    *,
    aad: AttachmentAad,
    key_provider: AttachmentKeyProvider,
) -> bytes:
    """Decrypt and authenticate. Crypto failures unify to ACCESS_DENIED.

    Internal primitive only. Stage 1A1 store does not expose a public read API.
    """
    if type(encrypted) is not AttachmentCiphertext:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    aad_bytes = _aad_bytes(aad)

    if encrypted.crypto_version != CRYPTO_VERSION_V1:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    if aad.crypto_version != encrypted.crypto_version:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    if aad.key_id != encrypted.key_id:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    if type(encrypted.nonce) is not bytes or len(encrypted.nonce) != NONCE_SIZE_BYTES:
        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
    if (
        type(encrypted.ciphertext) is not bytes
        or len(encrypted.ciphertext) < AES_GCM_TAG_BYTES
    ):
        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
    if aad.plaintext_size != len(encrypted.ciphertext) - AES_GCM_TAG_BYTES:
        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None

    try:
        raw_key = key_provider.get_key(encrypted.key_id)
    except AttachmentError:
        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None

    if type(raw_key) is not bytes or len(raw_key) != KEY_SIZE_BYTES:
        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None

    try:
        aesgcm = AESGCM(raw_key)
        plaintext = aesgcm.decrypt(encrypted.nonce, encrypted.ciphertext, aad_bytes)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None

    if type(plaintext) is not bytes or len(plaintext) != aad.plaintext_size:
        raise AttachmentError("ATTACHMENT_ACCESS_DENIED") from None
    return plaintext
