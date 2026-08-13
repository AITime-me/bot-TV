"""Closed types for durable amoCRM CRM OAuth token encryption (AMO-01B2).

Reuses the project AES-256-GCM + env key-provider pattern (same as ephemeral
PII / attachment spool). No Fernet, no custom ciphers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

_ALLOWED_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "AMOCRM_CRM_OAUTH_CONFIG_INVALID",
        "AMOCRM_CRM_OAUTH_KEY_UNAVAILABLE",
        "AMOCRM_CRM_OAUTH_ENCRYPT_FAILED",
        "AMOCRM_CRM_OAUTH_ACCESS_DENIED",
        "AMOCRM_CRM_OAUTH_VALUE_INVALID",
        "AMOCRM_CRM_OAUTH_STALE_LEASE",
        "AMOCRM_CRM_OAUTH_NOT_FOUND",
        "AMOCRM_CRM_OAUTH_STORE_FAILED",
        # Post-200 local persist failed; remote refresh must not be retried.
        "AMOCRM_CRM_OAUTH_ROTATE_PERSIST_FAILED",
        # DB no longer holds the pre-refresh pair used for that HTTP call.
        "AMOCRM_CRM_OAUTH_ROTATE_SUPERSEDED",
    }
)

CRYPTO_VERSION_V1: Final[int] = 1
KEY_SIZE_BYTES: Final[int] = 32
NONCE_SIZE_BYTES: Final[int] = 12
MIN_CIPHERTEXT_BYTES: Final[int] = 16
MAX_TOKEN_PLAINTEXT_BYTES: Final[int] = 4096
DEFAULT_CONNECTION_SCOPE: Final[str] = "default"


class AmoCrmCrmOauthError(RuntimeError):
    """Fixed codes only — never embed token material."""

    def __init__(self, code: object) -> None:
        if type(code) is not str or code not in _ALLOWED_ERROR_CODES:
            super().__init__("AMOCRM_CRM_OAUTH_CONFIG_INVALID")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "AMOCRM_CRM_OAUTH_CONFIG_INVALID"

    def __repr__(self) -> str:
        return f"AmoCrmCrmOauthError({self.code!r})"


class AmoCrmOauthTokenKind(StrEnum):
    ACCESS = "ACCESS"
    REFRESH = "REFRESH"


def _require_key_id_token(value: object) -> str:
    if type(value) is not str:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
    if not value or len(value) > 64:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
    for char in value:
        o = ord(char)
        if not ((48 <= o <= 57) or (65 <= o <= 90) or char == "_"):
            raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
    return value


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmOauthAad:
    """AAD binds ciphertext to connection scope + token kind."""

    crypto_version: int
    key_id: str
    connection_scope: str
    token_kind: AmoCrmOauthTokenKind

    def __post_init__(self) -> None:
        if self.crypto_version != CRYPTO_VERSION_V1:
            raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
        object.__setattr__(self, "key_id", _require_key_id_token(self.key_id))
        if type(self.connection_scope) is not str or not self.connection_scope:
            raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
        if len(self.connection_scope) > 64:
            raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
        if any(ch.isspace() or ord(ch) < 33 for ch in self.connection_scope):
            raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
        if type(self.token_kind) is not AmoCrmOauthTokenKind:
            raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None

    def to_bytes(self) -> bytes:
        return (
            f"v{self.crypto_version}|{self.key_id}|"
            f"{self.connection_scope}|{self.token_kind.value}"
        ).encode("utf-8")

    def __repr__(self) -> str:
        return (
            "AmoCrmOauthAad("
            f"crypto_version={self.crypto_version!r}, "
            "key_id=<redacted>, connection_scope=<redacted>, "
            f"token_kind={self.token_kind.value!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmOauthCiphertext:
    crypto_version: int
    key_id: str
    nonce: bytes
    ciphertext: bytes

    def __post_init__(self) -> None:
        if self.crypto_version != CRYPTO_VERSION_V1:
            raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
        object.__setattr__(self, "key_id", _require_key_id_token(self.key_id))
        if type(self.nonce) is not bytes or len(self.nonce) != NONCE_SIZE_BYTES:
            raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
        if (
            type(self.ciphertext) is not bytes
            or len(self.ciphertext) < MIN_CIPHERTEXT_BYTES
        ):
            raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None

    def __repr__(self) -> str:
        return (
            "AmoCrmOauthCiphertext("
            f"crypto_version={self.crypto_version!r}, "
            "key_id=<redacted>, nonce=<redacted>, ciphertext=<redacted>)"
        )
