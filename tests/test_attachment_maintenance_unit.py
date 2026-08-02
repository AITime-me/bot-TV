"""Unit tests for attachment spool maintenance runner (CURSOR-13 Stage 1)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from app.core.attachment_maintenance_types import (
    AttachmentMaintenanceConfig,
    AttachmentMaintenanceCycleResult,
    AttachmentMaintenanceCycleStatus,
    AttachmentMaintenanceStatus,
)
from app.core.attachment_types import (
    AttachmentError,
    AttachmentPurgeResult,
    AttachmentReconcileResult,
)
from app.services.attachment_maintenance import AttachmentMaintenanceRunner

_UTC = timezone.utc
_T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=_UTC)
_TEST_TIMEOUT = 2.0
_FORBIDDEN = (
    str(uuid4()),
    "/var/spool/secret-path",
    "lease-token-SECRET",
    "deadbeefdigest",
    "SELECT * FROM attachment_spool_objects",
    "super-secret-password",
    "boom-sensitive-message",
)


def _reconcile_ok(**overrides: int) -> AttachmentReconcileResult:
    base = {
        "promoted_to_stored": 1,
        "deleted_writing_rows": 2,
        "deleted_orphan_temps": 3,
        "deleted_orphan_finals": 4,
        "deleted_unrecoverable_stored": 5,
        "deleted_delete_pending": 6,
        "unsafe_skipped": 0,
        "io_unavailable_skipped": 0,
    }
    base.update(overrides)
    return AttachmentReconcileResult(**base)


def _purge_ok(**overrides: int) -> AttachmentPurgeResult:
    base = {
        "transitioned_stored": 1,
        "transitioned_leased": 2,
        "deleted": 3,
        "unsafe_skipped": 0,
        "io_unavailable_skipped": 0,
        "skipped": 0,
    }
    base.update(overrides)
    return AttachmentPurgeResult(**base)


def _config(**overrides: Any) -> AttachmentMaintenanceConfig:
    base: dict[str, Any] = {
        "interval_seconds": 60,
        "reconcile_limit": 100,
        "purge_limit": 50,
    }
    base.update(overrides)
    return AttachmentMaintenanceConfig(**base)


class _FakeStore:
    def __init__(self) -> None:
        self.reconcile_limits: list[int] = []
        self.purge_limits: list[int] = []
        self.reconcile_calls = 0
        self.purge_calls = 0
        self.active_ops = 0
        self.max_active_ops = 0
        self.operation_completions = 0
        self.operation_cancellations = 0
        self.reconcile_gate: asyncio.Event | None = None
        self.reconcile_entered = asyncio.Event()
        self.purge_gate: asyncio.Event | None = None
        self.purge_entered = asyncio.Event()
        self.reconcile_result: AttachmentReconcileResult | BaseException = _reconcile_ok()
        self.purge_result: AttachmentPurgeResult | BaseException = _purge_ok()

    async def reconcile(self, *, limit: int) -> AttachmentReconcileResult:
        self.reconcile_calls += 1
        self.reconcile_limits.append(limit)
        self.active_ops += 1
        self.max_active_ops = max(self.max_active_ops, self.active_ops)
        self.reconcile_entered.set()
        try:
            if self.reconcile_gate is not None:
                await self.reconcile_gate.wait()
            outcome = self.reconcile_result
            if isinstance(outcome, BaseException):
                raise outcome
            self.operation_completions += 1
            return outcome
        except asyncio.CancelledError:
            self.operation_cancellations += 1
            raise
        finally:
            self.active_ops -= 1

    async def purge_expired(self, *, limit: int) -> AttachmentPurgeResult:
        self.purge_calls += 1
        self.purge_limits.append(limit)
        self.active_ops += 1
        self.max_active_ops = max(self.max_active_ops, self.active_ops)
        self.purge_entered.set()
        try:
            if self.purge_gate is not None:
                await self.purge_gate.wait()
            outcome = self.purge_result
            if isinstance(outcome, BaseException):
                raise outcome
            self.operation_completions += 1
            return outcome
        except asyncio.CancelledError:
            self.operation_cancellations += 1
            raise
        finally:
            self.active_ops -= 1


class _FakeWaiter:
    def __init__(self) -> None:
        self.delays: list[int] = []
        self._queue: asyncio.Queue[bool | BaseException] = asyncio.Queue()
        self.entered = asyncio.Event()

    def enqueue(self, stop_requested: bool) -> None:
        self._queue.put_nowait(stop_requested)

    def enqueue_error(self, exc: BaseException) -> None:
        self._queue.put_nowait(exc)

    async def __call__(
        self,
        *,
        stop_event: asyncio.Event,
        delay_seconds: int,
    ) -> bool:
        self.delays.append(delay_seconds)
        self.entered.set()
        if stop_event.is_set():
            return True
        item = await self._queue.get()
        if stop_event.is_set():
            return True
        if isinstance(item, BaseException):
            raise item
        return item


class _Clock:
    def __init__(self, start: datetime = _T0) -> None:
        self._current = start
        self.calls = 0
        self.fail_on_call: int | None = None
        self.fail_message = "now-fn-boom"
        self.return_sequence: list[datetime] | None = None

    def now(self) -> datetime:
        self.calls += 1
        if self.fail_on_call == self.calls:
            raise RuntimeError(self.fail_message)
        if self.return_sequence is not None:
            index = min(self.calls - 1, len(self.return_sequence) - 1)
            return self.return_sequence[index]
        return self._current

    def advance(self, seconds: int = 1) -> datetime:
        self._current = self._current + timedelta(seconds=seconds)
        return self._current


class _ThrowingLogger(logging.Logger):
    def __init__(self) -> None:
        super().__init__("test.attachment_maintenance.throwing")
        self.calls = 0

    def log(self, level: int, msg: object, *args: object, **kwargs: object) -> None:
        self.calls += 1
        raise RuntimeError("logger-boom")

    def info(self, msg: object, *args: object, **kwargs: object) -> None:
        self.log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: object, *args: object, **kwargs: object) -> None:
        self.log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg: object, *args: object, **kwargs: object) -> None:
        self.log(logging.ERROR, msg, *args, **kwargs)

    def exception(self, msg: object, *args: object, **kwargs: object) -> None:
        raise AssertionError("logger.exception must not be used")


class _ObservedAsyncLock:
    """Test-only lock wrapper that proves a second waiter is blocked."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.acquire_attempts = 0
        self.second_waiter_seen = asyncio.Event()

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self) -> _ObservedAsyncLock:
        self.acquire_attempts += 1
        if self._lock.locked():
            self.second_waiter_seen.set()
        await self._lock.acquire()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._lock.release()


def _runner(
    store: _FakeStore | None = None,
    *,
    config: AttachmentMaintenanceConfig | None = None,
    waiter: _FakeWaiter | None = None,
    clock: _Clock | None = None,
    logger: logging.Logger | None = None,
    heartbeat_writer: object | None = None,
) -> tuple[AttachmentMaintenanceRunner, _FakeStore, _FakeWaiter, _Clock]:
    fake_store = store if store is not None else _FakeStore()
    fake_waiter = waiter if waiter is not None else _FakeWaiter()
    fake_clock = clock if clock is not None else _Clock()
    runner = AttachmentMaintenanceRunner(
        store=fake_store,  # type: ignore[arg-type]
        config=config or _config(),
        _waiter=fake_waiter,
        _now_fn=fake_clock.now,
        _logger=logger,
        _heartbeat_writer=heartbeat_writer,  # type: ignore[arg-type]
    )
    return runner, fake_store, fake_waiter, fake_clock


async def _cancel_tasks(*tasks: asyncio.Task[Any] | None) -> None:
    pending = [task for task in tasks if task is not None and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _assert_log_records_safe(records: list[logging.LogRecord]) -> None:
    for record in records:
        message = record.getMessage()
        blob_parts = [message]
        if record.args is None:
            pass
        elif isinstance(record.args, dict):
            blob_parts.extend(str(value) for value in record.args.values())
            for value in record.args.values():
                assert not isinstance(value, BaseException)
                assert not hasattr(value, "reconcile")
        else:
            blob_parts.extend(str(value) for value in record.args)
            for value in record.args:
                assert not isinstance(value, BaseException)
                assert type(value) in {int, str}
        blob = "\n".join(blob_parts)
        for item in _FORBIDDEN:
            assert item not in blob
        assert record.exc_info is None
        assert record.exc_text is None


# --- Config -----------------------------------------------------------------


def test_config_requires_mandatory_fields() -> None:
    with pytest.raises(TypeError):
        AttachmentMaintenanceConfig(reconcile_limit=1, purge_limit=1)  # type: ignore[call-arg]


def test_config_initial_delay_default_and_bounds() -> None:
    assert _config().initial_delay_seconds == 0
    with pytest.raises(ValueError, match="initial_delay_seconds"):
        _config(initial_delay_seconds=-1)


@pytest.mark.parametrize("field", ["interval_seconds", "reconcile_limit", "purge_limit"])
def test_config_rejects_bool(field: str) -> None:
    kwargs = {
        "interval_seconds": 60,
        "reconcile_limit": 10,
        "purge_limit": 10,
        field: True,
    }
    with pytest.raises(ValueError, match="must be an integer"):
        AttachmentMaintenanceConfig(**kwargs)


def test_config_accepts_boundaries() -> None:
    AttachmentMaintenanceConfig(
        interval_seconds=1,
        reconcile_limit=1,
        purge_limit=1,
        initial_delay_seconds=0,
    )
    AttachmentMaintenanceConfig(
        interval_seconds=86400,
        reconcile_limit=1000,
        purge_limit=1000,
        initial_delay_seconds=86400,
    )


# --- CycleResult / Status ---------------------------------------------------


def test_cycle_result_invariants() -> None:
    AttachmentMaintenanceCycleResult(
        status=AttachmentMaintenanceCycleStatus.SUCCESS,
        reconcile=_reconcile_ok(),
        purge=_purge_ok(),
        reconcile_error_code=None,
        purge_error_code=None,
    )
    with pytest.raises(ValueError, match="success, partial, or failed"):
        AttachmentMaintenanceCycleResult(
            status=AttachmentMaintenanceCycleStatus.CANCELLED,
            reconcile=None,
            purge=None,
            reconcile_error_code="ATTACHMENT_RECONCILE_FAILED",
            purge_error_code="ATTACHMENT_RECONCILE_FAILED",
        )


def test_status_idle_and_active_invariants() -> None:
    idle = AttachmentMaintenanceStatus(
        loop_running=False,
        cycle_running=False,
        last_cycle_started_at=None,
        last_cycle_finished_at=None,
        last_success_at=None,
        last_cycle_status=None,
        consecutive_unsuccessful_cycles=0,
        last_reconcile_error_code=None,
        last_purge_error_code=None,
    )
    assert idle.last_cycle_status is None
    AttachmentMaintenanceStatus(
        loop_running=True,
        cycle_running=True,
        last_cycle_started_at=_T0,
        last_cycle_finished_at=None,
        last_success_at=None,
        last_cycle_status=None,
        consecutive_unsuccessful_cycles=0,
        last_reconcile_error_code=None,
        last_purge_error_code=None,
    )
    with pytest.raises(ValueError, match="active cycle forbids last_cycle_status"):
        AttachmentMaintenanceStatus(
            loop_running=True,
            cycle_running=True,
            last_cycle_started_at=_T0,
            last_cycle_finished_at=None,
            last_success_at=None,
            last_cycle_status=AttachmentMaintenanceCycleStatus.SUCCESS,
            consecutive_unsuccessful_cycles=0,
            last_reconcile_error_code=None,
            last_purge_error_code=None,
        )


def test_status_completed_invariants() -> None:
    AttachmentMaintenanceStatus(
        loop_running=False,
        cycle_running=False,
        last_cycle_started_at=_T0,
        last_cycle_finished_at=_T0 + timedelta(seconds=1),
        last_success_at=_T0 + timedelta(seconds=1),
        last_cycle_status=AttachmentMaintenanceCycleStatus.SUCCESS,
        consecutive_unsuccessful_cycles=0,
        last_reconcile_error_code=None,
        last_purge_error_code=None,
    )
    with pytest.raises(ValueError, match="success status forbids error codes"):
        AttachmentMaintenanceStatus(
            loop_running=False,
            cycle_running=False,
            last_cycle_started_at=_T0,
            last_cycle_finished_at=_T0 + timedelta(seconds=1),
            last_success_at=None,
            last_cycle_status=AttachmentMaintenanceCycleStatus.SUCCESS,
            consecutive_unsuccessful_cycles=0,
            last_reconcile_error_code="ATTACHMENT_RECONCILE_FAILED",
            last_purge_error_code=None,
        )
    with pytest.raises(ValueError, match="finished_at must not precede"):
        AttachmentMaintenanceStatus(
            loop_running=False,
            cycle_running=False,
            last_cycle_started_at=_T0 + timedelta(seconds=2),
            last_cycle_finished_at=_T0,
            last_success_at=None,
            last_cycle_status=AttachmentMaintenanceCycleStatus.CANCELLED,
            consecutive_unsuccessful_cycles=0,
            last_reconcile_error_code=None,
            last_purge_error_code=None,
        )
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        AttachmentMaintenanceStatus(
            loop_running=False,
            cycle_running=False,
            last_cycle_started_at=datetime(2026, 8, 1, 12, 0, 0),
            last_cycle_finished_at=None,
            last_success_at=None,
            last_cycle_status=None,
            consecutive_unsuccessful_cycles=0,
            last_reconcile_error_code=None,
            last_purge_error_code=None,
        )


# --- run_once ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_order_limits_and_success() -> None:
    runner, store, _, _ = _runner()
    result = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert result.status is AttachmentMaintenanceCycleStatus.SUCCESS
    assert store.reconcile_limits == [100]
    assert store.purge_limits == [50]
    assert runner.status.cycle_running is False
    assert runner.status.last_cycle_status is AttachmentMaintenanceCycleStatus.SUCCESS


@pytest.mark.asyncio
async def test_run_once_partial_reconcile_failure_still_purges() -> None:
    store = _FakeStore()
    store.reconcile_result = AttachmentError("ATTACHMENT_RECONCILE_FAILED")
    runner, store, _, _ = _runner(store)
    result = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert result.status is AttachmentMaintenanceCycleStatus.PARTIAL
    assert store.purge_calls == 1
    assert result.reconcile_error_code == "ATTACHMENT_RECONCILE_FAILED"


@pytest.mark.asyncio
async def test_run_once_unexpected_reconcile_skips_purge() -> None:
    store = _FakeStore()
    store.reconcile_result = RuntimeError("boom-sensitive-message")
    runner, store, _, _ = _runner(store)
    with pytest.raises(RuntimeError, match="boom-sensitive-message"):
        await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert store.purge_calls == 0
    assert runner.status.last_cycle_status is (
        AttachmentMaintenanceCycleStatus.INTERNAL_ERROR
    )
    assert runner.status.cycle_running is False


@pytest.mark.asyncio
async def test_run_once_cancelled_from_reconcile() -> None:
    store = _FakeStore()
    store.reconcile_result = asyncio.CancelledError()
    runner, store, _, _ = _runner(store)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert store.purge_calls == 0
    assert runner.status.last_cycle_status is AttachmentMaintenanceCycleStatus.CANCELLED
    assert runner.status.consecutive_unsuccessful_cycles == 0


@pytest.mark.asyncio
async def test_throwing_logger_does_not_skip_purge_after_attachment_error() -> None:
    store = _FakeStore()
    store.reconcile_result = AttachmentError("ATTACHMENT_RECONCILE_FAILED")
    throwing = _ThrowingLogger()
    runner, store, _, _ = _runner(store, logger=throwing)
    result = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert result.status is AttachmentMaintenanceCycleStatus.PARTIAL
    assert store.purge_calls == 1
    assert throwing.calls >= 1


# --- Serialization (deterministic observed lock) ----------------------------


@pytest.mark.asyncio
async def test_concurrent_run_once_serialize() -> None:
    store = _FakeStore()
    store.reconcile_gate = asyncio.Event()
    observed = _ObservedAsyncLock()
    runner, store, _, _ = _runner(store)
    runner._cycle_lock = observed  # type: ignore[assignment]
    first: asyncio.Task[Any] | None = None
    second: asyncio.Task[Any] | None = None
    try:
        first = asyncio.create_task(runner.run_once())
        await asyncio.wait_for(store.reconcile_entered.wait(), timeout=_TEST_TIMEOUT)
        assert observed.locked()

        second = asyncio.create_task(runner.run_once())
        await asyncio.wait_for(observed.second_waiter_seen.wait(), timeout=_TEST_TIMEOUT)
        assert store.reconcile_calls == 1
        assert store.max_active_ops == 1
        assert store.active_ops == 1

        store.reconcile_gate.set()
        results = await asyncio.wait_for(
            asyncio.gather(first, second),
            timeout=_TEST_TIMEOUT,
        )
        first = None
        second = None
        assert all(r.status is AttachmentMaintenanceCycleStatus.SUCCESS for r in results)
        assert store.reconcile_calls == 2
        assert store.max_active_ops == 1
        assert observed.acquire_attempts == 2
    finally:
        if store.reconcile_gate is not None:
            store.reconcile_gate.set()
        await _cancel_tasks(first, second)


@pytest.mark.asyncio
async def test_run_once_waits_during_run_forever_cycle() -> None:
    store = _FakeStore()
    store.reconcile_gate = asyncio.Event()
    observed = _ObservedAsyncLock()
    waiter = _FakeWaiter()
    runner, store, waiter, _ = _runner(store, waiter=waiter)
    runner._cycle_lock = observed  # type: ignore[assignment]
    stop = asyncio.Event()
    loop_task: asyncio.Task[Any] | None = None
    once_task: asyncio.Task[Any] | None = None
    try:
        loop_task = asyncio.create_task(runner.run_forever(stop_event=stop))
        await asyncio.wait_for(store.reconcile_entered.wait(), timeout=_TEST_TIMEOUT)
        once_task = asyncio.create_task(runner.run_once())
        await asyncio.wait_for(observed.second_waiter_seen.wait(), timeout=_TEST_TIMEOUT)
        assert store.reconcile_calls == 1
        assert store.max_active_ops == 1

        store.reconcile_gate.set()
        once_result = await asyncio.wait_for(once_task, timeout=_TEST_TIMEOUT)
        once_task = None
        assert once_result.status is AttachmentMaintenanceCycleStatus.SUCCESS
        stop.set()
        waiter.enqueue(True)
        await asyncio.wait_for(loop_task, timeout=_TEST_TIMEOUT)
        loop_task = None
    finally:
        if store.reconcile_gate is not None:
            store.reconcile_gate.set()
        stop.set()
        waiter.enqueue(True)
        await _cancel_tasks(loop_task, once_task)


@pytest.mark.asyncio
async def test_interval_wait_does_not_hold_cycle_lock() -> None:
    waiter = _FakeWaiter()
    runner, store, waiter, _ = _runner(waiter=waiter)
    stop = asyncio.Event()
    loop_task: asyncio.Task[Any] | None = None
    try:
        loop_task = asyncio.create_task(runner.run_forever(stop_event=stop))
        await asyncio.wait_for(waiter.entered.wait(), timeout=_TEST_TIMEOUT)
        assert store.reconcile_calls == 1
        result = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
        assert result.status is AttachmentMaintenanceCycleStatus.SUCCESS
        assert store.reconcile_calls == 2
        stop.set()
        waiter.enqueue(True)
        await asyncio.wait_for(loop_task, timeout=_TEST_TIMEOUT)
        loop_task = None
    finally:
        stop.set()
        waiter.enqueue(True)
        await _cancel_tasks(loop_task)


# --- Duplicate loop / guard -------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_run_forever_fails_fast() -> None:
    store = _FakeStore()
    store.reconcile_gate = asyncio.Event()
    runner, store, waiter, _ = _runner(store)
    stop = asyncio.Event()
    first: asyncio.Task[Any] | None = None
    try:
        first = asyncio.create_task(runner.run_forever(stop_event=stop))
        await asyncio.wait_for(store.reconcile_entered.wait(), timeout=_TEST_TIMEOUT)
        with pytest.raises(RuntimeError, match="ATTACHMENT_MAINTENANCE_ALREADY_RUNNING"):
            await runner.run_forever(stop_event=asyncio.Event())
        assert runner.status.loop_running is True
        stop.set()
        store.reconcile_gate.set()
        await asyncio.wait_for(first, timeout=_TEST_TIMEOUT)
        first = None
        assert runner.status.loop_running is False
    finally:
        stop.set()
        if store.reconcile_gate is not None:
            store.reconcile_gate.set()
        await _cancel_tasks(first)


@pytest.mark.asyncio
async def test_started_logger_failure_does_not_stick_guard() -> None:
    throwing = _ThrowingLogger()
    waiter = _FakeWaiter()
    runner, store, waiter, _ = _runner(waiter=waiter, logger=throwing)
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(runner.run_forever(stop_event=stop), timeout=_TEST_TIMEOUT)
    assert runner.status.loop_running is False
    stop2 = asyncio.Event()
    stop2.set()
    await asyncio.wait_for(runner.run_forever(stop_event=stop2), timeout=_TEST_TIMEOUT)
    assert store.reconcile_calls == 0


@pytest.mark.asyncio
async def test_waiter_unexpected_exception_clears_guard() -> None:
    waiter = _FakeWaiter()
    runner, store, waiter, _ = _runner(
        waiter=waiter,
        config=_config(initial_delay_seconds=5),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_forever(stop_event=stop))
    try:
        await asyncio.wait_for(waiter.entered.wait(), timeout=_TEST_TIMEOUT)
        waiter.enqueue_error(RuntimeError("waiter-boom"))
        with pytest.raises(RuntimeError, match="waiter-boom"):
            await asyncio.wait_for(task, timeout=_TEST_TIMEOUT)
        task = None  # type: ignore[assignment]
        assert runner.status.loop_running is False
        stop2 = asyncio.Event()
        stop2.set()
        await asyncio.wait_for(runner.run_forever(stop_event=stop2), timeout=_TEST_TIMEOUT)
        assert store.reconcile_calls == 0
    finally:
        stop.set()
        await _cancel_tasks(task)


@pytest.mark.asyncio
async def test_guard_clears_after_cancellation() -> None:
    store = _FakeStore()
    store.reconcile_gate = asyncio.Event()
    runner, store, _, _ = _runner(store)
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_forever(stop_event=stop))
    try:
        await asyncio.wait_for(store.reconcile_entered.wait(), timeout=_TEST_TIMEOUT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=_TEST_TIMEOUT)
        task = None  # type: ignore[assignment]
        assert runner.status.loop_running is False
        assert runner.status.last_cycle_status is AttachmentMaintenanceCycleStatus.CANCELLED
        stop2 = asyncio.Event()
        stop2.set()
        await asyncio.wait_for(runner.run_forever(stop_event=stop2), timeout=_TEST_TIMEOUT)
    finally:
        if store.reconcile_gate is not None:
            store.reconcile_gate.set()
        await _cancel_tasks(task)


# --- Shutdown / status race -------------------------------------------------


@pytest.mark.asyncio
async def test_stop_during_active_reconcile_completes_full_cycle() -> None:
    store = _FakeStore()
    store.reconcile_gate = asyncio.Event()
    waiter = _FakeWaiter()
    runner, store, waiter, _ = _runner(store, waiter=waiter)
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_forever(stop_event=stop))
    try:
        await asyncio.wait_for(store.reconcile_entered.wait(), timeout=_TEST_TIMEOUT)
        stop.set()
        store.reconcile_gate.set()
        await asyncio.wait_for(task, timeout=_TEST_TIMEOUT)
        task = None  # type: ignore[assignment]
        assert store.reconcile_calls == 1
        assert store.purge_calls == 1
        assert waiter.delays == []
    finally:
        if store.reconcile_gate is not None:
            store.reconcile_gate.set()
        stop.set()
        await _cancel_tasks(task)


@pytest.mark.asyncio
async def test_status_race_finally_waits_for_active_run_once() -> None:
    store = _FakeStore()
    observed = _ObservedAsyncLock()
    waiter = _FakeWaiter()
    runner, store, waiter, _ = _runner(store, waiter=waiter)
    runner._cycle_lock = observed  # type: ignore[assignment]
    stop = asyncio.Event()
    loop_task: asyncio.Task[Any] | None = None
    once_task: asyncio.Task[Any] | None = None
    try:
        loop_task = asyncio.create_task(runner.run_forever(stop_event=stop))
        await asyncio.wait_for(waiter.entered.wait(), timeout=_TEST_TIMEOUT)
        assert store.reconcile_calls == 1

        store.reconcile_gate = asyncio.Event()
        store.reconcile_entered.clear()
        once_task = asyncio.create_task(runner.run_once())
        await asyncio.wait_for(store.reconcile_entered.wait(), timeout=_TEST_TIMEOUT)
        assert runner.status.cycle_running is True

        stop.set()
        waiter.enqueue(True)
        await asyncio.wait_for(observed.second_waiter_seen.wait(), timeout=_TEST_TIMEOUT)
        assert runner.status.cycle_running is True
        assert runner.status.loop_running is True

        store.reconcile_gate.set()
        once_result = await asyncio.wait_for(once_task, timeout=_TEST_TIMEOUT)
        once_task = None
        await asyncio.wait_for(loop_task, timeout=_TEST_TIMEOUT)
        loop_task = None

        assert once_result.status is AttachmentMaintenanceCycleStatus.SUCCESS
        assert runner.status.loop_running is False
        assert runner.status.cycle_running is False
        assert runner.status.last_cycle_status is AttachmentMaintenanceCycleStatus.SUCCESS
    finally:
        if store.reconcile_gate is not None:
            store.reconcile_gate.set()
        stop.set()
        waiter.enqueue(True)
        await _cancel_tasks(loop_task, once_task)


@pytest.mark.asyncio
async def test_cancellation_during_wait_preserves_last_cycle_status() -> None:
    waiter = _FakeWaiter()
    runner, _, waiter, _ = _runner(waiter=waiter)
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_forever(stop_event=stop))
    try:
        await asyncio.wait_for(waiter.entered.wait(), timeout=_TEST_TIMEOUT)
        assert runner.status.last_cycle_status is AttachmentMaintenanceCycleStatus.SUCCESS
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=_TEST_TIMEOUT)
        task = None  # type: ignore[assignment]
        assert runner.status.last_cycle_status is AttachmentMaintenanceCycleStatus.SUCCESS
        assert runner.status.loop_running is False
    finally:
        await _cancel_tasks(task)


# --- now_fn -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_now_fn_start_failure_no_zombie_cycle() -> None:
    clock = _Clock()
    clock.fail_on_call = 1
    runner, store, _, clock = _runner(clock=clock)
    with pytest.raises(RuntimeError, match="now-fn-boom"):
        await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert store.reconcile_calls == 0
    assert runner.status.cycle_running is False
    assert runner.status.last_cycle_status is None


@pytest.mark.asyncio
async def test_now_fn_finish_failure_internal_error() -> None:
    clock = _Clock()
    clock.fail_on_call = 2
    clock.fail_message = "now-fn-finish-boom"
    runner, store, _, clock = _runner(clock=clock)
    with pytest.raises(RuntimeError, match="now-fn-finish-boom"):
        await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert runner.status.cycle_running is False
    assert runner.status.last_cycle_status is (
        AttachmentMaintenanceCycleStatus.INTERNAL_ERROR
    )


@pytest.mark.asyncio
async def test_now_fn_failure_during_cancellation_preserves_cancelled() -> None:
    store = _FakeStore()
    store.reconcile_gate = asyncio.Event()
    clock = _Clock()
    runner, store, _, clock = _runner(store, clock=clock)
    task = asyncio.create_task(runner.run_once())
    try:
        await asyncio.wait_for(store.reconcile_entered.wait(), timeout=_TEST_TIMEOUT)
        clock.fail_on_call = clock.calls + 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=_TEST_TIMEOUT)
        task = None  # type: ignore[assignment]
        assert runner.status.last_cycle_status is AttachmentMaintenanceCycleStatus.CANCELLED
        assert runner.status.cycle_running is False
    finally:
        if store.reconcile_gate is not None:
            store.reconcile_gate.set()
        await _cancel_tasks(task)


# --- Status counters --------------------------------------------------------


@pytest.mark.asyncio
async def test_status_counters_and_success_reset() -> None:
    store = _FakeStore()
    store.reconcile_result = AttachmentError("ATTACHMENT_RECONCILE_FAILED")
    runner, store, _, clock = _runner(store)
    await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert runner.status.consecutive_unsuccessful_cycles == 1

    store.reconcile_result = _reconcile_ok()
    store.purge_result = _purge_ok()
    clock.advance(3)
    await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert runner.status.consecutive_unsuccessful_cycles == 0
    assert runner.status.last_success_at == _T0 + timedelta(seconds=3)


@pytest.mark.asyncio
async def test_status_cycle_running_while_gated() -> None:
    store = _FakeStore()
    store.reconcile_gate = asyncio.Event()
    runner, store, _, clock = _runner(store)
    task = asyncio.create_task(runner.run_once())
    try:
        await asyncio.wait_for(store.reconcile_entered.wait(), timeout=_TEST_TIMEOUT)
        assert runner.status.cycle_running is True
        assert runner.status.last_cycle_status is None
        assert runner.status.last_cycle_finished_at is None
        clock.advance(5)
        store.reconcile_gate.set()
        await asyncio.wait_for(task, timeout=_TEST_TIMEOUT)
        task = None  # type: ignore[assignment]
        assert runner.status.cycle_running is False
        assert runner.status.last_cycle_finished_at == _T0 + timedelta(seconds=5)
    finally:
        if store.reconcile_gate is not None:
            store.reconcile_gate.set()
        await _cancel_tasks(task)


# --- Logging safety ---------------------------------------------------------


@pytest.mark.asyncio
async def test_logging_safety_records_and_args(caplog: pytest.LogCaptureFixture) -> None:
    store = _FakeStore()
    store.reconcile_result = AttachmentError("ATTACHMENT_RECONCILE_FAILED")
    store.purge_result = _purge_ok(deleted=9)
    logger = logging.getLogger("test.attachment_maintenance.safe")
    runner, store, _, _ = _runner(store, logger=logger)
    toxic = " ".join(_FORBIDDEN)

    with caplog.at_level(logging.INFO, logger=logger.name):
        await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
        store.reconcile_result = RuntimeError(toxic)
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)

    records = [record for record in caplog.records if record.name == logger.name]
    _assert_log_records_safe(records)
    blob = "\n".join(record.getMessage() for record in records)
    assert "ATTACHMENT_RECONCILE_FAILED" in blob
    assert "RuntimeError" in blob
    assert "9" in blob


@pytest.mark.asyncio
async def test_logger_exception_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    store.reconcile_result = RuntimeError("boom-sensitive-message")
    logger = logging.getLogger("test.attachment_maintenance.no_exception")

    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("logger.exception must not be used")

    monkeypatch.setattr(logger, "exception", boom)
    runner, store, _, _ = _runner(store, logger=logger)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)


@pytest.mark.asyncio
async def test_run_forever_multiple_cycles_then_stop() -> None:
    waiter = _FakeWaiter()
    runner, store, waiter, _ = _runner(waiter=waiter, config=_config(interval_seconds=3))
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_forever(stop_event=stop))
    try:
        await asyncio.wait_for(waiter.entered.wait(), timeout=_TEST_TIMEOUT)
        assert store.reconcile_calls == 1
        waiter.entered.clear()
        waiter.enqueue(False)
        await asyncio.wait_for(waiter.entered.wait(), timeout=_TEST_TIMEOUT)
        assert store.reconcile_calls == 2
        assert waiter.delays == [3, 3]
        stop.set()
        waiter.enqueue(True)
        await asyncio.wait_for(task, timeout=_TEST_TIMEOUT)
        task = None  # type: ignore[assignment]
    finally:
        stop.set()
        waiter.enqueue(True)
        await _cancel_tasks(task)


# --- C13-R01 backward clock / finish clamp ----------------------------------


@pytest.mark.asyncio
async def test_backward_clock_success_clamps_finished_at() -> None:
    t2 = _T0 + timedelta(seconds=10)
    t1 = _T0 + timedelta(seconds=1)
    assert t1 < t2
    clock = _Clock()
    clock.return_sequence = [t2, t1]
    runner, store, _, clock = _runner(clock=clock)
    result = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert result.status is AttachmentMaintenanceCycleStatus.SUCCESS
    assert runner.status.cycle_running is False
    assert runner.status.last_cycle_status is AttachmentMaintenanceCycleStatus.SUCCESS
    assert runner.status.last_cycle_started_at == t2
    assert runner.status.last_cycle_finished_at == t2
    assert runner.status.last_success_at == t2
    assert runner.status.consecutive_unsuccessful_cycles == 0
    second = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert second.status is AttachmentMaintenanceCycleStatus.SUCCESS
    assert store.reconcile_calls == 2


@pytest.mark.asyncio
async def test_backward_clock_partial_clamps_finished_at() -> None:
    t2 = _T0 + timedelta(seconds=10)
    t1 = _T0
    clock = _Clock()
    clock.return_sequence = [t2, t1]
    store = _FakeStore()
    store.reconcile_result = AttachmentError("ATTACHMENT_RECONCILE_FAILED")
    runner, store, _, _ = _runner(store, clock=clock)
    result = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert result.status is AttachmentMaintenanceCycleStatus.PARTIAL
    assert store.purge_calls == 1
    assert result.reconcile_error_code == "ATTACHMENT_RECONCILE_FAILED"
    assert runner.status.last_cycle_finished_at == t2
    assert runner.status.cycle_running is False
    assert runner.status.consecutive_unsuccessful_cycles == 1


@pytest.mark.asyncio
async def test_backward_clock_cancellation_clamps_and_propagates() -> None:
    t2 = _T0 + timedelta(seconds=10)
    t1 = _T0
    store = _FakeStore()
    store.reconcile_gate = asyncio.Event()
    clock = _Clock()
    clock.return_sequence = [t2, t1]
    runner, store, _, clock = _runner(store, clock=clock)
    task = asyncio.create_task(runner.run_once())
    try:
        await asyncio.wait_for(store.reconcile_entered.wait(), timeout=_TEST_TIMEOUT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=_TEST_TIMEOUT)
        task = None  # type: ignore[assignment]
        assert runner.status.last_cycle_status is AttachmentMaintenanceCycleStatus.CANCELLED
        assert runner.status.last_cycle_started_at == t2
        assert runner.status.last_cycle_finished_at == t2
        assert runner.status.cycle_running is False
        assert runner.status.last_reconcile_error_code is None
        assert runner.status.consecutive_unsuccessful_cycles == 0
        store.reconcile_gate.set()
        store.reconcile_gate = None
        follow = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
        assert follow.status is AttachmentMaintenanceCycleStatus.SUCCESS
    finally:
        if store.reconcile_gate is not None:
            store.reconcile_gate.set()
        await _cancel_tasks(task)


@pytest.mark.asyncio
async def test_now_fn_raise_during_cancellation_keeps_cancelled_priority(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _FakeStore()
    store.reconcile_gate = asyncio.Event()
    clock = _Clock()
    clock.fail_message = "boom-sensitive-message /var/spool/secret-path"
    logger = logging.getLogger("test.attachment_maintenance.cancel_clock")
    runner, store, _, clock = _runner(store, clock=clock, logger=logger)
    task = asyncio.create_task(runner.run_once())
    try:
        await asyncio.wait_for(store.reconcile_entered.wait(), timeout=_TEST_TIMEOUT)
        started = runner.status.last_cycle_started_at
        assert started is not None
        clock.fail_on_call = clock.calls + 1
        with caplog.at_level(logging.INFO, logger=logger.name):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=_TEST_TIMEOUT)
        task = None  # type: ignore[assignment]
        assert runner.status.last_cycle_status is AttachmentMaintenanceCycleStatus.CANCELLED
        assert runner.status.cycle_running is False
        assert runner.status.last_cycle_finished_at == started
        blob = "\n".join(record.getMessage() for record in caplog.records)
        assert "boom-sensitive-message" not in blob
        assert "/var/spool/secret-path" not in blob
    finally:
        if store.reconcile_gate is not None:
            store.reconcile_gate.set()
        await _cancel_tasks(task)


# --- C13-R02 operational paths ----------------------------------------------


@pytest.mark.asyncio
async def test_run_once_partial_purge_only_attachment_error() -> None:
    store = _FakeStore()
    store.purge_result = AttachmentError("ATTACHMENT_POLICY_INVALID")
    runner, store, _, _ = _runner(store)
    result = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert result.status is AttachmentMaintenanceCycleStatus.PARTIAL
    assert result.reconcile is not None
    assert result.purge is None
    assert result.reconcile_error_code is None
    assert result.purge_error_code == "ATTACHMENT_POLICY_INVALID"
    assert store.reconcile_calls == 1
    assert store.purge_calls == 1
    assert runner.status.last_cycle_status is AttachmentMaintenanceCycleStatus.PARTIAL
    assert runner.status.consecutive_unsuccessful_cycles == 1


@pytest.mark.asyncio
async def test_run_once_both_attachment_errors_failed() -> None:
    store = _FakeStore()
    store.reconcile_result = AttachmentError("ATTACHMENT_RECONCILE_FAILED")
    store.purge_result = AttachmentError("ATTACHMENT_POLICY_INVALID")
    runner, store, _, _ = _runner(store)
    result = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert result.status is AttachmentMaintenanceCycleStatus.FAILED
    assert result.reconcile is None
    assert result.purge is None
    assert result.reconcile_error_code == "ATTACHMENT_RECONCILE_FAILED"
    assert result.purge_error_code == "ATTACHMENT_POLICY_INVALID"
    assert store.purge_calls == 1
    assert runner.status.last_cycle_status is AttachmentMaintenanceCycleStatus.FAILED
    assert runner.status.consecutive_unsuccessful_cycles == 1


@pytest.mark.asyncio
async def test_run_once_unexpected_purge_exception(caplog: pytest.LogCaptureFixture) -> None:
    store = _FakeStore()
    store.purge_result = RuntimeError("boom-sensitive-message")
    logger = logging.getLogger("test.attachment_maintenance.purge_unexpected")
    runner, store, _, _ = _runner(store, logger=logger)
    with caplog.at_level(logging.INFO, logger=logger.name):
        with pytest.raises(RuntimeError, match="boom-sensitive-message"):
            await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert store.reconcile_calls == 1
    assert store.purge_calls == 1
    assert runner.status.last_cycle_status is (
        AttachmentMaintenanceCycleStatus.INTERNAL_ERROR
    )
    assert runner.status.cycle_running is False
    assert runner.status.last_reconcile_error_code is None
    assert runner.status.last_purge_error_code is None
    assert runner.status.consecutive_unsuccessful_cycles == 1
    records = [record for record in caplog.records if record.name == logger.name]
    _assert_log_records_safe(records)
    blob = "\n".join(record.getMessage() for record in records)
    assert "RuntimeError" in blob
    assert "boom-sensitive-message" not in blob


@pytest.mark.asyncio
async def test_run_once_cancelled_from_purge() -> None:
    store = _FakeStore()
    store.purge_gate = asyncio.Event()
    runner, store, _, _ = _runner(store)
    task = asyncio.create_task(runner.run_once())
    try:
        await asyncio.wait_for(store.purge_entered.wait(), timeout=_TEST_TIMEOUT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=_TEST_TIMEOUT)
        task = None  # type: ignore[assignment]
        assert store.reconcile_calls == 1
        assert runner.status.last_cycle_status is AttachmentMaintenanceCycleStatus.CANCELLED
        assert runner.status.cycle_running is False
        assert runner.status.last_reconcile_error_code is None
        assert runner.status.consecutive_unsuccessful_cycles == 0
        store.purge_gate.set()
        store.purge_gate = None
        follow = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
        assert follow.status is AttachmentMaintenanceCycleStatus.SUCCESS
    finally:
        if store.purge_gate is not None:
            store.purge_gate.set()
        await _cancel_tasks(task)


# --- C13-R02 shutdown paths -------------------------------------------------


@pytest.mark.asyncio
async def test_stop_set_before_initial_delay_zero_cycles() -> None:
    waiter = _FakeWaiter()
    runner, store, waiter, _ = _runner(
        waiter=waiter,
        config=_config(initial_delay_seconds=10),
    )
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(runner.run_forever(stop_event=stop), timeout=_TEST_TIMEOUT)
    assert store.reconcile_calls == 0
    assert store.purge_calls == 0
    assert waiter.delays == []
    assert runner.status.loop_running is False


@pytest.mark.asyncio
async def test_stop_during_initial_delay_zero_cycles() -> None:
    waiter = _FakeWaiter()
    runner, store, waiter, _ = _runner(
        waiter=waiter,
        config=_config(initial_delay_seconds=10),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_forever(stop_event=stop))
    try:
        await asyncio.wait_for(waiter.entered.wait(), timeout=_TEST_TIMEOUT)
        assert waiter.delays == [10]
        waiter.enqueue(True)
        await asyncio.wait_for(task, timeout=_TEST_TIMEOUT)
        task = None  # type: ignore[assignment]
        assert store.reconcile_calls == 0
        assert store.purge_calls == 0
        assert runner.status.loop_running is False
    finally:
        stop.set()
        waiter.enqueue(True)
        await _cancel_tasks(task)


@pytest.mark.asyncio
async def test_stop_during_active_purge_completes_then_exits() -> None:
    store = _FakeStore()
    store.purge_gate = asyncio.Event()
    waiter = _FakeWaiter()
    runner, store, waiter, _ = _runner(store, waiter=waiter)
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_forever(stop_event=stop))
    try:
        await asyncio.wait_for(store.purge_entered.wait(), timeout=_TEST_TIMEOUT)
        stop.set()
        store.purge_gate.set()
        await asyncio.wait_for(task, timeout=_TEST_TIMEOUT)
        task = None  # type: ignore[assignment]
        assert store.reconcile_calls == 1
        assert store.purge_calls == 1
        assert store.operation_cancellations == 0
        assert store.operation_completions == 2
        assert waiter.delays == []
        assert runner.status.loop_running is False
        assert runner.status.last_cycle_status is AttachmentMaintenanceCycleStatus.SUCCESS
    finally:
        if store.purge_gate is not None:
            store.purge_gate.set()
        stop.set()
        await _cancel_tasks(task)


@pytest.mark.asyncio
async def test_stop_event_alone_is_not_cancellation() -> None:
    store = _FakeStore()
    store.reconcile_gate = asyncio.Event()
    waiter = _FakeWaiter()
    runner, store, waiter, _ = _runner(store, waiter=waiter)
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_forever(stop_event=stop))
    try:
        await asyncio.wait_for(store.reconcile_entered.wait(), timeout=_TEST_TIMEOUT)
        stop.set()
        store.reconcile_gate.set()
        await asyncio.wait_for(task, timeout=_TEST_TIMEOUT)
        task = None  # type: ignore[assignment]
        assert store.operation_cancellations == 0
        assert store.operation_completions == 2
        assert runner.status.last_cycle_status is AttachmentMaintenanceCycleStatus.SUCCESS
        assert runner.status.loop_running is False
        assert waiter.delays == []
    finally:
        if store.reconcile_gate is not None:
            store.reconcile_gate.set()
        stop.set()
        await _cancel_tasks(task)


@pytest.mark.asyncio
async def test_throwing_logger_does_not_mask_cancellation() -> None:
    store = _FakeStore()
    store.reconcile_gate = asyncio.Event()
    throwing = _ThrowingLogger()
    runner, store, _, _ = _runner(store, logger=throwing)
    task = asyncio.create_task(runner.run_once())
    try:
        await asyncio.wait_for(store.reconcile_entered.wait(), timeout=_TEST_TIMEOUT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=_TEST_TIMEOUT)
        task = None  # type: ignore[assignment]
        assert runner.status.last_cycle_status is AttachmentMaintenanceCycleStatus.CANCELLED
        assert runner.status.cycle_running is False
        store.reconcile_gate.set()
        store.reconcile_gate = None
        follow = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
        assert follow.status is AttachmentMaintenanceCycleStatus.SUCCESS
    finally:
        if store.reconcile_gate is not None:
            store.reconcile_gate.set()
        await _cancel_tasks(task)


# --- SUCCESS-only heartbeat (CURSOR-14) ------------------------------------


@pytest.mark.asyncio
async def test_success_writes_heartbeat_once_with_finish_timestamp(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writes: list[datetime] = []

    def writer(completed_at: datetime) -> None:
        writes.append(completed_at)

    runner, store, _, clock = _runner(heartbeat_writer=writer)
    with caplog.at_level(logging.INFO):
        result = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert result.status is AttachmentMaintenanceCycleStatus.SUCCESS
    assert len(writes) == 1
    assert writes[0] == runner.status.last_success_at
    assert writes[0] == runner.status.last_cycle_finished_at
    assert store.reconcile_calls == 1
    assert store.purge_calls == 1
    assert clock.calls >= 2


@pytest.mark.asyncio
async def test_partial_does_not_write_heartbeat() -> None:
    writes: list[datetime] = []
    store = _FakeStore()
    store.reconcile_result = AttachmentError("ATTACHMENT_RECONCILE_FAILED")
    runner, _, _, _ = _runner(store, heartbeat_writer=writes.append)
    result = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert result.status is AttachmentMaintenanceCycleStatus.PARTIAL
    assert writes == []


@pytest.mark.asyncio
async def test_failed_does_not_write_heartbeat() -> None:
    writes: list[datetime] = []
    store = _FakeStore()
    store.reconcile_result = AttachmentError("ATTACHMENT_RECONCILE_FAILED")
    store.purge_result = AttachmentError("ATTACHMENT_RECONCILE_FAILED")
    runner, _, _, _ = _runner(store, heartbeat_writer=writes.append)
    result = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert result.status is AttachmentMaintenanceCycleStatus.FAILED
    assert writes == []


@pytest.mark.asyncio
async def test_unexpected_exception_does_not_write_heartbeat() -> None:
    writes: list[datetime] = []
    store = _FakeStore()
    store.reconcile_result = RuntimeError("boom-sensitive-message")
    runner, _, _, _ = _runner(store, heartbeat_writer=writes.append)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert writes == []


@pytest.mark.asyncio
async def test_heartbeat_writer_failure_does_not_kill_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(_completed_at: datetime) -> None:
        raise RuntimeError("heartbeat-disk-full-secret")

    runner, _, _, _ = _runner(heartbeat_writer=boom)
    with caplog.at_level(logging.WARNING):
        result = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert result.status is AttachmentMaintenanceCycleStatus.SUCCESS
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        msg.startswith("attachment_maintenance_heartbeat_write_failed")
        and "error_code=RuntimeError" in msg
        for msg in messages
    )
    blob = " ".join(messages)
    assert "heartbeat-disk-full-secret" not in blob
    for item in _FORBIDDEN:
        assert item not in blob


@pytest.mark.asyncio
async def test_heartbeat_advances_across_successful_cycles() -> None:
    writes: list[datetime] = []
    runner, _, _, _ = _runner(heartbeat_writer=writes.append)
    first = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    second = await asyncio.wait_for(runner.run_once(), timeout=_TEST_TIMEOUT)
    assert first.status is AttachmentMaintenanceCycleStatus.SUCCESS
    assert second.status is AttachmentMaintenanceCycleStatus.SUCCESS
    assert len(writes) == 2
    assert writes[1] >= writes[0]
