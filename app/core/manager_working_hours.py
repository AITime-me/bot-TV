"""Single source of truth for manager working hours (CURSOR-15).

Timezone: Asia/Yekaterinburg. Hours: every day [09:00, 18:00).
No persona copy, HTTP, or channel wiring lives here.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MANAGER_TIMEZONE_NAME: Final[str] = "Asia/Yekaterinburg"
# Asia/Yekaterinburg has been permanent UTC+5 (no DST) since 2011.
_MANAGER_UTC_OFFSET: Final[timedelta] = timedelta(hours=5)

MANAGER_WORKDAY_START: Final[time] = time(9, 0)
MANAGER_WORKDAY_END: Final[time] = time(18, 0)


def _build_manager_timezone() -> tzinfo:
    try:
        return ZoneInfo(MANAGER_TIMEZONE_NAME)
    except ZoneInfoNotFoundError:
        # Hosts without IANA tzdata (typical bare Windows) still need a
        # deterministic Asia/Yekaterinburg clock for pure domain logic.
        return timezone(_MANAGER_UTC_OFFSET, name=MANAGER_TIMEZONE_NAME)


MANAGER_TIMEZONE: Final[tzinfo] = _build_manager_timezone()


def manager_timezone() -> tzinfo:
    """Return the canonical manager timezone (Asia/Yekaterinburg)."""

    return MANAGER_TIMEZONE


def to_manager_local(moment: datetime) -> datetime:
    """Convert an aware datetime into Asia/Yekaterinburg local time."""

    if not isinstance(moment, datetime):
        raise TypeError("moment must be datetime") from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("moment must be timezone-aware") from None
    return moment.astimezone(MANAGER_TIMEZONE)


def is_manager_working_time(moment: datetime) -> bool:
    """Return True iff local manager time is in [09:00, 18:00) any calendar day.

    Boundaries (Asia/Yekaterinburg):
    - 08:59 — outside
    - 09:00 — inside
    - 17:59 — inside
    - 18:00 — outside
    - 18:01 — outside
    """

    local = to_manager_local(moment)
    return MANAGER_WORKDAY_START <= local.time() < MANAGER_WORKDAY_END
