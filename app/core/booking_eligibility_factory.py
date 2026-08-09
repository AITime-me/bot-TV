"""Factory for booking S2S clients (CURSOR-16/22/23/25).

Builds eligibility, availability, and booking-create HTTP clients from Settings
without env reads or network probes. Returns None when the integration is fully
unconfigured. All clients share BOOKING_ELIGIBILITY_* settings and the same
stdlib transport defaults.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.core.booking_availability_http import (
    BookingAvailabilityHttpClient,
    BookingAvailabilityHttpError,
)
from app.core.booking_create_http import (
    BookingCreateHttpClient,
    BookingCreateHttpError,
)
from app.core.booking_eligibility_http import (
    BookingEligibilityHttpClient,
    BookingEligibilityHttpConfig,
    BookingEligibilityHttpError,
)
from app.core.master_command_http import (
    MasterCommandHttpClient,
    MasterCommandHttpError,
)
from app.core.s2s_http_stdlib import S2sHttpStdlibTransport
from app.core.s2s_http_transport import S2sHttpTransport
from app.services.booking_eligibility_flow import BookingEligibilityFlowService
from app.services.booking_flow import BookingFlowService


@dataclass(frozen=True, slots=True)
class BookingS2sClients:
    """Paired S2S clients sharing one config and one transport instance."""

    eligibility: BookingEligibilityHttpClient | None
    availability: BookingAvailabilityHttpClient | None
    booking_create: BookingCreateHttpClient | None
    master_command: MasterCommandHttpClient | None
    transport: S2sHttpTransport | None


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


def build_booking_s2s_clients(
    settings: Settings,
    *,
    transport: S2sHttpTransport | None = None,
) -> BookingS2sClients:
    """Build eligibility + availability + create clients with one shared transport."""

    config = build_booking_s2s_config(settings)
    if config is None:
        return BookingS2sClients(
            eligibility=None,
            availability=None,
            booking_create=None,
            master_command=None,
            transport=None,
        )
    selected = _select_transport(transport)
    try:
        eligibility = BookingEligibilityHttpClient(config, selected)
        availability = BookingAvailabilityHttpClient(config, selected)
        booking_create = BookingCreateHttpClient(config, selected)
        master_command = MasterCommandHttpClient(config, selected)
    except (
        BookingEligibilityHttpError,
        BookingAvailabilityHttpError,
        BookingCreateHttpError,
        MasterCommandHttpError,
    ):
        raise ValueError("BOOKING_ELIGIBILITY configuration is invalid") from None
    return BookingS2sClients(
        eligibility=eligibility,
        availability=availability,
        booking_create=booking_create,
        master_command=master_command,
        transport=selected,
    )


def build_booking_flow_from_settings(
    settings: Settings,
    *,
    transport: S2sHttpTransport | None = None,
) -> BookingFlowService:
    """Compose BookingFlowService with shared S2S clients (no HTTP I/O)."""

    clients = build_booking_s2s_clients(settings, transport=transport)
    return BookingFlowService(
        BookingEligibilityFlowService(clients.eligibility),
        clients.availability,
        clients.booking_create,
    )


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


def build_booking_create_client(
    settings: Settings,
    *,
    transport: S2sHttpTransport | None = None,
) -> BookingCreateHttpClient | None:
    """Create the booking-create write client or return None when unset.

    Uses the same BOOKING_ELIGIBILITY_* settings as eligibility/availability.
    Never performs HTTP I/O during construction.
    """

    config = build_booking_s2s_config(settings)
    if config is None:
        return None

    try:
        return BookingCreateHttpClient(config, _select_transport(transport))
    except BookingCreateHttpError:
        raise ValueError("BOOKING_ELIGIBILITY configuration is invalid") from None


def build_master_command_client(
    settings: Settings,
    *,
    transport: S2sHttpTransport | None = None,
) -> MasterCommandHttpClient | None:
    """Create the master-command S2S client or return None when unset.

    Uses the same BOOKING_ELIGIBILITY_* settings. Never performs HTTP I/O.
    """

    config = build_booking_s2s_config(settings)
    if config is None:
        return None

    try:
        return MasterCommandHttpClient(config, _select_transport(transport))
    except MasterCommandHttpError:
        raise ValueError("BOOKING_ELIGIBILITY configuration is invalid") from None
