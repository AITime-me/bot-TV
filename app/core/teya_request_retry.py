"""Teya request retry taxonomy and bounded exponential backoff + jitter."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Final

__all__ = (
    "MANUAL_REVIEW_CODES",
    "RETRYABLE_REMOTE_CODES",
    "RETRYABLE_CRM_ERROR_CODES",
    "TeyaRetryPolicy",
    "classify_remote_code",
    "is_retryable_crm_error",
    "is_retryable_remote_code",
    "load_teya_retry_policy",
    "compute_next_retry_delay_seconds",
)


RETRYABLE_REMOTE_CODES: Final[frozenset[str]] = frozenset(
    {
        "RATE_LIMITED",
        "IDEMPOTENCY_IN_PROGRESS",
        "TRANSPORT_ERROR",
        "TIMEOUT",
        "INTERNAL_ERROR",
        "SERVICE_UNAVAILABLE",
    }
)

MANUAL_REVIEW_CODES: Final[frozenset[str]] = frozenset(
    {
        "IDENTITY_AMBIGUOUS",
        "ACTIVE_DEAL_AMBIGUOUS",
        "REANIMATION_AMBIGUOUS",
        "AMOCRM_NOTE_AMBIGUOUS",
        "AMOCRM_TASK_AMBIGUOUS",
        "AMOCRM_ANALYTICS_FIELD_AMBIGUOUS",
        "CRM_UNBOUND",
        "AMOCRM_CRM_REST_DISABLED",
        "AMOCRM_CRM_BUSINESS_WRITE_DISABLED",
        "AMOCRM_CRM_BUSINESS_WRITE_CONFIG_INVALID",
        "AMOCRM_CRM_BUSINESS_WRITE_IDS_INVALID",
        "AMOCRM_CRM_OAUTH_NOT_FOUND",
        "MAX_ATTEMPTS_EXCEEDED",
        "BREAKER_OPEN_EXHAUSTED",
    }
)

RETRYABLE_CRM_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "IDENTITY_TRANSIENT",
        "DEAL_TRANSIENT",
        "AMOCRM_CONTACT_CREATE_TRANSIENT",
        "AMOCRM_LEAD_CREATE_TRANSIENT",
        "AMOCRM_LEAD_REANIMATE_TRANSIENT",
        "AMOCRM_NOTE_CREATE_TRANSIENT",
        "AMOCRM_NOTE_LIST_TRANSIENT",
        "AMOCRM_TASK_LIST_TRANSIENT",
        "AMOCRM_TASK_CREATE_TRANSIENT",
        "AMOCRM_ANALYTICS_READ_TRANSIENT",
        "AMOCRM_ANALYTICS_PATCH_TRANSIENT",
        "AMOCRM_ANALYTICS_POSTCHECK_TRANSIENT",
        "AMOCRM_BREAKER_OPEN",
        "AMOCRM_BREAKER_PROBE_BUSY",
    }
)


def is_retryable_remote_code(code: str) -> bool:
    return type(code) is str and code in RETRYABLE_REMOTE_CODES


def is_retryable_crm_error(code: str | None) -> bool:
    return type(code) is str and code in RETRYABLE_CRM_ERROR_CODES


def classify_remote_code(code: str) -> str:
    """Return RETRY | MANUAL | FAIL_CLOSED for booking remote adapter codes."""

    if is_retryable_remote_code(code):
        return "RETRY"
    if code in {
        "CONSULTATION_SERVICE_REQUIRED",
        "RECONCILIATION_REQUIRED",
    }:
        return "SPECIAL"
    if code in MANUAL_REVIEW_CODES:
        return "MANUAL"
    return "FAIL_CLOSED"


@dataclass(frozen=True, slots=True)
class TeyaRetryPolicy:
    """Bounded exponential backoff with optional jitter."""

    base_seconds: float = 30.0
    max_seconds: float = 900.0
    jitter_ratio: float = 0.2
    max_attempts: int = 8

    def delay_seconds(
        self, attempt_count: int, *, rng: random.Random | None = None
    ) -> float:
        return compute_next_retry_delay_seconds(
            attempt_count=attempt_count,
            policy=self,
            rng=rng,
        )


def compute_next_retry_delay_seconds(
    *,
    attempt_count: int,
    policy: TeyaRetryPolicy,
    rng: random.Random | None = None,
) -> float:
    """Delay before next claim. attempt_count is post-claim (already bumped)."""

    exp = max(0, int(attempt_count) - 1)
    raw = min(policy.base_seconds * (2**exp), policy.max_seconds)
    if policy.jitter_ratio <= 0:
        return float(raw)
    span = raw * policy.jitter_ratio
    generator = rng if rng is not None else random.SystemRandom()
    return float(max(0.0, raw + generator.uniform(-span, span)))


def _env_float(env: dict[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < 0:
        return default
    return value


def _env_int(env: dict[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    if not raw.isdigit():
        return default
    value = int(raw)
    return value if value >= 1 else default


def load_teya_retry_policy(
    env: dict[str, str] | None = None,
) -> TeyaRetryPolicy:
    source = env if env is not None else dict(os.environ)
    base = _env_float(source, "TEYA_RETRY_BASE_SECONDS", 30.0)
    max_delay = _env_float(source, "TEYA_RETRY_MAX_SECONDS", 900.0)
    jitter = _env_float(source, "TEYA_RETRY_JITTER_RATIO", 0.2)
    attempts = _env_int(source, "TEYA_RETRY_MAX_ATTEMPTS", 8)
    if max_delay < base:
        max_delay = base
    if jitter > 1.0:
        jitter = 1.0
    return TeyaRetryPolicy(
        base_seconds=base,
        max_seconds=max_delay,
        jitter_ratio=jitter,
        max_attempts=attempts,
    )
