"""VK master adapter configuration (CURSOR-29). Default-off, fail-closed."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from app.config import BotMode, Settings

__all__ = (
    "VkMasterAdapterConfig",
    "VkMasterConfigError",
    "connection_scope_for_group",
    "vk_master_business_allowed",
)

_DEFAULT_API_VERSION: Final[str] = "5.199"
_DEFAULT_API_BASE: Final[str] = "https://api.vk.com"
_SECRET_MAX: Final[int] = 256
_CONFIRMATION_MAX: Final[int] = 128
_TOKEN_MIN: Final[int] = 16
_TOKEN_MAX: Final[int] = 512


class VkMasterConfigError(ValueError):
    """Fixed message only — never embed secrets."""

    def __init__(self, code: str = "VK_MASTER_CONFIG_INVALID") -> None:
        super().__init__(code)

    def __repr__(self) -> str:
        return f"VkMasterConfigError({self.args[0]!r})"


def connection_scope_for_group(group_id: int) -> str:
    if type(group_id) is not int or isinstance(group_id, bool) or group_id <= 0:
        raise VkMasterConfigError("VK_MASTER_CONFIG_INVALID") from None
    return f"vk-group-{group_id}"


def _optional_str(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return value


def _require_printable_secret(name: str, value: str, *, min_len: int, max_len: int) -> str:
    if type(value) is not str or not value:
        raise VkMasterConfigError("VK_MASTER_CONFIG_INVALID") from None
    if len(value) < min_len or len(value) > max_len:
        raise VkMasterConfigError("VK_MASTER_CONFIG_INVALID") from None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise VkMasterConfigError("VK_MASTER_CONFIG_INVALID") from None
    if any(ch.isspace() for ch in value):
        raise VkMasterConfigError("VK_MASTER_CONFIG_INVALID") from None
    return value


@dataclass(frozen=True, slots=True, repr=False)
class VkMasterAdapterConfig:
    """Trusted VK Callback + send settings. Secrets never appear in repr."""

    enabled: bool = False
    group_id: int | None = None
    callback_secret: str | None = None
    confirmation: str | None = None
    access_token: str | None = None
    api_version: str = _DEFAULT_API_VERSION
    api_base_url: str = _DEFAULT_API_BASE

    def __repr__(self) -> str:
        return (
            "VkMasterAdapterConfig("
            f"enabled={self.enabled!r}, "
            f"group_id={self.group_id!r}, "
            "callback_secret=<redacted>, "
            "confirmation=<redacted>, "
            "access_token=<redacted>, "
            f"api_version={self.api_version!r}, "
            f"api_base_url={self.api_base_url!r})"
        )

    @property
    def connection_scope(self) -> str:
        if self.group_id is None:
            raise VkMasterConfigError("VK_MASTER_CONFIG_INVALID") from None
        return connection_scope_for_group(self.group_id)

    def callback_config_complete(self) -> bool:
        return (
            type(self.group_id) is int
            and self.group_id > 0
            and type(self.callback_secret) is str
            and bool(self.callback_secret)
            and type(self.confirmation) is str
            and bool(self.confirmation)
        )

    def runtime_config_complete(self) -> bool:
        return (
            self.callback_config_complete()
            and type(self.access_token) is str
            and bool(self.access_token)
        )

    def require_callback_config(self) -> None:
        if not self.callback_config_complete():
            raise VkMasterConfigError("VK_MASTER_CONFIG_INVALID") from None

    def require_runtime_config(self) -> None:
        if not self.runtime_config_complete():
            raise VkMasterConfigError("VK_MASTER_CONFIG_INVALID") from None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> VkMasterAdapterConfig:
        source = os.environ if environ is None else environ
        enabled_raw = source.get("VK_MASTER_ADAPTER_ENABLED", "false")
        if enabled_raw == "true":
            enabled = True
        elif enabled_raw == "false":
            enabled = False
        else:
            raise VkMasterConfigError("VK_MASTER_CONFIG_INVALID") from None

        group_raw = _optional_str(source.get("VK_MASTER_GROUP_ID"))
        secret_raw = _optional_str(source.get("VK_MASTER_CALLBACK_SECRET"))
        confirmation_raw = _optional_str(source.get("VK_MASTER_CONFIRMATION"))
        token_raw = _optional_str(source.get("VK_MASTER_ACCESS_TOKEN"))
        api_version = _optional_str(source.get("VK_MASTER_API_VERSION")) or _DEFAULT_API_VERSION
        api_base = _optional_str(source.get("VK_MASTER_API_BASE_URL")) or _DEFAULT_API_BASE

        present = [
            group_raw is not None,
            secret_raw is not None,
            confirmation_raw is not None,
        ]
        if any(present) and not all(present):
            raise VkMasterConfigError("VK_MASTER_CONFIG_INVALID") from None

        group_id: int | None = None
        secret: str | None = None
        confirmation: str | None = None
        token: str | None = None

        if all(present):
            assert group_raw is not None and secret_raw is not None
            assert confirmation_raw is not None
            try:
                group_id = int(group_raw)
            except ValueError:
                raise VkMasterConfigError("VK_MASTER_CONFIG_INVALID") from None
            if group_id <= 0:
                raise VkMasterConfigError("VK_MASTER_CONFIG_INVALID") from None
            secret = _require_printable_secret(
                "VK_MASTER_CALLBACK_SECRET",
                secret_raw,
                min_len=8,
                max_len=_SECRET_MAX,
            )
            confirmation = _require_printable_secret(
                "VK_MASTER_CONFIRMATION",
                confirmation_raw,
                min_len=1,
                max_len=_CONFIRMATION_MAX,
            )

        if token_raw is not None:
            token = _require_printable_secret(
                "VK_MASTER_ACCESS_TOKEN",
                token_raw,
                min_len=_TOKEN_MIN,
                max_len=_TOKEN_MAX,
            )

        if enabled and (
            group_id is None
            or secret is None
            or confirmation is None
            or token is None
        ):
            raise VkMasterConfigError("VK_MASTER_CONFIG_INVALID") from None

        if type(api_version) is not str or not api_version or len(api_version) > 16:
            raise VkMasterConfigError("VK_MASTER_CONFIG_INVALID") from None
        if type(api_base) is not str or not api_base.startswith("https://"):
            raise VkMasterConfigError("VK_MASTER_CONFIG_INVALID") from None

        return cls(
            enabled=enabled,
            group_id=group_id,
            callback_secret=secret,
            confirmation=confirmation,
            access_token=token,
            api_version=api_version,
            api_base_url=api_base.rstrip("/"),
        )


def vk_master_business_allowed(
    settings: Settings,
    config: VkMasterAdapterConfig,
) -> bool:
    """Gates C28 execution and VK master reply send. Does not weaken outbound policy."""

    if type(settings) is not Settings:
        return False
    if type(config) is not VkMasterAdapterConfig:
        return False
    if not config.enabled:
        return False
    if not config.runtime_config_complete():
        return False
    if settings.emergency_lock:
        return False
    if settings.bot_mode is BotMode.OFF:
        return False
    return True
