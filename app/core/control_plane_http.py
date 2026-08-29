"""Control-plane S2S HTTP consumer (settings + knowledge publications).

Reuses ``BookingEligibilityHttpConfig`` / ``BOOKING_ELIGIBILITY_*`` and
``S2sHttpTransport``. No retries. No redirects. Never logs bearer token or
full publication payloads.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Mapping, NoReturn

from app.core.booking_eligibility_http import BookingEligibilityHttpConfig
from app.core.control_plane_remote import (
    KNOWLEDGE_ROUTE_PATH,
    SETTINGS_ROUTE_PATH,
)
from app.core.control_plane_types import (
    ControlPlaneParseError,
    KnowledgePublicationV1,
    SettingsPublicationV1,
    parse_knowledge_publication_v1,
    parse_settings_publication_v1,
)
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransport,
    S2sHttpTransportError,
)

logger = logging.getLogger(__name__)

__all__ = (
    "ControlPlaneFetchCode",
    "ControlPlaneHttpClient",
    "ControlPlaneHttpError",
    "ControlPlaneKnowledgeFetchResult",
    "ControlPlaneSettingsFetchResult",
)

_ALLOWED_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "CONFIG_INVALID",
        "TRANSPORT_ERROR",
        "TIMEOUT",
        "RESPONSE_TOO_LARGE",
        "RESPONSE_INVALID",
        "NOT_PUBLISHED",
        "INVALID",
        "AUTH_ERROR",
        "UNAVAILABLE",
        "REMOTE_REJECTED",
    }
)


class ControlPlaneFetchCode(StrEnum):
    OK = "OK"
    NOT_PUBLISHED = "NOT_PUBLISHED"
    INVALID = "INVALID"
    AUTH_ERROR = "AUTH_ERROR"
    UNAVAILABLE = "UNAVAILABLE"
    RESPONSE_INVALID = "RESPONSE_INVALID"
    CONFIG_INVALID = "CONFIG_INVALID"


class ControlPlaneHttpError(RuntimeError):
    def __init__(self, code: object) -> None:
        if type(code) is not str or code not in _ALLOWED_ERROR_CODES:
            super().__init__("CONFIG_INVALID")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "CONFIG_INVALID"

    def __repr__(self) -> str:
        return f"ControlPlaneHttpError({self.code!r})"

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class ControlPlaneSettingsFetchResult:
    code: ControlPlaneFetchCode
    publication: SettingsPublicationV1 | None = None


@dataclass(frozen=True, slots=True)
class ControlPlaneKnowledgeFetchResult:
    code: ControlPlaneFetchCode
    publication: KnowledgePublicationV1 | None = None


def _log_fail(code: str) -> None:
    if code not in _ALLOWED_ERROR_CODES:
        return
    try:
        logger.info("control_plane_http_fail_closed code=%s", code)
    except Exception:
        return


def _fail(code: ControlPlaneFetchCode) -> NoReturn:
    mapped = code.value if code.value in _ALLOWED_ERROR_CODES else "CONFIG_INVALID"
    _log_fail(mapped)
    raise ControlPlaneHttpError(mapped) from None


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


class ControlPlaneHttpClient:
    """Fetch ACTIVE settings/knowledge publications. One GET per method."""

    def __init__(
        self,
        config: BookingEligibilityHttpConfig,
        transport: S2sHttpTransport,
    ) -> None:
        if type(config) is not BookingEligibilityHttpConfig:
            raise ControlPlaneHttpError("CONFIG_INVALID") from None
        if transport is None:
            raise ControlPlaneHttpError("CONFIG_INVALID") from None
        self._config = config
        self._transport = transport

    def fetch_settings(self) -> ControlPlaneSettingsFetchResult:
        return self._fetch_settings()

    def fetch_knowledge(self) -> ControlPlaneKnowledgeFetchResult:
        return self._fetch_knowledge()

    def _fetch_settings(self) -> ControlPlaneSettingsFetchResult:
        outcome = self._get_json(SETTINGS_ROUTE_PATH)
        if outcome.code is not ControlPlaneFetchCode.OK:
            return ControlPlaneSettingsFetchResult(code=outcome.code)
        try:
            publication = parse_settings_publication_v1(outcome.payload)
        except ControlPlaneParseError:
            _log_fail("RESPONSE_INVALID")
            return ControlPlaneSettingsFetchResult(
                code=ControlPlaneFetchCode.RESPONSE_INVALID
            )
        return ControlPlaneSettingsFetchResult(
            code=ControlPlaneFetchCode.OK,
            publication=publication,
        )

    def _fetch_knowledge(self) -> ControlPlaneKnowledgeFetchResult:
        outcome = self._get_json(KNOWLEDGE_ROUTE_PATH)
        if outcome.code is not ControlPlaneFetchCode.OK:
            return ControlPlaneKnowledgeFetchResult(code=outcome.code)
        try:
            publication = parse_knowledge_publication_v1(outcome.payload)
        except ControlPlaneParseError:
            _log_fail("RESPONSE_INVALID")
            return ControlPlaneKnowledgeFetchResult(
                code=ControlPlaneFetchCode.RESPONSE_INVALID
            )
        return ControlPlaneKnowledgeFetchResult(
            code=ControlPlaneFetchCode.OK,
            publication=publication,
        )

    def _get_json(self, path: str) -> "_RawFetch":
        url = f"{self._config.base_url}{path}"
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
                return _RawFetch(code=ControlPlaneFetchCode.UNAVAILABLE)
            if code == "RESPONSE_TOO_LARGE":
                _log_fail("RESPONSE_TOO_LARGE")
                return _RawFetch(code=ControlPlaneFetchCode.UNAVAILABLE)
            _log_fail("TRANSPORT_ERROR")
            return _RawFetch(code=ControlPlaneFetchCode.UNAVAILABLE)
        except Exception:
            _log_fail("TRANSPORT_ERROR")
            return _RawFetch(code=ControlPlaneFetchCode.UNAVAILABLE)

        return self._map_response(response)

    def _map_response(self, response: S2sHttpResponse) -> "_RawFetch":
        status = response.status_code
        body = response.body if type(response.body) is bytes else b""

        if status in {401, 403}:
            _log_fail("AUTH_ERROR")
            return _RawFetch(code=ControlPlaneFetchCode.AUTH_ERROR)

        if status == 404:
            # Any 404 is fail-closed unpublished. Never stale-grace.
            _log_fail("NOT_PUBLISHED")
            return _RawFetch(code=ControlPlaneFetchCode.NOT_PUBLISHED)

        if status == 409:
            # Any 409 is fail-closed invalid. Never stale-grace.
            _log_fail("INVALID")
            return _RawFetch(code=ControlPlaneFetchCode.INVALID)

        if 500 <= status <= 599:
            _log_fail("UNAVAILABLE")
            return _RawFetch(code=ControlPlaneFetchCode.UNAVAILABLE)

        if status != 200:
            _log_fail("REMOTE_REJECTED")
            return _RawFetch(code=ControlPlaneFetchCode.UNAVAILABLE)

        content_type = _header_value(response.headers, "content-type")
        if not _content_type_is_json(content_type):
            _log_fail("RESPONSE_INVALID")
            return _RawFetch(code=ControlPlaneFetchCode.RESPONSE_INVALID)

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _log_fail("RESPONSE_INVALID")
            return _RawFetch(code=ControlPlaneFetchCode.RESPONSE_INVALID)
        if type(payload) is not dict:
            _log_fail("RESPONSE_INVALID")
            return _RawFetch(code=ControlPlaneFetchCode.RESPONSE_INVALID)
        return _RawFetch(code=ControlPlaneFetchCode.OK, payload=payload)


@dataclass(frozen=True, slots=True)
class _RawFetch:
    code: ControlPlaneFetchCode
    payload: dict[str, object] | None = None
