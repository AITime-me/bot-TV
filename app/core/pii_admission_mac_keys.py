"""Persistent env key provider for PII admission content MAC.

Separate from EphemeralPii AEAD keys and process-local log fingerprints.
"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Mapping
from typing import Protocol

from app.core.pii_admission_mac_types import (
    MAC_KEY_SIZE_BYTES,
    ActivePiiAdmissionMacKey,
    PiiAdmissionMacError,
)

_ACTIVE_KEY_ID_ENV = "PII_ADMISSION_MAC_ACTIVE_KEY_ID"
_KEY_ENV_PREFIX = "PII_ADMISSION_MAC_KEY_"
_KEY_ID_RE = re.compile(r"^[A-Z0-9_]{1,64}$")


def validate_mac_key_id(value: object) -> str:
    if type(value) is not str or value == "":
        raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None
    if _KEY_ID_RE.fullmatch(value) is None:
        raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None
    return value


def _decode_key_material(raw: object) -> bytes:
    if type(raw) is not str or raw == "":
        raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None
    for char in raw:
        if char.isspace():
            raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None
    if not re.fullmatch(r"[A-Za-z0-9_-]+=*", raw):
        raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None
    try:
        decoded = base64.urlsafe_b64decode(raw)
    except Exception:
        raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None
    if type(decoded) is not bytes or len(decoded) != MAC_KEY_SIZE_BYTES:
        raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii")
    if raw != canonical:
        raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None
    return decoded


class PiiAdmissionMacKeyProvider(Protocol):
    def active_key_id(self) -> str:
        """Return the active key id for new MAC operations."""

    def get_key(self, key_id: str) -> bytes:
        """Return exactly 32 raw key bytes for the given key id."""

    def get_active_key(self) -> ActivePiiAdmissionMacKey:
        """Return an immutable active key id/material pair."""


class EnvPiiAdmissionMacKeyProvider:
    """Read PII admission MAC keys from environment mappings on demand."""

    __slots__ = ("_environ",)

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def __repr__(self) -> str:
        return "EnvPiiAdmissionMacKeyProvider()"

    def __str__(self) -> str:
        return "EnvPiiAdmissionMacKeyProvider()"

    def active_key_id(self) -> str:
        try:
            raw = self._environ.get(_ACTIVE_KEY_ID_ENV)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None
        if raw is None:
            raise PiiAdmissionMacError("PII_ADMISSION_MAC_KEY_UNAVAILABLE") from None
        try:
            return validate_mac_key_id(raw)
        except PiiAdmissionMacError as exc:
            if exc.code == "PII_ADMISSION_MAC_CONFIG_INVALID":
                raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None
            raise PiiAdmissionMacError("PII_ADMISSION_MAC_KEY_UNAVAILABLE") from None

    def get_key(self, key_id: object) -> bytes:
        try:
            validated = validate_mac_key_id(key_id)
        except PiiAdmissionMacError:
            raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None
        env_name = f"{_KEY_ENV_PREFIX}{validated}"
        try:
            raw = self._environ.get(env_name)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise PiiAdmissionMacError("PII_ADMISSION_MAC_KEY_UNAVAILABLE") from None
        if raw is None:
            raise PiiAdmissionMacError("PII_ADMISSION_MAC_KEY_UNAVAILABLE") from None
        try:
            return _decode_key_material(raw)
        except PiiAdmissionMacError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None

    def get_active_key(self) -> ActivePiiAdmissionMacKey:
        key_id = self.active_key_id()
        key = self.get_key(key_id)
        return ActivePiiAdmissionMacKey(key_id=key_id, key=key)
