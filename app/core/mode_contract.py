"""OWNER-approved dual-enum mode contract (CONTRACT-MODE-01).

Control plane (online-zapis-tv) and Bot Core (bot-TV) keep separate enums.
This module documents the explicit mapping and enforces Bot Core runtime
capability checks. It does not rename BotMode and does not invent aliases.

Control plane exposure intent
-----------------------------
OFF     exposure disabled
TEST    closed-test / admin / synthetic exposure only — NOT a BotMode value
HINTS   maps to Bot Core HINTS
DRAFT   maps to Bot Core DRAFT
AUTO    control plane may allow client auto-exposure intent;
        Bot Core capability is at most AUTO_READ until a separate OWNER write
        gate; AUTO never silently becomes AUTO_WRITE

Bot Core runtime capability
---------------------------
OFF / HINTS / DRAFT  deny live Booking Service S2S reads
AUTO_READ            allow live S2S reads when EMERGENCY_LOCK is false
AUTO_WRITE           allow live S2S reads when EMERGENCY_LOCK is false;
                     booking writes remain under separate existing write gates
EMERGENCY_LOCK=true  absolute deny for live S2S reads in every mode

Outbound automatic sends remain denied by ``outbound_policy`` (unchanged).
"""

from __future__ import annotations

from app.config import BotMode, Settings

# Explicit dual-enum mapping (documentation + tests). Not a runtime translator.
CONTROL_PLANE_TO_BOT_CORE_CAPABILITY: dict[str, str] = {
    "OFF": "OFF",
    "TEST": "EXPOSURE_ONLY_NOT_BOT_MODE",
    "HINTS": "HINTS",
    "DRAFT": "DRAFT",
    "AUTO": "AUTO_READ_MAX_UNTIL_WRITE_GATE",
}


def is_live_booking_s2s_read_allowed(settings: Settings | None) -> bool:
    """Return whether live Booking Service eligibility/availability reads may run.

    Fail closed on missing/malformed settings. EMERGENCY_LOCK has absolute
    priority over BotMode.
    """

    if not isinstance(settings, Settings):
        return False
    if type(settings.emergency_lock) is not bool:
        return False
    if settings.emergency_lock:
        return False
    if not isinstance(settings.bot_mode, BotMode):
        return False
    return settings.bot_mode in {BotMode.AUTO_READ, BotMode.AUTO_WRITE}
