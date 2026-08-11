"""Factory for booking S2S clients (CURSOR-16/22/23/25 + CONTRACT-MODE-01/M1).

Builds eligibility, availability, and booking-create HTTP clients from Settings
without env reads or network probes. Returns None when the integration is fully
unconfigured. All clients share BOOKING_ELIGIBILITY_* settings and the same
stdlib transport defaults.

Live eligibility/availability reads are gated by ``mode_contract`` (M1): only
AUTO_READ/AUTO_WRITE with EMERGENCY_LOCK=false may receive live-read clients.
HTTP clients also re-check the same Settings-bound policy before each network
read. Injected live HTTP clients are rebound to runtime Settings at composition
roots so DI cannot keep a permissive policy from construction time.

Booking-create / master-command construction is unchanged (separate write gates).
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
from app.core.mode_contract import is_live_booking_s2s_read_allowed
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


def rebind_eligibility_client_to_runtime_settings(
    settings: Settings,
    client: object | None,
) -> object | None:
    """Force live HTTP eligibility clients onto runtime Settings; keep test fakes."""

    if client is None:
        return None
    if isinstance(client, BookingEligibilityHttpClient):
        return BookingEligibilityHttpClient(
            client._config,  # noqa: SLF001
            client._transport,  # noqa: SLF001
            settings=settings,
        )
    return client


def rebind_availability_client_to_runtime_settings(
    settings: Settings,
    client: object | None,
) -> object | None:
    """Force live HTTP availability clients onto runtime Settings; keep test fakes."""

    if client is None:
        return None
    if isinstance(client, BookingAvailabilityHttpClient):
        return BookingAvailabilityHttpClient(
            client._config,  # noqa: SLF001
            client._transport,  # noqa: SLF001
            settings=settings,
        )
    return client


def rebind_booking_flow_to_runtime_settings(
    settings: Settings,
    booking_flow: object,
) -> object:
    """Rebind live HTTP read clients inside an injected BookingFlowService.

    Protocol/test fakes are left untouched so existing DI tests keep working.
    Identity is preserved when no live HTTP read client is present.
    """

    if not isinstance(booking_flow, BookingFlowService):
        return booking_flow

    eligibility_flow = booking_flow._eligibility_flow  # noqa: SLF001
    availability_client = booking_flow._availability_client  # noqa: SLF001
    booking_create_client = booking_flow._booking_create_client  # noqa: SLF001
    changed = False

    new_eligibility_flow = eligibility_flow
    if isinstance(eligibility_flow, BookingEligibilityFlowService):
        bound_client = rebind_eligibility_client_to_runtime_settings(
            settings,
            eligibility_flow._client,  # noqa: SLF001
        )
        if bound_client is not eligibility_flow._client:  # noqa: SLF001
            new_eligibility_flow = BookingEligibilityFlowService(
                bound_client  # type: ignore[arg-type]
            )
            changed = True

    new_availability = rebind_availability_client_to_runtime_settings(
        settings,
        availability_client,
    )
    if new_availability is not availability_client:
        changed = True

    if not changed:
        return booking_flow
    return BookingFlowService(
        new_eligibility_flow,  # type: ignore[arg-type]
        new_availability,  # type: ignore[arg-type]
        booking_create_client,  # type: ignore[arg-type]
    )


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
    live_read = is_live_booking_s2s_read_allowed(settings)
    try:
        eligibility = (
            BookingEligibilityHttpClient(config, selected, settings=settings)
            if live_read
            else None
        )
        availability = (
            BookingAvailabilityHttpClient(config, selected, settings=settings)
            if live_read
            else None
        )
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
    """Create the eligibility client or return None when unset/denied.

    Partial or invalid configuration fails closed. Mode/emergency lock gate
    (M1) applies here. Never performs HTTP I/O.
    """

    if not is_live_booking_s2s_read_allowed(settings):
        return None

    config = build_booking_s2s_config(settings)
    if config is None:
        return None

    try:
        return BookingEligibilityHttpClient(
            config, _select_transport(transport), settings=settings
        )
    except BookingEligibilityHttpError:
        raise ValueError("BOOKING_ELIGIBILITY configuration is invalid") from None


def build_booking_availability_client(
    settings: Settings,
    *,
    transport: S2sHttpTransport | None = None,
) -> BookingAvailabilityHttpClient | None:
    """Create the availability read client or return None when unset/denied.

    Uses the same BOOKING_ELIGIBILITY_* settings as eligibility. Mode/emergency
    lock gate (M1) applies here. Never performs HTTP I/O during construction.
    """

    if not is_live_booking_s2s_read_allowed(settings):
        return None

    config = build_booking_s2s_config(settings)
    if config is None:
        return None

    try:
        return BookingAvailabilityHttpClient(
            config, _select_transport(transport), settings=settings
        )
    except BookingAvailabilityHttpError:
        raise ValueError("BOOKING_ELIGIBILITY configuration is invalid") from None


def build_booking_create_client(
    settings: Settings,
    *,
    transport: S2sHttpTransport | None = None,
) -> BookingCreateHttpClient | None:
    """Create the booking-create write client or return None when unset.

    Uses the same BOOKING_ELIGIBILITY_* settings as eligibility/availability.
    Never performs HTTP I/O during construction. Write authorization remains
    with existing booking-create gates (not this M1 read policy).
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
