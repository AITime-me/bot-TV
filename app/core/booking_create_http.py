"""Booking create HTTP adapter (CURSOR-25).

Write S2S client for online-zapis-tv:
- ``POST /api/internal/bot/v1/bookings``

Reuses BookingEligibilityHttpConfig, Bearer token, timeout, max-response, and
stdlib S2sHttpTransport. No retries. No redirects. No env loading. No dialog,
pipeline, live-channel, or automatic UUID generation.

Remote JSON is untrusted: contradictory / malformed / oversized payloads raise
fail-closed BookingCreateHttpError and never yield a confirmed booking.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import Final, Mapping, NoReturn
from urllib.parse import urlsplit

from app.core.booking_create_remote import (
    BOOKINGS_ROUTE_PATH,
    BOOKING_CREATE_REMOTE_ERROR_CODES,
    REMOTE_ERROR_CODE_BY_STATUS,
    BookingCreateRemoteRequest,
    BookingCreateRemoteSuccess,
    build_booking_create_remote_request,
    parse_booking_create_success_payload,
)
from app.core.booking_eligibility_http import BookingEligibilityHttpConfig
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransport,
    S2sHttpTransportError,
)

logger = logging.getLogger(__name__)

__all__ = (
    "BOOKINGS_ROUTE_PATH",
    "MAX_BOOKING_CREATE_REQUEST_BYTES",
    "BookingCreateAdapterReasonCode",
    "BookingCreateHttpClient",
    "BookingCreateHttpError",
    "encode_booking_create_request_body",
    "parse_booking_create_success_payload",
)

_MAX_REQUEST_BYTES: Final[int] = 4096
MAX_BOOKING_CREATE_REQUEST_BYTES: Final[int] = _MAX_REQUEST_BYTES

_ALLOWED_ADAPTER_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "CONFIG_INVALID",
        "REQUEST_INVALID",
        "TRANSPORT_ERROR",
        "TIMEOUT",
        "RESPONSE_TOO_LARGE",
        "RESPONSE_INVALID",
        "REMOTE_REJECTED",
        *BOOKING_CREATE_REMOTE_ERROR_CODES,
    }
)

_REMOTE_CODE_TO_ADAPTER: Final[dict[str, str]] = {
    code: code for code in BOOKING_CREATE_REMOTE_ERROR_CODES
}


class BookingCreateAdapterReasonCode(StrEnum):
    """Fixed adapter failure codes. Never embed URL, token, body, or IDs."""

    CONFIG_INVALID = "CONFIG_INVALID"
    REQUEST_INVALID = "REQUEST_INVALID"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    TIMEOUT = "TIMEOUT"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    RESPONSE_INVALID = "RESPONSE_INVALID"
    REMOTE_REJECTED = "REMOTE_REJECTED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNAUTHORIZED = "UNAUTHORIZED"
    RATE_LIMITED = "RATE_LIMITED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
    SLOT_INVALID = "SLOT_INVALID"
    SLOT_NO_LONGER_AVAILABLE = "SLOT_NO_LONGER_AVAILABLE"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    MASTER_UNAVAILABLE = "MASTER_UNAVAILABLE"
    SERVICE_MASTER_MISMATCH = "SERVICE_MASTER_MISMATCH"
    CLIENT_AMBIGUOUS = "CLIENT_AMBIGUOUS"
    BOOKING_REQUEST_INVALID = "BOOKING_REQUEST_INVALID"
    BOOKING_REQUEST_CONFLICT = "BOOKING_REQUEST_CONFLICT"
    BOOKING_CONFLICT = "BOOKING_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class BookingCreateHttpError(RuntimeError):
    """Fail-closed booking-create adapter error. Message is a fixed code only."""

    def __init__(self, code: object) -> None:
        if type(code) is not str or code not in _ALLOWED_ADAPTER_ERROR_CODES:
            super().__init__("CONFIG_INVALID")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "CONFIG_INVALID"

    def __repr__(self) -> str:
        return f"BookingCreateHttpError({self.code!r})"

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


def _fail(code: BookingCreateAdapterReasonCode) -> NoReturn:
    _log_adapter_event("booking_create_http_fail_closed", code.value)
    raise BookingCreateHttpError(code.value) from None


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


def _map_error_envelope(status_code: int, body: bytes) -> BookingCreateAdapterReasonCode:
    allowed_codes = REMOTE_ERROR_CODE_BY_STATUS.get(status_code)
    if allowed_codes is None:
        return BookingCreateAdapterReasonCode.REMOTE_REJECTED
    if not body:
        return BookingCreateAdapterReasonCode.REMOTE_REJECTED
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return BookingCreateAdapterReasonCode.REMOTE_REJECTED
    if type(payload) is not dict:
        return BookingCreateAdapterReasonCode.REMOTE_REJECTED
    if payload.get("ok") is not False:
        return BookingCreateAdapterReasonCode.REMOTE_REJECTED
    remote_code = payload.get("code")
    if type(remote_code) is not str or remote_code not in allowed_codes:
        return BookingCreateAdapterReasonCode.REMOTE_REJECTED
    mapped = _REMOTE_CODE_TO_ADAPTER.get(remote_code)
    if mapped is None or mapped not in _ALLOWED_ADAPTER_ERROR_CODES:
        return BookingCreateAdapterReasonCode.REMOTE_REJECTED
    return BookingCreateAdapterReasonCode(mapped)


def encode_booking_create_request_body(body_object: object) -> bytes:
    """Serialize create JSON with a hard byte bound. Never logs body contents.

    Architectural note: under the public field validators (max name/phone/slot/
    idempotency sizes) a valid request cannot reach this bound. The check remains
    as defense-in-depth for the serializer path and is unit-tested directly.
    """

    if type(body_object) is not dict:
        _fail(BookingCreateAdapterReasonCode.CONFIG_INVALID)
    try:
        body = json.dumps(
            body_object,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        _fail(BookingCreateAdapterReasonCode.CONFIG_INVALID)
    if len(body) > _MAX_REQUEST_BYTES:
        _fail(BookingCreateAdapterReasonCode.REQUEST_INVALID)
    return body


class BookingCreateHttpClient:
    """S2S booking-create client over injected transport. No retries. No redirects."""

    def __init__(
        self,
        config: BookingEligibilityHttpConfig,
        transport: S2sHttpTransport,
    ) -> None:
        if type(config) is not BookingEligibilityHttpConfig:
            raise BookingCreateHttpError("CONFIG_INVALID") from None
        if transport is None:
            raise BookingCreateHttpError("CONFIG_INVALID") from None
        self._config = config
        self._transport = transport

    @property
    def bookings_url(self) -> str:
        return f"{self._config.base_url}{BOOKINGS_ROUTE_PATH}"

    def create_booking(
        self,
        *,
        idempotency_key: object,
        slot_id: object,
        client_name: object,
        phone: object,
        personal_data_consent: object,
        offer_acknowledgement: object,
    ) -> BookingCreateRemoteSuccess:
        """Perform exactly one create POST. Never generates an idempotency key."""

        try:
            request = build_booking_create_remote_request(
                idempotency_key=idempotency_key,
                slot_id=slot_id,
                client_name=client_name,
                phone=phone,
                personal_data_consent=personal_data_consent,
                offer_acknowledgement=offer_acknowledgement,
            )
        except ValueError:
            _fail(BookingCreateAdapterReasonCode.REQUEST_INVALID)

        return self._post_create(request)

    def create_booking_request(
        self, request: BookingCreateRemoteRequest
    ) -> BookingCreateRemoteSuccess:
        """Create from a pre-validated remote request. Exactly one HTTP call."""

        if type(request) is not BookingCreateRemoteRequest:
            _fail(BookingCreateAdapterReasonCode.REQUEST_INVALID)
        return self._post_create(request)

    def _post_create(
        self, request: BookingCreateRemoteRequest
    ) -> BookingCreateRemoteSuccess:
        try:
            token = self._config.bearer_token
            timeout = self._config.timeout_seconds
            max_bytes = self._config.max_response_bytes
            url = self.bookings_url
            if not url.startswith(("http://", "https://")):
                _fail(BookingCreateAdapterReasonCode.CONFIG_INVALID)
            if urlsplit(url).path != BOOKINGS_ROUTE_PATH:
                _fail(BookingCreateAdapterReasonCode.CONFIG_INVALID)
        except BookingCreateHttpError:
            raise
        except Exception:
            _fail(BookingCreateAdapterReasonCode.CONFIG_INVALID)

        try:
            body = encode_booking_create_request_body(request.to_json_object())
        except BookingCreateHttpError:
            raise
        except Exception:
            _fail(BookingCreateAdapterReasonCode.CONFIG_INVALID)

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
                _fail(BookingCreateAdapterReasonCode.TIMEOUT)
            if code == "RESPONSE_TOO_LARGE":
                _fail(BookingCreateAdapterReasonCode.RESPONSE_TOO_LARGE)
            _fail(BookingCreateAdapterReasonCode.TRANSPORT_ERROR)
        except Exception:
            _fail(BookingCreateAdapterReasonCode.TRANSPORT_ERROR)

        payload = self._interpret_success_response(response, max_bytes=max_bytes)
        parsed = parse_booking_create_success_payload(payload, request=request)
        if parsed is None:
            _fail(BookingCreateAdapterReasonCode.RESPONSE_INVALID)
        return parsed

    def _interpret_success_response(
        self,
        response: object,
        *,
        max_bytes: int,
    ) -> object:
        if type(response) is not S2sHttpResponse:
            _fail(BookingCreateAdapterReasonCode.TRANSPORT_ERROR)

        if len(response.body) > max_bytes:
            _fail(BookingCreateAdapterReasonCode.RESPONSE_TOO_LARGE)

        if response.status_code != 200:
            _fail(_map_error_envelope(response.status_code, response.body))

        if not _content_type_is_json(_header_value(response.headers, "Content-Type")):
            _fail(BookingCreateAdapterReasonCode.RESPONSE_INVALID)

        if not response.body:
            _fail(BookingCreateAdapterReasonCode.RESPONSE_INVALID)

        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _fail(BookingCreateAdapterReasonCode.RESPONSE_INVALID)
