"""Factory for booking eligibility S2S client (CURSOR-16).

Builds BookingEligibilityHttpClient from Settings without env reads or
network probes. Returns None when the integration is fully unconfigured.
"""

from __future__ import annotations

from app.config import Settings
from app.core.booking_eligibility_http import (
    BookingEligibilityHttpClient,
    BookingEligibilityHttpConfig,
    BookingEligibilityHttpError,
)
from app.core.s2s_http_stdlib import S2sHttpStdlibTransport
from app.core.s2s_http_transport import S2sHttpTransport


def build_booking_eligibility_client(
    settings: Settings,
    *,
    transport: S2sHttpTransport | None = None,
) -> BookingEligibilityHttpClient | None:
    """Create the eligibility client or return None when unset.

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
        config = BookingEligibilityHttpConfig(
            base_url=base_url,
            bearer_token=token,
            timeout_seconds=settings.booking_eligibility_timeout_seconds,
            max_response_bytes=settings.booking_eligibility_max_response_bytes,
        )
    except BookingEligibilityHttpError:
        raise ValueError("BOOKING_ELIGIBILITY configuration is invalid") from None

    selected_transport: S2sHttpTransport
    if transport is None:
        selected_transport = S2sHttpStdlibTransport()
    else:
        selected_transport = transport

    try:
        return BookingEligibilityHttpClient(config, selected_transport)
    except BookingEligibilityHttpError:
        raise ValueError("BOOKING_ELIGIBILITY configuration is invalid") from None
