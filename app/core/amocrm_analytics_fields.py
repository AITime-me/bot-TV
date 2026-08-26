"""Centralized live amoCRM analytics custom-field contract (account-specific IDs).

Proven 2026-08-25. Do not discover fields dynamically. Do not write Channel.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Final

__all__ = (
    "AMOCRM_ANALYTICS_BOOKING_METHOD_ENUM_IDS",
    "AMOCRM_ANALYTICS_CHANNEL_FIELD_ID",
    "AMOCRM_ANALYTICS_SOURCE_PRIMARY_ENUM_IDS",
    "AMOCRM_ANALYTICS_WRITABLE_FIELD_IDS",
    "AmoCrmAnalyticsApplyDecision",
    "AmoCrmAnalyticsBookingMethodEnum",
    "AmoCrmAnalyticsFieldId",
    "AmoCrmAnalyticsSourcePrimaryEnum",
    "assert_enum_allowed_for_field",
    "assert_writable_analytics_field_id",
)


class AmoCrmAnalyticsFieldId(IntEnum):
    """Lead custom field IDs for analytics writes."""

    SOURCE_PRIMARY = 1258095
    BOOKING_CREATION_METHOD = 1321305


# Contact Channel — owned by amoCRM automation. NEVER write via analytics adapter.
AMOCRM_ANALYTICS_CHANNEL_FIELD_ID: Final[int] = 1321303

AMOCRM_ANALYTICS_WRITABLE_FIELD_IDS: Final[frozenset[int]] = frozenset(
    {
        int(AmoCrmAnalyticsFieldId.SOURCE_PRIMARY),
        int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD),
    }
)


class AmoCrmAnalyticsSourcePrimaryEnum(IntEnum):
    """Источник первичный enum_ids (field 1258095)."""

    SITE = 721341
    CASTDEV = 723321
    VK_ADS = 723323
    YANDEX = 730677
    WALK_IN = 782973
    TWO_GIS = 784167
    VK_CONTENT = 784331
    WORD_OF_MOUTH = 809011
    UNDEFINED = 851499
    REPEAT_CLIENT = 851501


class AmoCrmAnalyticsBookingMethodEnum(IntEnum):
    """Способ создания записи enum_ids (field 1321305)."""

    SELF_SERVICE = 851489
    TEYA = 851491
    MANAGER = 851493
    MASTER = 851495
    OTHER = 851497


AMOCRM_ANALYTICS_SOURCE_PRIMARY_ENUM_IDS: Final[frozenset[int]] = frozenset(
    int(v) for v in AmoCrmAnalyticsSourcePrimaryEnum
)

AMOCRM_ANALYTICS_BOOKING_METHOD_ENUM_IDS: Final[frozenset[int]] = frozenset(
    int(v) for v in AmoCrmAnalyticsBookingMethodEnum
)


class AmoCrmAnalyticsApplyDecision(StrEnum):
    """Durable decision for an analytics field apply attempt."""

    APPLIED = "APPLIED"
    ALREADY_SAME = "ALREADY_SAME"
    CONFLICT_NONEMPTY = "CONFLICT_NONEMPTY"
    SKIPPED_NO_EVIDENCE = "SKIPPED_NO_EVIDENCE"
    TRANSIENT_RETRY = "TRANSIENT_RETRY"
    MANUAL_REVIEW = "MANUAL_REVIEW"


def assert_writable_analytics_field_id(field_id: int) -> None:
    """Reject Channel and any non-allowlisted analytics field."""

    if type(field_id) is not int or isinstance(field_id, bool) or field_id <= 0:
        raise ValueError("AMOCRM_ANALYTICS_FIELD_ID_INVALID")
    if field_id == AMOCRM_ANALYTICS_CHANNEL_FIELD_ID:
        raise ValueError("AMOCRM_ANALYTICS_CHANNEL_WRITE_FORBIDDEN")
    if field_id not in AMOCRM_ANALYTICS_WRITABLE_FIELD_IDS:
        raise ValueError("AMOCRM_ANALYTICS_FIELD_NOT_WRITABLE")


def assert_enum_allowed_for_field(field_id: int, enum_id: int) -> None:
    """Reject cross-field / unknown enums before any analytics PATCH."""

    assert_writable_analytics_field_id(field_id)
    if type(enum_id) is not int or isinstance(enum_id, bool) or enum_id <= 0:
        raise ValueError("AMOCRM_ANALYTICS_ENUM_ID_INVALID")
    if field_id == int(AmoCrmAnalyticsFieldId.SOURCE_PRIMARY):
        if enum_id not in AMOCRM_ANALYTICS_SOURCE_PRIMARY_ENUM_IDS:
            raise ValueError("AMOCRM_ANALYTICS_ENUM_NOT_ALLOWED_FOR_FIELD")
        return
    if field_id == int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD):
        if enum_id not in AMOCRM_ANALYTICS_BOOKING_METHOD_ENUM_IDS:
            raise ValueError("AMOCRM_ANALYTICS_ENUM_NOT_ALLOWED_FOR_FIELD")
        return
    raise ValueError("AMOCRM_ANALYTICS_FIELD_NOT_WRITABLE")
