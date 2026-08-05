"""Booking eligibility HTTP adapter foundation (CURSOR-15).

Typed client for POST /api/internal/bot/v1/eligibility only.
No env loading, no dialog/pipeline wiring. Backend IDs are canonical UUIDs.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Mapping
from urllib.parse import urlsplit, urlunsplit

from app.core.booking_dialog_policy import normalize_eligibility_outcome
from app.core.booking_eligibility_remote import (
    EligibilityRemoteAlternativeMaster,
    EligibilityRemoteOutcome,
    EligibilityRemoteRequest,
    EligibilityRemoteSuccess,
)
from app.core.booking_types import (
    BookingEligibilityOutcome,
    BookingEligibilityResult,
    SelectedMaster,
    SelectedService,
)
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransport,
    S2sHttpTransportError,
)

logger = logging.getLogger(__name__)

ELIGIBILITY_ROUTE_PATH: Final[str] = "/api/internal/bot/v1/eligibility"
_MIN_TOKEN_LENGTH: Final[int] = 32
_MAX_PUBLIC_NAME_LENGTH: Final[int] = 256
_MAX_REQUEST_BYTES: Final[int] = 4096
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0
_DEFAULT_MAX_RESPONSE_BYTES: Final[int] = 65_536
DEFAULT_TIMEOUT_SECONDS: Final[float] = _DEFAULT_TIMEOUT_SECONDS
DEFAULT_MAX_RESPONSE_BYTES: Final[int] = _DEFAULT_MAX_RESPONSE_BYTES
_ABSENT: Final[object] = object()
_CANONICAL_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

_KNOWN_BACKEND_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        "STUDIO_ONLINE_DISABLED",
        "SERVICE_INACTIVE",
        "MASTER_INACTIVE",
        "ONLINE_DISABLED",
        "MASTER_SERVICE_UNAVAILABLE",
        "MANAGER_ONLY",
    }
)

_ALLOWED_ADAPTER_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "CONFIG_INVALID",
        "TRANSPORT_ERROR",
        "TIMEOUT",
        "REMOTE_REJECTED",
        "RESPONSE_TOO_LARGE",
        "RESPONSE_INVALID",
    }
)


class BookingEligibilityAdapterReasonCode(StrEnum):
    """Fixed adapter failure codes. Never embed URL, token, body, or IDs."""

    CONFIG_INVALID = "CONFIG_INVALID"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    TIMEOUT = "TIMEOUT"
    REMOTE_REJECTED = "REMOTE_REJECTED"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    RESPONSE_INVALID = "RESPONSE_INVALID"


class BookingEligibilityHttpError(RuntimeError):
    """Fail-closed adapter config/construction error. Message is a fixed code only."""

    def __init__(self, code: object) -> None:
        if type(code) is not str or code not in _ALLOWED_ADAPTER_ERROR_CODES:
            super().__init__("CONFIG_INVALID")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "CONFIG_INVALID"

    def __repr__(self) -> str:
        return f"BookingEligibilityHttpError({self.code!r})"

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


def _contains_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def require_canonical_backend_uuid(value: object) -> str:
    """Normalize a backend service/master ID to canonical lowercase UUID.

    Rejects braces, URN prefixes, missing hyphens, and non-UUID strings.
    Raises BookingEligibilityHttpError(CONFIG_INVALID) without echoing the value.
    """

    if type(value) is not str or not value:
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    if any(ch.isspace() for ch in value) or _contains_control_chars(value):
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    if value.startswith("{") or value.lower().startswith("urn:"):
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    lowered = value.lower()
    if not _CANONICAL_UUID_RE.fullmatch(lowered):
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    try:
        parsed = uuid.UUID(lowered)
    except ValueError:
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    canonical = str(parsed)
    if canonical != lowered:
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    return canonical


def _is_canonical_uuid(value: object) -> bool:
    try:
        require_canonical_backend_uuid(value)
    except BookingEligibilityHttpError:
        return False
    return True


def _validate_base_url(raw: object) -> str:
    if type(raw) is not str or not raw or any(ch.isspace() for ch in raw):
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    if _contains_control_chars(raw) or "\\" in raw:
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    if not parts.hostname:
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    if parts.username is not None or parts.password is not None:
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    if parts.query or parts.fragment:
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    path = parts.path if parts.path else ""
    if path not in ("", "/"):
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    host = parts.hostname
    if not host or any(ch.isspace() for ch in host) or _contains_control_chars(host):
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    try:
        port = parts.port
    except ValueError:
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    if port is not None and (port <= 0 or port > 65535):
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]" if port is None else f"[{host}]:{port}"
    else:
        netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, "", "", ""))


def _validate_bearer_token(raw: object) -> str:
    if type(raw) is not str:
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    if not raw or len(raw) < _MIN_TOKEN_LENGTH:
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    if any(ch.isspace() for ch in raw) or _contains_control_chars(raw):
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    return raw


def _validate_timeout(raw: object) -> float:
    if type(raw) is not float and type(raw) is not int:
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    value = float(raw)
    if value != value or value <= 0.0 or value > 120.0:
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    return value


def _validate_max_response_bytes(raw: object) -> int:
    if type(raw) is not int or isinstance(raw, bool):
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    if raw <= 0 or raw > 1_000_000:
        raise BookingEligibilityHttpError("CONFIG_INVALID") from None
    return raw


@dataclass(frozen=True, slots=True, repr=False)
class BookingEligibilityHttpConfig:
    """Explicit constructor config. No env reading."""

    base_url: str
    bearer_token: str
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _validate_base_url(self.base_url))
        object.__setattr__(self, "bearer_token", _validate_bearer_token(self.bearer_token))
        object.__setattr__(
            self, "timeout_seconds", _validate_timeout(self.timeout_seconds)
        )
        object.__setattr__(
            self,
            "max_response_bytes",
            _validate_max_response_bytes(self.max_response_bytes),
        )

    @property
    def eligibility_url(self) -> str:
        return f"{self.base_url}{ELIGIBILITY_ROUTE_PATH}"

    def __repr__(self) -> str:
        return (
            "BookingEligibilityHttpConfig("
            "base_url=<redacted>, "
            "bearer_token=<redacted>, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"max_response_bytes={self.max_response_bytes!r})"
        )


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


def _parse_reason_code(value: object) -> str | None | object:
    if value is None:
        return None
    if type(value) is not str or value not in _KNOWN_BACKEND_REASON_CODES:
        return object()
    return value


def _parse_alternative_master(raw: object) -> EligibilityRemoteAlternativeMaster | None:
    if type(raw) is not dict:
        return None
    master_id = raw.get("id")
    public_name = raw.get("publicName")
    if not _is_canonical_uuid(master_id):
        return None
    assert type(master_id) is str
    canonical_id = master_id.lower()
    if type(public_name) is not str or not public_name:
        return None
    if len(public_name) > _MAX_PUBLIC_NAME_LENGTH:
        return None
    if _contains_control_chars(public_name):
        return None
    return EligibilityRemoteAlternativeMaster(id=canonical_id, public_name=public_name)


def parse_eligibility_success_payload(raw: object) -> EligibilityRemoteSuccess | None:
    """Strict success JSON object parser. Returns None on any contract violation."""

    if type(raw) is not dict:
        return None
    if raw.get("ok") is not True:
        return None

    outcome_raw = raw.get("outcome")
    if type(outcome_raw) is not str:
        return None
    try:
        outcome = EligibilityRemoteOutcome(outcome_raw)
    except ValueError:
        return None

    reason = _parse_reason_code(raw.get("reasonCode"))
    if type(reason) is not str and reason is not None:
        return None

    selected_pair = raw.get("selectedPairAllowed")
    if selected_pair is not None and type(selected_pair) is not bool:
        return None

    service_online = raw.get("serviceOnlineInGeneral")
    if type(service_online) is not bool:
        return None

    count = raw.get("otherOnlineMasterCount")
    if type(count) is not int or isinstance(count, bool) or count < 0:
        return None

    alternatives_raw = raw.get("otherOnlineMasters", _ABSENT)
    alternatives: tuple[EligibilityRemoteAlternativeMaster, ...] | None
    if alternatives_raw is _ABSENT:
        alternatives = None
    elif alternatives_raw is None:
        return None
    elif type(alternatives_raw) is not list:
        return None
    else:
        parsed: list[EligibilityRemoteAlternativeMaster] = []
        seen_ids: set[str] = set()
        for item in alternatives_raw:
            master = _parse_alternative_master(item)
            if master is None:
                return None
            if master.id in seen_ids:
                return None
            seen_ids.add(master.id)
            parsed.append(master)
        alternatives = tuple(parsed)
        if count != len(alternatives):
            return None

    return EligibilityRemoteSuccess(
        outcome=outcome,
        reason_code=reason if type(reason) is str else None,
        selected_pair_allowed=selected_pair if type(selected_pair) is bool else None,
        service_online_in_general=service_online,
        other_online_master_count=count,
        other_online_masters=alternatives,
    )


def _unavailable(
    *,
    selected_service: SelectedService,
    selected_master: SelectedMaster | None,
    reason: BookingEligibilityAdapterReasonCode,
) -> BookingEligibilityResult:
    _log_adapter_event("booking_eligibility_http_fail_closed", reason.value)
    return normalize_eligibility_outcome(
        BookingEligibilityOutcome.SERVICE_UNAVAILABLE,
        selected_service=selected_service,
        selected_master=selected_master,
        other_online_master_ids=(),
        internal_reason_code=reason.value,
    )


def _map_success_to_domain(
    *,
    remote: EligibilityRemoteSuccess,
    selected_service: SelectedService,
    selected_master: SelectedMaster | None,
    include_alternatives: bool,
) -> BookingEligibilityResult:
    master_present = selected_master is not None

    if remote.outcome is EligibilityRemoteOutcome.SELF_BOOKING_ALLOWED:
        if remote.reason_code is not None:
            return _unavailable(
                selected_service=selected_service,
                selected_master=selected_master,
                reason=BookingEligibilityAdapterReasonCode.RESPONSE_INVALID,
            )
        if remote.service_online_in_general is not True:
            return _unavailable(
                selected_service=selected_service,
                selected_master=selected_master,
                reason=BookingEligibilityAdapterReasonCode.RESPONSE_INVALID,
            )
        if master_present:
            if remote.selected_pair_allowed is not True:
                return _unavailable(
                    selected_service=selected_service,
                    selected_master=selected_master,
                    reason=BookingEligibilityAdapterReasonCode.RESPONSE_INVALID,
                )
        elif remote.selected_pair_allowed is not None:
            return _unavailable(
                selected_service=selected_service,
                selected_master=selected_master,
                reason=BookingEligibilityAdapterReasonCode.RESPONSE_INVALID,
            )
        domain_outcome: object = BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    elif remote.outcome is EligibilityRemoteOutcome.MANAGER_HANDOFF:
        if remote.reason_code is None:
            return _unavailable(
                selected_service=selected_service,
                selected_master=selected_master,
                reason=BookingEligibilityAdapterReasonCode.RESPONSE_INVALID,
            )
        if master_present:
            if remote.selected_pair_allowed is not False:
                return _unavailable(
                    selected_service=selected_service,
                    selected_master=selected_master,
                    reason=BookingEligibilityAdapterReasonCode.RESPONSE_INVALID,
                )
        elif remote.selected_pair_allowed is not None:
            return _unavailable(
                selected_service=selected_service,
                selected_master=selected_master,
                reason=BookingEligibilityAdapterReasonCode.RESPONSE_INVALID,
            )
        domain_outcome = BookingEligibilityOutcome.MANAGER_HANDOFF
    else:
        return _unavailable(
            selected_service=selected_service,
            selected_master=selected_master,
            reason=BookingEligibilityAdapterReasonCode.RESPONSE_INVALID,
        )

    if include_alternatives:
        if remote.other_online_masters is None:
            return _unavailable(
                selected_service=selected_service,
                selected_master=selected_master,
                reason=BookingEligibilityAdapterReasonCode.RESPONSE_INVALID,
            )
        selected_id = None
        if selected_master is not None:
            try:
                selected_id = require_canonical_backend_uuid(selected_master.master_id)
            except BookingEligibilityHttpError:
                return _unavailable(
                    selected_service=selected_service,
                    selected_master=selected_master,
                    reason=BookingEligibilityAdapterReasonCode.RESPONSE_INVALID,
                )
        alternative_ids: list[str] = []
        for master in remote.other_online_masters:
            if selected_id is not None and master.id == selected_id:
                return _unavailable(
                    selected_service=selected_service,
                    selected_master=selected_master,
                    reason=BookingEligibilityAdapterReasonCode.RESPONSE_INVALID,
                )
            alternative_ids.append(master.id)
        other_ids = tuple(alternative_ids)
    else:
        if remote.other_online_masters is not None:
            return _unavailable(
                selected_service=selected_service,
                selected_master=selected_master,
                reason=BookingEligibilityAdapterReasonCode.RESPONSE_INVALID,
            )
        other_ids = ()

    return normalize_eligibility_outcome(
        domain_outcome,
        selected_service=selected_service,
        selected_master=selected_master,
        other_online_master_ids=other_ids,
        internal_reason_code=remote.reason_code,
    )


class BookingEligibilityHttpClient:
    """S2S eligibility client over an injected transport. No retries. No redirects."""

    def __init__(
        self,
        config: BookingEligibilityHttpConfig,
        transport: S2sHttpTransport,
    ) -> None:
        if type(config) is not BookingEligibilityHttpConfig:
            raise BookingEligibilityHttpError("CONFIG_INVALID") from None
        if transport is None:
            raise BookingEligibilityHttpError("CONFIG_INVALID") from None
        self._config = config
        self._transport = transport

    def check_eligibility(
        self,
        service: SelectedService,
        master: SelectedMaster | None = None,
        *,
        include_alternatives: bool = True,
    ) -> BookingEligibilityResult:
        if type(service) is not SelectedService:
            raise BookingEligibilityHttpError("CONFIG_INVALID") from None
        if master is not None and type(master) is not SelectedMaster:
            raise BookingEligibilityHttpError("CONFIG_INVALID") from None
        if type(include_alternatives) is not bool:
            raise BookingEligibilityHttpError("CONFIG_INVALID") from None

        # Backend-specific UUID boundary — raise before any network I/O.
        service_id = require_canonical_backend_uuid(service.service_id)
        master_id = (
            require_canonical_backend_uuid(master.master_id) if master is not None else None
        )

        try:
            token = _validate_bearer_token(self._config.bearer_token)
            timeout = _validate_timeout(self._config.timeout_seconds)
            max_bytes = _validate_max_response_bytes(self._config.max_response_bytes)
            url = self._config.eligibility_url
            if not url.startswith(("http://", "https://")):
                raise BookingEligibilityHttpError("CONFIG_INVALID") from None
            if urlsplit(url).path != ELIGIBILITY_ROUTE_PATH:
                raise BookingEligibilityHttpError("CONFIG_INVALID") from None
        except BookingEligibilityHttpError:
            return _unavailable(
                selected_service=service,
                selected_master=master,
                reason=BookingEligibilityAdapterReasonCode.CONFIG_INVALID,
            )

        remote_request = EligibilityRemoteRequest(
            service_id=service_id,
            master_id=master_id,
            include_alternatives=include_alternatives,
        )
        try:
            body = json.dumps(
                remote_request.to_json_object(),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            return _unavailable(
                selected_service=service,
                selected_master=master,
                reason=BookingEligibilityAdapterReasonCode.CONFIG_INVALID,
            )
        if len(body) > _MAX_REQUEST_BYTES:
            return _unavailable(
                selected_service=service,
                selected_master=master,
                reason=BookingEligibilityAdapterReasonCode.CONFIG_INVALID,
            )

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
                reason = BookingEligibilityAdapterReasonCode.TIMEOUT
            elif code == "RESPONSE_TOO_LARGE":
                reason = BookingEligibilityAdapterReasonCode.RESPONSE_TOO_LARGE
            else:
                reason = BookingEligibilityAdapterReasonCode.TRANSPORT_ERROR
            return _unavailable(
                selected_service=service,
                selected_master=master,
                reason=reason,
            )
        except Exception:
            return _unavailable(
                selected_service=service,
                selected_master=master,
                reason=BookingEligibilityAdapterReasonCode.TRANSPORT_ERROR,
            )

        return self._interpret_response(
            response,
            selected_service=service,
            selected_master=master,
            include_alternatives=include_alternatives,
            max_bytes=max_bytes,
        )

    def _interpret_response(
        self,
        response: object,
        *,
        selected_service: SelectedService,
        selected_master: SelectedMaster | None,
        include_alternatives: bool,
        max_bytes: int,
    ) -> BookingEligibilityResult:
        if type(response) is not S2sHttpResponse:
            return _unavailable(
                selected_service=selected_service,
                selected_master=selected_master,
                reason=BookingEligibilityAdapterReasonCode.TRANSPORT_ERROR,
            )

        if len(response.body) > max_bytes:
            return _unavailable(
                selected_service=selected_service,
                selected_master=selected_master,
                reason=BookingEligibilityAdapterReasonCode.RESPONSE_TOO_LARGE,
            )

        if response.status_code != 200:
            return _unavailable(
                selected_service=selected_service,
                selected_master=selected_master,
                reason=BookingEligibilityAdapterReasonCode.REMOTE_REJECTED,
            )

        if not _content_type_is_json(_header_value(response.headers, "Content-Type")):
            return _unavailable(
                selected_service=selected_service,
                selected_master=selected_master,
                reason=BookingEligibilityAdapterReasonCode.RESPONSE_INVALID,
            )

        if not response.body:
            return _unavailable(
                selected_service=selected_service,
                selected_master=selected_master,
                reason=BookingEligibilityAdapterReasonCode.RESPONSE_INVALID,
            )

        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return _unavailable(
                selected_service=selected_service,
                selected_master=selected_master,
                reason=BookingEligibilityAdapterReasonCode.RESPONSE_INVALID,
            )

        remote = parse_eligibility_success_payload(payload)
        if remote is None:
            return _unavailable(
                selected_service=selected_service,
                selected_master=selected_master,
                reason=BookingEligibilityAdapterReasonCode.RESPONSE_INVALID,
            )

        return _map_success_to_domain(
            remote=remote,
            selected_service=selected_service,
            selected_master=selected_master,
            include_alternatives=include_alternatives,
        )
