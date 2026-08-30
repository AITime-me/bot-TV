"""Control-plane S2S remote path + error-code constants (BOT-CONTROL-PLANE-04).

Routes live on online-zapis-tv under ``/api/internal/bot/v1/*``.
Auth reuses ``BOOKING_ELIGIBILITY_*`` → ``BOT_INTERNAL_API_TOKEN``.
"""

from __future__ import annotations

from typing import Final

SETTINGS_ROUTE_PATH: Final[str] = "/api/internal/bot/v1/settings"
KNOWLEDGE_ROUTE_PATH: Final[str] = "/api/internal/bot/v1/knowledge"

BOT_SETTINGS_NOT_PUBLISHED_CODE: Final[str] = "BOT_SETTINGS_NOT_PUBLISHED"
BOT_SETTINGS_PUBLICATION_INVALID_CODE: Final[str] = (
    "BOT_SETTINGS_PUBLICATION_INVALID"
)
BOT_KNOWLEDGE_NOT_PUBLISHED_CODE: Final[str] = "BOT_KNOWLEDGE_NOT_PUBLISHED"
BOT_KNOWLEDGE_PUBLICATION_INVALID_CODE: Final[str] = (
    "BOT_KNOWLEDGE_PUBLICATION_INVALID"
)

CONTROL_PLANE_SCHEMA_VERSION: Final[int] = 1
# Snapshot refresh issues one GET per kind (settings + knowledge).
# Live-facts are acquired on the runtime-context path, not this tick.
CONTROL_PLANE_S2S_GETS_PER_REFRESH: Final[int] = 2
