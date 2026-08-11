"""Booking availability HTTP adapter (CURSOR-22).

Read-only S2S client for online-zapis-tv:
- ``POST /api/internal/bot/v1/available-days``
- ``POST /api/internal/bot/v1/slots``

Reuses BookingEligibilityHttpConfig, Bearer token, timeout, max-response, and
stdlib S2sHttpTransport. No retries. No redirects. No env loading. No dialog,
pipeline, live-channel, or booking-write wiring.

Remote JSON is untrusted: contradictory / malformed / oversized payloads raise
fail-closed BookingAvailabilityHttpError and never yield invented days/slots.
Empty ``dateKeys`` / ``slots`` on a valid success body are legitimate.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Final, Mapping, NoReturn
from urllib.parse import urlsplit

from app.core.booking_availability_remote import (
    AVAILABLE_DAYS_ROUTE_PATH,
    SLOTS_ROUTE_PATH,
    AvailableDaysRemoteRequest,
    AvailableDaysResult,
    AvailableSlotsRemoteRequest,
    AvailableSlotsResult,
    require_calendar_date as _require_calendar_date,
    require_calendar_month as _require_calendar_month,
    require_canonical_booking_starts_at,
)
from app.core.booking_eligibility_http import (
    BookingEligibilityHttpConfig,
    BookingEligibilityHttpError,
    require_canonical_backend_uuid,
)
from app.core.booking_types import AvailableSlot
from app.core.manager_working_hours import MANAGER_TIMEZONE_NAME
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransport,
    S2sHttpTransportError,
)

logger = logging.getLogger(__name__)

# Re-export route paths for callers/tests; canonical definitions live in
# booking_availability_remote.
__all__ = (
    "AVAILABLE_DAYS_ROUTE_PATH",
    "SLOTS_ROUTE_PATH",
    "BookingAvailabilityAdapterReasonCode",
    "BookingAvailabilityHttpClient",
    "BookingAvailabilityHttpError",
    "parse_available_days_success_payload",
    "parse_available_slots_success_payload",
    "require_calendar_date",
    "require_calendar_month",
)

_MAX_REQUEST_BYTES: Final[int] = 4096
_MAX_DATE_KEYS: Final[int] = 31
_MAX_SLOTS: Final[int] = 288
_MAX_SLOT_ID_LENGTH: Final[int] = 128

# Studio wall-clock offset matches Asia/Yekaterinburg (permanent UTC+5).
_STUDIO_UTC_OFFSET: Final[timedelta] = timedelta(hours=5)
_STUDIO_TZ: Final[timezone] = timezone(_STUDIO_UTC_OFFSET, name=MANAGER_TIMEZONE_NAME)

_AVAILABLE_DAYS_SUCCESS_KEYS: Final[frozenset[str]] = frozenset(
    {
        "ok",
        "serviceId",
        "masterId",
        "month",
        "studioToday",
        "dateKeys",
    }
)
_SLOTS_SUCCESS_KEYS: Final[frozenset[str]] = frozenset(
    {
        "ok",
        "serviceId",
        "masterId",
        "date",
        "studioToday",
        "slots",
    }
)
_SLOT_OBJECT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "slotId",
        "serviceId",
        "masterId",
        "startsAt",
    }
)

_ALLOWED_ADAPTER_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "CONFIG_INVALID",
        "REQUEST_INVALID",
        "UNAUTHORIZED",
        "RATE_LIMITED",
        "SERVICE_UNAVAILABLE",
        "VALIDATION_ERROR",
        "TRANSPORT_ERROR",
        "TIMEOUT",
        "RESPONSE_TOO_LARGE",
        "RESPONSE_INVALID",
        "REMOTE_REJECTED",
    }
)

_REMOTE_ERROR_CODE_BY_STATUS: Final[dict[int, frozenset[str]]] = {
    400: frozenset({"VALIDATION_ERROR", "SERVICE_UNAVAILABLE"}),
    401: frozenset({"UNAUTHORIZED"}),
    413: frozenset({"PAYLOAD_TOO_LARGE"}),
    429: frozenset({"RATE_LIMITED"}),
    500: frozenset({"INTERNAL_ERROR"}),
}

_REMOTE_CODE_TO_ADAPTER: Final[dict[str, str]] = {
    "VALIDATION_ERROR": "VALIDATION_ERROR",
    "SERVICE_UNAVAILABLE": "SERVICE_UNAVAILABLE",
    "UNAUTHORIZED": "UNAUTHORIZED",
    "RATE_LIMITED": "RATE_LIMITED",
    "PAYLOAD_TOO_LARGE": "REMOTE_REJECTED",
    "INTERNAL_ERROR": "TRANSPORT_ERROR",
}


class BookingAvailabilityAdapterReasonCode(StrEnum):
    """Fixed adapter failure codes. Never embed URL, token, body, or IDs."""

    CONFIG_INVALID = "CONFIG_INVALID"
    REQUEST_INVALID = "REQUEST_INVALID"
    UNAUTHORIZED = "UNAUTHORIZED"
    RATE_LIMITED = "RATE_LIMITED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    TIMEOUT = "TIMEOUT"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    RESPONSE_INVALID = "RESPONSE_INVALID"
    REMOTE_REJECTED = "REMOTE_REJECTED"


class BookingAvailabilityHttpError(RuntimeError):
    """Fail-closed availability adapter error. Message is a fixed code only."""

    def __init__(self, code: object) -> None:
        if type(code) is not str or code not in _ALLOWED_ADAPTER_ERROR_CODES:
            super().__init__("CONFIG_INVALID")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "CONFIG_INVALID"

    def __repr__(self) -> str:
        return f"BookingAvailabilityHttpError({self.code!r})"

    def __str__(self) -> str:
        return self.code


def _log_adapter_event(event: str, code: str) -> None:
    if type(event) is not str or not event:
        return
    if type(code) is not str or code not in _ALLOWED_ADAPTER_ERROR_CODES:
        return
    try:
        logger.info("%s code=%s", event, code)
    except Exception:
        return


def _fail(code: BookingAvailabilityAdapterReasonCode) -> NoReturn:
    _log_adapter_event("booking_availability_http_fail_closed", code.value)
    raise BookingAvailabilityHttpError(code.value) from None


def _contains_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def require_calendar_month(value: object) -> str:
    """Require a real calendar month ``YYYY-MM``. Never echoes the value."""

    try:
        return _require_calendar_month(value)
    except ValueError:
        raise BookingAvailabilityHttpError("REQUEST_INVALID") from None


def require_calendar_date(value: object) -> str:
    """Require a real calendar day ``YYYY-MM-DD``. Never echoes the value."""

    try:
        return _require_calendar_date(value)
    except ValueError:
        raise BookingAvailabilityHttpError("REQUEST_INVALID") from None


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if type(key) is str and key.lower() == target:
            return value if type(value) is str else None
    return None


def _content_type_is_json(content_type: str | None) -> bool:
    if type(content_type) is not str or not content_type:
        return False
    media = content_type.split(";", 1)[0].strip().lower()
    return media == "application/json"


def _parse_starts_at(raw: object, *, expected_date: str) -> datetime | None:
    try:
        canonical = require_canonical_booking_starts_at(raw)
    except ValueError:
        return None
    # Format is fixed: YYYY-MM-DDTHH:MM:00+05:00
    day_part = canonical[0:10]
    if day_part != expected_date:
        return None
    year = int(canonical[0:4])
    month = int(canonical[5:7])
    day = int(canonical[8:10])
    hour = int(canonical[11:13])
    minute = int(canonical[14:16])
    return datetime(year, month, day, hour, minute, 0, tzinfo=_STUDIO_TZ)


def _parse_slot_object(
    raw: object,
    *,
    service_id: str,
    master_id: str,
    expected_date: str,
) -> AvailableSlot | None:
    if type(raw) is not dict:
        return None
    if set(raw) != _SLOT_OBJECT_KEYS:
        return None
    slot_id = raw.get("slotId")
    if type(slot_id) is not str or not slot_id:
        return None
    if len(slot_id) > _MAX_SLOT_ID_LENGTH:
        return None
    if any(ch.isspace() for ch in slot_id) or _contains_control_chars(slot_id):
        return None
    remote_service = raw.get("serviceId")
    remote_master = raw.get("masterId")
    if type(remote_service) is not str or type(remote_master) is not str:
        return None
    try:
        if require_canonical_backend_uuid(remote_service) != service_id:
            return None
        if require_canonical_backend_uuid(remote_master) != master_id:
            return None
    except BookingEligibilityHttpError:
        return None
    starts_at = _parse_starts_at(raw.get("startsAt"), expected_date=expected_date)
    if starts_at is None:
        return None
    try:
        return AvailableSlot(
            slot_id=slot_id,
            starts_at=starts_at,
            master_id=master_id,
            service_id=service_id,
        )
    except Exception:
        return None


def parse_available_days_success_payload(
    raw: object,
    *,
    request: AvailableDaysRemoteRequest,
) -> AvailableDaysResult | None:
    """Strict available-days success parser. Returns None on any contract violation."""

    if type(raw) is not dict:
        return None
    if set(raw) != _AVAILABLE_DAYS_SUCCESS_KEYS:
        return None
    if raw.get("ok") is not True:
        return None

    try:
        service_id = require_canonical_backend_uuid(raw.get("serviceId"))
        master_id = require_canonical_backend_uuid(raw.get("masterId"))
        month = require_calendar_month(raw.get("month"))
        studio_today = require_calendar_date(raw.get("studioToday"))
    except (BookingEligibilityHttpError, BookingAvailabilityHttpError):
        return None

    if service_id != request.service_id:
        return None
    if master_id != request.master_id:
        return None
    if month != request.month:
        return None

    date_keys_raw = raw.get("dateKeys")
    if type(date_keys_raw) is not list:
        return None
    if len(date_keys_raw) > _MAX_DATE_KEYS:
        return None

    parsed_keys: list[str] = []
    seen: set[str] = set()
    previous: str | None = None
    year_s, month_s = month.split("-", 1)
    year_i = int(year_s)
    month_i = int(month_s)
    for item in date_keys_raw:
        try:
            key = require_calendar_date(item)
        except BookingAvailabilityHttpError:
            return None
        parsed_date = date.fromisoformat(key)
        if parsed_date.year != year_i or parsed_date.month != month_i:
            return None
        if key in seen:
            return None
        if previous is not None and key <= previous:
            return None
        seen.add(key)
        previous = key
        parsed_keys.append(key)

    return AvailableDaysResult(
        service_id=service_id,
        master_id=master_id,
        month=month,
        studio_today=studio_today,
        date_keys=tuple(parsed_keys),
    )


def parse_available_slots_success_payload(
    raw: object,
    *,
    request: AvailableSlotsRemoteRequest,
) -> AvailableSlotsResult | None:
    """Strict slots success parser. Returns None on any contract violation."""

    if type(raw) is not dict:
        return None
    if set(raw) != _SLOTS_SUCCESS_KEYS:
        return None
    if raw.get("ok") is not True:
        return None

    try:
        service_id = require_canonical_backend_uuid(raw.get("serviceId"))
        master_id = require_canonical_backend_uuid(raw.get("masterId"))
        day = require_calendar_date(raw.get("date"))
        studio_today = require_calendar_date(raw.get("studioToday"))
    except (BookingEligibilityHttpError, BookingAvailabilityHttpError):
        return None

    if service_id != request.service_id:
        return None
    if master_id != request.master_id:
        return None
    if day != request.date:
        return None

    slots_raw = raw.get("slots")
    if type(slots_raw) is not list:
        return None
    if len(slots_raw) > _MAX_SLOTS:
        return None

    parsed_slots: list[AvailableSlot] = []
    seen_ids: set[str] = set()
    seen_starts: set[datetime] = set()
    previous_start: datetime | None = None
    for item in slots_raw:
        slot = _parse_slot_object(
            item,
            service_id=service_id,
            master_id=master_id,
            expected_date=day,
        )
        if slot is None:
            return None
        if slot.slot_id in seen_ids:
            return None
        if slot.starts_at in seen_starts:
            return None
        if previous_start is not None and slot.starts_at <= previous_start:
            return None
        seen_ids.add(slot.slot_id)
        seen_starts.add(slot.starts_at)
        previous_start = slot.starts_at
        parsed_slots.append(slot)

    return AvailableSlotsResult(
        service_id=service_id,
        master_id=master_id,
        date=day,
        studio_today=studio_today,
        slots=tuple(parsed_slots),
    )


def _map_error_envelope(status_code: int, body: bytes) -> BookingAvailabilityAdapterReasonCode:
    allowed_codes = _REMOTE_ERROR_CODE_BY_STATUS.get(status_code)
    if allowed_codes is None:
        return BookingAvailabilityAdapterReasonCode.REMOTE_REJECTED
    if not body:
        return BookingAvailabilityAdapterReasonCode.REMOTE_REJECTED
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return BookingAvailabilityAdapterReasonCode.REMOTE_REJECTED
    if type(payload) is not dict:
        return BookingAvailabilityAdapterReasonCode.REMOTE_REJECTED
    if payload.get("ok") is not False:
        return BookingAvailabilityAdapterReasonCode.REMOTE_REJECTED
    remote_code = payload.get("code")
    if type(remote_code) is not str or remote_code not in allowed_codes:
        return BookingAvailabilityAdapterReasonCode.REMOTE_REJECTED
    mapped = _REMOTE_CODE_TO_ADAPTER.get(remote_code)
    if mapped is None or mapped not in _ALLOWED_ADAPTER_ERROR_CODES:
        return BookingAvailabilityAdapterReasonCode.REMOTE_REJECTED
    return BookingAvailabilityAdapterReasonCode(mapped)


class BookingAvailabilityHttpClient:
    """S2S availability client over injected transport. No retries. No redirects.

    Live network reads are allowed only when bound ``Settings`` pass
    ``is_live_booking_s2s_read_allowed`` — re-checked immediately before I/O.
    Callers cannot enable live reads via a boolean flag.
    """

    def __init__(
        self,
        config: BookingEligibilityHttpConfig,
        transport: S2sHttpTransport,
        *,
        settings: object | None = None,
    ) -> None:
        if type(config) is not BookingEligibilityHttpConfig:
            raise BookingAvailabilityHttpError("CONFIG_INVALID") from None
        if transport is None:
            raise BookingAvailabilityHttpError("CONFIG_INVALID") from None
        if settings is not None:
            # Lazy import: app.config imports eligibility http (shared config) at load.
            from app.config import Settings as RuntimeSettings

            if type(settings) is not RuntimeSettings:
                raise BookingAvailabilityHttpError("CONFIG_INVALID") from None
        self._config = config
        self._transport = transport
        # None / non-allowing Settings → fail closed at every read boundary.
        self._settings = settings

    @property
    def available_days_url(self) -> str:
        return f"{self._config.base_url}{AVAILABLE_DAYS_ROUTE_PATH}"

    @property
    def available_slots_url(self) -> str:
        return f"{self._config.base_url}{SLOTS_ROUTE_PATH}"

    def get_available_days(
        self,
        *,
        service_id: object,
        master_id: object,
        month: object,
    ) -> AvailableDaysResult:
        from app.core.mode_contract import is_live_booking_s2s_read_allowed

        if not is_live_booking_s2s_read_allowed(self._settings):  # type: ignore[arg-type]
            _fail(BookingAvailabilityAdapterReasonCode.CONFIG_INVALID)
        try:
            canonical_service = require_canonical_backend_uuid(service_id)
            canonical_master = require_canonical_backend_uuid(master_id)
        except BookingEligibilityHttpError:
            _fail(BookingAvailabilityAdapterReasonCode.REQUEST_INVALID)
        try:
            canonical_month = require_calendar_month(month)
        except BookingAvailabilityHttpError:
            _fail(BookingAvailabilityAdapterReasonCode.REQUEST_INVALID)

        request = AvailableDaysRemoteRequest(
            service_id=canonical_service,
            master_id=canonical_master,
            month=canonical_month,
        )
        payload = self._post_json(
            path=AVAILABLE_DAYS_ROUTE_PATH,
            url=self.available_days_url,
            body_object=request.to_json_object(),
        )
        parsed = parse_available_days_success_payload(payload, request=request)
        if parsed is None:
            _fail(BookingAvailabilityAdapterReasonCode.RESPONSE_INVALID)
        return parsed

    def get_available_slots(
        self,
        *,
        service_id: object,
        master_id: object,
        date: object,
    ) -> AvailableSlotsResult:
        from app.core.mode_contract import is_live_booking_s2s_read_allowed

        if not is_live_booking_s2s_read_allowed(self._settings):  # type: ignore[arg-type]
            _fail(BookingAvailabilityAdapterReasonCode.CONFIG_INVALID)
        try:
            canonical_service = require_canonical_backend_uuid(service_id)
            canonical_master = require_canonical_backend_uuid(master_id)
        except BookingEligibilityHttpError:
            _fail(BookingAvailabilityAdapterReasonCode.REQUEST_INVALID)
        try:
            canonical_date = require_calendar_date(date)
        except BookingAvailabilityHttpError:
            _fail(BookingAvailabilityAdapterReasonCode.REQUEST_INVALID)

        request = AvailableSlotsRemoteRequest(
            service_id=canonical_service,
            master_id=canonical_master,
            date=canonical_date,
        )
        payload = self._post_json(
            path=SLOTS_ROUTE_PATH,
            url=self.available_slots_url,
            body_object=request.to_json_object(),
        )
        parsed = parse_available_slots_success_payload(payload, request=request)
        if parsed is None:
            _fail(BookingAvailabilityAdapterReasonCode.RESPONSE_INVALID)
        return parsed

    def _post_json(
        self,
        *,
        path: str,
        url: str,
        body_object: dict[str, object],
    ) -> object:
        try:
            token = self._config.bearer_token
            timeout = self._config.timeout_seconds
            max_bytes = self._config.max_response_bytes
            if not url.startswith(("http://", "https://")):
                _fail(BookingAvailabilityAdapterReasonCode.CONFIG_INVALID)
            if urlsplit(url).path != path:
                _fail(BookingAvailabilityAdapterReasonCode.CONFIG_INVALID)
        except BookingAvailabilityHttpError:
            raise
        except Exception:
            _fail(BookingAvailabilityAdapterReasonCode.CONFIG_INVALID)

        try:
            body = json.dumps(
                body_object,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            _fail(BookingAvailabilityAdapterReasonCode.CONFIG_INVALID)
        if len(body) > _MAX_REQUEST_BYTES:
            _fail(BookingAvailabilityAdapterReasonCode.CONFIG_INVALID)

        http_request = S2sHttpRequest(
            method="POST",
            url=url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=body,
            timeout_seconds=timeout,
            allow_redirects=False,
            max_response_bytes=max_bytes,
        )

        try:
            response = self._transport.request(http_request)
        except S2sHttpTransportError as exc:
            code = exc.code
            if code == "TIMEOUT":
                _fail(BookingAvailabilityAdapterReasonCode.TIMEOUT)
            if code == "RESPONSE_TOO_LARGE":
                _fail(BookingAvailabilityAdapterReasonCode.RESPONSE_TOO_LARGE)
            _fail(BookingAvailabilityAdapterReasonCode.TRANSPORT_ERROR)
        except Exception:
            _fail(BookingAvailabilityAdapterReasonCode.TRANSPORT_ERROR)

        return self._interpret_success_response(response, max_bytes=max_bytes)

    def _interpret_success_response(
        self,
        response: object,
        *,
        max_bytes: int,
    ) -> object:
        if type(response) is not S2sHttpResponse:
            _fail(BookingAvailabilityAdapterReasonCode.TRANSPORT_ERROR)

        if len(response.body) > max_bytes:
            _fail(BookingAvailabilityAdapterReasonCode.RESPONSE_TOO_LARGE)

        if response.status_code != 200:
            _fail(_map_error_envelope(response.status_code, response.body))

        if not _content_type_is_json(_header_value(response.headers, "Content-Type")):
            _fail(BookingAvailabilityAdapterReasonCode.RESPONSE_INVALID)

        if not response.body:
            _fail(BookingAvailabilityAdapterReasonCode.RESPONSE_INVALID)

        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _fail(BookingAvailabilityAdapterReasonCode.RESPONSE_INVALID)
