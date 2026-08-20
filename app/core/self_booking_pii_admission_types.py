"""Closed types for self-booking pre-durability PII admission (03H)."""

from __future__ import annotations

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
