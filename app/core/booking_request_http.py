"""BookingRequest S2S HTTP adapter (TEYA_REQUEST_ORCHESTRATOR Phase 1).

Reuses BookingEligibilityHttpConfig, Bearer token, timeout, max-response, and
stdlib S2sHttpTransport. No retries. No redirects. Fail-closed on malformed JSON.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import Final, Mapping, NoReturn

from app.core.booking_eligibility_http import BookingEligibilityHttpConfig
from app.core.booking_request_remote import (
    BOOKING_REQUESTS_APPOINTMENTS_LOOKUP_PATH,
    BOOKING_REQUESTS_AVAILABILITY_PATH,
    BOOKING_REQUESTS_BOOK_PATH,
    BOOKING_REQUESTS_FEED_PATH,
    BOOKING_REQUESTS_GET_PATH,
    BOOKING_REQUEST_REMOTE_ERROR_CODES,
    REMOTE_ERROR_CODE_BY_STATUS,
    BookingRequestAppointmentsLookupResult,
    BookingRequestBookSuccess,
    BookingRequestFeedCursor,
    BookingRequestFeedPage,
    BotBookingRequestDto,
    build_appointments_lookup_body,
    build_book_request_body,
    build_feed_request_body,
    build_get_request_body,
    parse_appointments_lookup_payload,
    parse_book_success_payload,
    parse_booking_request_feed_payload,
    parse_bot_booking_request_dto,
    require_error_code_from_envelope,
)
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransport,
    S2sHttpTransportError,
)

logger = logging.getLogger(__name__)

__all__ = (
    "BookingRequestAdapterReasonCode",
    "BookingRequestHttpClient",
    "BookingRequestHttpError",
)

_MAX_REQUEST_BYTES: Final[int] = 4096

_ALLOWED_ADAPTER_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "CONFIG_INVALID",
        "REQUEST_INVALID",
        "TRANSPORT_ERROR",
        "TIMEOUT",
        "RESPONSE_TOO_LARGE",
        "RESPONSE_INVALID",
        "REMOTE_REJECTED",
        *BOOKING_REQUEST_REMOTE_ERROR_CODES,
    }
)


class BookingRequestAdapterReasonCode(StrEnum):
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
    NOT_FOUND = "NOT_FOUND"
    BOOKING_REQUEST_INVALID = "BOOKING_REQUEST_INVALID"
    BOOKING_REQUEST_CONFLICT = "BOOKING_REQUEST_CONFLICT"
    CONSULTATION_SERVICE_REQUIRED = "CONSULTATION_SERVICE_REQUIRED"
    SLOT_NO_LONGER_AVAILABLE = "SLOT_NO_LONGER_AVAILABLE"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    MASTER_UNAVAILABLE = "MASTER_UNAVAILABLE"
    SERVICE_MASTER_MISMATCH = "SERVICE_MASTER_MISMATCH"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    BOOKING_CONFLICT = "BOOKING_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class BookingRequestHttpError(RuntimeError):
    def __init__(self, code: object) -> None:
        if type(code) is not str or code not in _ALLOWED_ADAPTER_ERROR_CODES:
            super().__init__("CONFIG_INVALID")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "CONFIG_INVALID"

    def __repr__(self) -> str:
        return f"BookingRequestHttpError({self.code!r})"

    def __str__(self) -> str:
        return self.code


def _log_fail(code: str) -> None:
    if code not in _ALLOWED_ADAPTER_ERROR_CODES:
        return
    try:
        logger.info("booking_request_http_fail_closed code=%s", code)
    except Exception:
        return


def _fail(code: BookingRequestAdapterReasonCode) -> NoReturn:
    _log_fail(code.value)
    raise BookingRequestHttpError(code.value) from None


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


def _encode_body(body_object: object) -> bytes:
    if type(body_object) is not dict:
        _fail(BookingRequestAdapterReasonCode.CONFIG_INVALID)
    try:
        body = json.dumps(
            body_object, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError):
        _fail(BookingRequestAdapterReasonCode.CONFIG_INVALID)
    if len(body) > _MAX_REQUEST_BYTES:
        _fail(BookingRequestAdapterReasonCode.REQUEST_INVALID)
    return body


def _map_error(status_code: int, body: bytes) -> BookingRequestAdapterReasonCode:
    if status_code not in REMOTE_ERROR_CODE_BY_STATUS:
        return BookingRequestAdapterReasonCode.REMOTE_REJECTED
    if not body:
        return BookingRequestAdapterReasonCode.REMOTE_REJECTED
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return BookingRequestAdapterReasonCode.REMOTE_REJECTED
    if type(payload) is not dict:
        return BookingRequestAdapterReasonCode.REMOTE_REJECTED
    try:
        code = require_error_code_from_envelope(status_code, payload)
    except ValueError:
        return BookingRequestAdapterReasonCode.REMOTE_REJECTED
    return BookingRequestAdapterReasonCode(code)


class BookingRequestHttpClient:
    """S2S BookingRequest client. Exactly one HTTP call per method."""

    def __init__(
        self,
        config: BookingEligibilityHttpConfig,
        transport: S2sHttpTransport,
    ) -> None:
        if type(config) is not BookingEligibilityHttpConfig:
            raise BookingRequestHttpError("CONFIG_INVALID") from None
        if transport is None:
            raise BookingRequestHttpError("CONFIG_INVALID") from None
        self._config = config
        self._transport = transport

    def feed(
        self,
        *,
        limit: int = 20,
        cursor: BookingRequestFeedCursor | None = None,
    ) -> BookingRequestFeedPage:
        try:
            body_obj = build_feed_request_body(limit=limit, cursor=cursor)
        except ValueError:
            _fail(BookingRequestAdapterReasonCode.REQUEST_INVALID)
        payload = self._post_json(BOOKING_REQUESTS_FEED_PATH, body_obj)
        try:
            return parse_booking_request_feed_payload(payload)
        except ValueError:
            _fail(BookingRequestAdapterReasonCode.RESPONSE_INVALID)

    def get(self, *, request_id: object) -> BotBookingRequestDto:
        try:
            body_obj = build_get_request_body(request_id=request_id)
        except ValueError:
            _fail(BookingRequestAdapterReasonCode.REQUEST_INVALID)
        payload = self._post_json(BOOKING_REQUESTS_GET_PATH, body_obj)
        if type(payload) is not dict or payload.get("ok") is not True:
            _fail(BookingRequestAdapterReasonCode.RESPONSE_INVALID)
        item = payload.get("item") or payload.get("request")
        try:
            return parse_bot_booking_request_dto(item)
        except ValueError:
            _fail(BookingRequestAdapterReasonCode.RESPONSE_INVALID)

    def appointments_lookup(
        self,
        *,
        phone: object = None,
        client_id: object = None,
    ) -> BookingRequestAppointmentsLookupResult:
        try:
            body_obj = build_appointments_lookup_body(
                phone=phone, client_id=client_id
            )
        except ValueError:
            _fail(BookingRequestAdapterReasonCode.REQUEST_INVALID)
        payload = self._post_json(
            BOOKING_REQUESTS_APPOINTMENTS_LOOKUP_PATH, body_obj
        )
        try:
            return parse_appointments_lookup_payload(payload)
        except ValueError:
            _fail(BookingRequestAdapterReasonCode.RESPONSE_INVALID)

    def availability(self, *, request_id: object, date_key: object) -> dict:
        """Return raw availability payload (opaque). Fail-closed on envelope."""

        try:
            rid = build_get_request_body(request_id=request_id)["id"]
        except ValueError:
            _fail(BookingRequestAdapterReasonCode.REQUEST_INVALID)
        if type(date_key) is not str or not date_key or len(date_key) > 32:
            _fail(BookingRequestAdapterReasonCode.REQUEST_INVALID)
        body_obj: dict[str, object] = {"requestId": rid, "date": date_key}
        payload = self._post_json(BOOKING_REQUESTS_AVAILABILITY_PATH, body_obj)
        if type(payload) is not dict or payload.get("ok") is not True:
            _fail(BookingRequestAdapterReasonCode.RESPONSE_INVALID)
        return payload

    def book(
        self,
        *,
        request_id: object,
        starts_at: object,
        idempotency_key: object,
        service_id: object = None,
    ) -> BookingRequestBookSuccess:
        try:
            body_obj = build_book_request_body(
                request_id=request_id,
                starts_at=starts_at,
                idempotency_key=idempotency_key,
                service_id=service_id,
            )
        except ValueError:
            _fail(BookingRequestAdapterReasonCode.REQUEST_INVALID)
        payload = self._post_json(BOOKING_REQUESTS_BOOK_PATH, body_obj)
        try:
            return parse_book_success_payload(payload)
        except ValueError:
            _fail(BookingRequestAdapterReasonCode.RESPONSE_INVALID)

    def _post_json(self, path: str, body_object: dict[str, object]) -> object:
        body = _encode_body(body_object)
        req = S2sHttpRequest(
            method="POST",
            url=f"{self._config.base_url}{path}",
            headers={
                "Authorization": f"Bearer {self._config.bearer_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=body,
            timeout_seconds=self._config.timeout_seconds,
            allow_redirects=False,
            max_response_bytes=self._config.max_response_bytes,
        )
        try:
            response = self._transport.request(req)
        except S2sHttpTransportError as exc:
            code = str(exc)
            if code == "TIMEOUT":
                _fail(BookingRequestAdapterReasonCode.TIMEOUT)
            if code == "RESPONSE_TOO_LARGE":
                _fail(BookingRequestAdapterReasonCode.RESPONSE_TOO_LARGE)
            _fail(BookingRequestAdapterReasonCode.TRANSPORT_ERROR)
        return self._parse_success_or_raise(response)

    def _parse_success_or_raise(self, response: S2sHttpResponse) -> object:
        if not (200 <= response.status_code < 300):
            _fail(_map_error(response.status_code, response.body))
        if not _content_type_is_json(_header_value(response.headers, "content-type")):
            if response.body:
                _fail(BookingRequestAdapterReasonCode.RESPONSE_INVALID)
        if not response.body:
            _fail(BookingRequestAdapterReasonCode.RESPONSE_INVALID)
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _fail(BookingRequestAdapterReasonCode.RESPONSE_INVALID)
