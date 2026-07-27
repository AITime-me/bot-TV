"""Guards and helpers for destructive PostgreSQL foundation tests.

Never reads DATABASE_URL for test-DB selection.
Never includes credentials in raised messages: the URL is carried by
SecretDatabaseUrl, and every message produced here passes scrub_secrets.
"""

from __future__ import annotations

import asyncio
import os
import re
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from sqlalchemy.engine.url import make_url

from app.config import redact_database_url

_TEST_URL_ENV = "BOT_TV_TEST_DATABASE_URL"
_FORBIDDEN_FALLBACK_ENV = "DATABASE_URL"
# 'test' as its own segment: start/end or separated by '_' / '-'.
_TEST_NAME_SEGMENT = re.compile(r"(?:^|[_\-])test(?:$|[_\-])", re.IGNORECASE)
# Any scheme://... token is dropped: it may carry user:password@host.
_URL_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>]*")
_PASSWORD_KEYWORD = re.compile(
    r"(?i)\b(password|passwd|pwd)\b\s*[=:]\s*\S+",
)


class UnsafeTestDatabaseError(RuntimeError):
    """Raised when a URL is unsafe for DROP/TRUNCATE/CREATE fixtures."""


class PgDatabaseUnavailableError(RuntimeError):
    """Raised when BOT_TV_TEST_DATABASE_URL is set but PostgreSQL is unreachable."""


class AlembicCommandError(RuntimeError):
    """Alembic failure re-raised with a credential-free message."""


class SecretDatabaseUrl:
    """Test database URL that never renders credentials.

    repr/str/format expose only host:port/database. The raw URL is available
    solely via reveal() for SQLAlchemy and Alembic, so a pytest traceback,
    --showlocals dump or assertion diff cannot print the password.
    """

    __slots__ = ("_url",)

    def __init__(self, url: str) -> None:
        self._url = url

    def reveal(self) -> str:
        return self._url

    def redacted(self) -> str:
        return redact_database_url(self._url)

    def target(self) -> str:
        return describe_database_target(self._url)

    def __repr__(self) -> str:
        return f"SecretDatabaseUrl({self.target()})"

    def __str__(self) -> str:
        return self.target()

    def __format__(self, format_spec: str) -> str:
        return format(self.target(), format_spec)


def reveal_database_url(url: str | SecretDatabaseUrl) -> str:
    """Return the raw URL from either representation.

    Callers must pass the result straight into the consumer instead of binding
    it to a local variable, which pytest --showlocals would print.
    """
    if isinstance(url, SecretDatabaseUrl):
        return url.reveal()
    return url


def as_secret_database_url(url: str | SecretDatabaseUrl) -> SecretDatabaseUrl:
    """Normalize any accepted URL representation to the redacting wrapper."""
    if isinstance(url, SecretDatabaseUrl):
        return url
    return SecretDatabaseUrl(url)


def describe_database_target(url: str | SecretDatabaseUrl) -> str:
    """Return 'host:port/database' — safe parts only, without any scheme."""
    raw = reveal_database_url(url)
    try:
        parsed = make_url(raw)
    except Exception:
        return "<unparsable-database-url>"
    host = parsed.host or "<no-host>"
    location = f"{host}:{parsed.port}" if parsed.port else host
    return f"{location}/{parsed.database or '<no-database>'}"


def scrub_secrets(
    message: str,
    url: str | SecretDatabaseUrl | None = None,
) -> str:
    """Remove credentials and any URL token from a message before it is raised."""
    scrubbed = message
    if url is not None:
        raw = reveal_database_url(url)
        scrubbed = scrubbed.replace(raw, "***")
        try:
            password = make_url(raw).password
        except Exception:
            password = None
        if password:
            # make_url unquotes the password; scrub both spellings.
            scrubbed = scrubbed.replace(password, "***")
            scrubbed = scrubbed.replace(quote(password, safe=""), "***")
    scrubbed = _URL_TOKEN.sub("***", scrubbed)
    return _PASSWORD_KEYWORD.sub(r"\1=***", scrubbed)


def resolve_test_database_url(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return BOT_TV_TEST_DATABASE_URL only.

    DATABASE_URL is intentionally ignored even when present.
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    # Explicit non-use of production/runtime DATABASE_URL.
    _ = source.get(_FORBIDDEN_FALLBACK_ENV)
    url = source.get(_TEST_URL_ENV)
    if url is None or url == "":
        return None
    return url


def resolve_secret_test_database_url(
    environ: Mapping[str, str] | None = None,
) -> SecretDatabaseUrl | None:
    """Same as resolve_test_database_url, wrapped so repr cannot leak."""
    url = resolve_test_database_url(environ)
    if url is None:
        return None
    return SecretDatabaseUrl(url)


def _database_name_has_test_segment(name: str) -> bool:
    """True when 'test' is a discrete name segment (not a substring)."""
    return _TEST_NAME_SEGMENT.search(name) is not None


def database_name_from_url(url: str | SecretDatabaseUrl) -> str:
    """Extract database name without exposing credentials."""
    try:
        parsed = make_url(reveal_database_url(url))
    except Exception:
        raise UnsafeTestDatabaseError(
            "BOT_TV_TEST_DATABASE_URL must be a SQLAlchemy URL"
        ) from None
    name = parsed.database
    if not name:
        raise UnsafeTestDatabaseError(
            "destructive PostgreSQL fixtures require a database name"
        )
    return unquote(name)


def assert_safe_test_database_url(url: str | SecretDatabaseUrl) -> str:
    """Validate URL for destructive fixtures; return database name.

    Requires a discrete 'test' segment in the database name (separated by
    '_' or '-', or the whole name). Marker must not come from username,
    password, hostname, or query string. Error messages never include the
    URL or credentials.
    """
    try:
        parsed = make_url(reveal_database_url(url))
    except Exception:
        raise UnsafeTestDatabaseError(
            "BOT_TV_TEST_DATABASE_URL must be a SQLAlchemy URL"
        ) from None

    backend = parsed.get_backend_name()
    if backend != "postgresql":
        raise UnsafeTestDatabaseError(
            "BOT_TV_TEST_DATABASE_URL must use postgresql+asyncpg"
        )

    name = parsed.database
    if not name:
        raise UnsafeTestDatabaseError(
            "destructive PostgreSQL fixtures require a database name"
        )
    name = unquote(name)
    if not _database_name_has_test_segment(name):
        raise UnsafeTestDatabaseError(
            "destructive PostgreSQL fixtures require a database name "
            "with a discrete 'test' segment (separated by '_' or '-')"
        )
    return name


def run_alembic_command(
    *,
    alembic_ini: str | Path,
    command_name: str,
    revision: str,
    database_url: str | SecretDatabaseUrl,
    command_module: Any | None = None,
) -> None:
    """Run one Alembic CLI operation with a temporary process DATABASE_URL.

    Intended for worker threads via asyncio.to_thread. Always restores the
    previous DATABASE_URL (or removes it). Never logs the URL. Failures are
    re-raised as AlembicCommandError carrying a scrubbed traceback, so a
    connection error cannot print the test URL or password.
    """
    secret = as_secret_database_url(database_url)
    assert_safe_test_database_url(secret)
    if command_name not in {"upgrade", "downgrade"}:
        raise ValueError(f"unsupported alembic command: {command_name}")

    if command_module is None:
        from alembic import command as command_module  # type: ignore[no-redef]

    from alembic.config import Config

    # Both URLs stay wrapped: a caller's own DATABASE_URL is a secret too.
    previous: SecretDatabaseUrl | None = (
        SecretDatabaseUrl(os.environ["DATABASE_URL"])
        if "DATABASE_URL" in os.environ
        else None
    )
    os.environ["DATABASE_URL"] = secret.reveal()
    failure: str | None = None
    try:
        config = Config(str(alembic_ini))
        if command_name == "upgrade":
            command_module.upgrade(config, revision)
        else:
            command_module.downgrade(config, revision)
    except Exception as exc:
        failure = (
            f"alembic {command_name} {revision} failed on "
            f"{secret.target()}: "
            f"{type(exc).__name__}: "
            f"{scrub_secrets(traceback.format_exc(), secret)}"
        )
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous.reveal()
    if failure is not None:
        # Raised outside the except block: the original exception must not stay
        # reachable through __cause__/__context__, since it may carry the URL.
        raise AlembicCommandError(failure)


async def run_alembic_command_async(
    *,
    alembic_ini: str | Path,
    command_name: str,
    revision: str,
    database_url: str | SecretDatabaseUrl,
    command_module: Any | None = None,
) -> None:
    """Async wrapper: run Alembic outside the active event loop."""
    await asyncio.to_thread(
        run_alembic_command,
        alembic_ini=alembic_ini,
        command_name=command_name,
        revision=revision,
        database_url=database_url,
        command_module=command_module,
    )
