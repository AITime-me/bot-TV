"""Authoritative user-facing bot reply text for durable SYNTHETIC_OUTBOUND.

BOT-REPLY-DURABLE-01: rendered client copy is persisted as outbound
``payload_json.text`` before INSERT. Delivery/retry reads only that field —
never re-renders and never falls back to inbound text, INTERNAL_DRAFT,
manager hints, or ``synthetic_token``.
"""

from __future__ import annotations

from typing import Any, Final, Mapping

from app.core.booking_types import (
    BookingClientMessageKind,
    BookingDialogAction,
    BookingDomainError,
    render_client_message,
)

__all__ = (
    "OutboundReplyTextError",
    "is_machine_only_outbound_payload",
    "persisted_outbound_reply_text",
    "require_persisted_outbound_text",
    "render_text_for_booking_fields",
)

_HANDOFF_KINDS: Final[frozenset[str]] = frozenset(
    {
        BookingClientMessageKind.HANDOFF_DURING_MANAGER_HOURS.value,
        BookingClientMessageKind.HANDOFF_OUTSIDE_MANAGER_HOURS.value,
    }
)


class OutboundReplyTextError(ValueError):
    """Fail-closed: no durable authoritative user-facing reply text."""

    def __init__(self, code: str = "OUTBOUND_REPLY_TEXT_MISSING") -> None:
        self.code = code
        super().__init__(code)


def is_machine_only_outbound_payload(payload: Mapping[str, Any]) -> bool:
    """OFFER_DAYS is durable machine wire without client-facing copy yet."""

    return payload.get("booking_action") == BookingDialogAction.OFFER_DAYS.value


def persisted_outbound_reply_text(payload: object) -> str | None:
    """Return durable ``text`` only. No fallbacks to token/draft/inbound."""

    if type(payload) is not dict:
        return None
    text = payload.get("text")
    if type(text) is not str:
        return None
    if not text.strip():
        return None
    token = payload.get("synthetic_token")
    if type(token) is str and token and text == token:
        return None
    return text


def require_persisted_outbound_text(payload: object) -> str:
    """Authoritative body for delivery/retry. Fail closed if missing/invalid."""

    text = persisted_outbound_reply_text(payload)
    if text is None:
        raise OutboundReplyTextError("OUTBOUND_REPLY_TEXT_MISSING")
    return text


def render_text_for_booking_fields(fields: Mapping[str, Any]) -> str:
    """Map durable booking envelope fields through the domain client renderer.

    Reuses ``render_client_message`` only. Does not invent copy, does not echo
    client/manager text, and refuses machine-only ``OFFER_DAYS``.
    """

    action = fields.get("booking_action")
    if action == BookingDialogAction.OFFER_SLOTS.value:
        return render_client_message(BookingClientMessageKind.OFFER_SLOTS)
    if action == BookingDialogAction.SERVICE_UNAVAILABLE.value:
        return render_client_message(
            BookingClientMessageKind.SERVICE_TEMPORARILY_UNAVAILABLE
        )
    if action == BookingDialogAction.MANAGER_HANDOFF.value:
        kind_raw = fields.get("client_message_kind")
        if type(kind_raw) is not str or kind_raw not in _HANDOFF_KINDS:
            raise OutboundReplyTextError("OUTBOUND_REPLY_TEXT_MISSING")
        try:
            kind = BookingClientMessageKind(kind_raw)
        except ValueError as exc:
            raise OutboundReplyTextError("OUTBOUND_REPLY_TEXT_INVALID") from exc
        try:
            return render_client_message(kind)
        except BookingDomainError as exc:
            raise OutboundReplyTextError("OUTBOUND_REPLY_TEXT_INVALID") from exc
    if action == BookingDialogAction.OFFER_DAYS.value:
        raise OutboundReplyTextError("OUTBOUND_REPLY_TEXT_NOT_RENDERABLE")
    raise OutboundReplyTextError("OUTBOUND_REPLY_TEXT_MISSING")
