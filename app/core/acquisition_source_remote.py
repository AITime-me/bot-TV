"""Remote DTOs for A2.3b2 acquisition-source feed + context S2S.

Wire contract (online-zapis-tv):
- POST /api/internal/bot/v1/acquisition-source/feed
- POST /api/internal/bot/v1/acquisition-source/context

Fail-closed parse. Repr never prints phone/PII.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from app.core.booking_eligibility_http import (
    BookingEligibilityHttpError,
    require_canonical_backend_uuid,
)
from app.core.acquisition_source_types import AcquisitionSourceOwnerKind

ACQUISITION_SOURCE_FEED_PATH: Final[str] = (
    "/api/internal/bot/v1/acquisition-source/feed"
)
ACQUISITION_SOURCE_CONTEXT_PATH: Final[str] = (
    "/api/internal/bot/v1/acquisition-source/context"
)

_CANONICAL_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_PHONE_E164_RE: Final[re.Pattern[str]] = re.compile(r"^\+\d{8,15}$")

_POSITIVE_DECIMAL_RE: Final[re.Pattern[str]] = re.compile(r"^[1-9]\d*$")

_ALLOWED_SOURCE_KEYS: Final[frozenset[str]] = frozenset(
    {"VK_ADS", "VK_CONTENT", "YANDEX", "TWO_GIS"}
)

_OWNER_KINDS: Final[frozenset[str]] = frozenset(
    k.value for k in AcquisitionSourceOwnerKind
)

ACQUISITION_SOURCE_REMOTE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "VALIDATION_ERROR",
        "PAYLOAD_TOO_LARGE",
        "UNAUTHORIZED",
        "RATE_LIMITED",
        "NOT_FOUND",
        "INTERNAL_ERROR",
        "UNAVAILABLE",
    }
)

REMOTE_ERROR_CODE_BY_STATUS: Final[dict[int, frozenset[str]]] = {
    400: frozenset({"VALIDATION_ERROR"}),
    401: frozenset({"UNAUTHORIZED"}),
    403: frozenset({"UNAUTHORIZED"}),
    404: frozenset({"NOT_FOUND", "UNAVAILABLE"}),
    413: frozenset({"PAYLOAD_TOO_LARGE"}),
    429: frozenset({"RATE_LIMITED"}),
    500: frozenset({"INTERNAL_ERROR"}),
    502: frozenset({"INTERNAL_ERROR"}),
    503: frozenset({"INTERNAL_ERROR"}),
    504: frozenset({"INTERNAL_ERROR"}),
}


def require_canonical_evidence_id(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("ACQUISITION_SOURCE_EVIDENCE_ID_INVALID") from None
    if any(ch.isspace() for ch in value):
        raise ValueError("ACQUISITION_SOURCE_EVIDENCE_ID_INVALID") from None
    if value != value.lower():
        raise ValueError("ACQUISITION_SOURCE_EVIDENCE_ID_INVALID") from None
    if len(value) != 36 or _CANONICAL_UUID_RE.fullmatch(value) is None:
        raise ValueError("ACQUISITION_SOURCE_EVIDENCE_ID_INVALID") from None
    try:
        return require_canonical_backend_uuid(value)
    except BookingEligibilityHttpError as exc:
        raise ValueError("ACQUISITION_SOURCE_EVIDENCE_ID_INVALID") from exc


def require_owner_kind(value: object) -> AcquisitionSourceOwnerKind:
    if type(value) is not str or value not in _OWNER_KINDS:
        raise ValueError("ACQUISITION_SOURCE_OWNER_KIND_INVALID") from None
    return AcquisitionSourceOwnerKind(value)


def require_source_key(value: object) -> str:
    if type(value) is not str or value not in _ALLOWED_SOURCE_KEYS:
        raise ValueError("ACQUISITION_SOURCE_KEY_INVALID") from None
    return value


def require_phone_e164(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("ACQUISITION_SOURCE_PHONE_INVALID") from None
    if len(value) > 32 or _PHONE_E164_RE.fullmatch(value) is None:
        raise ValueError("ACQUISITION_SOURCE_PHONE_INVALID") from None
    return value


def require_feed_order(value: object) -> str:
    if type(value) is not str or not value or len(value) > 32:
        raise ValueError("ACQUISITION_SOURCE_FEED_ORDER_INVALID") from None
    if _POSITIVE_DECIMAL_RE.fullmatch(value) is None:
        raise ValueError("ACQUISITION_SOURCE_FEED_ORDER_INVALID") from None
    return value


@dataclass(frozen=True, slots=True, repr=False)
class AcquisitionSourceFeedCursor:
    feed_order: str
    evidence_id: str

    def __repr__(self) -> str:
        return (
            "AcquisitionSourceFeedCursor("
            f"feed_order={'set' if self.feed_order else None}, "
            "evidence_id=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AcquisitionSourceFeedItem:
    evidence_id: str
    owner_kind: AcquisitionSourceOwnerKind
    owner_id: str
    source_key: str
    consumed_at: str
    feed_order: str

    def __repr__(self) -> str:
        return (
            "AcquisitionSourceFeedItem("
            "evidence_id=<redacted>, "
            f"owner_kind={self.owner_kind.value!r}, "
            "owner_id=<redacted>, "
            f"source_key={self.source_key!r}, "
            f"consumed_at={'set' if self.consumed_at else None}, "
            f"feed_order={'set' if self.feed_order else None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AcquisitionSourceFeedPage:
    items: tuple[AcquisitionSourceFeedItem, ...]
    next_cursor: AcquisitionSourceFeedCursor | None = None

    def __repr__(self) -> str:
        return (
            "AcquisitionSourceFeedPage("
            f"items_count={len(self.items)}, "
            f"next_cursor={'set' if self.next_cursor else None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AcquisitionSourceContextDto:
    evidence_id: str
    owner_kind: AcquisitionSourceOwnerKind
    owner_id: str
    source_key: str
    phone_e164: str

    def __repr__(self) -> str:
        return (
            "AcquisitionSourceContextDto("
            "evidence_id=<redacted>, "
            f"owner_kind={self.owner_kind.value!r}, "
            "owner_id=<redacted>, "
            f"source_key={self.source_key!r}, "
            "phone_e164=<redacted>)"
        )


def _parse_feed_cursor(raw: object) -> AcquisitionSourceFeedCursor:
    if type(raw) is not dict:
        raise ValueError("ACQUISITION_SOURCE_FEED_CURSOR_INVALID") from None
    feed_order = require_feed_order(raw.get("feedOrder"))
    evidence_id = raw.get("evidenceId")
    require_canonical_evidence_id(evidence_id)
    return AcquisitionSourceFeedCursor(
        feed_order=feed_order, evidence_id=str(evidence_id)
    )


def parse_acquisition_source_feed_item(
    payload: object,
) -> AcquisitionSourceFeedItem:
    if type(payload) is not dict:
        raise ValueError("ACQUISITION_SOURCE_FEED_ITEM_INVALID") from None
    evidence_id = require_canonical_evidence_id(payload.get("evidenceId"))
    owner_kind = require_owner_kind(payload.get("ownerKind"))
    owner_id = require_canonical_evidence_id(payload.get("ownerId"))
    source_key = require_source_key(payload.get("sourceKey"))
    consumed_at = payload.get("consumedAt")
    if type(consumed_at) is not str or not consumed_at or len(consumed_at) > 64:
        raise ValueError("ACQUISITION_SOURCE_FEED_ITEM_INVALID") from None
    feed_order = require_feed_order(payload.get("feedOrder"))
    return AcquisitionSourceFeedItem(
        evidence_id=evidence_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        source_key=source_key,
        consumed_at=consumed_at,
        feed_order=feed_order,
    )


def parse_acquisition_source_feed_payload(
    payload: object,
) -> AcquisitionSourceFeedPage:
    if type(payload) is not dict:
        raise ValueError("ACQUISITION_SOURCE_FEED_INVALID") from None
    if payload.get("ok") is not True:
        raise ValueError("ACQUISITION_SOURCE_FEED_INVALID") from None
    items_raw = payload.get("items")
    if type(items_raw) is not list:
        raise ValueError("ACQUISITION_SOURCE_FEED_INVALID") from None
    items = tuple(
        parse_acquisition_source_feed_item(item) for item in items_raw
    )
    next_cursor_raw = payload.get("nextCursor")
    next_cursor: AcquisitionSourceFeedCursor | None = None
    if next_cursor_raw is not None:
        next_cursor = _parse_feed_cursor(next_cursor_raw)
    return AcquisitionSourceFeedPage(items=items, next_cursor=next_cursor)


def parse_acquisition_source_context_payload(
    payload: object,
) -> AcquisitionSourceContextDto:
    if type(payload) is not dict:
        raise ValueError("ACQUISITION_SOURCE_CONTEXT_INVALID") from None
    if payload.get("ok") is not True:
        raise ValueError("ACQUISITION_SOURCE_CONTEXT_INVALID") from None
    evidence_id = require_canonical_evidence_id(payload.get("evidenceId"))
    owner_kind = require_owner_kind(payload.get("ownerKind"))
    owner_id = require_canonical_evidence_id(payload.get("ownerId"))
    source_key = require_source_key(payload.get("sourceKey"))
    phone_e164 = require_phone_e164(payload.get("phoneE164"))
    return AcquisitionSourceContextDto(
        evidence_id=evidence_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        source_key=source_key,
        phone_e164=phone_e164,
    )


def build_feed_request_body(
    *,
    limit: int = 20,
    cursor: AcquisitionSourceFeedCursor | None = None,
) -> dict[str, object]:
    if type(limit) is not int or isinstance(limit, bool) or limit < 1 or limit > 50:
        raise ValueError("ACQUISITION_SOURCE_FEED_LIMIT_INVALID") from None
    body: dict[str, object] = {"limit": limit}
    if cursor is not None:
        if type(cursor) is not AcquisitionSourceFeedCursor:
            raise ValueError("ACQUISITION_SOURCE_FEED_CURSOR_INVALID") from None
        body["cursor"] = {
            "feedOrder": cursor.feed_order,
            "evidenceId": cursor.evidence_id,
        }
    return body


def build_context_request_body(
    *,
    evidence_id: object,
    owner_kind: object,
    owner_id: object,
) -> dict[str, object]:
    eid = require_canonical_evidence_id(evidence_id)
    kind = require_owner_kind(owner_kind)
    oid = require_canonical_evidence_id(owner_id)
    return {
        "evidenceId": eid,
        "ownerKind": kind.value,
        "ownerId": oid,
    }
