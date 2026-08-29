"""Live business facts S2S remote path (AI-DIALOGUE-01).

Producer: online-zapis-tv ``GET /api/internal/bot/v1/live-facts`` schemaVersion=1.
Auth reuses ``BOOKING_ELIGIBILITY_*`` → ``BOT_INTERNAL_API_TOKEN``.

This is CURRENT business Source of Truth — not a publication snapshot.
"""

from __future__ import annotations

from typing import Final

LIVE_FACTS_ROUTE_PATH: Final[str] = "/api/internal/bot/v1/live-facts"

BOT_LIVE_FACTS_SCHEMA_VERSION: Final[int] = 1
BOT_LIVE_FACTS_CURRENCY: Final[str] = "RUB"

LIVE_FACTS_OWNERSHIP_INVARIANT: Final[str] = (
    "LIVE_FACTS_WINS_OVER_KB_PROSE_FOR_PRICE_DURATION_MASTER_ASSIGNMENT_"
    "BOOKING_MODE_ACTIVE_STATE_STUDIO_STRUCTURED"
)

LIVE_FACTS_AVAILABILITY_BOUNDARY: Final[str] = (
    "LIVE_FACTS_EXCLUDES_AVAILABILITY_SLOTS_DATES_BLOCKS_APPOINTMENT_STATE"
)

LIVE_FACTS_PROMOTIONS_GAP: Final[str] = (
    "PROMOTIONS_GIFTS_OMITTED_V1_SPLIT_BRAIN_PROMO_RULES_VS_DB_PROMOTIONS"
)
