"""Attachment spool maintenance runner (CURSOR-13 Stage 1).

Library-only orchestration over public AttachmentSpoolStore APIs.
No CLI, FastAPI, WorkerRuntime, compose, or env wiring.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from app.core.attachment_maintenance_types import (
    AttachmentMaintenanceConfig,
    AttachmentMaintenanceCycleResult,
    AttachmentMaintenanceCycleStatus,
    AttachmentMaintenanceStatus,
    idle_maintenance_status,
)
from app.core.attachment_types import (
    AttachmentError,
    AttachmentPurgeResult,
    AttachmentReconcileResult,
)
from app.services.attachment_spool_store import AttachmentSpoolStore

logger = logging.getLogger(__name__)

_ALREADY_RUNNING = "ATTACHMENT_MAINTENANCE_ALREADY_RUNNING"

Waiter = Callable[..., Awaitable[bool]]
NowFn = Callable[[], datetime]
HeartbeatWriter = Callable[[datetime], None]


def _default_utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _default_waiter(
    *,
    stop_event: asyncio.Event,
    delay_seconds: int,
) -> bool:
    """Return True when stop requested, False when delay elapsed."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_seconds)
    except TimeoutError:
        return False
    return True


def _safe_attachment_error_code(exc: AttachmentError) -> str:
    return exc.code


def _is_safe_scalar(value: object) -> bool:
    return type(value) is int or type(value) is str


class AttachmentMaintenanceRunner:
    """Serialize reconcile+purge cycles and optionally loop until stop_event."""

    def __init__(
        self,
        *,
        store: AttachmentSpoolStore,
        config: AttachmentMaintenanceConfig,
        _waiter: Waiter | None = None,
        _now_fn: NowFn | None = None,
        _logger: logging.Logger | None = None,
        _heartbeat_writer: HeartbeatWriter | None = None,
    ) -> None:
        if not isinstance(config, AttachmentMaintenanceConfig):
            raise TypeError("config must be AttachmentMaintenanceConfig")
        self._store = store
        self._config = config
        self._waiter: Waiter = _default_waiter if _waiter is None else _waiter
        self._now_fn: NowFn = _default_utc_now if _now_fn is None else _now_fn
        self._logger = logger if _logger is None else _logger
        self._heartbeat_writer = _heartbeat_writer
        self._cycle_lock = asyncio.Lock()
        self._loop_active = False
        self._status = idle_maintenance_status()

    @property
    def status(self) -> AttachmentMaintenanceStatus:
        return self._status

    async def run_once(self) -> AttachmentMaintenanceCycleResult:
        async with self._cycle_lock:
            return await self._run_cycle_locked()

    async def run_forever(self, *, stop_event: asyncio.Event) -> None:
        if self._loop_active:
            raise RuntimeError(_ALREADY_RUNNING)
        self._loop_active = True
        try:
            async with self._cycle_lock:
                previous = self._status
                self._set_status(
                    AttachmentMaintenanceStatus(
                        loop_running=True,
                        cycle_running=previous.cycle_running,
                        last_cycle_started_at=previous.last_cycle_started_at,
                        last_cycle_finished_at=previous.last_cycle_finished_at,
                        last_success_at=previous.last_success_at,
                        last_cycle_status=previous.last_cycle_status,
                        consecutive_unsuccessful_cycles=(
                            previous.consecutive_unsuccessful_cycles
                        ),
                        last_reconcile_error_code=previous.last_reconcile_error_code,
                        last_purge_error_code=previous.last_purge_error_code,
                    )
                )
            self._log_safely(logging.INFO, "attachment_maintenance_started")

            if stop_event.is_set():
                return
            if self._config.initial_delay_seconds > 0:
                stop_requested = await self._waiter(
                    stop_event=stop_event,
                    delay_seconds=self._config.initial_delay_seconds,
                )
                if stop_requested:
                    return
            while True:
                if stop_event.is_set():
                    return
                await self.run_once()
                if stop_event.is_set():
                    return
                stop_requested = await self._waiter(
                    stop_event=stop_event,
                    delay_seconds=self._config.interval_seconds,
                )
                if stop_requested:
                    return
        finally:
            async with self._cycle_lock:
                current = self._status
                self._set_status(
                    AttachmentMaintenanceStatus(
                        loop_running=False,
                        cycle_running=current.cycle_running,
                        last_cycle_started_at=current.last_cycle_started_at,
                        last_cycle_finished_at=current.last_cycle_finished_at,
                        last_success_at=current.last_success_at,
                        last_cycle_status=current.last_cycle_status,
                        consecutive_unsuccessful_cycles=(
                            current.consecutive_unsuccessful_cycles
                        ),
                        last_reconcile_error_code=current.last_reconcile_error_code,
                        last_purge_error_code=current.last_purge_error_code,
                    )
                )
                self._loop_active = False
            self._log_safely(logging.INFO, "attachment_maintenance_stopped")

    async def _run_cycle_locked(self) -> AttachmentMaintenanceCycleResult:
        # now_fn failure before publishing active status: lock released by caller.
        started_at = self._now_fn()
        previous = self._status
        self._set_status(
            AttachmentMaintenanceStatus(
                loop_running=previous.loop_running,
                cycle_running=True,
                last_cycle_started_at=started_at,
                last_cycle_finished_at=None,
                last_success_at=previous.last_success_at,
                last_cycle_status=None,
                consecutive_unsuccessful_cycles=(
                    previous.consecutive_unsuccessful_cycles
                ),
                last_reconcile_error_code=None,
                last_purge_error_code=None,
            )
        )
        self._log_safely(logging.INFO, "attachment_maintenance_cycle_started")

        reconcile_result: AttachmentReconcileResult | None = None
        purge_result: AttachmentPurgeResult | None = None
        reconcile_error_code: str | None = None
        purge_error_code: str | None = None

        try:
            reconcile_result = await self._store.reconcile(
                limit=self._config.reconcile_limit
            )
        except asyncio.CancelledError:
            self._finish_cancelled(started_at=started_at)
            raise
        except AttachmentError as exc:
            reconcile_error_code = _safe_attachment_error_code(exc)
            self._log_safely(
                logging.WARNING,
                "attachment_maintenance_reconcile_failed",
                error_code=reconcile_error_code,
            )
        except Exception as exc:
            self._finish_internal_error(started_at=started_at)
            self._log_safely(
                logging.ERROR,
                "attachment_maintenance_cycle_internal_error",
                error_code=type(exc).__name__,
            )
            raise
        else:
            self._log_reconcile_completed(reconcile_result)

        try:
            purge_result = await self._store.purge_expired(
                limit=self._config.purge_limit
            )
        except asyncio.CancelledError:
            self._finish_cancelled(started_at=started_at)
            raise
        except AttachmentError as exc:
            purge_error_code = _safe_attachment_error_code(exc)
            self._log_safely(
                logging.WARNING,
                "attachment_maintenance_purge_failed",
                error_code=purge_error_code,
            )
        except Exception as exc:
            self._finish_internal_error(started_at=started_at)
            self._log_safely(
                logging.ERROR,
                "attachment_maintenance_cycle_internal_error",
                error_code=type(exc).__name__,
            )
            raise
        else:
            self._log_purge_completed(purge_result)

        result = self._build_cycle_result(
            reconcile_result=reconcile_result,
            purge_result=purge_result,
            reconcile_error_code=reconcile_error_code,
            purge_error_code=purge_error_code,
        )
        self._finish_result_cycle(result, started_at=started_at)
        if result.status is AttachmentMaintenanceCycleStatus.SUCCESS:
            self._write_heartbeat_best_effort()
        self._log_safely(
            logging.INFO,
            "attachment_maintenance_cycle_completed",
            status=result.status.value,
        )
        return result

    def _write_heartbeat_best_effort(self) -> None:
        writer = self._heartbeat_writer
        if writer is None:
            return
        completed_at = self._status.last_success_at
        if completed_at is None:
            return
        try:
            writer(completed_at)
        except Exception as exc:
            code = type(exc).__name__
            code_attr = getattr(exc, "code", None)
            if type(code_attr) is str and code_attr != "":
                code = code_attr
            self._log_safely(
                logging.WARNING,
                "attachment_maintenance_heartbeat_write_failed",
                error_code=code,
            )

    def _build_cycle_result(
        self,
        *,
        reconcile_result: AttachmentReconcileResult | None,
        purge_result: AttachmentPurgeResult | None,
        reconcile_error_code: str | None,
        purge_error_code: str | None,
    ) -> AttachmentMaintenanceCycleResult:
        if reconcile_result is not None and purge_result is not None:
            status = AttachmentMaintenanceCycleStatus.SUCCESS
        elif reconcile_result is None and purge_result is None:
            status = AttachmentMaintenanceCycleStatus.FAILED
        else:
            status = AttachmentMaintenanceCycleStatus.PARTIAL
        return AttachmentMaintenanceCycleResult(
            status=status,
            reconcile=reconcile_result,
            purge=purge_result,
            reconcile_error_code=reconcile_error_code,
            purge_error_code=purge_error_code,
        )

    def _resolve_finished_at(
        self,
        started_at: datetime,
    ) -> tuple[datetime, BaseException | None]:
        """Return a completion timestamp that cannot precede started_at.

        Non-monotonic wall-clock returns are clamped to started_at (not fatal).
        Injected now_fn exceptions use started_at as fallback and surface the
        exception to the caller for taxonomy handling.
        """
        try:
            returned = self._now_fn()
        except Exception as exc:
            return started_at, exc

        if (
            isinstance(returned, datetime)
            and returned.tzinfo is not None
            and returned.utcoffset() == timedelta(0)
            and returned < started_at
        ):
            return started_at, None
        return returned, None

    def _finish_result_cycle(
        self,
        result: AttachmentMaintenanceCycleResult,
        *,
        started_at: datetime,
    ) -> None:
        finished_at, now_exc = self._resolve_finished_at(started_at)
        previous = self._status
        if now_exc is not None:
            self._set_status(
                AttachmentMaintenanceStatus(
                    loop_running=previous.loop_running,
                    cycle_running=False,
                    last_cycle_started_at=started_at,
                    last_cycle_finished_at=finished_at,
                    last_success_at=previous.last_success_at,
                    last_cycle_status=AttachmentMaintenanceCycleStatus.INTERNAL_ERROR,
                    consecutive_unsuccessful_cycles=(
                        previous.consecutive_unsuccessful_cycles + 1
                    ),
                    last_reconcile_error_code=None,
                    last_purge_error_code=None,
                )
            )
            raise now_exc

        if result.status is AttachmentMaintenanceCycleStatus.SUCCESS:
            consecutive = 0
            last_success_at = finished_at
        else:
            consecutive = previous.consecutive_unsuccessful_cycles + 1
            last_success_at = previous.last_success_at
        self._set_status(
            AttachmentMaintenanceStatus(
                loop_running=previous.loop_running,
                cycle_running=False,
                last_cycle_started_at=started_at,
                last_cycle_finished_at=finished_at,
                last_success_at=last_success_at,
                last_cycle_status=result.status,
                consecutive_unsuccessful_cycles=consecutive,
                last_reconcile_error_code=result.reconcile_error_code,
                last_purge_error_code=result.purge_error_code,
            )
        )

    def _finish_cancelled(self, *, started_at: datetime) -> None:
        finished_at, _now_exc = self._resolve_finished_at(started_at)
        previous = self._status
        self._set_status(
            AttachmentMaintenanceStatus(
                loop_running=previous.loop_running,
                cycle_running=False,
                last_cycle_started_at=started_at,
                last_cycle_finished_at=finished_at,
                last_success_at=previous.last_success_at,
                last_cycle_status=AttachmentMaintenanceCycleStatus.CANCELLED,
                consecutive_unsuccessful_cycles=(
                    previous.consecutive_unsuccessful_cycles
                ),
                last_reconcile_error_code=None,
                last_purge_error_code=None,
            )
        )
        self._log_safely(logging.INFO, "attachment_maintenance_cycle_cancelled")

    def _finish_internal_error(self, *, started_at: datetime) -> None:
        finished_at, _now_exc = self._resolve_finished_at(started_at)
        previous = self._status
        self._set_status(
            AttachmentMaintenanceStatus(
                loop_running=previous.loop_running,
                cycle_running=False,
                last_cycle_started_at=started_at,
                last_cycle_finished_at=finished_at,
                last_success_at=previous.last_success_at,
                last_cycle_status=AttachmentMaintenanceCycleStatus.INTERNAL_ERROR,
                consecutive_unsuccessful_cycles=(
                    previous.consecutive_unsuccessful_cycles + 1
                ),
                last_reconcile_error_code=None,
                last_purge_error_code=None,
            )
        )

    def _set_status(self, status: AttachmentMaintenanceStatus) -> None:
        self._status = status

    def _log_safely(
        self,
        level: int,
        event: str,
        **fields: object,
    ) -> None:
        try:
            if fields:
                for value in fields.values():
                    if not _is_safe_scalar(value):
                        return
                keys = tuple(fields.keys())
                message = event + "".join(f" {key}=%s" for key in keys)
                args = tuple(fields[key] for key in keys)
                self._logger.log(level, message, *args)
            else:
                self._logger.log(level, event)
        except Exception:
            return

    def _log_reconcile_completed(self, result: AttachmentReconcileResult) -> None:
        self._log_safely(
            logging.INFO,
            "attachment_maintenance_reconcile_completed",
            promoted_to_stored=result.promoted_to_stored,
            deleted_writing_rows=result.deleted_writing_rows,
            deleted_orphan_temps=result.deleted_orphan_temps,
            deleted_orphan_finals=result.deleted_orphan_finals,
            deleted_unrecoverable_stored=result.deleted_unrecoverable_stored,
            deleted_delete_pending=result.deleted_delete_pending,
            unsafe_skipped=result.unsafe_skipped,
            io_unavailable_skipped=result.io_unavailable_skipped,
        )

    def _log_purge_completed(self, result: AttachmentPurgeResult) -> None:
        self._log_safely(
            logging.INFO,
            "attachment_maintenance_purge_completed",
            transitioned_stored=result.transitioned_stored,
            transitioned_leased=result.transitioned_leased,
            deleted=result.deleted,
            unsafe_skipped=result.unsafe_skipped,
            io_unavailable_skipped=result.io_unavailable_skipped,
            skipped=result.skipped,
        )
