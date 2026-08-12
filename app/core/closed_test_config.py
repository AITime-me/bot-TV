"""Closed-test exposure gate (BOT-CLOSED-TEST-01A).

Separate from BotMode / mode_contract. Default-off. Does not authorize live
booking reads, booking writes, or public outbound.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

__all__ = (
    "ClosedTestConfig",
    "ClosedTestConfigError",
    "CLOSED_TEST_TOKEN_HEADER",
)

CLOSED_TEST_TOKEN_HEADER: Final[str] = "X-Bot-Closed-Test-Token"
_TOKEN_MIN: Final[int] = 32
_TOKEN_MAX: Final[int] = 256


class ClosedTestConfigError(ValueError):
    """Fixed message only — never embed the closed-test token."""

    def __init__(self, code: str = "CLOSED_TEST_CONFIG_INVALID") -> None:
        super().__init__(code)

    def __repr__(self) -> str:
        return f"ClosedTestConfigError({self.args[0]!r})"


def _require_strong_token(value: str) -> str:
    if type(value) is not str or not value:
        raise ClosedTestConfigError("CLOSED_TEST_TOKEN_INVALID") from None
    if len(value) < _TOKEN_MIN or len(value) > _TOKEN_MAX:
        raise ClosedTestConfigError("CLOSED_TEST_TOKEN_INVALID") from None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ClosedTestConfigError("CLOSED_TEST_TOKEN_INVALID") from None
    if any(ch.isspace() for ch in value):
        raise ClosedTestConfigError("CLOSED_TEST_TOKEN_INVALID") from None
    return value


@dataclass(frozen=True, slots=True, repr=False)
class ClosedTestConfig:
    """Trusted closed-test exposure settings. Token never appears in repr."""

    enabled: bool = False
    token: str | None = None

    def __repr__(self) -> str:
        return (
            "ClosedTestConfig("
            f"enabled={self.enabled!r}, "
            "token=<redacted>)"
        )

    def require_runtime(self) -> None:
        if not self.enabled:
            raise ClosedTestConfigError("CLOSED_TEST_DISABLED") from None
        if self.token is None:
            raise ClosedTestConfigError("CLOSED_TEST_TOKEN_INVALID") from None

    def verify_token(self, provided: object) -> bool:
        """Constant-time compare. Never logs the token."""

        if not self.enabled or self.token is None:
            return False
        if type(provided) is not str or not provided:
            return False
        try:
            return secrets.compare_digest(provided, self.token)
        except (TypeError, ValueError):
            return False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ClosedTestConfig:
        source = os.environ if environ is None else environ
        enabled_raw = source.get("BOT_CLOSED_TEST_ENABLED", "false")
        if enabled_raw == "false":
            return cls(enabled=False, token=None)
        if enabled_raw != "true":
            raise ClosedTestConfigError("CLOSED_TEST_CONFIG_INVALID") from None

        token_raw = source.get("BOT_CLOSED_TEST_TOKEN")
        if token_raw is None or token_raw == "":
            raise ClosedTestConfigError("CLOSED_TEST_TOKEN_REQUIRED") from None
        token = _require_strong_token(token_raw)
        return cls(enabled=True, token=token)
