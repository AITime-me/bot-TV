from enum import Enum

from app.config import BotMode, Settings


class OutboundAction(str, Enum):
    SEND_MESSAGE = "SEND_MESSAGE"


def is_automatic_outbound_allowed(
    settings: Settings | None,
    action: OutboundAction | str,
) -> bool:
    """Return whether Bot Core may perform an automatic outbound action.

    No outbound action is enabled in the baseline. The explicit checks keep
    safety-critical precedence visible and make malformed inputs fail closed.
    """

    if not isinstance(settings, Settings):
        return False
    if settings.emergency_lock:
        return False
    if settings.bot_mode is BotMode.OFF:
        return False

    try:
        OutboundAction(action)
    except (TypeError, ValueError):
        return False

    return False
