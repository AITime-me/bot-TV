"""Centralized PII boundary for logs, diagnostics, and future AI context.

Plaintext durable storage is out of scope: PostgreSQL retains canonical message
text for business functions. This module is mandatory on unsafe boundaries only.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import math
import re
import secrets
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

_PROCESS_HMAC_KEY: bytes = secrets.token_bytes(32)

_FINGERPRINT_HEX_LENGTH = 16
_FINGERPRINT_PREFIX = "pii_fp"

_REDACTED_MARKER = "<redacted>"
_TRUNCATED_MARKER = "<truncated>"
_MAX_DEPTH_MARKER = "<max-depth>"
_MAX_NODES_MARKER = "<max-nodes>"
_CYCLE_MARKER = "<cycle>"
_UNSUPPORTED_PREFIX = "<unsupported:"
_REDACTION_ERROR_MARKER = "<redaction-error>"
_UNTRUSTED_MAPPING_MARKER = "<untrusted-mapping>"
_NON_FINITE_NUMBER_MARKER = "<non-finite-number>"
_UNSET_MARKER = "<unset>"

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(
    r"(?:"
    r"\+7(?:[\s\-.()\u00a0]*\d){10}"
    r"|8(?:[\s\-.()\u00a0]*\d){10}"
    r"|\+\d{1,3}(?:[\s\-.()\u00a0]*\d){7,14}"
    r")"
)
_SELF_INTRO_RE = re.compile(
    r"(?i)\b(?:меня зовут|my name is)\s+[\w\u0400-\u04FF\-]+",
)

_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "text",
        "body",
        "body_text",
        "draft_text",
        "message",
        "content",
        "phone",
        "telephone",
        "email",
        "address",
        "birth_date",
        "birthday",
        "first_name",
        "last_name",
        "full_name",
        "name",
        "client_name",
        "username",
        "user_id",
        "external_user_id",
        "external_message_id",
        "external_event_id",
        "external_conversation_id",
        "token",
        "password",
        "secret",
        "authorization",
        "cookie",
        "envelope_json",
        "payload_json",
    }
)

_SENSITIVE_PREFIXES: tuple[str, ...] = ("birth_",)

_DASH_NORMALIZE_RE = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")

_MIN_KNOWN_PII_LEN = 3


class _NodeCounter:
    __slots__ = ("count", "max_nodes")

    def __init__(self, *, max_nodes: int) -> None:
        self.count = 0
        self.max_nodes = max_nodes

    def consume(self) -> bool:
        self.count += 1
        return self.count <= self.max_nodes


class PiiGatewayError(RuntimeError):
    """Fail-closed PII boundary violation. Messages never contain raw values."""


def fingerprint_for_log(value: object, *, purpose: str) -> str:
    """Return a process-local HMAC fingerprint for log correlation."""
    if not isinstance(purpose, str) or not purpose.strip():
        raise PiiGatewayError("FINGERPRINT_PURPOSE_INVALID")
    if not isinstance(value, str):
        raise PiiGatewayError("FINGERPRINT_VALUE_UNSUPPORTED")
    normalized = value.strip()
    if not normalized:
        raise PiiGatewayError("FINGERPRINT_VALUE_INVALID")
    digest = hmac.new(
        _PROCESS_HMAC_KEY,
        f"{purpose}\0{normalized}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:_FINGERPRINT_HEX_LENGTH]
    safe_purpose = re.sub(r"[^a-zA-Z0-9_.-]", "_", purpose)[:64]
    return f"{_FINGERPRINT_PREFIX}:{safe_purpose}:{digest}"


def redact_for_log(
    value: object,
    *,
    allowed_keys: frozenset[str] | set[str] | None = None,
    max_depth: int = 8,
    max_nodes: int = 500,
    max_string_chars: int = 512,
) -> object:
    """Return a JSON-like structure safe for logs and diagnostics."""
    try:
        allowed = (
            frozenset(_normalize_key_name(key) for key in allowed_keys)
            if allowed_keys is not None
            else frozenset()
        )
        seen: set[int] = set()
        counter = _NodeCounter(max_nodes=max_nodes)

        return _redact_value(
            value,
            depth=0,
            max_depth=max_depth,
            max_string_chars=max_string_chars,
            allowed_keys=allowed,
            seen=seen,
            counter=counter,
        )
    except Exception:
        return _REDACTION_ERROR_MARKER


def sanitize_for_ai(
    text: str,
    *,
    known_pii: Sequence[str] = (),
    max_chars: int = 12000,
) -> str:
    """Mask identifying values while preserving conversational shape for AI.

    ``max_chars`` must be a nonnegative ``int`` (``bool`` is rejected).
    ``max_chars=0`` yields only ``<truncated>`` after masking.
    """
    if not isinstance(text, str):
        raise PiiGatewayError("SANITIZE_TEXT_UNSUPPORTED")
    if type(max_chars) is not int or max_chars < 0:
        raise PiiGatewayError("SANITIZE_LIMIT_INVALID") from None
    try:
        working = _normalize_for_pii_detection(text)

        replacements = _normalized_known_pii(known_pii)
        for item in replacements:
            working = _replace_case_insensitive(working, item, _REDACTED_MARKER)

        working = _EMAIL_RE.sub(_REDACTED_MARKER, working)
        working = _PHONE_RE.sub(_REDACTED_MARKER, working)
        working = _SELF_INTRO_RE.sub("[NAME_REDACTED]", working)

        if len(working) > max_chars:
            working = working[:max_chars] + _TRUNCATED_MARKER
        return working
    except PiiGatewayError:
        raise
    except Exception:
        raise PiiGatewayError("SANITIZE_FAILED") from None


def assert_safe_mapping(
    payload: object,
    *,
    allowlist: frozenset[str] | set[str] | list[str] | tuple[str, ...],
    max_depth: int = 8,
    max_nodes: int = 500,
    max_string_chars: int = 512,
) -> None:
    """Validate a mapping contains only allowlisted, non-PII values."""
    seen: set[int] = set()
    nodes = [0]

    def _walk(value: object, *, depth: int) -> None:
        if depth > max_depth:
            raise PiiGatewayError("SAFE_MAPPING_MAX_DEPTH")
        nodes[0] += 1
        if nodes[0] > max_nodes:
            raise PiiGatewayError("SAFE_MAPPING_MAX_NODES")

        if value is None or isinstance(value, (bool, int, float)):
            return
        if isinstance(value, (datetime, date)):
            return
        if isinstance(value, UUID):
            return
        if isinstance(value, enum.Enum):
            _walk(value.value, depth=depth)
            return
        if isinstance(value, str):
            if len(value) > max_string_chars:
                raise PiiGatewayError("SAFE_MAPPING_STRING_TOO_LONG")
            if _contains_pii_string(value):
                raise PiiGatewayError("SAFE_MAPPING_PII_STRING")
            return
        if isinstance(value, dict):
            obj_id = id(value)
            if obj_id in seen:
                raise PiiGatewayError("SAFE_MAPPING_CYCLE")
            seen.add(obj_id)
            for key in dict.__iter__(value):
                if not isinstance(key, str):
                    raise PiiGatewayError("SAFE_MAPPING_KEY_UNSUPPORTED")
                normalized_key = _normalize_key_name(key)
                if _is_sensitive_key(normalized_key):
                    raise PiiGatewayError("SAFE_MAPPING_SENSITIVE_KEY")
                if normalized_key not in normalized_allowlist:
                    raise PiiGatewayError("SAFE_MAPPING_UNKNOWN_KEY")
                _walk(dict.__getitem__(value, key), depth=depth + 1)
            seen.discard(obj_id)
            return
        if isinstance(value, Mapping):
            raise PiiGatewayError("SAFE_MAPPING_UNTRUSTED_MAPPING")
        if isinstance(value, (list, tuple, set)):
            obj_id = id(value)
            if obj_id in seen:
                raise PiiGatewayError("SAFE_MAPPING_CYCLE")
            seen.add(obj_id)
            for item in value:
                _walk(item, depth=depth + 1)
            seen.discard(obj_id)
            return
        raise PiiGatewayError("SAFE_MAPPING_UNSUPPORTED_VALUE")

    try:
        if not isinstance(payload, Mapping):
            raise PiiGatewayError("SAFE_MAPPING_NOT_MAPPING")
        normalized_allowlist = _normalize_allowlist(allowlist)
        _walk(payload, depth=0)
    except PiiGatewayError:
        raise
    except Exception:
        raise PiiGatewayError("SAFE_MAPPING_FAILED") from None


def orm_local_column(obj: object, name: str) -> object | None:
    """Read a SQLAlchemy column from local state without lazy loading."""
    try:
        from sqlalchemy import inspect as sa_inspect

        state = sa_inspect(obj)
        if state is None:
            return None
        local = getattr(state, "dict", None)
        if not isinstance(local, dict) or name not in local:
            return None
        return local[name]
    except Exception:
        return None


def repr_orm_literal(value: object | None) -> str:
    """Format a safe ORM scalar for repr; missing values use ``<unset>``."""
    if value is None:
        return _UNSET_MARKER
    if isinstance(value, (str, int, bool)):
        return repr(value)
    return repr(value)


def repr_orm_fingerprint(value: object | None, *, purpose: str) -> str:
    """Fingerprint an ORM identifier for repr; missing values use ``<unset>``."""
    if value is None:
        return _UNSET_MARKER
    return repr(safe_fingerprint(value, purpose=purpose))


def safe_fingerprint(value: object, *, purpose: str) -> str:
    """Best-effort fingerprint for repr paths; never raises to callers."""
    try:
        if isinstance(value, str):
            text = value
        elif isinstance(value, UUID):
            text = str(value)
        else:
            text = str(value)
        return fingerprint_for_log(text, purpose=purpose)
    except Exception:
        return _REDACTED_MARKER


def _normalize_allowlist(
    allowlist: frozenset[str] | set[str] | list[str] | tuple[str, ...],
) -> frozenset[str]:
    if not isinstance(allowlist, (frozenset, set, list, tuple)):
        raise PiiGatewayError("SAFE_MAPPING_ALLOWLIST_INVALID")
    normalized: set[str] = set()
    for item in allowlist:
        if type(item) is not str:
            raise PiiGatewayError("SAFE_MAPPING_ALLOWLIST_INVALID")
        normalized.add(_normalize_key_name(item))
    return frozenset(normalized)


def _normalize_key_name(key: str) -> str:
    normalized = unicodedata.normalize("NFKC", key)
    normalized = normalized.lower()
    normalized = re.sub(r"[\s\-.]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized)
    return normalized.strip("_")


def _normalize_for_pii_detection(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = "".join(
        ch for ch in normalized if unicodedata.category(ch) != "Cf"
    )
    return _DASH_NORMALIZE_RE.sub("-", normalized)


def _is_sensitive_key(normalized_key: str) -> bool:
    if normalized_key in _SENSITIVE_KEYS:
        return True
    return any(normalized_key.startswith(prefix) for prefix in _SENSITIVE_PREFIXES)


def _safe_output_key_name(
    key: object,
    *,
    index: int,
    allowed_keys: frozenset[str],
    used_names: set[str],
) -> str:
    if not isinstance(key, str):
        candidate = f"<key:{index}>"
    else:
        normalized = _normalize_key_name(key)
        if (
            _is_sensitive_key(normalized)
            or _contains_pii_string(key)
            or _contains_pii_string(normalized)
            or normalized not in allowed_keys
        ):
            candidate = f"<key:{index}>"
        else:
            candidate = key
    while candidate in used_names:
        index += 1
        candidate = f"<key:{index}>"
    used_names.add(candidate)
    return candidate


def _should_redact_mapping_value(key: object, *, allowed_keys: frozenset[str]) -> bool:
    if not isinstance(key, str):
        return True
    normalized = _normalize_key_name(key)
    if _is_sensitive_key(normalized):
        return True
    if _contains_pii_string(key) or _contains_pii_string(normalized):
        return True
    return normalized not in allowed_keys


def _iter_dict_items(mapping: dict[Any, Any]) -> list[tuple[Any, Any]]:
    items: list[tuple[Any, Any]] = []
    for key in dict.__iter__(mapping):
        items.append((key, dict.__getitem__(mapping, key)))
    return items


def _redact_mapping(
    value: Mapping[Any, Any],
    *,
    depth: int,
    max_depth: int,
    max_string_chars: int,
    allowed_keys: frozenset[str],
    seen: set[int],
    counter: _NodeCounter,
) -> object:
    if isinstance(value, dict):
        obj_id = id(value)
        if obj_id in seen:
            return _CYCLE_MARKER
        seen.add(obj_id)
        result: dict[str, object] = {}
        used_names: set[str] = set()
        for index, (key, nested) in enumerate(_iter_dict_items(value)):
            output_key = _safe_output_key_name(
                key,
                index=index,
                allowed_keys=allowed_keys,
                used_names=used_names,
            )
            if _should_redact_mapping_value(key, allowed_keys=allowed_keys):
                result[output_key] = _REDACTED_MARKER
                continue
            result[output_key] = _redact_value(
                nested,
                depth=depth + 1,
                max_depth=max_depth,
                max_string_chars=max_string_chars,
                allowed_keys=allowed_keys,
                seen=seen,
                counter=counter,
            )
        seen.discard(obj_id)
        return result
    return _UNTRUSTED_MAPPING_MARKER


def _redact_value(
    value: object,
    *,
    depth: int,
    max_depth: int,
    max_string_chars: int,
    allowed_keys: frozenset[str],
    seen: set[int],
    counter: _NodeCounter,
) -> object:
    if depth > max_depth:
        return _MAX_DEPTH_MARKER
    if not counter.consume():
        return _MAX_NODES_MARKER

    if value is None:
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return _NON_FINITE_NUMBER_MARKER
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return safe_fingerprint(value, purpose="uuid")
    if isinstance(value, enum.Enum):
        return _redact_value(
            value.value,
            depth=depth,
            max_depth=max_depth,
            max_string_chars=max_string_chars,
            allowed_keys=allowed_keys,
            seen=seen,
            counter=counter,
        )
    if isinstance(value, str):
        return _redact_string(value, max_string_chars=max_string_chars)
    if depth >= max_depth:
        return _MAX_DEPTH_MARKER
    if isinstance(value, Mapping):
        return _redact_mapping(
            value,
            depth=depth,
            max_depth=max_depth,
            max_string_chars=max_string_chars,
            allowed_keys=allowed_keys,
            seen=seen,
            counter=counter,
        )
    if isinstance(value, (list, tuple, set)):
        obj_id = id(value)
        if obj_id in seen:
            return _CYCLE_MARKER
        seen.add(obj_id)
        items = [
            _redact_value(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_string_chars=max_string_chars,
                allowed_keys=allowed_keys,
                seen=seen,
                counter=counter,
            )
            for item in value
        ]
        seen.discard(obj_id)
        return items
    if is_dataclass(value):
        obj_id = id(value)
        if obj_id in seen:
            return _CYCLE_MARKER
        seen.add(obj_id)
        result: dict[str, object] = {}
        used_names: set[str] = set()
        for index, field in enumerate(fields(value)):
            normalized = _normalize_key_name(field.name)
            output_key = _safe_output_key_name(
                field.name,
                index=index,
                allowed_keys=allowed_keys,
                used_names=used_names,
            )
            if _is_sensitive_key(normalized) or normalized not in allowed_keys:
                result[output_key] = _REDACTED_MARKER
                continue
            result[output_key] = _redact_value(
                getattr(value, field.name),
                depth=depth + 1,
                max_depth=max_depth,
                max_string_chars=max_string_chars,
                allowed_keys=allowed_keys,
                seen=seen,
                counter=counter,
            )
        seen.discard(obj_id)
        return result
    if isinstance(value, BaseModel):
        obj_id = id(value)
        if obj_id in seen:
            return _CYCLE_MARKER
        seen.add(obj_id)
        result = {}
        used_names = set()
        for index, name in enumerate(type(value).model_fields):
            normalized = _normalize_key_name(name)
            output_key = _safe_output_key_name(
                name,
                index=index,
                allowed_keys=allowed_keys,
                used_names=used_names,
            )
            if _is_sensitive_key(normalized) or normalized not in allowed_keys:
                result[output_key] = _REDACTED_MARKER
                continue
            result[output_key] = _redact_value(
                getattr(value, name),
                depth=depth + 1,
                max_depth=max_depth,
                max_string_chars=max_string_chars,
                allowed_keys=allowed_keys,
                seen=seen,
                counter=counter,
            )
        seen.discard(obj_id)
        return result

    type_name = type(value).__name__
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", type_name)[:64] or "object"
    return f"{_UNSUPPORTED_PREFIX}{safe_name}>"


def _redact_string(value: str, *, max_string_chars: int) -> str:
    if len(value) > max_string_chars:
        return _TRUNCATED_MARKER
    if _contains_pii_string(value):
        return _REDACTED_MARKER
    return value


def _contains_pii_string(value: str) -> bool:
    normalized = _normalize_for_pii_detection(value).strip()
    if _UUID_RE.fullmatch(normalized):
        return False
    if _EMAIL_RE.search(normalized):
        return True
    if _PHONE_RE.search(normalized):
        return True
    return False


def _normalized_known_pii(known_pii: Sequence[str]) -> tuple[str, ...]:
    items = [
        item.strip()
        for item in known_pii
        if isinstance(item, str) and len(item.strip()) >= _MIN_KNOWN_PII_LEN
    ]
    return tuple(sorted(set(items), key=len, reverse=True))


def _replace_case_insensitive(text: str, needle: str, replacement: str) -> str:
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    return pattern.sub(replacement, text)
