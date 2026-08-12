"""Deterministic AMO-01A identity helpers for durable ingress / manager rows.

Keeps amoCRM chat+message identity namespaced so synthetic manager traffic
cannot collide with raw amo message ids in the shared synthetic channel.
"""

from __future__ import annotations

from typing import Final

AMOCRM_MANAGER_ID_PREFIX: Final[str] = "amo"
AMOCRM_MANAGER_EXTERNAL_ID_MAX_LENGTH: Final[int] = 256


class AmoCrmManagerIdError(ValueError):
    """Fail-closed invalid AMO manager identity input."""

    def __init__(self, code: str = "AMOCRM_MANAGER_ID_INVALID") -> None:
        super().__init__(code)
        self.code = code


def amocrm_manager_namespaced_id(
    *,
    amocrm_chat_id: str,
    amocrm_message_id: str,
) -> str:
    """Build ingress + manager external id: ``amo:{chat_id}:{message_id}``."""

    if not isinstance(amocrm_chat_id, str) or not amocrm_chat_id:
        raise AmoCrmManagerIdError("AMOCRM_MANAGER_ID_INVALID")
    if not isinstance(amocrm_message_id, str) or not amocrm_message_id:
        raise AmoCrmManagerIdError("AMOCRM_MANAGER_ID_INVALID")
    if ":" in amocrm_chat_id or ":" in amocrm_message_id:
        # Colon is the namespace separator; reject so keys stay unambiguous.
        raise AmoCrmManagerIdError("AMOCRM_MANAGER_ID_INVALID")
    key = (
        f"{AMOCRM_MANAGER_ID_PREFIX}:{amocrm_chat_id}:{amocrm_message_id}"
    )
    if len(key) > AMOCRM_MANAGER_EXTERNAL_ID_MAX_LENGTH:
        raise AmoCrmManagerIdError("AMOCRM_MANAGER_ID_TOO_LONG")
    return key
