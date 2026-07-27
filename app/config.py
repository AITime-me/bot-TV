from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

# Mode values stay local to bot-TV. Do not map to online-zapis-tv control-plane
# enums (TEST/AUTO) until CONTRACT-MODE-01 — see docs/adr/001-mode-contract-deferred.md.


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


def _parse_optional_database_url(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if "://" not in value:
        raise ValueError("DATABASE_URL must be a SQLAlchemy URL")
    scheme = value.split("://", 1)[0]
    if scheme not in {"postgresql+asyncpg", "postgresql"}:
        raise ValueError(
            "DATABASE_URL must use postgresql+asyncpg (or postgresql, rewritten)"
        )
    return value


def normalize_async_database_url(url: str) -> str:
    """Return an async SQLAlchemy URL without logging credentials."""
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    if url.startswith("postgresql+asyncpg://"):
        return url
    raise ValueError("DATABASE_URL must use postgresql+asyncpg")


def redact_database_url(url: str) -> str:
    """Render a database URL without its password.

    Host, port and database name are considered safe to display; password and
    query string are never rendered. Used by Settings.__repr__ so credentials
    cannot reach tracebacks, logs or assertion diffs.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname or ""
        username = parts.username
        has_password = parts.password is not None
    except ValueError:
        return "<unparsable-database-url>"
    if not parts.scheme or not parts.netloc:
        return "<redacted-database-url>"
    location = f"{host}:{port}" if port else host
    if username and has_password:
        userinfo = f"{username}:***@"
    elif username:
        userinfo = f"{username}@"
    elif has_password:
        userinfo = ":***@"
    else:
        userinfo = ""
    database = parts.path.lstrip("/")
    query = "?<redacted>" if parts.query else ""
    return f"{parts.scheme}://{userinfo}{location}/{database}{query}"


@dataclass(frozen=True, repr=False)
class Settings:
    bot_mode: BotMode = BotMode.OFF
    emergency_lock: bool = True
    database_url: str | None = None

    def __repr__(self) -> str:
        if self.database_url is None:
            rendered = "None"
        else:
            rendered = repr(redact_database_url(self.database_url))
        return (
            f"Settings(bot_mode={self.bot_mode!r}, "
            f"emergency_lock={self.emergency_lock!r}, "
            f"database_url={rendered})"
        )

    def __post_init__(self) -> None:
        if not isinstance(self.bot_mode, BotMode):
            raise ValueError("bot_mode must be a BotMode")
        if type(self.emergency_lock) is not bool:
            raise ValueError("emergency_lock must be a boolean")
        if self.database_url is not None and type(self.database_url) is not str:
            raise ValueError("database_url must be a string or None")

    @property
    def async_database_url(self) -> str:
        if self.database_url is None:
            raise ValueError("DATABASE_URL is not configured")
        return normalize_async_database_url(self.database_url)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if environ is None else environ
        return cls(
            bot_mode=_parse_bot_mode(source.get("BOT_MODE", BotMode.OFF.value)),
            emergency_lock=_parse_bool(
                "EMERGENCY_LOCK",
                source.get("EMERGENCY_LOCK", "true"),
            ),
            database_url=_parse_optional_database_url(
                source.get("DATABASE_URL"),
            ),
        )
