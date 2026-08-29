"""Live-facts S2S HTTP consumer (current business SoT, no durable cache).

Reuses ``BookingEligibilityHttpConfig`` / ``BOOKING_ELIGIBILITY_*`` and
``S2sHttpTransport``. No retries. No redirects. Never logs bearer token or
full live-facts payloads (studio phone/email redacted from diagnostics).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Mapping, NoReturn

from app.core.booking_eligibility_http import BookingEligibilityHttpConfig
from app.core.live_facts_remote import LIVE_FACTS_ROUTE_PATH
from app.core.live_facts_types import (
    LiveFactsParseError,
    LiveFactsPayloadV1,
    parse_live_facts_response_v1,
)
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransport,
    S2sHttpTransportError,
)

logger = logging.getLogger(__name__)

__all__ = (
    "LiveFactsFetchCode",
    "LiveFactsHttpClient",
    "LiveFactsHttpError",
    "LiveFactsFetchResult",
)

_ALLOWED_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "CONFIG_INVALID",
        "TRANSPORT_ERROR",
        "TIMEOUT",
        "RESPONSE_TOO_LARGE",
        "RESPONSE_INVALID",
        "AUTH_ERROR",
        "CONTRACT_ERROR",
        "UNAVAILABLE",
        "REMOTE_REJECTED",
    }
)


class LiveFactsFetchCode(StrEnum):
    OK = "OK"
    AUTH_ERROR = "AUTH_ERROR"
    CONTRACT_ERROR = "CONTRACT_ERROR"
    UNAVAILABLE = "UNAVAILABLE"
    RESPONSE_INVALID = "RESPONSE_INVALID"
    CONFIG_INVALID = "CONFIG_INVALID"


class LiveFactsHttpError(RuntimeError):
    def __init__(self, code: object) -> None:
        if type(code) is not str or code not in _ALLOWED_ERROR_CODES:
            super().__init__("CONFIG_INVALID")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "CONFIG_INVALID"

    def __repr__(self) -> str:
        return f"LiveFactsHttpError({self.code!r})"

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class LiveFactsFetchResult:
    code: LiveFactsFetchCode
    payload: LiveFactsPayloadV1 | None = None


def _log_fail(code: str) -> None:
    if code not in _ALLOWED_ERROR_CODES:
        return
    try:
        logger.info("live_facts_http_fail_closed code=%s", code)
    except Exception:
        return


def _fail(code: LiveFactsFetchCode) -> NoReturn:
    mapped = code.value if code.value in _ALLOWED_ERROR_CODES else "CONFIG_INVALID"
    _log_fail(mapped)
    raise LiveFactsHttpError(mapped) from None


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


class LiveFactsHttpClient:
    """Fetch current live-facts. One GET. No local business-facts cache."""

    def __init__(
        self,
        config: BookingEligibilityHttpConfig,
        transport: S2sHttpTransport,
    ) -> None:
        if type(config) is not BookingEligibilityHttpConfig:
            raise LiveFactsHttpError("CONFIG_INVALID") from None
        if transport is None:
            raise LiveFactsHttpError("CONFIG_INVALID") from None
        self._config = config
        self._transport = transport

    def fetch(self) -> LiveFactsFetchResult:
        return self._fetch()

    def _fetch(self) -> LiveFactsFetchResult:
        outcome = self._get_json()
        if outcome.code is not LiveFactsFetchCode.OK:
            return LiveFactsFetchResult(code=outcome.code)
        try:
            payload = parse_live_facts_response_v1(outcome.payload)
        except LiveFactsParseError:
            _log_fail("RESPONSE_INVALID")
            return LiveFactsFetchResult(code=LiveFactsFetchCode.RESPONSE_INVALID)
        return LiveFactsFetchResult(
            code=LiveFactsFetchCode.OK,
            payload=payload,
        )

    def _get_json(self) -> "_RawFetch":
        url = f"{self._config.base_url}{LIVE_FACTS_ROUTE_PATH}"
        request = S2sHttpRequest(
            method="GET",
            url=url,
            headers={
                "Authorization": f"Bearer {self._config.bearer_token}",
                "Accept": "application/json",
            },
            body=b"",
            timeout_seconds=float(self._config.timeout_seconds),
            allow_redirects=False,
            max_response_bytes=int(self._config.max_response_bytes),
        )
        try:
            response = self._transport.request(request)
        except S2sHttpTransportError as exc:
            code = exc.code
            if code == "TIMEOUT":
                _log_fail("TIMEOUT")
                return _RawFetch(code=LiveFactsFetchCode.UNAVAILABLE)
            if code == "RESPONSE_TOO_LARGE":
                _log_fail("RESPONSE_TOO_LARGE")
                return _RawFetch(code=LiveFactsFetchCode.UNAVAILABLE)
            _log_fail("TRANSPORT_ERROR")
            return _RawFetch(code=LiveFactsFetchCode.UNAVAILABLE)
        except Exception:
            _log_fail("TRANSPORT_ERROR")
            return _RawFetch(code=LiveFactsFetchCode.UNAVAILABLE)

        return self._map_response(response)

    def _map_response(self, response: S2sHttpResponse) -> "_RawFetch":
        status = response.status_code
        body = response.body if type(response.body) is bytes else b""

        if status in {401, 403}:
            _log_fail("AUTH_ERROR")
            return _RawFetch(code=LiveFactsFetchCode.AUTH_ERROR)

        if status in {404, 409}:
            _log_fail("CONTRACT_ERROR")
            return _RawFetch(code=LiveFactsFetchCode.CONTRACT_ERROR)

        if 500 <= status <= 599:
            _log_fail("UNAVAILABLE")
            return _RawFetch(code=LiveFactsFetchCode.UNAVAILABLE)

        if status != 200:
            _log_fail("REMOTE_REJECTED")
            return _RawFetch(code=LiveFactsFetchCode.UNAVAILABLE)

        content_type = _header_value(response.headers, "content-type")
        if not _content_type_is_json(content_type):
            _log_fail("RESPONSE_INVALID")
            return _RawFetch(code=LiveFactsFetchCode.RESPONSE_INVALID)

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _log_fail("RESPONSE_INVALID")
            return _RawFetch(code=LiveFactsFetchCode.RESPONSE_INVALID)
        if type(payload) is not dict:
            _log_fail("RESPONSE_INVALID")
            return _RawFetch(code=LiveFactsFetchCode.RESPONSE_INVALID)
        return _RawFetch(code=LiveFactsFetchCode.OK, payload=payload)


@dataclass(frozen=True, slots=True)
class _RawFetch:
    code: LiveFactsFetchCode
    payload: dict[str, object] | None = None
