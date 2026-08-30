"""Shared online-zapis-tv botInternal rate-limit mapping for worker loops.

The production botInternal bucket is 120 requests / 60s across all S2S
paths (feeds, control-plane GETs, live-facts, on-demand booking calls).

Idle worker ticks must stay well below that budget. Expected HTTP 429
``RATE_LIMITED`` is transient: fail-closed (no outbound, no cursor advance),
never WorkerRuntimeFatal.
"""

from __future__ import annotations

from typing import Final, Protocol

from app.core.acquisition_source_http import AcquisitionSourceHttpError
from app.core.booking_method_http import BookingMethodHttpError
from app.core.booking_request_http import BookingRequestHttpError
from app.core.control_plane_remote import CONTROL_PLANE_S2S_GETS_PER_REFRESH

RATE_LIMITED_CODE: Final[str] = "RATE_LIMITED"
# online-zapis-tv ``botInternal`` policy (windowMs=60000, maxRequests=120).
OZ_BOT_INTERNAL_MAX_PER_MINUTE: Final[int] = 120
# Idle polling must keep at least 4x headroom for on-demand S2S.
IDLE_S2S_HEADROOM: Final[int] = 4

# Idle HTTP calls per tick (empty queues / no pending recon rows).
IDLE_TEYA_FEED_REQUESTS: Final[int] = 1
IDLE_TEYA_RECON_REQUESTS: Final[int] = 0
IDLE_BOOKING_METHOD_FEED_REQUESTS: Final[int] = 1
IDLE_ACQUISITION_SOURCE_FEED_REQUESTS: Final[int] = 1

_RATE_LIMIT_ERRORS: Final[tuple[type[BaseException], ...]] = (
    BookingRequestHttpError,
    BookingMethodHttpError,
    AcquisitionSourceHttpError,
)


class IdleS2sPollSettings(Protocol):
    teya_request_poll_seconds: int
    teya_request_reconciliation_poll_seconds: int
    booking_method_analytics_poll_seconds: int
    acquisition_source_analytics_poll_seconds: int
    control_plane_refresh_seconds: int


def is_expected_s2s_rate_limited(exc: BaseException) -> bool:
    if not isinstance(exc, _RATE_LIMIT_ERRORS):
        return False
    code = getattr(exc, "code", None)
    return code == RATE_LIMITED_CODE


def idle_s2s_requests_per_minute(settings: IdleS2sPollSettings) -> float:
    """Regular idle S2S polling only. On-demand business calls are excluded."""

    teya = 60.0 / float(settings.teya_request_poll_seconds)
    recon = (
        60.0 / float(settings.teya_request_reconciliation_poll_seconds)
        * IDLE_TEYA_RECON_REQUESTS
    )
    booking_method = 60.0 / float(
        settings.booking_method_analytics_poll_seconds
    )
    acquisition = 60.0 / float(
        settings.acquisition_source_analytics_poll_seconds
    )
    control_plane = (
        60.0
        / float(settings.control_plane_refresh_seconds)
        * CONTROL_PLANE_S2S_GETS_PER_REFRESH
    )
    return (
        teya * IDLE_TEYA_FEED_REQUESTS
        + recon
        + booking_method * IDLE_BOOKING_METHOD_FEED_REQUESTS
        + acquisition * IDLE_ACQUISITION_SOURCE_FEED_REQUESTS
        + control_plane
    )


def idle_s2s_budget_ok(settings: IdleS2sPollSettings) -> bool:
    return (
        idle_s2s_requests_per_minute(settings) * IDLE_S2S_HEADROOM
        <= OZ_BOT_INTERNAL_MAX_PER_MINUTE
    )
