from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum


class BotMode(str, Enum):
    OFF = "OFF"
    HINTS = "HINTS"
    DRAFT = "DRAFT"
    AUTO_READ = "AUTO_READ"
    AUTO_WRITE = "AUTO_WRITE"


def _parse_bot_mode(value: str) -> BotMode:
    try:
        return BotMode(value)
    except ValueError as error:
        allowed = ", ".join(mode.value for mode in BotMode)
        raise ValueError(f"BOT_MODE must be one of: {allowed}") from error


def _parse_bool(name: str, value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True)
class Settings:
    bot_mode: BotMode = BotMode.OFF
    emergency_lock: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.bot_mode, BotMode):
            raise ValueError("bot_mode must be a BotMode")
        if type(self.emergency_lock) is not bool:
            raise ValueError("emergency_lock must be a boolean")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if environ is None else environ
        return cls(
            bot_mode=_parse_bot_mode(source.get("BOT_MODE", BotMode.OFF.value)),
            emergency_lock=_parse_bool(
                "EMERGENCY_LOCK",
                source.get("EMERGENCY_LOCK", "true"),
            ),
        )
