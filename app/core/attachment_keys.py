"""Lazy environment key provider for attachment spool AEAD keys.

Keys are never required at import or BOT_MODE=OFF health startup.
Public API does not enumerate keys or expose key material via repr.
"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from app.core.attachment_types import (
    KEY_SIZE_BYTES,
    AttachmentError,
    _require_key_id_token,
)

_ACTIVE_KEY_ID_ENV = "ATTACHMENT_SPOOL_ACTIVE_KEY_ID"
_KEY_ENV_PREFIX = "ATTACHMENT_SPOOL_KEY_"

_KEY_ID_RE = re.compile(r"^[A-Z0-9_]{1,64}$")


@dataclass(frozen=True, slots=True, repr=False)
class ActiveAttachmentKey:
    """Immutable active key snapshot for encrypt without re-reading env."""

    key_id: str
    key: bytes

    def __post_init__(self) -> None:
        validated_id = validate_key_id(self.key_id)
        if type(self.key) is not bytes or len(self.key) != KEY_SIZE_BYTES:
            raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
        object.__setattr__(self, "key_id", validated_id)

    def __repr__(self) -> str:
        return "ActiveAttachmentKey(key_id=<redacted>, key=<redacted>)"

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


class AttachmentKeyProvider(Protocol):
    def active_key_id(self) -> str:
        """Return the active key id for new encrypt operations."""

    def get_key(self, key_id: str) -> bytes:
        """Return exactly 32 raw key bytes for the given key id."""

    def get_active_key(self) -> ActiveAttachmentKey:
        """Return an immutable active key id/material pair."""


def validate_key_id(value: object) -> str:
    """Validate a key id without echoing it into errors."""
    try:
        token = _require_key_id_token(value)
    except AttachmentError:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    if _KEY_ID_RE.fullmatch(token) is None:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    return token


def _decode_key_material(raw: object) -> bytes:
    if type(raw) is not str:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    if raw == "":
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    for char in raw:
        if char.isspace():
            raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    if not re.fullmatch(r"[A-Za-z0-9_-]+=*", raw):
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    try:
        decoded = base64.urlsafe_b64decode(raw)
    except Exception:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    if type(decoded) is not bytes or len(decoded) != KEY_SIZE_BYTES:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii")
    if raw != canonical:
        raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
    return decoded


class EnvAttachmentKeyProvider:
    """Read attachment spool keys from environment mappings on demand."""

    __slots__ = ("_environ",)

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def __repr__(self) -> str:
        return "EnvAttachmentKeyProvider()"

    def __str__(self) -> str:
        return "EnvAttachmentKeyProvider()"

    def active_key_id(self) -> str:
        try:
            raw = self._environ.get(_ACTIVE_KEY_ID_ENV)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
        if raw is None:
            raise AttachmentError("ATTACHMENT_KEY_UNAVAILABLE") from None
        try:
            return validate_key_id(raw)
        except AttachmentError as exc:
            if exc.code == "ATTACHMENT_CONFIG_INVALID":
                raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
            raise AttachmentError("ATTACHMENT_KEY_UNAVAILABLE") from None

    def get_key(self, key_id: object) -> bytes:
        try:
            validated = validate_key_id(key_id)
        except AttachmentError:
            raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
        env_name = f"{_KEY_ENV_PREFIX}{validated}"
        try:
            raw = self._environ.get(env_name)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise AttachmentError("ATTACHMENT_KEY_UNAVAILABLE") from None
        if raw is None:
            raise AttachmentError("ATTACHMENT_KEY_UNAVAILABLE") from None
        try:
            return _decode_key_material(raw)
        except AttachmentError:
            raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise AttachmentError("ATTACHMENT_CONFIG_INVALID") from None

    def get_active_key(self) -> ActiveAttachmentKey:
        key_id = self.active_key_id()
        key = self.get_key(key_id)
        return ActiveAttachmentKey(key_id=key_id, key=key)
