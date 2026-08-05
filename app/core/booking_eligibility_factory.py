"""Factory for booking S2S clients (CURSOR-16/22).

Builds eligibility and availability HTTP clients from Settings without env
reads or network probes. Returns None when the integration is fully
unconfigured. Both clients share BOOKING_ELIGIBILITY_* settings and the
same stdlib transport defaults.
"""

from __future__ import annotations

from app.config import Settings
from app.core.booking_availability_http import (
    BookingAvailabilityHttpClient,
    BookingAvailabilityHttpError,
)
from app.core.booking_eligibility_http import (
    BookingEligibilityHttpClient,
    BookingEligibilityHttpConfig,
    BookingEligibilityHttpError,
)
from app.core.s2s_http_stdlib import S2sHttpStdlibTransport
from app.core.s2s_http_transport import S2sHttpTransport


def build_booking_s2s_config(settings: Settings) -> BookingEligibilityHttpConfig | None:
    """Resolve shared booking S2S config or return None when unset.

    Partial or invalid configuration fails closed. Never performs HTTP I/O.
    """

    if type(settings) is not Settings:
        raise ValueError("BOOKING_ELIGIBILITY configuration is invalid") from None

    base_url = settings.booking_eligibility_base_url
    token = settings.booking_eligibility_bearer_token
    if base_url is None and token is None:
        return None
    if base_url is None or token is None:
        raise ValueError("BOOKING_ELIGIBILITY configuration is incomplete") from None

    try:
        return BookingEligibilityHttpConfig(
            base_url=base_url,
            bearer_token=token,
            timeout_seconds=settings.booking_eligibility_timeout_seconds,
            max_response_bytes=settings.booking_eligibility_max_response_bytes,
        )
    except BookingEligibilityHttpError:
        raise ValueError("BOOKING_ELIGIBILITY configuration is invalid") from None


def _select_transport(transport: S2sHttpTransport | None) -> S2sHttpTransport:
    if transport is None:
        return S2sHttpStdlibTransport()
    return transport


def build_booking_eligibility_client(
    settings: Settings,
    *,
    transport: S2sHttpTransport | None = None,
) -> BookingEligibilityHttpClient | None:
    """Create the eligibility client or return None when unset.

    Partial or invalid configuration fails closed. Never performs HTTP I/O.
    """

    config = build_booking_s2s_config(settings)
    if config is None:
        return None

    try:
        return BookingEligibilityHttpClient(config, _select_transport(transport))
    except BookingEligibilityHttpError:
        raise ValueError("BOOKING_ELIGIBILITY configuration is invalid") from None


def build_booking_availability_client(
    settings: Settings,
    *,
    transport: S2sHttpTransport | None = None,
) -> BookingAvailabilityHttpClient | None:
    """Create the availability read client or return None when unset.

    Uses the same BOOKING_ELIGIBILITY_* settings as eligibility. Never
    performs HTTP I/O during construction.
    """

    config = build_booking_s2s_config(settings)
    if config is None:
        return None

    try:
        return BookingAvailabilityHttpClient(config, _select_transport(transport))
    except BookingAvailabilityHttpError:
        raise ValueError("BOOKING_ELIGIBILITY configuration is invalid") from None
