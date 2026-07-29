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


def _parse_int_range(name: str, value: str, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


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
    handoff_pause_seconds: int = 15 * 60
    handoff_expiry_poll_seconds: int = 1
    worker_poll_seconds: int = 1
    worker_batch_size: int = 100
    worker_tick_timeout_seconds: int = 20
    worker_heartbeat_interval_seconds: int = 10
    worker_heartbeat_stale_seconds: int = 45
    worker_max_consecutive_failures: int = 3

    def __repr__(self) -> str:
        if self.database_url is None:
            rendered = "None"
        else:
            rendered = repr(redact_database_url(self.database_url))
        return (
            f"Settings(bot_mode={self.bot_mode!r}, "
            f"emergency_lock={self.emergency_lock!r}, "
            f"database_url={rendered}, "
            f"handoff_pause_seconds={self.handoff_pause_seconds!r}, "
            f"worker_poll_seconds={self.worker_poll_seconds!r}, "
            f"worker_batch_size={self.worker_batch_size!r}, "
            f"worker_tick_timeout_seconds={self.worker_tick_timeout_seconds!r}, "
            "worker_heartbeat_interval_seconds="
            f"{self.worker_heartbeat_interval_seconds!r}, "
            "worker_heartbeat_stale_seconds="
            f"{self.worker_heartbeat_stale_seconds!r}, "
            "worker_max_consecutive_failures="
            f"{self.worker_max_consecutive_failures!r}, "
            "handoff_expiry_poll_seconds="
            f"{self.handoff_expiry_poll_seconds!r})"
        )

    def __post_init__(self) -> None:
        if not isinstance(self.bot_mode, BotMode):
            raise ValueError("bot_mode must be a BotMode")
        if type(self.emergency_lock) is not bool:
            raise ValueError("emergency_lock must be a boolean")
        if self.database_url is not None and type(self.database_url) is not str:
            raise ValueError("database_url must be a string or None")
        if type(self.handoff_pause_seconds) is not int:
            raise ValueError("handoff_pause_seconds must be an integer")
        if not 10 * 60 <= self.handoff_pause_seconds <= 15 * 60:
            raise ValueError("handoff_pause_seconds must be between 600 and 900")
        if type(self.handoff_expiry_poll_seconds) is not int:
            raise ValueError("handoff_expiry_poll_seconds must be an integer")
        if not 1 <= self.handoff_expiry_poll_seconds <= 60:
            raise ValueError(
                "handoff_expiry_poll_seconds must be between 1 and 60"
            )
        if type(self.worker_poll_seconds) is not int:
            raise ValueError("worker_poll_seconds must be an integer")
        if not 1 <= self.worker_poll_seconds <= 60:
            raise ValueError("worker_poll_seconds must be between 1 and 60")
        if type(self.worker_batch_size) is not int:
            raise ValueError("worker_batch_size must be an integer")
        if not 1 <= self.worker_batch_size <= 1000:
            raise ValueError("worker_batch_size must be between 1 and 1000")
        if type(self.worker_tick_timeout_seconds) is not int:
            raise ValueError("worker_tick_timeout_seconds must be an integer")
        if not 5 <= self.worker_tick_timeout_seconds <= 300:
            raise ValueError(
                "worker_tick_timeout_seconds must be between 5 and 300"
            )
        if type(self.worker_heartbeat_interval_seconds) is not int:
            raise ValueError(
                "worker_heartbeat_interval_seconds must be an integer"
            )
        if not 1 <= self.worker_heartbeat_interval_seconds <= 60:
            raise ValueError(
                "worker_heartbeat_interval_seconds must be between 1 and 60"
            )
        if type(self.worker_heartbeat_stale_seconds) is not int:
            raise ValueError("worker_heartbeat_stale_seconds must be an integer")
        if not 10 <= self.worker_heartbeat_stale_seconds <= 600:
            raise ValueError(
                "worker_heartbeat_stale_seconds must be between 10 and 600"
            )
        if type(self.worker_max_consecutive_failures) is not int:
            raise ValueError(
                "worker_max_consecutive_failures must be an integer"
            )
        if not 1 <= self.worker_max_consecutive_failures <= 20:
            raise ValueError(
                "worker_max_consecutive_failures must be between 1 and 20"
            )

    def validate_worker_runtime(self) -> None:
        """Validate cross-field constraints used only by the worker process."""
        if self.database_url is None:
            raise ValueError("DATABASE_URL is required for the worker runtime")
        longest_poll = max(
            self.worker_poll_seconds,
            self.handoff_expiry_poll_seconds,
            self.worker_heartbeat_interval_seconds,
        )
        minimum_stale = max(
            self.worker_tick_timeout_seconds + 5,
            (2 * longest_poll) + 5,
        )
        if self.worker_heartbeat_stale_seconds < minimum_stale:
            raise ValueError(
                "WORKER_HEARTBEAT_STALE_SECONDS is too small for configured "
                "poll/heartbeat/tick timeout values"
            )

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
            handoff_pause_seconds=_parse_int_range(
                "HANDOFF_PAUSE_SECONDS",
                source.get("HANDOFF_PAUSE_SECONDS", "900"),
                minimum=600,
                maximum=900,
            ),
            handoff_expiry_poll_seconds=_parse_int_range(
                "HANDOFF_EXPIRY_POLL_SECONDS",
                source.get("HANDOFF_EXPIRY_POLL_SECONDS", "1"),
                minimum=1,
                maximum=60,
            ),
            worker_poll_seconds=_parse_int_range(
                "WORKER_POLL_SECONDS",
                source.get("WORKER_POLL_SECONDS", "1"),
                minimum=1,
                maximum=60,
            ),
            worker_batch_size=_parse_int_range(
                "WORKER_BATCH_SIZE",
                source.get("WORKER_BATCH_SIZE", "100"),
                minimum=1,
                maximum=1000,
            ),
            worker_tick_timeout_seconds=_parse_int_range(
                "WORKER_TICK_TIMEOUT_SECONDS",
                source.get("WORKER_TICK_TIMEOUT_SECONDS", "20"),
                minimum=5,
                maximum=300,
            ),
            worker_heartbeat_interval_seconds=_parse_int_range(
                "WORKER_HEARTBEAT_INTERVAL_SECONDS",
                source.get("WORKER_HEARTBEAT_INTERVAL_SECONDS", "10"),
                minimum=1,
                maximum=60,
            ),
            worker_heartbeat_stale_seconds=_parse_int_range(
                "WORKER_HEARTBEAT_STALE_SECONDS",
                source.get("WORKER_HEARTBEAT_STALE_SECONDS", "45"),
                minimum=10,
                maximum=600,
            ),
            worker_max_consecutive_failures=_parse_int_range(
                "WORKER_MAX_CONSECUTIVE_FAILURES",
                source.get("WORKER_MAX_CONSECUTIVE_FAILURES", "3"),
                minimum=1,
                maximum=20,
            ),
        )
