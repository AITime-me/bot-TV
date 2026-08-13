"""AES-256-GCM encrypt/decrypt for durable amoCRM CRM OAuth tokens.

Identical primitive stack to ephemeral PII / attachment spool.
"""

from __future__ import annotations

import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.amocrm_crm_oauth_keys import (
    ActiveAmoCrmOauthKey,
    AmoCrmOauthKeyProvider,
)
from app.core.amocrm_crm_oauth_types import (
    CRYPTO_VERSION_V1,
    KEY_SIZE_BYTES,
    MAX_TOKEN_PLAINTEXT_BYTES,
    MIN_CIPHERTEXT_BYTES,
    NONCE_SIZE_BYTES,
    AmoCrmCrmOauthError,
    AmoCrmOauthAad,
    AmoCrmOauthCiphertext,
)


def _require_key_bytes(key: object) -> bytes:
    if type(key) is not bytes or len(key) != KEY_SIZE_BYTES:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
    return key


def encrypt_token(
    value: object,
    *,
    aad: AmoCrmOauthAad,
    key_provider: AmoCrmOauthKeyProvider,
    active_key: ActiveAmoCrmOauthKey | None = None,
) -> AmoCrmOauthCiphertext:
    if type(value) is not str or value == "":
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_VALUE_INVALID") from None
    try:
        plaintext = value.encode("utf-8")
    except Exception:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_VALUE_INVALID") from None
    if len(plaintext) > MAX_TOKEN_PLAINTEXT_BYTES:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_VALUE_INVALID") from None
    if type(aad) is not AmoCrmOauthAad:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
    if aad.crypto_version != CRYPTO_VERSION_V1:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None

    if active_key is not None:
        if type(active_key) is not ActiveAmoCrmOauthKey:
            raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
        if active_key.key_id != aad.key_id:
            raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
        key = _require_key_bytes(active_key.key)
        key_id = active_key.key_id
    else:
        try:
            key_id = key_provider.active_key_id()
            if key_id != aad.key_id:
                raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
            key = _require_key_bytes(key_provider.get_key(key_id))
        except AmoCrmCrmOauthError:
            raise
        except Exception:
            raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_KEY_UNAVAILABLE") from None

    try:
        nonce = secrets.token_bytes(NONCE_SIZE_BYTES)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad.to_bytes())
    except AmoCrmCrmOauthError:
        raise
    except Exception:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_ENCRYPT_FAILED") from None

    return AmoCrmOauthCiphertext(
        crypto_version=CRYPTO_VERSION_V1,
        key_id=key_id,
        nonce=nonce,
        ciphertext=ciphertext,
    )


def decrypt_token(
    encrypted: object,
    *,
    aad: AmoCrmOauthAad,
    key_provider: AmoCrmOauthKeyProvider,
) -> str:
    if type(encrypted) is not AmoCrmOauthCiphertext:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_ACCESS_DENIED") from None
    if type(aad) is not AmoCrmOauthAad:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_ACCESS_DENIED") from None
    if encrypted.crypto_version != CRYPTO_VERSION_V1:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_ACCESS_DENIED") from None
    if aad.crypto_version != encrypted.crypto_version or aad.key_id != encrypted.key_id:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_ACCESS_DENIED") from None
    if len(encrypted.nonce) != NONCE_SIZE_BYTES:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_ACCESS_DENIED") from None
    if len(encrypted.ciphertext) < MIN_CIPHERTEXT_BYTES:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_ACCESS_DENIED") from None
    try:
        key = _require_key_bytes(key_provider.get_key(encrypted.key_id))
        plaintext = AESGCM(key).decrypt(
            encrypted.nonce, encrypted.ciphertext, aad.to_bytes()
        )
        return plaintext.decode("utf-8")
    except AmoCrmCrmOauthError:
        raise
    except Exception:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_ACCESS_DENIED") from None
