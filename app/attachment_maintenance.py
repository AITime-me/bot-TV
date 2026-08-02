"""Attachment spool maintenance process entrypoint (CURSOR-13 Stage 2A).

Separate process host for AttachmentMaintenanceRunner.
Not wired into FastAPI, WorkerRuntime, compose, or deploy.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from collections.abc import Mapping
from pathlib import Path

from app.config import Settings, _parse_int_range
from app.core.attachment_keys import EnvAttachmentKeyProvider
from app.core.attachment_maintenance_types import AttachmentMaintenanceConfig
from app.core.attachment_types import (
    MAX_TTL_SECONDS,
    AttachmentError,
    AttachmentSpoolPolicy,
)
from app.db.session import create_engine, create_session_factory
from app.services.attachment_maintenance import AttachmentMaintenanceRunner
from app.services.attachment_spool_store import AttachmentSpoolStore

logger = logging.getLogger(__name__)

_DEFAULT_SPOOL_TTL_SECONDS = 900


def _is_safe_scalar(value: object) -> bool:
    return type(value) is int or type(value) is str


def _log_safely(level: int, event: str, **fields: object) -> None:
    try:
        if fields:
            for value in fields.values():
                if not _is_safe_scalar(value):
                    return
            keys = tuple(fields.keys())
            message = event + "".join(f" {key}=%s" for key in keys)
            args = tuple(fields[key] for key in keys)
            logger.log(level, message, *args)
        else:
            logger.log(level, event)
    except Exception:
        return


def _safe_error_code(exc: BaseException) -> str:
    if isinstance(exc, AttachmentError):
        return exc.code
    return type(exc).__name__


def _require_existing_spool_root(environ: Mapping[str, str]) -> Path:
    """Parse ATTACHMENT_SPOOL_ROOT without logging the path value."""
    try:
        raw = environ.get("ATTACHMENT_SPOOL_ROOT")
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise ValueError("ATTACHMENT_SPOOL_ROOT is invalid") from None
    if raw is None or raw == "":
        raise ValueError("ATTACHMENT_SPOOL_ROOT is required")
    if type(raw) is not str:
        raise ValueError("ATTACHMENT_SPOOL_ROOT is required")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("ATTACHMENT_SPOOL_ROOT must be an absolute path")
    try:
        if path.is_symlink() or os.path.islink(path):
            raise ValueError("ATTACHMENT_SPOOL_ROOT must not be a symlink")
        if not path.exists():
            raise ValueError("ATTACHMENT_SPOOL_ROOT must exist")
        if not path.is_dir():
            raise ValueError("ATTACHMENT_SPOOL_ROOT must be a directory")
    except ValueError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise ValueError("ATTACHMENT_SPOOL_ROOT is invalid") from None
    return path


def _parse_spool_ttl_seconds(environ: Mapping[str, str]) -> int:
    return _parse_int_range(
        "ATTACHMENT_SPOOL_TTL_SECONDS",
        environ.get(
            "ATTACHMENT_SPOOL_TTL_SECONDS",
            str(_DEFAULT_SPOOL_TTL_SECONDS),
        ),
        minimum=1,
        maximum=MAX_TTL_SECONDS,
    )


def _install_stop_signals(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass


async def _dispose_engine(engine: object) -> Exception | None:
    """Attempt dispose once. Catch only ``Exception``; return it or ``None``.

    Does not log. Does not catch ``CancelledError`` / ``KeyboardInterrupt`` /
    ``SystemExit``. Caller classifies primary vs secondary failures.
    """
    try:
        await engine.dispose()  # type: ignore[misc]
    except Exception as exc:
        return exc
    return None


async def run_attachment_maintenance(
    settings: Settings,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    source = os.environ if environ is None else environ
    _log_safely(logging.INFO, "attachment_maintenance_process_starting")
    if not settings.attachment_maintenance_enabled:
        _log_safely(logging.INFO, "attachment_maintenance_process_disabled")
        return

    engine = None
    primary_error: BaseException | None = None
    clean_complete = False
    try:
        try:
            settings.validate_attachment_maintenance_runtime()
            spool_root = _require_existing_spool_root(source)
            ttl_seconds = _parse_spool_ttl_seconds(source)
            key_provider = EnvAttachmentKeyProvider(source)
            key_provider.get_active_key()
            engine = create_engine(settings)
            session_factory = create_session_factory(engine)
            policy = AttachmentSpoolPolicy(spool_root, ttl_seconds)
            store = AttachmentSpoolStore(
                session_factory=session_factory,
                key_provider=key_provider,
                policy=policy,
            )
            config = AttachmentMaintenanceConfig(
                interval_seconds=settings.attachment_maintenance_interval_seconds,
                reconcile_limit=settings.attachment_reconcile_batch_limit,
                purge_limit=settings.attachment_purge_batch_limit,
                initial_delay_seconds=(
                    settings.attachment_maintenance_initial_delay_seconds
                ),
            )
            runner = AttachmentMaintenanceRunner(store=store, config=config)
            stop_event = asyncio.Event()
            _install_stop_signals(stop_event)
            _log_safely(logging.INFO, "attachment_maintenance_process_started")
        except asyncio.CancelledError as exc:
            primary_error = exc
            raise
        except Exception as exc:
            primary_error = exc
            _log_safely(
                logging.ERROR,
                "attachment_maintenance_process_startup_failed",
                error_code=_safe_error_code(exc),
            )
            raise

        try:
            await runner.run_forever(stop_event=stop_event)
        except asyncio.CancelledError as exc:
            primary_error = exc
            raise
        except Exception as exc:
            primary_error = exc
            _log_safely(
                logging.ERROR,
                "attachment_maintenance_process_fatal",
                error_code=_safe_error_code(exc),
            )
            raise
        _log_safely(logging.INFO, "attachment_maintenance_process_stopping")
        clean_complete = True
    finally:
        dispose_error: Exception | None = None
        if engine is not None:
            dispose_error = await _dispose_engine(engine)
        if primary_error is not None:
            # Secondary dispose Exception is suppressed; do not log stopped.
            pass
        elif dispose_error is not None:
            _log_safely(
                logging.ERROR,
                "attachment_maintenance_process_fatal",
                error_code=_safe_error_code(dispose_error),
            )
            raise dispose_error
        elif clean_complete:
            _log_safely(logging.INFO, "attachment_maintenance_process_stopped")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_attachment_maintenance(Settings.from_env()))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Never print exception text: driver errors may embed credentials.
        print(
            f"attachment_maintenance stopped error_code={type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
