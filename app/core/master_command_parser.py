"""Deterministic Russian master-command parser (CURSOR-28).

Fail-closed. No LLM. Relative dates resolve in Asia/Yekaterinburg only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Final

from app.core.manager_working_hours import MANAGER_TIMEZONE, to_manager_local
from app.core.master_command_types import (
    MasterCommandClarificationNeed,
    MasterCommandKind,
    MasterCommandSafePayload,
)

__all__ = (
    "MasterCommandParseStatus",
    "MasterCommandParseResult",
    "MasterCommandControlIntent",
    "parse_master_command_text",
    "classify_control_intent",
)

_DATE_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DOT_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b"
)
_ISO_DATE_RE: Final[re.Pattern[str]] = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# Colon-only times so dotted calendar dates (10.08) are never treated as HH.MM.
_TIME_RE: Final[re.Pattern[str]] = re.compile(
    r"\b([01]?\d|2[0-3]):([0-5]\d)\b"
)
_SLOT_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"\bbs1\.[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-"
    r"[0-9a-f]{12}\.[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-"
    r"[0-9a-f]{12}\.\d{4}-\d{2}-\d{2}\.\d{4}\b",
    re.IGNORECASE,
)
_PHONE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)(?:\+7|8)?[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)"
)
_INTERVAL_BLOCK_TYPES: Final[frozenset[str]] = frozenset(
    {"BREAK", "LUNCH", "PERSONAL", "DO_NOT_BOOK"}
)
_DAY_BLOCK_TYPES: Final[frozenset[str]] = frozenset(
    {"DAY_OFF", "VACATION", "SICK_LEAVE", "DO_NOT_BOOK"}
)


class MasterCommandParseStatus(StrEnum):
    READY = "READY"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    UNKNOWN = "UNKNOWN"
    CONTROL = "CONTROL"


class MasterCommandControlIntent(StrEnum):
    CONFIRM = "CONFIRM"
    CANCEL = "CANCEL"
    NONE = "NONE"


@dataclass(frozen=True, slots=True, repr=False)
class MasterCommandParseResult:
    status: MasterCommandParseStatus
    kind: MasterCommandKind | None = None
    payload: MasterCommandSafePayload | None = None
    needs: tuple[MasterCommandClarificationNeed, ...] = ()
    control: MasterCommandControlIntent = MasterCommandControlIntent.NONE
    client_name: str | None = None
    phone: str | None = None

    def __repr__(self) -> str:
        return (
            "MasterCommandParseResult("
            f"status={self.status.value!r}, "
            f"kind={None if self.kind is None else self.kind.value!r}, "
            f"payload={self.payload!r}, "
            f"needs={tuple(n.value for n in self.needs)!r}, "
            f"control={self.control.value!r}, "
            f"client_name={'<set>' if self.client_name else None}, "
            f"phone={'<set>' if self.phone else None})"
        )


def classify_control_intent(text: str) -> MasterCommandControlIntent:
    token = " ".join(text.lower().split())
    if token in {"да", "подтверждаю", "подтвердить", "ок", "ok", "+"}:
        return MasterCommandControlIntent.CONFIRM
    if token in {"нет", "отмена", "отменить", "cancel", "-"}:
        return MasterCommandControlIntent.CANCEL
    return MasterCommandControlIntent.NONE


def parse_master_command_text(
    text: str,
    *,
    now: datetime,
) -> MasterCommandParseResult:
    """Parse one inbound master message. Relative dates use Asia/Yekaterinburg."""

    if type(text) is not str or not text.strip():
        return MasterCommandParseResult(status=MasterCommandParseStatus.UNKNOWN)
    if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
        return MasterCommandParseResult(status=MasterCommandParseStatus.UNKNOWN)

    local_now = to_manager_local(now)
    normalized = " ".join(text.split())
    control = classify_control_intent(normalized)
    if control is not MasterCommandControlIntent.NONE:
        return MasterCommandParseResult(
            status=MasterCommandParseStatus.CONTROL,
            control=control,
        )

    lower = normalized.casefold()

    if _looks_like_schedule(lower):
        return _parse_schedule(lower, local_now)

    if _looks_like_close_day(lower):
        return _parse_close_day(lower, normalized, local_now)

    if _looks_like_close_interval(lower):
        return _parse_close_interval(lower, normalized, local_now)

    if _looks_like_booking(lower):
        return _parse_booking(lower, normalized, local_now)

    return MasterCommandParseResult(status=MasterCommandParseStatus.UNKNOWN)


def _looks_like_schedule(lower: str) -> bool:
    return bool(
        re.search(r"\b(расписание|мои\s+записи|мое\s+расписание|моё\s+расписание)\b", lower)
    )


def _looks_like_close_day(lower: str) -> bool:
    return bool(
        re.search(
            r"\b(выходной|отпуск|больничн\w*|закрыть\s+день|день\s+выходной)\b",
            lower,
        )
    )


def _looks_like_close_interval(lower: str) -> bool:
    return bool(
        re.search(
            r"\b(закрыть\s+интервал|закрыть\s+слот|закрыть\s+с|перерыв|обед)\b",
            lower,
        )
        or (
            "закрыть" in lower
            and re.search(r"\bс\s+\d", lower)
            and re.search(r"\bдо\s+\d", lower)
        )
    )


def _looks_like_booking(lower: str) -> bool:
    return bool(
        re.search(r"\b(запись|записать|поставь\s+запись|создать\s+запись)\b", lower)
    )


def _parse_schedule(lower: str, local_now: datetime) -> MasterCommandParseResult:
    today = local_now.date()
    if "недел" in lower:
        start = today
        end = today + timedelta(days=6)
    else:
        dates = _extract_dates(lower, today)
        if len(dates) > 1:
            return MasterCommandParseResult(
                status=MasterCommandParseStatus.CLARIFICATION_REQUIRED,
                kind=MasterCommandKind.SCHEDULE_READ,
                needs=(MasterCommandClarificationNeed.AMBIGUOUS,),
            )
        if len(dates) == 1:
            start = end = dates[0]
        else:
            start = end = today
    return MasterCommandParseResult(
        status=MasterCommandParseStatus.READY,
        kind=MasterCommandKind.SCHEDULE_READ,
        payload=MasterCommandSafePayload(
            from_date_key=_date_key(start),
            to_date_key=_date_key(end),
        ),
    )


def _parse_close_day(
    lower: str, original: str, local_now: datetime
) -> MasterCommandParseResult:
    today = local_now.date()
    dates = _extract_dates(lower, today)
    needs: list[MasterCommandClarificationNeed] = []
    date_key: str | None = None
    if len(dates) == 0:
        needs.append(MasterCommandClarificationNeed.DATE)
    elif len(dates) > 1:
        needs.append(MasterCommandClarificationNeed.AMBIGUOUS)
    else:
        date_key = _date_key(dates[0])

    block_type = "DAY_OFF"
    if re.search(r"\bотпуск\b", lower):
        block_type = "VACATION"
    elif re.search(r"\bбольничн", lower):
        block_type = "SICK_LEAVE"
    elif re.search(r"\bне\s+записывать|не\s+бронировать\b", lower):
        block_type = "DO_NOT_BOOK"
    if block_type not in _DAY_BLOCK_TYPES:
        needs.append(MasterCommandClarificationNeed.BLOCK_TYPE)

    payload = MasterCommandSafePayload(
        date_key=date_key,
        block_type=block_type,
        missing=tuple(n.value for n in needs),
    )
    if needs:
        return MasterCommandParseResult(
            status=MasterCommandParseStatus.CLARIFICATION_REQUIRED,
            kind=MasterCommandKind.CLOSE_DAY,
            payload=payload,
            needs=tuple(needs),
        )
    return MasterCommandParseResult(
        status=MasterCommandParseStatus.READY,
        kind=MasterCommandKind.CLOSE_DAY,
        payload=payload,
    )


def _parse_close_interval(
    lower: str, original: str, local_now: datetime
) -> MasterCommandParseResult:
    today = local_now.date()
    dates = _extract_dates(lower, today)
    times = _extract_times(lower)
    needs: list[MasterCommandClarificationNeed] = []

    date_key: str | None = None
    if len(dates) == 0:
        needs.append(MasterCommandClarificationNeed.DATE)
    elif len(dates) > 1:
        needs.append(MasterCommandClarificationNeed.AMBIGUOUS)
    else:
        date_key = _date_key(dates[0])

    start_time: str | None = None
    end_time: str | None = None
    if len(times) < 1:
        needs.append(MasterCommandClarificationNeed.TIME)
    if len(times) < 2:
        needs.append(MasterCommandClarificationNeed.END_TIME)
    if len(times) >= 2:
        start_time, end_time = times[0], times[1]
        if start_time >= end_time:
            needs.append(MasterCommandClarificationNeed.AMBIGUOUS)

    block_type = "BREAK"
    if re.search(r"\bобед\b", lower):
        block_type = "LUNCH"
    elif re.search(r"\bличн", lower):
        block_type = "PERSONAL"
    elif re.search(r"\bне\s+записывать|не\s+бронировать\b", lower):
        block_type = "DO_NOT_BOOK"
    if block_type not in _INTERVAL_BLOCK_TYPES:
        needs.append(MasterCommandClarificationNeed.BLOCK_TYPE)

    # Deduplicate needs while preserving order.
    deduped: list[MasterCommandClarificationNeed] = []
    for item in needs:
        if item not in deduped:
            deduped.append(item)

    payload = MasterCommandSafePayload(
        date_key=date_key,
        start_time=start_time,
        end_time=end_time,
        block_type=block_type,
        missing=tuple(n.value for n in deduped),
    )
    if deduped:
        return MasterCommandParseResult(
            status=MasterCommandParseStatus.CLARIFICATION_REQUIRED,
            kind=MasterCommandKind.CLOSE_INTERVAL,
            payload=payload,
            needs=tuple(deduped),
        )
    return MasterCommandParseResult(
        status=MasterCommandParseStatus.READY,
        kind=MasterCommandKind.CLOSE_INTERVAL,
        payload=payload,
    )


def _parse_booking(
    lower: str, original: str, local_now: datetime
) -> MasterCommandParseResult:
    today = local_now.date()
    needs: list[MasterCommandClarificationNeed] = []

    slot_match = _SLOT_ID_RE.search(original)
    slot_id = slot_match.group(0).lower() if slot_match else None

    phone = _normalize_phone(_PHONE_RE.search(original))
    client_name = _extract_client_name(original, lower)

    date_key: str | None = None
    start_time: str | None = None
    if slot_id is None:
        dates = _extract_dates(lower, today)
        times = _extract_times(lower)
        if len(dates) == 1:
            date_key = _date_key(dates[0])
        elif len(dates) > 1:
            needs.append(MasterCommandClarificationNeed.AMBIGUOUS)
        if len(times) >= 1:
            start_time = times[0]
        # Without opaque slotId, service is not safely knowable from free text.
        needs.append(MasterCommandClarificationNeed.SLOT_ID)

    if client_name is None:
        needs.append(MasterCommandClarificationNeed.CLIENT_NAME)
    if phone is None:
        needs.append(MasterCommandClarificationNeed.PHONE)

    deduped: list[MasterCommandClarificationNeed] = []
    for item in needs:
        if item not in deduped:
            deduped.append(item)

    payload = MasterCommandSafePayload(
        date_key=date_key,
        start_time=start_time,
        slot_id=slot_id,
        missing=tuple(n.value for n in deduped),
    )
    if deduped:
        return MasterCommandParseResult(
            status=MasterCommandParseStatus.CLARIFICATION_REQUIRED,
            kind=MasterCommandKind.CREATE_BOOKING,
            payload=payload,
            needs=tuple(deduped),
            client_name=client_name,
            phone=phone,
        )
    return MasterCommandParseResult(
        status=MasterCommandParseStatus.READY,
        kind=MasterCommandKind.CREATE_BOOKING,
        payload=payload,
        client_name=client_name,
        phone=phone,
    )


def _extract_dates(lower: str, today: date) -> list[date]:
    found: list[date] = []
    if re.search(r"\bсегодня\b", lower):
        found.append(today)
    if re.search(r"\bзавтра\b", lower):
        found.append(today + timedelta(days=1))
    if re.search(r"\bпослезавтра\b", lower):
        found.append(today + timedelta(days=2))

    for match in _ISO_DATE_RE.finditer(lower):
        parsed = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if parsed is not None:
            found.append(parsed)

    for match in _DOT_DATE_RE.finditer(lower):
        day = int(match.group(1))
        month = int(match.group(2))
        year_raw = match.group(3)
        if year_raw is None:
            year = today.year
            candidate = _safe_date(year, month, day)
            if candidate is not None and candidate < today:
                candidate = _safe_date(year + 1, month, day)
        else:
            year = int(year_raw)
            if year < 100:
                year += 2000
            candidate = _safe_date(year, month, day)
        if candidate is not None:
            found.append(candidate)

    # Deduplicate preserving order.
    out: list[date] = []
    for item in found:
        if item not in out:
            out.append(item)
    return out


def _extract_times(lower: str) -> list[str]:
    out: list[str] = []
    for match in _TIME_RE.finditer(lower):
        hh = int(match.group(1))
        mm = int(match.group(2))
        out.append(f"{hh:02d}:{mm:02d}")
    return out


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _date_key(value: date) -> str:
    return value.isoformat()


def _normalize_phone(match: re.Match[str] | None) -> str | None:
    if match is None:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) < 11 or len(digits) > 15:
        return None
    return f"+{digits}"


def _extract_client_name(original: str, lower: str) -> str | None:
    # Explicit patterns only — never guess free-form prose as a name.
    blocked = {
        "на",
        "в",
        "к",
        "по",
        "для",
        "клиент",
        "клиента",
        "клиенту",
        "завтра",
        "сегодня",
        "послезавтра",
    }
    patterns = (
        r"клиент[ау]?\s+([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-]{1,40})",
        r"имя\s+([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-]{1,40})",
        r"записать\s+([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-]{1,40})",
    )
    for pattern in patterns:
        match = re.search(pattern, original, flags=re.IGNORECASE)
        if match:
            name = match.group(1).strip()
            if len(name) >= 2 and name.casefold() not in blocked:
                return name
    return None
