"""Master command HTTP adapter (CURSOR-28 / CURSOR-26 S2S).

Typed client for ``/api/internal/bot/v1/master/*``.
Reuses BookingEligibilityHttpConfig + stdlib transport. No retries. No redirects.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import Final, Mapping, NoReturn

from app.core.booking_eligibility_http import BookingEligibilityHttpConfig
from app.core.master_command_remote import (
    MASTER_BOOKINGS_ROUTE_PATH,
    MASTER_CLOSE_DAY_ROUTE_PATH,
    MASTER_CLOSE_INTERVAL_ROUTE_PATH,
    MASTER_COMMAND_REMOTE_ERROR_CODES,
    MASTER_SCHEDULE_ROUTE_PATH,
    REMOTE_ERROR_CODE_BY_STATUS,
    MasterMutationRemoteSuccess,
    MasterScheduleRemoteSuccess,
    build_close_day_request_body,
    build_close_interval_request_body,
    build_master_booking_request_body,
    build_schedule_request_body,
    parse_mutation_success_payload,
    parse_schedule_success_payload,
)
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransport,
    S2sHttpTransportError,
)

logger = logging.getLogger(__name__)

__all__ = (
    "MasterCommandHttpClient",
    "MasterCommandHttpError",
    "MasterCommandAdapterReasonCode",
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
        *MASTER_COMMAND_REMOTE_ERROR_CODES,
    }
)


class MasterCommandAdapterReasonCode(StrEnum):
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
    MASTER_NOT_FOUND = "MASTER_NOT_FOUND"
    MASTER_SCOPE_VIOLATION = "MASTER_SCOPE_VIOLATION"
    RANGE_TOO_LARGE = "RANGE_TOO_LARGE"
    APPOINTMENT_CONFLICT = "APPOINTMENT_CONFLICT"
    BLOCK_CONFLICT = "BLOCK_CONFLICT"
    BLOCK_NOT_FOUND = "BLOCK_NOT_FOUND"
    BLOCK_NOT_OWNED = "BLOCK_NOT_OWNED"
    EXTRA_WORK_NOT_FOUND = "EXTRA_WORK_NOT_FOUND"
    EXTRA_WORK_NOT_OWNED = "EXTRA_WORK_NOT_OWNED"
    EXTRA_WORK_IN_USE = "EXTRA_WORK_IN_USE"
    SLOT_INVALID = "SLOT_INVALID"
    SLOT_NO_LONGER_AVAILABLE = "SLOT_NO_LONGER_AVAILABLE"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    MASTER_UNAVAILABLE = "MASTER_UNAVAILABLE"
    SERVICE_MASTER_MISMATCH = "SERVICE_MASTER_MISMATCH"
    CLIENT_AMBIGUOUS = "CLIENT_AMBIGUOUS"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class MasterCommandHttpError(RuntimeError):
    def __init__(self, code: object) -> None:
        if type(code) is not str or code not in _ALLOWED_ADAPTER_ERROR_CODES:
            super().__init__("CONFIG_INVALID")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "CONFIG_INVALID"

    def __repr__(self) -> str:
        return f"MasterCommandHttpError({self.code!r})"

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


def _fail(code: MasterCommandAdapterReasonCode) -> NoReturn:
    _log_adapter_event("master_command_http_fail_closed", code.value)
    raise MasterCommandHttpError(code.value) from None


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
        _fail(MasterCommandAdapterReasonCode.CONFIG_INVALID)
    try:
        body = json.dumps(
            body_object,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        _fail(MasterCommandAdapterReasonCode.CONFIG_INVALID)
    if len(body) > _MAX_REQUEST_BYTES:
        _fail(MasterCommandAdapterReasonCode.REQUEST_INVALID)
    return body


def _map_error_envelope(status_code: int, body: bytes) -> MasterCommandAdapterReasonCode:
    allowed_codes = REMOTE_ERROR_CODE_BY_STATUS.get(status_code)
    if allowed_codes is None:
        return MasterCommandAdapterReasonCode.REMOTE_REJECTED
    if not body:
        return MasterCommandAdapterReasonCode.REMOTE_REJECTED
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return MasterCommandAdapterReasonCode.REMOTE_REJECTED
    if type(payload) is not dict or payload.get("ok") is not False:
        return MasterCommandAdapterReasonCode.REMOTE_REJECTED
    remote_code = payload.get("code")
    if type(remote_code) is not str or remote_code not in allowed_codes:
        return MasterCommandAdapterReasonCode.REMOTE_REJECTED
    if remote_code not in _ALLOWED_ADAPTER_ERROR_CODES:
        return MasterCommandAdapterReasonCode.REMOTE_REJECTED
    return MasterCommandAdapterReasonCode(remote_code)


class MasterCommandHttpClient:
    """S2S master-command client. Exactly one HTTP call per method."""

    def __init__(
        self,
        config: BookingEligibilityHttpConfig,
        transport: S2sHttpTransport,
    ) -> None:
        if type(config) is not BookingEligibilityHttpConfig:
            raise MasterCommandHttpError("CONFIG_INVALID") from None
        if transport is None:
            raise MasterCommandHttpError("CONFIG_INVALID") from None
        self._config = config
        self._transport = transport

    def read_schedule(
        self,
        *,
        master_id: object,
        from_date_key: object,
        to_date_key: object,
    ) -> MasterScheduleRemoteSuccess:
        try:
            body_obj = build_schedule_request_body(
                master_id=master_id,
                from_date_key=from_date_key,
                to_date_key=to_date_key,
            )
        except ValueError:
            _fail(MasterCommandAdapterReasonCode.REQUEST_INVALID)
        payload = self._post_json(MASTER_SCHEDULE_ROUTE_PATH, body_obj)
        try:
            return parse_schedule_success_payload(payload)
        except ValueError:
            _fail(MasterCommandAdapterReasonCode.RESPONSE_INVALID)

    def close_interval(
        self,
        *,
        idempotency_key: object,
        master_id: object,
        date_key: object,
        start_time: object,
        end_time: object,
        block_type: object,
    ) -> MasterMutationRemoteSuccess:
        try:
            body_obj = build_close_interval_request_body(
                idempotency_key=idempotency_key,
                master_id=master_id,
                date_key=date_key,
                start_time=start_time,
                end_time=end_time,
                block_type=block_type,
            )
        except ValueError:
            _fail(MasterCommandAdapterReasonCode.REQUEST_INVALID)
        payload = self._post_json(MASTER_CLOSE_INTERVAL_ROUTE_PATH, body_obj)
        try:
            return parse_mutation_success_payload(payload, resource_kind="block")
        except ValueError:
            _fail(MasterCommandAdapterReasonCode.RESPONSE_INVALID)

    def close_day(
        self,
        *,
        idempotency_key: object,
        master_id: object,
        date_key: object,
        block_type: object,
    ) -> MasterMutationRemoteSuccess:
        try:
            body_obj = build_close_day_request_body(
                idempotency_key=idempotency_key,
                master_id=master_id,
                date_key=date_key,
                block_type=block_type,
            )
        except ValueError:
            _fail(MasterCommandAdapterReasonCode.REQUEST_INVALID)
        payload = self._post_json(MASTER_CLOSE_DAY_ROUTE_PATH, body_obj)
        try:
            return parse_mutation_success_payload(payload, resource_kind="block")
        except ValueError:
            _fail(MasterCommandAdapterReasonCode.RESPONSE_INVALID)

    def create_booking(
        self,
        *,
        idempotency_key: object,
        master_id: object,
        slot_id: object,
        client_name: object,
        phone: object,
    ) -> MasterMutationRemoteSuccess:
        try:
            body_obj = build_master_booking_request_body(
                idempotency_key=idempotency_key,
                master_id=master_id,
                slot_id=slot_id,
                client_name=client_name,
                phone=phone,
            )
        except ValueError:
            _fail(MasterCommandAdapterReasonCode.REQUEST_INVALID)
        payload = self._post_json(MASTER_BOOKINGS_ROUTE_PATH, body_obj)
        try:
            return parse_mutation_success_payload(payload, resource_kind="booking")
        except ValueError:
            _fail(MasterCommandAdapterReasonCode.RESPONSE_INVALID)

    def _post_json(self, path: str, body_object: dict[str, object]) -> object:
        body = _encode_body(body_object)
        url = f"{self._config.base_url}{path}"
        request = S2sHttpRequest(
            method="POST",
            url=url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._config.bearer_token}",
                "Content-Type": "application/json",
            },
            body=body,
            timeout_seconds=self._config.timeout_seconds,
            max_response_bytes=self._config.max_response_bytes,
            allow_redirects=False,
        )
        try:
            response = self._transport.request(request)
        except S2sHttpTransportError as exc:
            code = getattr(exc, "code", None)
            if code == "TIMEOUT":
                _fail(MasterCommandAdapterReasonCode.TIMEOUT)
            if code == "RESPONSE_TOO_LARGE":
                _fail(MasterCommandAdapterReasonCode.RESPONSE_TOO_LARGE)
            _fail(MasterCommandAdapterReasonCode.TRANSPORT_ERROR)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail(MasterCommandAdapterReasonCode.TRANSPORT_ERROR)

        return self._parse_success_response(response)

    def _parse_success_response(self, response: S2sHttpResponse) -> object:
        if type(response) is not S2sHttpResponse:
            _fail(MasterCommandAdapterReasonCode.RESPONSE_INVALID)
        status = response.status_code
        body = response.body if type(response.body) is bytes else b""
        if status != 200:
            _fail(_map_error_envelope(status, body))
        if not _content_type_is_json(_header_value(response.headers, "Content-Type")):
            _fail(MasterCommandAdapterReasonCode.RESPONSE_INVALID)
        if not body:
            _fail(MasterCommandAdapterReasonCode.RESPONSE_INVALID)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _fail(MasterCommandAdapterReasonCode.RESPONSE_INVALID)
        return payload
