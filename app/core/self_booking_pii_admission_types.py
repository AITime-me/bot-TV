"""Closed types for self-booking pre-durability PII admission (03H)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final
from uuid import UUID

_ALLOWED_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "PII_ADMISSION_INPUT_INVALID",
        "PII_ADMISSION_CONFLICT",
        "PII_ADMISSION_EXPIRED",
        "PII_ADMISSION_CONFIG_INVALID",
        "PII_ADMISSION_STORE_FAILED",
    }
)

REQUEST_ID_MAX_LENGTH: Final[int] = 128
_REQUEST_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[!-~]+$")


def require_pii_admission_request_id(value: object) -> str:
    """Canonical opaque request_id for PII admission and CONFIRM binding.

    Printable ASCII only, length 1..REQUEST_ID_MAX_LENGTH. Not PII and not a ref.
    """

    if type(value) is not str or value == "":
        raise ValueError("request_id invalid")
    if len(value) > REQUEST_ID_MAX_LENGTH:
        raise ValueError("request_id invalid")
    if _REQUEST_ID_RE.fullmatch(value) is None:
        raise ValueError("request_id invalid")
    return value


class PiiAdmissionError(RuntimeError):
    """Fail-closed PII admission error. Message is a fixed code only."""

    def __init__(self, code: object) -> None:
        if type(code) is not str or code not in _ALLOWED_ERROR_CODES:
            super().__init__("PII_ADMISSION_CONFIG_INVALID")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "PII_ADMISSION_CONFIG_INVALID"


@dataclass(frozen=True, slots=True, repr=False)
class PiiAdmissionResult:
    """Opaque refs returned to caller. Never embeds plaintext."""

    conversation_id: UUID
    request_id: str
    phone_ref_token: str
    name_ref_token: str
    reused: bool

    def __repr__(self) -> str:
        return (
            "PiiAdmissionResult("
            "conversation_id=<redacted>, "
            "request_id=<redacted>, "
            "phone_ref_token=<redacted>, "
            "name_ref_token=<redacted>, "
            f"reused={self.reused!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()
