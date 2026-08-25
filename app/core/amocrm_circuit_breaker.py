"""Durable amoCRM business-writes circuit breaker (integration-level)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

__all__ = (
    "AMOCRM_BUSINESS_WRITES_BREAKER_KEY",
    "CircuitBreakerState",
    "CircuitBreakerSnapshot",
    "CircuitBreakerPolicy",
    "ProbeClaimOutcome",
    "ProbeClaimResult",
    "load_amocrm_breaker_policy",
    "is_breaker_failure_code",
    "next_open_retry_at",
)

AMOCRM_BUSINESS_WRITES_BREAKER_KEY: Final[str] = "amocrm_business_writes"

_BREAKER_FAILURE_CODES: Final[frozenset[str]] = frozenset(
    {
        "AMOCRM_CONTACT_CREATE_TRANSIENT",
        "AMOCRM_LEAD_CREATE_TRANSIENT",
        "AMOCRM_LEAD_REANIMATE_TRANSIENT",
        "AMOCRM_NOTE_CREATE_TRANSIENT",
        "AMOCRM_NOTE_LIST_TRANSIENT",
        "AMOCRM_TASK_LIST_TRANSIENT",
        "AMOCRM_TASK_CREATE_TRANSIENT",
        "IDENTITY_TRANSIENT",
        "DEAL_TRANSIENT",
    }
)


class CircuitBreakerState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class ProbeClaimOutcome(StrEnum):
    """Result of atomic half-open probe claim."""

    ALLOWED = "ALLOWED"
    DENIED_OPEN = "DENIED_OPEN"
    DENIED_PROBE_BUSY = "DENIED_PROBE_BUSY"


@dataclass(frozen=True, slots=True)
class CircuitBreakerPolicy:
    failure_threshold: int = 5
    cooldown_seconds: float = 60.0
    half_open_successes: int = 1
    probe_lease_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class CircuitBreakerSnapshot:
    key: str
    state: CircuitBreakerState
    failure_count: int
    opened_at: datetime | None
    half_open_successes: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProbeClaimResult:
    outcome: ProbeClaimOutcome
    snapshot: CircuitBreakerSnapshot


def is_breaker_failure_code(code: str | None) -> bool:
    return type(code) is str and code in _BREAKER_FAILURE_CODES


def load_amocrm_breaker_policy(
    env: dict[str, str] | None = None,
) -> CircuitBreakerPolicy:
    source = env if env is not None else dict(os.environ)
    threshold = source.get("AMOCRM_BREAKER_FAILURE_THRESHOLD", "5")
    cooldown = source.get("AMOCRM_BREAKER_COOLDOWN_SECONDS", "60")
    half = source.get("AMOCRM_BREAKER_HALF_OPEN_SUCCESSES", "1")
    probe = source.get("AMOCRM_BREAKER_PROBE_LEASE_SECONDS", "30")
    try:
        t = int(threshold) if threshold.isdigit() else 5
    except ValueError:
        t = 5
    try:
        c = float(cooldown)
    except ValueError:
        c = 60.0
    try:
        h = int(half) if half.isdigit() else 1
    except ValueError:
        h = 1
    try:
        p = float(probe)
    except ValueError:
        p = 30.0
    return CircuitBreakerPolicy(
        failure_threshold=max(1, t),
        cooldown_seconds=max(1.0, c),
        half_open_successes=max(1, h),
        probe_lease_seconds=max(1.0, p),
    )


def next_open_retry_at(now: datetime, policy: CircuitBreakerPolicy) -> datetime:
    return now + timedelta(seconds=policy.cooldown_seconds)
