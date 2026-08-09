"""Remote DTOs for CURSOR-26 master command S2S API.

Wire paths under ``/api/internal/bot/v1/master/*``.
Caller owns idempotencyKey. Repr never prints phone, name, or master_id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

from app.core.booking_eligibility_http import (
    BookingEligibilityHttpError,
    require_canonical_backend_uuid,
)


def _require_master_id(value: object) -> str:
    try:
        return require_canonical_backend_uuid(value)
    except BookingEligibilityHttpError as exc:
        raise ValueError("INVALID_MASTER_ID") from exc

MASTER_SCHEDULE_ROUTE_PATH: Final[str] = "/api/internal/bot/v1/master/schedule"
MASTER_CLOSE_INTERVAL_ROUTE_PATH: Final[str] = (
    "/api/internal/bot/v1/master/blocks/close-interval"
)
MASTER_CLOSE_DAY_ROUTE_PATH: Final[str] = (
    "/api/internal/bot/v1/master/blocks/close-day"
)
MASTER_BOOKINGS_ROUTE_PATH: Final[str] = "/api/internal/bot/v1/master/bookings"

MASTER_SCHEDULE_MAX_RANGE_DAYS: Final[int] = 14

MASTER_INTERVAL_BLOCK_TYPES: Final[frozenset[str]] = frozenset(
    {"BREAK", "LUNCH", "PERSONAL", "DO_NOT_BOOK"}
)
MASTER_FULL_DAY_BLOCK_TYPES: Final[frozenset[str]] = frozenset(
    {"DAY_OFF", "VACATION", "SICK_LEAVE", "DO_NOT_BOOK"}
)

_CANONICAL_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_DATE_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HHMM_RE: Final[re.Pattern[str]] = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_SLOT_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"^bs1\."
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\."
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\."
    r"\d{4}-\d{2}-\d{2}\."
    r"\d{4}$"
)
_PHONE_E164_RE: Final[re.Pattern[str]] = re.compile(r"^\+\d{10,15}$")

MASTER_COMMAND_REMOTE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "VALIDATION_ERROR",
        "PAYLOAD_TOO_LARGE",
        "UNAUTHORIZED",
        "RATE_LIMITED",
        "IDEMPOTENCY_CONFLICT",
        "IDEMPOTENCY_IN_PROGRESS",
        "MASTER_NOT_FOUND",
        "MASTER_SCOPE_VIOLATION",
        "RANGE_TOO_LARGE",
        "APPOINTMENT_CONFLICT",
        "BLOCK_CONFLICT",
        "BLOCK_NOT_FOUND",
        "BLOCK_NOT_OWNED",
        "EXTRA_WORK_NOT_FOUND",
        "EXTRA_WORK_NOT_OWNED",
        "EXTRA_WORK_IN_USE",
        "SLOT_INVALID",
        "SLOT_NO_LONGER_AVAILABLE",
        "SERVICE_UNAVAILABLE",
        "MASTER_UNAVAILABLE",
        "SERVICE_MASTER_MISMATCH",
        "CLIENT_AMBIGUOUS",
        "INTERNAL_ERROR",
    }
)

REMOTE_ERROR_CODE_BY_STATUS: Final[dict[int, frozenset[str]]] = {
    400: frozenset(
        {
            "VALIDATION_ERROR",
            "MASTER_SCOPE_VIOLATION",
            "RANGE_TOO_LARGE",
            "SLOT_INVALID",
            "SERVICE_UNAVAILABLE",
            "MASTER_UNAVAILABLE",
            "SERVICE_MASTER_MISMATCH",
            "BLOCK_NOT_OWNED",
            "EXTRA_WORK_NOT_OWNED",
        }
    ),
    401: frozenset({"UNAUTHORIZED"}),
    404: frozenset(
        {"MASTER_NOT_FOUND", "BLOCK_NOT_FOUND", "EXTRA_WORK_NOT_FOUND"}
    ),
    409: frozenset(
        {
            "IDEMPOTENCY_CONFLICT",
            "IDEMPOTENCY_IN_PROGRESS",
            "APPOINTMENT_CONFLICT",
            "BLOCK_CONFLICT",
            "EXTRA_WORK_IN_USE",
            "SLOT_NO_LONGER_AVAILABLE",
            "CLIENT_AMBIGUOUS",
        }
    ),
    413: frozenset({"PAYLOAD_TOO_LARGE"}),
    429: frozenset({"RATE_LIMITED"}),
    500: frozenset({"INTERNAL_ERROR"}),
}


@dataclass(frozen=True, slots=True, repr=False)
class MasterScheduleRemoteSuccess:
    from_date_key: str
    to_date_key: str
    days: tuple[dict[str, object], ...]

    def __repr__(self) -> str:
        return (
            "MasterScheduleRemoteSuccess("
            f"from_date_key={self.from_date_key!r}, "
            f"to_date_key={self.to_date_key!r}, "
            f"days_len={len(self.days)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class MasterMutationRemoteSuccess:
    idempotent_replay: bool
    resource_kind: str

    def __repr__(self) -> str:
        return (
            "MasterMutationRemoteSuccess("
            f"idempotent_replay={self.idempotent_replay!r}, "
            f"resource_kind={self.resource_kind!r})"
        )


def require_date_key(value: object) -> str:
    if type(value) is not str or _DATE_KEY_RE.fullmatch(value) is None:
        raise ValueError("INVALID_DATE_KEY") from None
    year = int(value[0:4])
    month = int(value[5:7])
    day = int(value[8:10])
    if month < 1 or month > 12 or day < 1 or day > 31:
        raise ValueError("INVALID_DATE_KEY") from None
    return value


def require_hhmm(value: object) -> str:
    if type(value) is not str or _HHMM_RE.fullmatch(value) is None:
        raise ValueError("INVALID_TIME") from None
    return value


def require_idempotency_key(value: object) -> str:
    if type(value) is not str or _CANONICAL_UUID_RE.fullmatch(value) is None:
        raise ValueError("INVALID_IDEMPOTENCY_KEY") from None
    if value != value.lower():
        raise ValueError("INVALID_IDEMPOTENCY_KEY") from None
    return value


def require_master_slot_id(value: object) -> str:
    if type(value) is not str or len(value) > 128:
        raise ValueError("INVALID_SLOT_ID") from None
    if _SLOT_ID_RE.fullmatch(value) is None:
        raise ValueError("INVALID_SLOT_ID") from None
    return value


def require_client_name(value: object) -> str:
    if type(value) is not str:
        raise ValueError("INVALID_CLIENT_NAME") from None
    name = " ".join(value.split())
    if len(name) < 2 or len(name) > 80:
        raise ValueError("INVALID_CLIENT_NAME") from None
    return name


def require_e164_phone(value: object) -> str:
    if type(value) is not str or _PHONE_E164_RE.fullmatch(value) is None:
        raise ValueError("INVALID_PHONE") from None
    return value


def build_schedule_request_body(
    *,
    master_id: object,
    from_date_key: object,
    to_date_key: object,
) -> dict[str, str]:
    mid = _require_master_id(master_id)
    frm = require_date_key(from_date_key)
    to = require_date_key(to_date_key)
    if frm > to:
        raise ValueError("INVALID_DATE_RANGE") from None
    return {"masterId": mid, "fromDateKey": frm, "toDateKey": to}


def build_close_interval_request_body(
    *,
    idempotency_key: object,
    master_id: object,
    date_key: object,
    start_time: object,
    end_time: object,
    block_type: object,
) -> dict[str, str]:
    if type(block_type) is not str or block_type not in MASTER_INTERVAL_BLOCK_TYPES:
        raise ValueError("INVALID_BLOCK_TYPE") from None
    start = require_hhmm(start_time)
    end = require_hhmm(end_time)
    if start >= end:
        raise ValueError("INVALID_TIME_RANGE") from None
    return {
        "idempotencyKey": require_idempotency_key(idempotency_key),
        "masterId": _require_master_id(master_id),
        "dateKey": require_date_key(date_key),
        "startTime": start,
        "endTime": end,
        "blockType": block_type,
    }


def build_close_day_request_body(
    *,
    idempotency_key: object,
    master_id: object,
    date_key: object,
    block_type: object,
) -> dict[str, str]:
    if type(block_type) is not str or block_type not in MASTER_FULL_DAY_BLOCK_TYPES:
        raise ValueError("INVALID_BLOCK_TYPE") from None
    return {
        "idempotencyKey": require_idempotency_key(idempotency_key),
        "masterId": _require_master_id(master_id),
        "dateKey": require_date_key(date_key),
        "blockType": block_type,
    }


def build_master_booking_request_body(
    *,
    idempotency_key: object,
    master_id: object,
    slot_id: object,
    client_name: object,
    phone: object,
) -> dict[str, object]:
    return {
        "idempotencyKey": require_idempotency_key(idempotency_key),
        "masterId": _require_master_id(master_id),
        "slotId": require_master_slot_id(slot_id),
        "clientName": require_client_name(client_name),
        "phone": require_e164_phone(phone),
        "personalDataConsent": True,
        "offerAcknowledgement": True,
    }


def parse_schedule_success_payload(payload: object) -> MasterScheduleRemoteSuccess:
    if type(payload) is not dict:
        raise ValueError("RESPONSE_INVALID") from None
    if payload.get("ok") is not True:
        raise ValueError("RESPONSE_INVALID") from None
    from_date_key = require_date_key(payload.get("fromDateKey"))
    to_date_key = require_date_key(payload.get("toDateKey"))
    days_raw = payload.get("days")
    if type(days_raw) is not list:
        raise ValueError("RESPONSE_INVALID") from None
    days: list[dict[str, object]] = []
    for item in days_raw:
        if type(item) is not dict:
            raise ValueError("RESPONSE_INVALID") from None
        require_date_key(item.get("dateKey"))
        # Strip masterId and any phone-like fields from persisted view.
        safe_day: dict[str, object] = {
            "dateKey": item["dateKey"],
            "appointments": _safe_appointments(item.get("appointments")),
            "scheduleBlocks": _safe_blocks(item.get("scheduleBlocks")),
            "extraWorkWindows": _safe_extra(item.get("extraWorkWindows")),
        }
        days.append(safe_day)
    return MasterScheduleRemoteSuccess(
        from_date_key=from_date_key,
        to_date_key=to_date_key,
        days=tuple(days),
    )


def parse_mutation_success_payload(
    payload: object,
    *,
    resource_kind: str,
) -> MasterMutationRemoteSuccess:
    if type(payload) is not dict:
        raise ValueError("RESPONSE_INVALID") from None
    if payload.get("ok") is not True:
        raise ValueError("RESPONSE_INVALID") from None
    replay = payload.get("idempotentReplay")
    if type(replay) is not bool:
        raise ValueError("RESPONSE_INVALID") from None
    return MasterMutationRemoteSuccess(
        idempotent_replay=replay,
        resource_kind=resource_kind,
    )


def _safe_appointments(value: object) -> list[dict[str, object]]:
    if type(value) is not list:
        raise ValueError("RESPONSE_INVALID") from None
    out: list[dict[str, object]] = []
    for item in value:
        if type(item) is not dict:
            raise ValueError("RESPONSE_INVALID") from None
        starts = item.get("startsAt")
        ends = item.get("endsAt")
        service = item.get("serviceName")
        if type(starts) is not str or type(ends) is not str:
            raise ValueError("RESPONSE_INVALID") from None
        # Intentionally omit clientName / phone / appointment id.
        row: dict[str, object] = {"startsAt": starts, "endsAt": ends}
        if type(service) is str:
            row["serviceName"] = service
        out.append(row)
    return out


def _safe_blocks(value: object) -> list[dict[str, object]]:
    if type(value) is not list:
        raise ValueError("RESPONSE_INVALID") from None
    out: list[dict[str, object]] = []
    for item in value:
        if type(item) is not dict:
            raise ValueError("RESPONSE_INVALID") from None
        block_type = item.get("blockType")
        is_full_day = item.get("isFullDay")
        if type(block_type) is not str or type(is_full_day) is not bool:
            raise ValueError("RESPONSE_INVALID") from None
        row: dict[str, object] = {
            "blockType": block_type,
            "isFullDay": is_full_day,
        }
        starts = item.get("startsAt")
        ends = item.get("endsAt")
        if type(starts) is str:
            row["startsAt"] = starts
        if type(ends) is str:
            row["endsAt"] = ends
        out.append(row)
    return out


def _safe_extra(value: object) -> list[dict[str, object]]:
    if type(value) is not list:
        raise ValueError("RESPONSE_INVALID") from None
    out: list[dict[str, object]] = []
    for item in value:
        if type(item) is not dict:
            raise ValueError("RESPONSE_INVALID") from None
        starts = item.get("startsAt")
        ends = item.get("endsAt")
        if type(starts) is not str or type(ends) is not str:
            raise ValueError("RESPONSE_INVALID") from None
        out.append({"startsAt": starts, "endsAt": ends})
    return out
