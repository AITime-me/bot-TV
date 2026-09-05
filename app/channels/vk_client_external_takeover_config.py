"""Feature gate for VK CLIENT message_reply → external takeover. Default OFF."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.channels.vk_client_types import vk_client_external_conversation_id

__all__ = (
    "VkClientExternalTakeoverMode",
    "VkClientExternalTakeoverConfig",
    "VkClientExternalTakeoverConfigError",
    "load_vk_client_external_takeover_config",
)

_CONV_MAX: Final[int] = 128
_ALLOWLIST_MAX_ITEMS: Final[int] = 64


class VkClientExternalTakeoverMode(StrEnum):
    OFF = "OFF"
    ALLOWLIST = "ALLOWLIST"
    ALL = "ALL"


class VkClientExternalTakeoverConfigError(ValueError):
    def __init__(self, code: str = "VK_CLIENT_EXTERNAL_TAKEOVER_CONFIG_INVALID") -> None:
        super().__init__(code)

    def __repr__(self) -> str:
        return f"VkClientExternalTakeoverConfigError({self.args[0]!r})"


@dataclass(frozen=True, slots=True, repr=False)
class VkClientExternalTakeoverConfig:
    mode: VkClientExternalTakeoverMode = VkClientExternalTakeoverMode.OFF
    allowlist: frozenset[str] = frozenset()
    provenance_key: str | None = None

    def __repr__(self) -> str:
        return (
            "VkClientExternalTakeoverConfig("
            f"mode={self.mode.value!r}, "
            f"allowlist_size={len(self.allowlist)!r}, "
            "provenance_key=<redacted>)"
        )

    def fsm_mutation_allowed(self, *, external_conversation_id: str) -> bool:
        if self.mode is VkClientExternalTakeoverMode.OFF:
            return False
        if type(external_conversation_id) is not str or not external_conversation_id:
            return False
        if self.mode is VkClientExternalTakeoverMode.ALL:
            return True
        if self.mode is VkClientExternalTakeoverMode.ALLOWLIST:
            return external_conversation_id in self.allowlist
        return False


def load_vk_client_external_takeover_config(
    environ: Mapping[str, str] | None = None,
) -> VkClientExternalTakeoverConfig:
    source = os.environ if environ is None else environ
    mode_raw = source.get("VK_CLIENT_EXTERNAL_TAKEOVER_MODE", "OFF")
    if type(mode_raw) is not str or not mode_raw:
        raise VkClientExternalTakeoverConfigError() from None
    mode_key = mode_raw.strip().upper()
    if mode_key == "OFF":
        return VkClientExternalTakeoverConfig(mode=VkClientExternalTakeoverMode.OFF)

    if mode_key == "ALL":
        provenance = _load_provenance_key(source)
        if provenance is None:
            raise VkClientExternalTakeoverConfigError() from None
        return VkClientExternalTakeoverConfig(
            mode=VkClientExternalTakeoverMode.ALL,
            provenance_key=provenance,
        )

    if mode_key != "ALLOWLIST":
        raise VkClientExternalTakeoverConfigError() from None

    allow_raw = source.get("VK_CLIENT_EXTERNAL_TAKEOVER_ALLOWLIST")
    if type(allow_raw) is not str or not allow_raw.strip():
        raise VkClientExternalTakeoverConfigError() from None
    items: set[str] = set()
    for part in allow_raw.split(","):
        item = part.strip()
        if not item:
            continue
        if len(item) > _CONV_MAX or any(ord(ch) < 32 for ch in item):
            raise VkClientExternalTakeoverConfigError() from None
        if not item.startswith("vk-"):
            raise VkClientExternalTakeoverConfigError() from None
        # Validate vk-{group}-{user} shape without heuristics.
        try:
            bits = item.split("-")
            if len(bits) != 3:
                raise ValueError
            group_id = int(bits[1])
            user_id = int(bits[2])
            canonical = vk_client_external_conversation_id(
                group_id=group_id,
                user_id=user_id,
            )
        except ValueError as exc:
            raise VkClientExternalTakeoverConfigError() from exc
        if canonical != item:
            raise VkClientExternalTakeoverConfigError() from None
        items.add(canonical)
    if not items or len(items) > _ALLOWLIST_MAX_ITEMS:
        raise VkClientExternalTakeoverConfigError() from None

    provenance = _load_provenance_key(source)
    if provenance is None:
        raise VkClientExternalTakeoverConfigError() from None
    return VkClientExternalTakeoverConfig(
        mode=VkClientExternalTakeoverMode.ALLOWLIST,
        allowlist=frozenset(items),
        provenance_key=provenance,
    )


def _load_provenance_key(source: Mapping[str, str]) -> str | None:
    raw = source.get("VK_CLIENT_OUTBOUND_PROVENANCE_KEY") or source.get(
        "VK_CLIENT_CALLBACK_SECRET"
    )
    if raw is None or raw == "":
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise VkClientExternalTakeoverConfigError() from None
    if any(ch.isspace() for ch in raw):
        raise VkClientExternalTakeoverConfigError() from None
    if len(raw) < 8 or len(raw) > 512:
        raise VkClientExternalTakeoverConfigError() from None
    return raw
