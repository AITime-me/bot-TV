"""Contract alignment: bot-TV parsers match online-zapis BookingRequest DTOs."""

from __future__ import annotations

from app.core.booking_request_remote import (
    AppointmentsLookupOutcome,
    BookingRequestFeedCursor,
    build_appointments_lookup_body,
    build_feed_request_body,
    build_get_request_body,
    parse_appointments_lookup_payload,
    parse_bot_booking_request_dto,
    parse_booking_request_feed_payload,
)

_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_APT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_CLIENT = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
_MASTER = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
_SERVICE = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"


def test_parse_online_zapis_dto_shape() -> None:
    dto = parse_bot_booking_request_dto(
        {
            "id": _ID,
            "type": "CONSULTATION_REQUEST",
            "status": "NEW",
            "createdAt": "2026-08-25T10:00:00.000Z",
            "updatedAt": "2026-08-25T10:00:00.000Z",
            "clientName": "Elena",
            "clientPhone": "+79001234567",
            "masterId": _MASTER,
            "serviceId": _SERVICE,
            "serviceNameSnapshot": None,
            "clientId": _CLIENT,
            "appointmentId": None,
            "gameCatalogId": None,
            "gameContext": {
                "gameTitle": "Wheel",
                "giftName": "mask",
                "procedure": "clean",
                "zone": "face",
                "activationMode": "SINGLE_PAID_SERVICE",
                "minCourseSessions": None,
                "prizeType": None,
                "eligibility": {
                    "activationMode": "SINGLE_PAID_SERVICE",
                    "minCourseSessions": None,
                    "managerConfirmationRequired": True,
                },
            },
        }
    )
    assert dto.request_id == _ID
    assert dto.request_type == "CONSULTATION_REQUEST"
    assert dto.phone_e164 == "+79001234567"
    assert dto.game_context is not None
    assert dto.game_context.gift == "mask"
    assert dto.game_context.procedure == "clean"


def test_feed_cursor_object_roundtrip() -> None:
    body = build_feed_request_body(
        limit=20,
        cursor=BookingRequestFeedCursor(
            created_at="2026-08-25T10:00:00.000Z", id=_ID
        ),
    )
    assert body["cursor"] == {"createdAt": "2026-08-25T10:00:00.000Z", "id": _ID}
    page = parse_booking_request_feed_payload(
        {
            "ok": True,
            "items": [],
            "nextCursor": {"createdAt": "2026-08-25T10:00:00.000Z", "id": _ID},
        }
    )
    assert page.next_cursor is not None
    assert page.next_cursor.id == _ID


def test_get_body_uses_id() -> None:
    assert build_get_request_body(request_id=_ID) == {"id": _ID}


def test_lookup_body_phone_and_client() -> None:
    assert build_appointments_lookup_body(phone="+79001234567") == {
        "phone": "+79001234567"
    }
    assert build_appointments_lookup_body(client_id=_CLIENT) == {
        "clientId": _CLIENT
    }


def test_lookup_unique_client_zero_appointments_is_none() -> None:
    result = parse_appointments_lookup_payload(
        {
            "ok": True,
            "clientOutcome": "UNIQUE",
            "clientId": _CLIENT,
            "appointments": [],
        }
    )
    assert result.outcome is AppointmentsLookupOutcome.NONE


def test_lookup_unique_client_one_appointment() -> None:
    result = parse_appointments_lookup_payload(
        {
            "ok": True,
            "clientOutcome": "UNIQUE",
            "clientId": _CLIENT,
            "appointments": [
                {
                    "id": _APT,
                    "clientId": _CLIENT,
                    "masterId": _MASTER,
                    "serviceId": _SERVICE,
                    "startsAt": "2026-08-26T10:00:00.000Z",
                    "createdAt": "2026-08-25T10:00:00.000Z",
                    "status": "SCHEDULED",
                    "source": "ONLINE",
                }
            ],
        }
    )
    assert result.outcome is AppointmentsLookupOutcome.UNIQUE
    assert result.appointment_id == _APT


def test_lookup_unique_client_many_appointments_ambiguous() -> None:
    apt2 = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    result = parse_appointments_lookup_payload(
        {
            "ok": True,
            "clientOutcome": "UNIQUE",
            "clientId": _CLIENT,
            "appointments": [
                {"id": _APT},
                {"id": apt2},
            ],
        }
    )
    assert result.outcome is AppointmentsLookupOutcome.AMBIGUOUS
    assert len(result.appointment_ids) == 2
