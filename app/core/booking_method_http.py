"""Booking-method S2S HTTP adapter (A2.2).

Reuses BookingEligibilityHttpConfig, Bearer token, timeout, max-response, and
stdlib S2sHttpTransport. No retries. No redirects. Fail-closed on malformed JSON.

Route-specific 404:
- feed: absent/old endpoint → FEED_UNAVAILABLE (staggered deploy safe)
- context: contract NOT_FOUND → NOT_FOUND; non-contract/absent → CONTEXT_UNAVAILABLE
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import Final, Literal, Mapping, NoReturn

from app.core.booking_eligibility_http import BookingEligibilityHttpConfig
from app.core.booking_method_remote import (
    BOOKING_METHOD_CONTEXT_PATH,
    BOOKING_METHOD_FEED_PATH,
    BOOKING_METHOD_REMOTE_ERROR_CODES,
    REMOTE_ERROR_CODE_BY_STATUS,
    BookingMethodContextDto,
    BookingMethodFeedCursor,
    BookingMethodFeedPage,
    build_context_request_body,
    build_feed_request_body,
    parse_booking_method_context_payload,
    parse_booking_method_feed_payload,
)
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransport,
    S2sHttpTransportError,
)

logger = logging.getLogger(__name__)

__all__ = (
    "BookingMethodAdapterReasonCode",
    "BookingMethodHttpClient",
    "BookingMethodHttpError",
)

_MAX_REQUEST_BYTES: Final[int] = 4096

_ErrorRoute = Literal["feed", "context"]

_ALLOWED_ADAPTER_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "CONFIG_INVALID",
        "REQUEST_INVALID",
        "TRANSPORT_ERROR",
        "TIMEOUT",
        "RESPONSE_TOO_LARGE",
        "RESPONSE_INVALID",
        "REMOTE_REJECTED",
        "FEED_UNAVAILABLE",
        "CONTEXT_UNAVAILABLE",
        "AUTH_UNAVAILABLE",
        "RATE_LIMITED",
        *BOOKING_METHOD_REMOTE_ERROR_CODES,
    }
)


class BookingMethodAdapterReasonCode(StrEnum):
    CONFIG_INVALID = "CONFIG_INVALID"
    REQUEST_INVALID = "REQUEST_INVALID"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    TIMEOUT = "TIMEOUT"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    RESPONSE_INVALID = "RESPONSE_INVALID"
    REMOTE_REJECTED = "REMOTE_REJECTED"
    FEED_UNAVAILABLE = "FEED_UNAVAILABLE"
    CONTEXT_UNAVAILABLE = "CONTEXT_UNAVAILABLE"
    AUTH_UNAVAILABLE = "AUTH_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNAUTHORIZED = "UNAUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class BookingMethodHttpError(RuntimeError):
    def __init__(self, code: object) -> None:
        if type(code) is not str or code not in _ALLOWED_ADAPTER_ERROR_CODES:
            super().__init__("CONFIG_INVALID")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "CONFIG_INVALID"

    def __repr__(self) -> str:
        return f"BookingMethodHttpError({self.code!r})"

    def __str__(self) -> str:
        return self.code


def _log_fail(code: str) -> None:
    if code not in _ALLOWED_ADAPTER_ERROR_CODES:
        return
    try:
        logger.info("booking_method_http_fail_closed code=%s", code)
    except Exception:
        return


def _fail(code: BookingMethodAdapterReasonCode) -> NoReturn:
    _log_fail(code.value)
    raise BookingMethodHttpError(code.value) from None


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
        _fail(BookingMethodAdapterReasonCode.CONFIG_INVALID)
    try:
        body = json.dumps(
            body_object, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError):
        _fail(BookingMethodAdapterReasonCode.CONFIG_INVALID)
    if len(body) > _MAX_REQUEST_BYTES:
        _fail(BookingMethodAdapterReasonCode.REQUEST_INVALID)
    return body


def _envelope_code(body: bytes) -> str | None:
    if not body:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if type(payload) is not dict:
        return None
    code = payload.get("code")
    if type(code) is not str:
        return None
    return code


def _map_error(
    status_code: int, body: bytes, *, route: _ErrorRoute
) -> BookingMethodAdapterReasonCode:
    """Map HTTP failure. Feed vs context diverge on 404 semantics only."""

    if status_code == 429:
        code = _envelope_code(body)
        if code == "RATE_LIMITED" or code is None:
            return BookingMethodAdapterReasonCode.RATE_LIMITED
        allowed = REMOTE_ERROR_CODE_BY_STATUS.get(429)
        if allowed is not None and code in allowed:
            return BookingMethodAdapterReasonCode.RATE_LIMITED
        return BookingMethodAdapterReasonCode.RATE_LIMITED

    if status_code in {401, 403}:
        return BookingMethodAdapterReasonCode.AUTH_UNAVAILABLE

    if 500 <= status_code <= 599:
        return BookingMethodAdapterReasonCode.INTERNAL_ERROR

    if status_code == 404:
        code = _envelope_code(body)
        if route == "feed":
            # Old online-zapis / absent route / any 404 → safe idle.
            return BookingMethodAdapterReasonCode.FEED_UNAVAILABLE
        # context: only contract NOT_FOUND is permanent absence.
        if code == "NOT_FOUND":
            allowed = REMOTE_ERROR_CODE_BY_STATUS.get(404)
            if allowed is not None and code in allowed:
                return BookingMethodAdapterReasonCode.NOT_FOUND
        if code == "UNAVAILABLE":
            return BookingMethodAdapterReasonCode.CONTEXT_UNAVAILABLE
        # HTML/empty/non-contract 404 (endpoint missing) → retryable.
        return BookingMethodAdapterReasonCode.CONTEXT_UNAVAILABLE

    if status_code not in REMOTE_ERROR_CODE_BY_STATUS:
        return BookingMethodAdapterReasonCode.REMOTE_REJECTED

    code = _envelope_code(body)
    if code is None:
        return BookingMethodAdapterReasonCode.REMOTE_REJECTED
    allowed = REMOTE_ERROR_CODE_BY_STATUS.get(status_code)
    if allowed is None or code not in allowed:
        return BookingMethodAdapterReasonCode.REMOTE_REJECTED
    if code == "UNAVAILABLE":
        if route == "feed":
            return BookingMethodAdapterReasonCode.FEED_UNAVAILABLE
        return BookingMethodAdapterReasonCode.CONTEXT_UNAVAILABLE
    if code == "UNAUTHORIZED":
        return BookingMethodAdapterReasonCode.AUTH_UNAVAILABLE
    if code == "RATE_LIMITED":
        return BookingMethodAdapterReasonCode.RATE_LIMITED
    return BookingMethodAdapterReasonCode(code)


class BookingMethodHttpClient:
    """S2S booking-method client. Exactly one HTTP call per method."""

    def __init__(
        self,
        config: BookingEligibilityHttpConfig,
        transport: S2sHttpTransport,
    ) -> None:
        if type(config) is not BookingEligibilityHttpConfig:
            raise BookingMethodHttpError("CONFIG_INVALID") from None
        if transport is None:
            raise BookingMethodHttpError("CONFIG_INVALID") from None
        self._config = config
        self._transport = transport

    def feed(
        self,
        *,
        limit: int = 20,
        cursor: BookingMethodFeedCursor | None = None,
    ) -> BookingMethodFeedPage:
        try:
            body_obj = build_feed_request_body(limit=limit, cursor=cursor)
        except ValueError:
            _fail(BookingMethodAdapterReasonCode.REQUEST_INVALID)
        payload = self._post_json(
            BOOKING_METHOD_FEED_PATH, body_obj, error_route="feed"
        )
        try:
            return parse_booking_method_feed_payload(payload)
        except ValueError:
            _fail(BookingMethodAdapterReasonCode.RESPONSE_INVALID)

    def context(self, *, appointment_id: object) -> BookingMethodContextDto:
        try:
            body_obj = build_context_request_body(appointment_id=appointment_id)
        except ValueError:
            _fail(BookingMethodAdapterReasonCode.REQUEST_INVALID)
        payload = self._post_json(
            BOOKING_METHOD_CONTEXT_PATH, body_obj, error_route="context"
        )
        try:
            return parse_booking_method_context_payload(payload)
        except ValueError:
            _fail(BookingMethodAdapterReasonCode.RESPONSE_INVALID)

    def _post_json(
        self,
        path: str,
        body_object: dict[str, object],
        *,
        error_route: _ErrorRoute,
    ) -> object:
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
                _fail(BookingMethodAdapterReasonCode.TIMEOUT)
            if code == "RESPONSE_TOO_LARGE":
                _fail(BookingMethodAdapterReasonCode.RESPONSE_TOO_LARGE)
            _fail(BookingMethodAdapterReasonCode.TRANSPORT_ERROR)
        return self._parse_success_or_raise(response, error_route=error_route)

    def _parse_success_or_raise(
        self, response: S2sHttpResponse, *, error_route: _ErrorRoute
    ) -> object:
        if not (200 <= response.status_code < 300):
            _fail(_map_error(response.status_code, response.body, route=error_route))
        if not _content_type_is_json(_header_value(response.headers, "content-type")):
            if response.body:
                _fail(BookingMethodAdapterReasonCode.RESPONSE_INVALID)
        if not response.body:
            _fail(BookingMethodAdapterReasonCode.RESPONSE_INVALID)
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _fail(BookingMethodAdapterReasonCode.RESPONSE_INVALID)
