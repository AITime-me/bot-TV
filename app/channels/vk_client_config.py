"""VK CLIENT callback configuration (shadow observer). Default-off, fail-closed.

Separate from VK_MASTER_* — secrets/config must never substitute either way.
Callback-only: no access token / send path.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

__all__ = (
    "VkClientCallbackConfig",
    "VkClientConfigError",
)

_SECRET_MAX: Final[int] = 256
_CONFIRMATION_MAX: Final[int] = 128


class VkClientConfigError(ValueError):
    """Fixed message only — never embed secrets."""

    def __init__(self, code: str = "VK_CLIENT_CONFIG_INVALID") -> None:
        super().__init__(code)

    def __repr__(self) -> str:
        return f"VkClientConfigError({self.args[0]!r})"


def _optional_str(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return value


def _require_printable_secret(value: str, *, min_len: int, max_len: int) -> str:
    if type(value) is not str or not value:
        raise VkClientConfigError("VK_CLIENT_CONFIG_INVALID") from None
    if len(value) < min_len or len(value) > max_len:
        raise VkClientConfigError("VK_CLIENT_CONFIG_INVALID") from None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise VkClientConfigError("VK_CLIENT_CONFIG_INVALID") from None
    if any(ch.isspace() for ch in value):
        raise VkClientConfigError("VK_CLIENT_CONFIG_INVALID") from None
    return value


@dataclass(frozen=True, slots=True, repr=False)
class VkClientCallbackConfig:
    """Trusted VK Callback settings for CLIENT shadow ingress. No send token."""

    enabled: bool = False
    group_id: int | None = None
    callback_secret: str | None = None
    confirmation: str | None = None

    def __repr__(self) -> str:
        return (
            "VkClientCallbackConfig("
            f"enabled={self.enabled!r}, "
            f"group_id={self.group_id!r}, "
            "callback_secret=<redacted>, "
            "confirmation=<redacted>)"
        )

    def callback_config_complete(self) -> bool:
        return (
            type(self.group_id) is int
            and self.group_id > 0
            and type(self.callback_secret) is str
            and bool(self.callback_secret)
            and type(self.confirmation) is str
            and bool(self.confirmation)
        )

    def require_callback_config(self) -> None:
        if not self.callback_config_complete():
            raise VkClientConfigError("VK_CLIENT_CONFIG_INVALID") from None

    def require_runtime(self) -> None:
        """Enabled client callback surface must be fully configured."""

        if not self.enabled:
            raise VkClientConfigError("VK_CLIENT_CONFIG_INVALID") from None
        self.require_callback_config()

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None
    ) -> VkClientCallbackConfig:
        source = os.environ if environ is None else environ
        enabled_raw = source.get("VK_CLIENT_CALLBACK_ENABLED", "false")
        if enabled_raw == "false":
            return cls(enabled=False)
        if enabled_raw != "true":
            raise VkClientConfigError("VK_CLIENT_CONFIG_INVALID") from None

        group_raw = _optional_str(source.get("VK_CLIENT_GROUP_ID"))
        secret_raw = _optional_str(source.get("VK_CLIENT_CALLBACK_SECRET"))
        confirmation_raw = _optional_str(source.get("VK_CLIENT_CONFIRMATION"))

        if (
            group_raw is None
            or secret_raw is None
            or confirmation_raw is None
        ):
            raise VkClientConfigError("VK_CLIENT_CONFIG_INVALID") from None

        try:
            group_id = int(group_raw)
        except ValueError:
            raise VkClientConfigError("VK_CLIENT_CONFIG_INVALID") from None
        if group_id <= 0:
            raise VkClientConfigError("VK_CLIENT_CONFIG_INVALID") from None
        secret = _require_printable_secret(
            secret_raw,
            min_len=8,
            max_len=_SECRET_MAX,
        )
        confirmation = _require_printable_secret(
            confirmation_raw,
            min_len=1,
            max_len=_CONFIRMATION_MAX,
        )

        return cls(
            enabled=True,
            group_id=group_id,
            callback_secret=secret,
            confirmation=confirmation,
        )
