from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from app.core.booking_eligibility_http import (
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    BookingEligibilityHttpConfig,
    BookingEligibilityHttpError,
)

# Mode values stay local to bot-TV. Dual-enum mapping to the online-zapis-tv
# control plane is OWNER-approved in app/core/mode_contract.py (CONTRACT-MODE-01).
# Do not invent silent aliases (TEST is not BotMode; AUTO is not AUTO_WRITE).


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


def _parse_float_range(name: str, value: str, *, minimum: float, maximum: float) -> float:
    # Reject boolean-looking strings before float() can mis-parse.
    if value in {"true", "false", "True", "False"}:
        raise ValueError(f"{name} must be a number") from None
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        raise ValueError(f"{name} must be a finite number") from None
    if not minimum < parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}") from None
    return parsed


def _parse_optional_eligibility_secret_pair(
    *,
    base_url: str | None,
    bearer_token: str | None,
) -> tuple[str | None, str | None]:
    """Return (url, token) or (None, None). Partial presence fails closed."""

    url_present = base_url is not None and base_url != ""
    token_present = bearer_token is not None and bearer_token != ""
    if not url_present and not token_present:
        return None, None
    if url_present != token_present:
        raise ValueError("BOOKING_ELIGIBILITY configuration is incomplete") from None
    assert base_url is not None and bearer_token is not None
    return base_url, bearer_token


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
    attachment_maintenance_enabled: bool = False
    attachment_maintenance_interval_seconds: int = 60
    attachment_maintenance_initial_delay_seconds: int = 0
    attachment_reconcile_batch_limit: int = 100
    attachment_purge_batch_limit: int = 100
    booking_eligibility_base_url: str | None = None
    booking_eligibility_bearer_token: str | None = None
    booking_eligibility_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    booking_eligibility_max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __repr__(self) -> str:
        if self.database_url is None:
            rendered = "None"
        else:
            rendered = repr(redact_database_url(self.database_url))
        eligibility_url_repr = (
            "None"
            if self.booking_eligibility_base_url is None
            else "<redacted>"
        )
        token_repr = (
            "None"
            if self.booking_eligibility_bearer_token is None
            else "<redacted>"
        )
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
            f"{self.handoff_expiry_poll_seconds!r}, "
            "attachment_maintenance_enabled="
            f"{self.attachment_maintenance_enabled!r}, "
            "attachment_maintenance_interval_seconds="
            f"{self.attachment_maintenance_interval_seconds!r}, "
            "attachment_maintenance_initial_delay_seconds="
            f"{self.attachment_maintenance_initial_delay_seconds!r}, "
            "attachment_reconcile_batch_limit="
            f"{self.attachment_reconcile_batch_limit!r}, "
            "attachment_purge_batch_limit="
            f"{self.attachment_purge_batch_limit!r}, "
            f"booking_eligibility_base_url={eligibility_url_repr}, "
            f"booking_eligibility_bearer_token={token_repr}, "
            "booking_eligibility_timeout_seconds="
            f"{self.booking_eligibility_timeout_seconds!r}, "
            "booking_eligibility_max_response_bytes="
            f"{self.booking_eligibility_max_response_bytes!r})"
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
        if type(self.attachment_maintenance_enabled) is not bool:
            raise ValueError("attachment_maintenance_enabled must be a boolean")
        if type(self.attachment_maintenance_interval_seconds) is not int:
            raise ValueError(
                "attachment_maintenance_interval_seconds must be an integer"
            )
        if not 1 <= self.attachment_maintenance_interval_seconds <= 86400:
            raise ValueError(
                "attachment_maintenance_interval_seconds must be between "
                "1 and 86400"
            )
        if type(self.attachment_maintenance_initial_delay_seconds) is not int:
            raise ValueError(
                "attachment_maintenance_initial_delay_seconds must be an integer"
            )
        if not 0 <= self.attachment_maintenance_initial_delay_seconds <= 86400:
            raise ValueError(
                "attachment_maintenance_initial_delay_seconds must be between "
                "0 and 86400"
            )
        if type(self.attachment_reconcile_batch_limit) is not int:
            raise ValueError(
                "attachment_reconcile_batch_limit must be an integer"
            )
        if not 1 <= self.attachment_reconcile_batch_limit <= 1000:
            raise ValueError(
                "attachment_reconcile_batch_limit must be between 1 and 1000"
            )
        if type(self.attachment_purge_batch_limit) is not int:
            raise ValueError("attachment_purge_batch_limit must be an integer")
        if not 1 <= self.attachment_purge_batch_limit <= 1000:
            raise ValueError(
                "attachment_purge_batch_limit must be between 1 and 1000"
            )
        self._validate_booking_eligibility_fields()

    def _validate_booking_eligibility_fields(self) -> None:
        base_url = self.booking_eligibility_base_url
        bearer_token = self.booking_eligibility_bearer_token
        if base_url is not None and type(base_url) is not str:
            raise ValueError("BOOKING_ELIGIBILITY configuration is invalid") from None
        if bearer_token is not None and type(bearer_token) is not str:
            raise ValueError("BOOKING_ELIGIBILITY configuration is invalid") from None
        if base_url == "":
            object.__setattr__(self, "booking_eligibility_base_url", None)
            base_url = None
        if bearer_token == "":
            object.__setattr__(self, "booking_eligibility_bearer_token", None)
            bearer_token = None

        timeout = self.booking_eligibility_timeout_seconds
        max_bytes = self.booking_eligibility_max_response_bytes
        if type(timeout) is bool or (
            type(timeout) is not float and type(timeout) is not int
        ):
            raise ValueError(
                "booking_eligibility_timeout_seconds must be a number"
            ) from None
        if type(max_bytes) is not int or type(max_bytes) is bool:
            raise ValueError(
                "booking_eligibility_max_response_bytes must be an integer"
            ) from None

        if base_url is None and bearer_token is None:
            timeout_value = float(timeout)
            if (
                timeout_value != timeout_value
                or timeout_value <= 0.0
                or timeout_value > 120.0
            ):
                raise ValueError(
                    "booking_eligibility_timeout_seconds must be between "
                    "0 and 120"
                ) from None
            if max_bytes <= 0 or max_bytes > 1_000_000:
                raise ValueError(
                    "booking_eligibility_max_response_bytes must be between "
                    "1 and 1000000"
                ) from None
            object.__setattr__(
                self, "booking_eligibility_timeout_seconds", timeout_value
            )
            return

        if base_url is None or bearer_token is None:
            raise ValueError(
                "BOOKING_ELIGIBILITY configuration is incomplete"
            ) from None

        try:
            validated = BookingEligibilityHttpConfig(
                base_url=base_url,
                bearer_token=bearer_token,
                timeout_seconds=timeout,
                max_response_bytes=max_bytes,
            )
        except BookingEligibilityHttpError:
            raise ValueError(
                "BOOKING_ELIGIBILITY configuration is invalid"
            ) from None

        object.__setattr__(
            self, "booking_eligibility_base_url", validated.base_url
        )
        object.__setattr__(
            self, "booking_eligibility_bearer_token", validated.bearer_token
        )
        object.__setattr__(
            self,
            "booking_eligibility_timeout_seconds",
            validated.timeout_seconds,
        )
        object.__setattr__(
            self,
            "booking_eligibility_max_response_bytes",
            validated.max_response_bytes,
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

    def validate_attachment_maintenance_runtime(self) -> None:
        """Validate constraints used only by the attachment maintenance process."""
        if not self.attachment_maintenance_enabled:
            return
        if self.database_url is None:
            raise ValueError(
                "DATABASE_URL is required for attachment maintenance"
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
            attachment_maintenance_enabled=_parse_bool(
                "ATTACHMENT_MAINTENANCE_ENABLED",
                source.get("ATTACHMENT_MAINTENANCE_ENABLED", "false"),
            ),
            attachment_maintenance_interval_seconds=_parse_int_range(
                "ATTACHMENT_MAINTENANCE_INTERVAL_SECONDS",
                source.get("ATTACHMENT_MAINTENANCE_INTERVAL_SECONDS", "60"),
                minimum=1,
                maximum=86400,
            ),
            attachment_maintenance_initial_delay_seconds=_parse_int_range(
                "ATTACHMENT_MAINTENANCE_INITIAL_DELAY_SECONDS",
                source.get(
                    "ATTACHMENT_MAINTENANCE_INITIAL_DELAY_SECONDS",
                    "0",
                ),
                minimum=0,
                maximum=86400,
            ),
            attachment_reconcile_batch_limit=_parse_int_range(
                "ATTACHMENT_RECONCILE_BATCH_LIMIT",
                source.get("ATTACHMENT_RECONCILE_BATCH_LIMIT", "100"),
                minimum=1,
                maximum=1000,
            ),
            attachment_purge_batch_limit=_parse_int_range(
                "ATTACHMENT_PURGE_BATCH_LIMIT",
                source.get("ATTACHMENT_PURGE_BATCH_LIMIT", "100"),
                minimum=1,
                maximum=1000,
            ),
            **cls._eligibility_kwargs_from_env(source),
        )

    @classmethod
    def _eligibility_kwargs_from_env(
        cls,
        source: Mapping[str, str],
    ) -> dict[str, object]:
        base_url, bearer_token = _parse_optional_eligibility_secret_pair(
            base_url=source.get("BOOKING_ELIGIBILITY_BASE_URL"),
            bearer_token=source.get("BOOKING_ELIGIBILITY_BEARER_TOKEN"),
        )
        timeout_raw = source.get("BOOKING_ELIGIBILITY_TIMEOUT_SECONDS")
        if timeout_raw is None:
            timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
        else:
            timeout_seconds = _parse_float_range(
                "BOOKING_ELIGIBILITY_TIMEOUT_SECONDS",
                timeout_raw,
                minimum=0.0,
                maximum=120.0,
            )
        max_raw = source.get("BOOKING_ELIGIBILITY_MAX_RESPONSE_BYTES")
        if max_raw is None:
            max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
        else:
            max_response_bytes = _parse_int_range(
                "BOOKING_ELIGIBILITY_MAX_RESPONSE_BYTES",
                max_raw,
                minimum=1,
                maximum=1_000_000,
            )
        return {
            "booking_eligibility_base_url": base_url,
            "booking_eligibility_bearer_token": bearer_token,
            "booking_eligibility_timeout_seconds": timeout_seconds,
            "booking_eligibility_max_response_bytes": max_response_bytes,
        }
