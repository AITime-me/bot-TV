"""Types for attachment spool maintenance runtime (CURSOR-13 Stage 1).

Library-only contracts. No env parsing, channels, or worker wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.core.attachment_types import (
    MAX_PURGE_BATCH,
    MAX_RECONCILE_BATCH,
    AttachmentError,
    AttachmentPurgeResult,
    AttachmentReconcileResult,
)

_MAX_INTERVAL_SECONDS = 86400
_MAX_INITIAL_DELAY_SECONDS = 86400


class AttachmentMaintenanceCycleStatus(StrEnum):
    """Completed-cycle outcomes for status and (subset) public results."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


def _require_exact_int(name: str, value: object) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer") from None
    return value


def _require_int_range(name: str, value: object, *, minimum: int, maximum: int) -> int:
    parsed = _require_exact_int(name, value)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}") from None
    return parsed


def _require_exact_bool(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean") from None
    return value


def _require_utc_datetime(name: str, value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime") from None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware UTC") from None
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC") from None
    return value


def _require_optional_utc_datetime(name: str, value: object) -> datetime | None:
    if value is None:
        return None
    return _require_utc_datetime(name, value)


def _require_operational_error_code(name: str, value: object) -> str:
    if type(value) is not str or isinstance(value, bool) or value == "":
        raise ValueError(f"{name} must be a non-empty AttachmentError code") from None
    probe = AttachmentError(value)
    if probe.code != value:
        raise ValueError(f"{name} must be a non-empty AttachmentError code") from None
    return value


def _require_optional_operational_error_code(name: str, value: object) -> str | None:
    if value is None:
        return None
    return _require_operational_error_code(name, value)


@dataclass(frozen=True, slots=True, repr=False)
class AttachmentMaintenanceConfig:
    """Constructor-injected maintenance pacing and batch limits."""

    interval_seconds: int
    reconcile_limit: int
    purge_limit: int
    initial_delay_seconds: int = 0

    def __post_init__(self) -> None:
        _require_int_range(
            "interval_seconds",
            self.interval_seconds,
            minimum=1,
            maximum=_MAX_INTERVAL_SECONDS,
        )
        _require_int_range(
            "reconcile_limit",
            self.reconcile_limit,
            minimum=1,
            maximum=MAX_RECONCILE_BATCH,
        )
        _require_int_range(
            "purge_limit",
            self.purge_limit,
            minimum=1,
            maximum=MAX_PURGE_BATCH,
        )
        _require_int_range(
            "initial_delay_seconds",
            self.initial_delay_seconds,
            minimum=0,
            maximum=_MAX_INITIAL_DELAY_SECONDS,
        )

    def __repr__(self) -> str:
        return "AttachmentMaintenanceConfig(redacted)"

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


@dataclass(frozen=True, slots=True, repr=False)
class AttachmentMaintenanceCycleResult:
    """Public one-cycle outcome. CANCELLED/INTERNAL_ERROR are not allowed."""

    status: AttachmentMaintenanceCycleStatus
    reconcile: AttachmentReconcileResult | None
    purge: AttachmentPurgeResult | None
    reconcile_error_code: str | None
    purge_error_code: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, AttachmentMaintenanceCycleStatus):
            raise ValueError("status must be AttachmentMaintenanceCycleStatus") from None
        if self.status in {
            AttachmentMaintenanceCycleStatus.CANCELLED,
            AttachmentMaintenanceCycleStatus.INTERNAL_ERROR,
        }:
            raise ValueError(
                "cycle result status must be success, partial, or failed"
            ) from None

        if self.reconcile is not None and not isinstance(
            self.reconcile, AttachmentReconcileResult
        ):
            raise ValueError("reconcile must be AttachmentReconcileResult") from None
        if self.purge is not None and not isinstance(self.purge, AttachmentPurgeResult):
            raise ValueError("purge must be AttachmentPurgeResult") from None

        reconcile_error = _require_optional_operational_error_code(
            "reconcile_error_code",
            self.reconcile_error_code,
        )
        purge_error = _require_optional_operational_error_code(
            "purge_error_code",
            self.purge_error_code,
        )

        if self.status is AttachmentMaintenanceCycleStatus.SUCCESS:
            if self.reconcile is None or self.purge is None:
                raise ValueError("success requires reconcile and purge results") from None
            if reconcile_error is not None or purge_error is not None:
                raise ValueError("success forbids error codes") from None
            return

        if self.status is AttachmentMaintenanceCycleStatus.FAILED:
            if self.reconcile is not None or self.purge is not None:
                raise ValueError("failed forbids operation results") from None
            if reconcile_error is None or purge_error is None:
                raise ValueError("failed requires both error codes") from None
            return

        # PARTIAL
        reconcile_ok = self.reconcile is not None
        purge_ok = self.purge is not None
        if reconcile_ok == purge_ok:
            raise ValueError("partial requires exactly one operation result") from None
        if reconcile_ok:
            if reconcile_error is not None:
                raise ValueError("partial success side forbids error code") from None
            if purge_error is None:
                raise ValueError("partial requires failed-side error code") from None
        else:
            if purge_error is not None:
                raise ValueError("partial success side forbids error code") from None
            if reconcile_error is None:
                raise ValueError("partial requires failed-side error code") from None

    def __repr__(self) -> str:
        return "AttachmentMaintenanceCycleResult(redacted)"

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


@dataclass(frozen=True, slots=True, repr=False)
class AttachmentMaintenanceStatus:
    """Immutable internal runtime snapshot with exact lifecycle invariants."""

    loop_running: bool
    cycle_running: bool
    last_cycle_started_at: datetime | None
    last_cycle_finished_at: datetime | None
    last_success_at: datetime | None
    last_cycle_status: AttachmentMaintenanceCycleStatus | None
    consecutive_unsuccessful_cycles: int
    last_reconcile_error_code: str | None
    last_purge_error_code: str | None

    def __post_init__(self) -> None:
        _require_exact_bool("loop_running", self.loop_running)
        _require_exact_bool("cycle_running", self.cycle_running)
        consecutive = _require_exact_int(
            "consecutive_unsuccessful_cycles",
            self.consecutive_unsuccessful_cycles,
        )
        if consecutive < 0:
            raise ValueError(
                "consecutive_unsuccessful_cycles must be non-negative"
            ) from None

        started = _require_optional_utc_datetime(
            "last_cycle_started_at",
            self.last_cycle_started_at,
        )
        finished = _require_optional_utc_datetime(
            "last_cycle_finished_at",
            self.last_cycle_finished_at,
        )
        _require_optional_utc_datetime("last_success_at", self.last_success_at)

        if self.last_cycle_status is not None and not isinstance(
            self.last_cycle_status,
            AttachmentMaintenanceCycleStatus,
        ):
            raise ValueError(
                "last_cycle_status must be AttachmentMaintenanceCycleStatus"
            ) from None

        reconcile_error = _require_optional_operational_error_code(
            "last_reconcile_error_code",
            self.last_reconcile_error_code,
        )
        purge_error = _require_optional_operational_error_code(
            "last_purge_error_code",
            self.last_purge_error_code,
        )

        if self.cycle_running:
            if started is None:
                raise ValueError("cycle_running requires last_cycle_started_at") from None
            if finished is not None:
                raise ValueError("active cycle forbids last_cycle_finished_at") from None
            if self.last_cycle_status is not None:
                raise ValueError("active cycle forbids last_cycle_status") from None
            if reconcile_error is not None or purge_error is not None:
                raise ValueError("active cycle forbids operational error codes") from None
            return

        if self.last_cycle_status is None:
            if started is not None or finished is not None:
                raise ValueError("idle status forbids cycle timestamps") from None
            if reconcile_error is not None or purge_error is not None:
                raise ValueError("idle status forbids operational error codes") from None
            return

        if started is None or finished is None:
            raise ValueError("completed cycle requires started_at and finished_at") from None
        if finished < started:
            raise ValueError("finished_at must not precede started_at") from None

        status = self.last_cycle_status
        if status is AttachmentMaintenanceCycleStatus.SUCCESS:
            if reconcile_error is not None or purge_error is not None:
                raise ValueError("success status forbids error codes") from None
            return
        if status is AttachmentMaintenanceCycleStatus.PARTIAL:
            has_reconcile = reconcile_error is not None
            has_purge = purge_error is not None
            if has_reconcile == has_purge:
                raise ValueError(
                    "partial status requires exactly one operational error code"
                ) from None
            return
        if status is AttachmentMaintenanceCycleStatus.FAILED:
            if reconcile_error is None or purge_error is None:
                raise ValueError("failed status requires both error codes") from None
            return
        if status is AttachmentMaintenanceCycleStatus.CANCELLED:
            if reconcile_error is not None or purge_error is not None:
                raise ValueError("cancelled status forbids error codes") from None
            return
        if status is AttachmentMaintenanceCycleStatus.INTERNAL_ERROR:
            if reconcile_error is not None or purge_error is not None:
                raise ValueError("internal_error status forbids error codes") from None
            return

    def __repr__(self) -> str:
        return "AttachmentMaintenanceStatus(redacted)"

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        return self.__repr__()


def idle_maintenance_status() -> AttachmentMaintenanceStatus:
    """Initial idle snapshot before any loop or cycle."""

    return AttachmentMaintenanceStatus(
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
