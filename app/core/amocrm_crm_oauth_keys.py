"""Lazy environment key provider for amoCRM CRM OAuth AEAD keys.

Same pattern as ephemeral PII / attachment spool: 32-byte base64url keys,
no import-time requirement, no key material in repr.
"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from app.core.amocrm_crm_oauth_types import (
    KEY_SIZE_BYTES,
    AmoCrmCrmOauthError,
    _require_key_id_token,
)

_ACTIVE_KEY_ID_ENV = "AMOCRM_CRM_OAUTH_ACTIVE_KEY_ID"
_KEY_ENV_PREFIX = "AMOCRM_CRM_OAUTH_KEY_"
_KEY_ID_RE = re.compile(r"^[A-Z0-9_]{1,64}$")


@dataclass(frozen=True, slots=True, repr=False)
class ActiveAmoCrmOauthKey:
    key_id: str
    key: bytes

    def __post_init__(self) -> None:
        validated_id = validate_key_id(self.key_id)
        if type(self.key) is not bytes or len(self.key) != KEY_SIZE_BYTES:
            raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
        object.__setattr__(self, "key_id", validated_id)

    def __repr__(self) -> str:
        return "ActiveAmoCrmOauthKey(key_id=<redacted>, key=<redacted>)"


class AmoCrmOauthKeyProvider(Protocol):
    def active_key_id(self) -> str: ...

    def get_key(self, key_id: str) -> bytes: ...

    def get_active_key(self) -> ActiveAmoCrmOauthKey: ...


def validate_key_id(value: object) -> str:
    try:
        token = _require_key_id_token(value)
    except AmoCrmCrmOauthError:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
    if _KEY_ID_RE.fullmatch(token) is None:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
    return token


def _decode_key_material(raw: object) -> bytes:
    if type(raw) is not str or raw == "":
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
    if any(ch.isspace() for ch in raw):
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
    if not re.fullmatch(r"[A-Za-z0-9_-]+=*", raw):
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
    try:
        decoded = base64.urlsafe_b64decode(raw)
    except Exception:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
    if type(decoded) is not bytes or len(decoded) != KEY_SIZE_BYTES:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii")
    if raw != canonical:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_CONFIG_INVALID") from None
    return decoded


class EnvAmoCrmOauthKeyProvider:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def active_key_id(self) -> str:
        raw = self._environ.get(_ACTIVE_KEY_ID_ENV)
        if raw is None or raw == "":
            raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_KEY_UNAVAILABLE") from None
        return validate_key_id(raw)

    def get_key(self, key_id: str) -> bytes:
        validated = validate_key_id(key_id)
        raw = self._environ.get(f"{_KEY_ENV_PREFIX}{validated}")
        if raw is None:
            raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_KEY_UNAVAILABLE") from None
        return _decode_key_material(raw)

    def get_active_key(self) -> ActiveAmoCrmOauthKey:
        key_id = self.active_key_id()
        return ActiveAmoCrmOauthKey(key_id=key_id, key=self.get_key(key_id))
