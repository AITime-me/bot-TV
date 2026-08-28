"""A2.2 booking-method analytics unit proofs."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

import pytest

from app.core.amocrm_analytics_fields import (
    AmoCrmAnalyticsBookingMethodEnum,
    AmoCrmAnalyticsFieldId,
)
from app.core.amocrm_deal_discovery import (
    AmoCrmDealDiscoveryOutcome,
    AmoCrmDealDiscoveryResult,
)
from app.core.amocrm_identity_lookup import (
    AmoCrmIdentityLookupOutcome,
    AmoCrmIdentityLookupResult,
)
from app.core.booking_eligibility_http import BookingEligibilityHttpConfig
from app.core.booking_method_http import (
    BookingMethodHttpClient,
    BookingMethodHttpError,
)
from app.core.booking_method_remote import (
    parse_booking_method_context_payload,
    parse_booking_method_feed_item,
    parse_booking_method_feed_payload,
    require_feed_creator_kind,
)
from app.core.booking_method_types import (
    BOOKING_METHOD_ANALYTICS_LOOP,
    CREATOR_KIND_TO_ENUM_ID,
    FEED_CURSOR_ID,
    TERMINAL_BOOKING_METHOD_STATES,
    BookingMethodCreatorKind,
    BookingMethodPendingState,
    enum_id_for_creator_kind,
)
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.models.worker_heartbeat import REQUIRED_WORKER_LOOPS
from app.services.teya_request_crm import (
    TeyaCrmActionOutcome,
    TeyaRequestCrmService,
)

_TOKEN = "a" * 32
_AID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@dataclass
class _FakeTransport:
    responses: list[S2sHttpResponse | BaseException]
    calls: list[S2sHttpRequest] = field(default_factory=list)

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        if not self.responses:
            raise AssertionError("unexpected HTTP call")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _json_response(status: int, payload: object | None = None) -> S2sHttpResponse:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return S2sHttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        body=body,
    )


def _config() -> BookingEligibilityHttpConfig:
    return BookingEligibilityHttpConfig(
        base_url="https://booking.example",
        bearer_token=_TOKEN,
        timeout_seconds=3.0,
        max_response_bytes=65536,
    )


def test_creator_kind_enum_mapping() -> None:
    assert enum_id_for_creator_kind(BookingMethodCreatorKind.SELF_SERVICE) == 851489
    assert enum_id_for_creator_kind("MANAGER") == 851493
    assert enum_id_for_creator_kind(BookingMethodCreatorKind.MASTER) == 851495
    assert CREATOR_KIND_TO_ENUM_ID[BookingMethodCreatorKind.SELF_SERVICE] == int(
        AmoCrmAnalyticsBookingMethodEnum.SELF_SERVICE
    )
    assert CREATOR_KIND_TO_ENUM_ID[BookingMethodCreatorKind.MANAGER] == int(
        AmoCrmAnalyticsBookingMethodEnum.MANAGER
    )
    assert CREATOR_KIND_TO_ENUM_ID[BookingMethodCreatorKind.MASTER] == int(
        AmoCrmAnalyticsBookingMethodEnum.MASTER
    )
    assert int(AmoCrmAnalyticsFieldId.BOOKING_CREATION_METHOD) == 1321305


def test_teya_other_null_excluded_from_feed_kinds() -> None:
    for bad in ("TEYA", "OTHER", "NULL", None, "", "teya"):
        with pytest.raises(ValueError):
            require_feed_creator_kind(bad)
        with pytest.raises(ValueError):
            enum_id_for_creator_kind(bad)  # type: ignore[arg-type]


def test_terminal_states() -> None:
    assert BookingMethodPendingState.DONE in TERMINAL_BOOKING_METHOD_STATES
    assert BookingMethodPendingState.MANUAL_REVIEW in TERMINAL_BOOKING_METHOD_STATES
    assert BookingMethodPendingState.SKIPPED in TERMINAL_BOOKING_METHOD_STATES
    assert BookingMethodPendingState.DISCOVERED not in TERMINAL_BOOKING_METHOD_STATES


def test_feed_cursor_id_and_loop() -> None:
    assert FEED_CURSOR_ID == "booking_method"
    assert BOOKING_METHOD_ANALYTICS_LOOP in REQUIRED_WORKER_LOOPS
    assert BOOKING_METHOD_ANALYTICS_LOOP == "booking_method_analytics"


def test_parse_feed_item_and_page() -> None:
    item = parse_booking_method_feed_item(
        {
            "appointmentId": _AID,
            "creatorKind": "SELF_SERVICE",
            "createdAt": "2026-08-26T10:00:00.000Z",
        }
    )
    assert item.appointment_id == _AID
    assert item.creator_kind is BookingMethodCreatorKind.SELF_SERVICE
    page = parse_booking_method_feed_payload(
        {
            "ok": True,
            "items": [
                {
                    "appointmentId": _AID,
                    "creatorKind": "MANAGER",
                    "createdAt": "2026-08-26T10:00:00.000Z",
                }
            ],
            "nextCursor": None,
        }
    )
    assert len(page.items) == 1
    assert page.items[0].creator_kind is BookingMethodCreatorKind.MANAGER


def test_parse_rejects_teya_in_feed() -> None:
    with pytest.raises(ValueError):
        parse_booking_method_feed_item(
            {
                "appointmentId": _AID,
                "creatorKind": "TEYA",
                "createdAt": "2026-08-26T10:00:00.000Z",
            }
        )


def test_parse_context_redacts_phone_in_repr() -> None:
    dto = parse_booking_method_context_payload(
        {
            "ok": True,
            "appointmentId": _AID,
            "creatorKind": "MASTER",
            "phoneE164": "+79001234567",
        }
    )
    text = repr(dto)
    assert "+79001234567" not in text
    assert "phone_e164=<redacted>" in text
    assert dto.phone_e164 == "+79001234567"


def test_feed_404_is_unavailable() -> None:
    transport = _FakeTransport([_json_response(404, {"ok": False, "code": "NOT_FOUND"})])
    client = BookingMethodHttpClient(_config(), transport)
    with pytest.raises(BookingMethodHttpError) as exc:
        client.feed()
    assert exc.value.code == "FEED_UNAVAILABLE"


def test_feed_unavailable_envelope() -> None:
    transport = _FakeTransport(
        [_json_response(404, {"ok": False, "code": "UNAVAILABLE", "error": "x"})]
    )
    client = BookingMethodHttpClient(_config(), transport)
    with pytest.raises(BookingMethodHttpError) as exc:
        client.feed()
    assert exc.value.code == "FEED_UNAVAILABLE"


def test_feed_bare_404_unavailable() -> None:
    transport = _FakeTransport(
        [
            S2sHttpResponse(
                status_code=404,
                headers={"content-type": "text/html"},
                body=b"<html>not found</html>",
            )
        ]
    )
    client = BookingMethodHttpClient(_config(), transport)
    with pytest.raises(BookingMethodHttpError) as exc:
        client.feed()
    assert exc.value.code == "FEED_UNAVAILABLE"


def test_context_404_not_found_permanent() -> None:
    transport = _FakeTransport(
        [_json_response(404, {"ok": False, "code": "NOT_FOUND", "error": "Not found"})]
    )
    client = BookingMethodHttpClient(_config(), transport)
    with pytest.raises(BookingMethodHttpError) as exc:
        client.context(appointment_id=_AID)
    assert exc.value.code == "NOT_FOUND"


def test_context_404_non_contract_unavailable() -> None:
    transport = _FakeTransport(
        [
            S2sHttpResponse(
                status_code=404,
                headers={"content-type": "text/html"},
                body=b"<html>missing</html>",
            )
        ]
    )
    client = BookingMethodHttpClient(_config(), transport)
    with pytest.raises(BookingMethodHttpError) as exc:
        client.context(appointment_id=_AID)
    assert exc.value.code == "CONTEXT_UNAVAILABLE"


def test_context_429_rate_limited() -> None:
    transport = _FakeTransport(
        [_json_response(429, {"ok": False, "code": "RATE_LIMITED", "error": "Too many"})]
    )
    client = BookingMethodHttpClient(_config(), transport)
    with pytest.raises(BookingMethodHttpError) as exc:
        client.context(appointment_id=_AID)
    assert exc.value.code == "RATE_LIMITED"


def test_context_401_auth_unavailable() -> None:
    transport = _FakeTransport(
        [_json_response(401, {"ok": False, "code": "UNAUTHORIZED", "error": "Unauthorized"})]
    )
    client = BookingMethodHttpClient(_config(), transport)
    with pytest.raises(BookingMethodHttpError) as exc:
        client.context(appointment_id=_AID)
    assert exc.value.code == "AUTH_UNAVAILABLE"


def test_context_5xx_internal() -> None:
    transport = _FakeTransport([_json_response(503, None)])
    client = BookingMethodHttpClient(_config(), transport)
    with pytest.raises(BookingMethodHttpError) as exc:
        client.context(appointment_id=_AID)
    assert exc.value.code == "INTERNAL_ERROR"


def test_teya_enum_untouched_by_a22_map() -> None:
    assert int(AmoCrmAnalyticsBookingMethodEnum.TEYA) == 851491
    assert "TEYA" not in {k.value for k in BookingMethodCreatorKind}
    assert BookingMethodCreatorKind.SELF_SERVICE.value not in {"TEYA", "OTHER"}


@dataclass
class _Identity:
    result: AmoCrmIdentityLookupResult
    calls: list[str] = field(default_factory=list)

    async def lookup_by_phone(self, *, phone_e164: str) -> AmoCrmIdentityLookupResult:
        self.calls.append(phone_e164)
        return self.result


@dataclass
class _Deals:
    result: AmoCrmDealDiscoveryResult
    calls: list[str] = field(default_factory=list)

    async def discover_deal_candidates(
        self, *, contact_id: str, known_technical_deal_ids: tuple[str, ...] = ()
    ):
        self.calls.append(contact_id)
        return self.result


class _Tokens:
    async def access_token(self) -> str | None:
        raise AssertionError("discover path must not require token")

    async def refresh_access_token(self, *, rejected_access_token: str) -> str | None:
        del rejected_access_token
        raise AssertionError("discover path must not refresh token")


class _Writes:
    def create_contact(self, **_kwargs):
        raise AssertionError("must not create contact")

    def create_lead(self, **_kwargs):
        raise AssertionError("must not create lead")

    def reanimate_lead(self, **_kwargs):
        raise AssertionError("must not reanimate")


@pytest.mark.asyncio
async def test_discover_existing_ready_no_create() -> None:
    identity = _Identity(
        AmoCrmIdentityLookupResult(
            outcome=AmoCrmIdentityLookupOutcome.FOUND, contact_id="11"
        )
    )
    deals = _Deals(
        AmoCrmDealDiscoveryResult(
            outcome=AmoCrmDealDiscoveryOutcome.FOUND,
            contact_id="11",
            business_active_lead_ids=("55",),
        )
    )
    crm = TeyaRequestCrmService(
        identity_lookup=identity,
        deal_discovery=deals,
        writes=_Writes(),  # type: ignore[arg-type]
        tokens=_Tokens(),
    )
    result = await crm.discover_existing_business_deal(phone_e164="+79001234567")
    assert result.outcome is TeyaCrmActionOutcome.READY
    assert result.deal_id == "55"
    assert result.contact_id == "11"


@pytest.mark.asyncio
async def test_discover_none_deal_and_contact() -> None:
    crm = TeyaRequestCrmService(
        identity_lookup=_Identity(
            AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.NOT_FOUND
            )
        ),
        deal_discovery=_Deals(
            AmoCrmDealDiscoveryResult(outcome=AmoCrmDealDiscoveryOutcome.FOUND)
        ),
        writes=_Writes(),  # type: ignore[arg-type]
        tokens=_Tokens(),
    )
    none_contact = await crm.discover_existing_business_deal(
        phone_e164="+79001234567"
    )
    assert none_contact.outcome is TeyaCrmActionOutcome.NONE
    assert none_contact.error_code == "CONTACT_NONE"

    crm2 = TeyaRequestCrmService(
        identity_lookup=_Identity(
            AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.FOUND, contact_id="11"
            )
        ),
        deal_discovery=_Deals(
            AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.FOUND,
                contact_id="11",
                business_active_lead_ids=(),
            )
        ),
        writes=_Writes(),  # type: ignore[arg-type]
        tokens=_Tokens(),
    )
    none_deal = await crm2.discover_existing_business_deal(phone_e164="+79001234567")
    assert none_deal.outcome is TeyaCrmActionOutcome.NONE
    assert none_deal.error_code == "DEAL_NONE"


@pytest.mark.asyncio
async def test_discover_ambiguous_manual() -> None:
    crm = TeyaRequestCrmService(
        identity_lookup=_Identity(
            AmoCrmIdentityLookupResult(
                outcome=AmoCrmIdentityLookupOutcome.FOUND, contact_id="11"
            )
        ),
        deal_discovery=_Deals(
            AmoCrmDealDiscoveryResult(
                outcome=AmoCrmDealDiscoveryOutcome.FOUND,
                contact_id="11",
                business_active_lead_ids=("1", "2"),
            )
        ),
        writes=_Writes(),  # type: ignore[arg-type]
        tokens=_Tokens(),
    )
    result = await crm.discover_existing_business_deal(phone_e164="+79001234567")
    assert result.outcome is TeyaCrmActionOutcome.MANUAL_REVIEW
    assert result.error_code == "ACTIVE_DEAL_AMBIGUOUS"


def test_pending_repr_has_no_phone() -> None:
    from datetime import datetime, timezone

    from app.models.booking_method_analytics_pending import (
        BookingMethodAnalyticsPending,
    )

    row = BookingMethodAnalyticsPending(
        id=uuid.uuid4(),
        appointment_id=uuid.UUID(_AID),
        purpose="BOOKING_CREATION_METHOD",
        creator_kind="SELF_SERVICE",
        state="DISCOVERED",
        attempt_count=0,
        max_attempts=8,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    text = repr(row)
    assert "phone" not in text.lower()
    assert "appointment_id=<redacted>" in text


def test_worker_runtime_includes_booking_method_loop() -> None:
    from unittest.mock import AsyncMock

    from app.config import Settings
    from app.services.worker_runtime import build_default_loop_specs

    settings = Settings(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:5432/bot_tv_foundation_test",
        worker_heartbeat_interval_seconds=1,
        worker_heartbeat_stale_seconds=45,
    )
    specs = build_default_loop_specs(
        settings=settings,
        session_factory=AsyncMock(),
        worker_id="unit-worker",
    )
    assert BOOKING_METHOD_ANALYTICS_LOOP in REQUIRED_WORKER_LOOPS
    assert tuple(spec.name for spec in specs) == REQUIRED_WORKER_LOOPS
