"""Types for durable PII admission content MAC (SELF-BOOKING-COMMAND-03H).

Persistent keyed HMAC only — not process-local log fingerprints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

_ALLOWED_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "PII_ADMISSION_MAC_CONFIG_INVALID",
        "PII_ADMISSION_MAC_KEY_UNAVAILABLE",
        "PII_ADMISSION_MAC_VALUE_INVALID",
    }
)

CONTENT_MAC_BYTES: Final[int] = 32
MAC_KEY_SIZE_BYTES: Final[int] = 32
# Domain-separated message prefix for booking phone+name binding.
BOOKING_PII_ADMISSION_MAC_DOMAIN: Final[str] = "BOOKING_PII_ADMISSION_V1"


class PiiAdmissionMacError(RuntimeError):
    """Fail-closed MAC boundary error. Message is a fixed code only."""

    def __init__(self, code: object) -> None:
        if type(code) is not str or code not in _ALLOWED_ERROR_CODES:
            super().__init__("PII_ADMISSION_MAC_CONFIG_INVALID")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "PII_ADMISSION_MAC_CONFIG_INVALID"


@dataclass(frozen=True, slots=True, repr=False)
class ActivePiiAdmissionMacKey:
    """Immutable active MAC key snapshot."""

    key_id: str
    key: bytes

    def __post_init__(self) -> None:
        if type(self.key_id) is not str or not self.key_id:
            raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None
        if type(self.key) is not bytes or len(self.key) != MAC_KEY_SIZE_BYTES:
            raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None

    def __repr__(self) -> str:
        return "ActivePiiAdmissionMacKey(key_id=<redacted>, key=<redacted>)"

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


@dataclass(frozen=True, slots=True, repr=False)
class PiiAdmissionContentMac:
    """Full HMAC-SHA256 digest + key id used to compute it."""

    digest: bytes
    key_id: str

    def __post_init__(self) -> None:
        if type(self.digest) is not bytes or len(self.digest) != CONTENT_MAC_BYTES:
            raise PiiAdmissionMacError("PII_ADMISSION_MAC_VALUE_INVALID") from None
        if type(self.key_id) is not str or not self.key_id:
            raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None

    def __repr__(self) -> str:
        return (
            "PiiAdmissionContentMac("
            "digest=<redacted>, "
            "key_id=<redacted>)"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()
