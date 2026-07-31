"""Lazy environment key provider for ephemeral PII AEAD keys.

Keys are never required at import or BOT_MODE=OFF health startup.
Public API does not enumerate keys or expose key material via repr.
"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Mapping
from typing import Protocol

from app.core.ephemeral_pii_types import (
    KEY_SIZE_BYTES,
    EphemeralPiiError,
    _require_key_id_token,
)

_ACTIVE_KEY_ID_ENV = "EPHEMERAL_PII_ACTIVE_KEY_ID"
_KEY_ENV_PREFIX = "EPHEMERAL_PII_KEY_"

# Exact closed pattern: ASCII uppercase, digits, underscore; 1–64 chars.
# Dots, hyphens, slashes, spaces, lowercase, and Unicode are rejected.
_KEY_ID_RE = re.compile(r"^[A-Z0-9_]{1,64}$")


class EphemeralPiiKeyProvider(Protocol):
    def active_key_id(self) -> str:
        """Return the active key id for new encrypt operations."""

    def get_key(self, key_id: str) -> bytes:
        """Return exactly 32 raw key bytes for the given key id."""


def validate_key_id(value: object) -> str:
    """Validate a key id without echoing it into errors."""
    try:
        token = _require_key_id_token(value)
    except EphemeralPiiError:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    if _KEY_ID_RE.fullmatch(token) is None:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    return token


def _decode_key_material(raw: object) -> bytes:
    if type(raw) is not str:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    if raw == "":
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    # Reject any whitespace, including surrounding.
    for char in raw:
        if char.isspace():
            raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    # Closed base64url alphabet with optional single padding block only as
    # produced by base64.urlsafe_b64encode for 32-byte inputs.
    if not re.fullmatch(r"[A-Za-z0-9_-]+=*", raw):
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    try:
        decoded = base64.urlsafe_b64decode(raw)
    except Exception:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    if type(decoded) is not bytes or len(decoded) != KEY_SIZE_BYTES:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii")
    if raw != canonical:
        raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
    return decoded


class EnvEphemeralPiiKeyProvider:
    """Read ephemeral PII keys from environment mappings on demand."""

    __slots__ = ("_environ",)

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        # Lazy: do not read or validate keys at construction time.
        self._environ = os.environ if environ is None else environ

    def __repr__(self) -> str:
        return "EnvEphemeralPiiKeyProvider()"

    def __str__(self) -> str:
        return "EnvEphemeralPiiKeyProvider()"

    def active_key_id(self) -> str:
        try:
            raw = self._environ.get(_ACTIVE_KEY_ID_ENV)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
        if raw is None:
            raise EphemeralPiiError("EPHEMERAL_PII_KEY_UNAVAILABLE") from None
        try:
            return validate_key_id(raw)
        except EphemeralPiiError as exc:
            if exc.code == "EPHEMERAL_PII_CONFIG_INVALID":
                raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
            raise EphemeralPiiError("EPHEMERAL_PII_KEY_UNAVAILABLE") from None

    def get_key(self, key_id: object) -> bytes:
        try:
            validated = validate_key_id(key_id)
        except EphemeralPiiError:
            raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
        env_name = f"{_KEY_ENV_PREFIX}{validated}"
        try:
            raw = self._environ.get(env_name)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise EphemeralPiiError("EPHEMERAL_PII_KEY_UNAVAILABLE") from None
        if raw is None:
            raise EphemeralPiiError("EPHEMERAL_PII_KEY_UNAVAILABLE") from None
        try:
            return _decode_key_material(raw)
        except EphemeralPiiError:
            raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise EphemeralPiiError("EPHEMERAL_PII_CONFIG_INVALID") from None
