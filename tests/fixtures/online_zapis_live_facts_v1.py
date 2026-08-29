"""Cross-repo contract fixture: online-zapis-tv live-facts schemaVersion=1.

Producer: online-zapis-tv GET /api/internal/bot/v1/live-facts
Source shape: scripts/security-bot-live-facts-check.ts + live-facts-contract.ts
(merged HTTP envelope adds ok:true).
"""

from __future__ import annotations

from typing import Any

# Representative exact producer payload after HTTP merge {ok:true, ...payload}.
# Ordering matches online-zapis builder (sortOrder then name/id).
ONLINE_ZAPIS_LIVE_FACTS_V1_REPRESENTATIVE: dict[str, Any] = {
    "ok": True,
    "schemaVersion": 1,
    "generatedAt": "2026-08-29T12:00:00.000Z",
    "studio": {
        "name": "Студия",
        "phone": "8 912 000-00-00",
        "email": "a@b.c",
        "address": "Адрес",
        "workingHoursText": "10–20",
        "isOnlineBookingEnabled": True,
    },
    "services": [
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "name": "Альфа",
            "category": None,
            "priceFrom": "2000",
            "priceTo": None,
            "currency": "RUB",
            "durationMinutes": 45,
            "bookingMode": "MANAGER_ONLY",
            "isActive": True,
            "isOnlineBookingEnabled": False,
        },
        {
            "id": "22222222-2222-4222-8222-222222222222",
            "name": "Бета",
            "category": "Категория",
            "priceFrom": "3500.00",
            "priceTo": "3500.00",
            "currency": "RUB",
            "durationMinutes": 60,
            "bookingMode": "ONLINE",
            "isActive": True,
            "isOnlineBookingEnabled": True,
        },
        {
            "id": "33333333-3333-4333-8333-333333333333",
            "name": "Неактивная",
            "category": "Категория",
            "priceFrom": None,
            "priceTo": None,
            "currency": "RUB",
            "durationMinutes": 30,
            "bookingMode": "MANAGER_ONLY",
            "isActive": False,
            "isOnlineBookingEnabled": True,
        },
    ],
    "masters": [
        {
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "name": "Мастер",
            "isActive": True,
            "isOnlineBookingEnabled": True,
            "serviceIds": [
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
            ],
        }
    ],
}

LIVE_FACTS_PRODUCER_ENDPOINT = (
    "online-zapis-tv /api/internal/bot/v1/live-facts schemaVersion=1"
)
