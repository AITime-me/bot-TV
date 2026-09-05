"""Durable VK CLIENT ingress adapter (shadow observer).

Persists webhook receipt only. Does not project to amoCRM, send VK outbound,
or run MasterCommand flow.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import session_scope
from app.models.ingress import IngressChannel, IngressEvent, IngressEventType
from app.repositories import ingress as ingress_repo
from app.repositories.ingress import DEFAULT_MAX_ATTEMPTS
from app.schemas.vk_client_ingress import (
    VkClientIngressEvent,
    VkClientMessageReplyIngressEvent,
)
from app.services.ingress import IngressAck, IngressPersistError


class VkClientIngressIdempotencyConflict(RuntimeError):
    """Same ingress key reused with a different conversation/envelope body."""

    def __init__(self) -> None:
        super().__init__("INGRESS_IDEMPOTENCY_CONFLICT")


class VkClientIngressAdapter:
    """VK client durable ingress adapter — persist then ACK."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self._session_factory = session_factory
        self._max_attempts = max_attempts

    async def accept(self, event: VkClientIngressEvent) -> IngressAck:
        """Persist first; ACK only after commit succeeds."""

        if event.channel != IngressChannel.VK.value:
            raise ValueError("UNSUPPORTED_CHANNEL")
        if event.event_type != IngressEventType.VK_CLIENT_MESSAGE.value:
            raise ValueError("UNSUPPORTED_EVENT_TYPE")

        correlation_id = event.correlation_id_or_new()
        try:
            async with session_scope(self._session_factory) as session:
                row, created = await ingress_repo.insert_if_absent(
                    session,
                    channel=IngressChannel.VK,
                    external_event_id=event.external_event_id,
                    external_conversation_id=event.external_conversation_id,
                    event_type=IngressEventType.VK_CLIENT_MESSAGE,
                    envelope_json=event.safe_envelope(),
                    correlation_id=correlation_id,
                    max_attempts=self._max_attempts,
                )
                if not created:
                    _assert_vk_duplicate_matches(row, event)
                ack = IngressAck(
                    accepted=True,
                    duplicate=not created,
                    event_id=row.id,
                    status=row.status,
                    correlation_id=row.correlation_id,
                )
        except VkClientIngressIdempotencyConflict:
            raise
        except Exception as exc:
            raise IngressPersistError(
                f"INGRESS_PERSIST_FAILED ({type(exc).__name__})"
            ) from None

        return ack

    async def accept_message_reply(
        self,
        event: VkClientMessageReplyIngressEvent,
    ) -> IngressAck:
        if event.channel != IngressChannel.VK.value:
            raise ValueError("UNSUPPORTED_CHANNEL")
        if event.event_type != IngressEventType.VK_CLIENT_MESSAGE_REPLY.value:
            raise ValueError("UNSUPPORTED_EVENT_TYPE")

        correlation_id = event.correlation_id_or_new()
        try:
            async with session_scope(self._session_factory) as session:
                row, created = await ingress_repo.insert_if_absent(
                    session,
                    channel=IngressChannel.VK,
                    external_event_id=event.external_event_id,
                    external_conversation_id=event.external_conversation_id,
                    event_type=IngressEventType.VK_CLIENT_MESSAGE_REPLY,
                    envelope_json=event.safe_envelope(),
                    correlation_id=correlation_id,
                    max_attempts=self._max_attempts,
                )
                if not created:
                    _assert_vk_reply_duplicate_matches(row, event)
                ack = IngressAck(
                    accepted=True,
                    duplicate=not created,
                    event_id=row.id,
                    status=row.status,
                    correlation_id=row.correlation_id,
                )
        except VkClientIngressIdempotencyConflict:
            raise
        except Exception as exc:
            raise IngressPersistError(
                f"INGRESS_PERSIST_FAILED ({type(exc).__name__})"
            ) from None

        return ack


def _assert_vk_duplicate_matches(
    row: IngressEvent,
    event: VkClientIngressEvent,
) -> None:
    """Identical key + payload → OK; altered conversation/body → conflict."""

    if row.channel != IngressChannel.VK.value:
        raise VkClientIngressIdempotencyConflict()
    if row.event_type != IngressEventType.VK_CLIENT_MESSAGE.value:
        raise VkClientIngressIdempotencyConflict()
    if row.external_event_id != event.external_event_id:
        raise VkClientIngressIdempotencyConflict()
    if row.external_conversation_id != event.external_conversation_id:
        raise VkClientIngressIdempotencyConflict()

    envelope = row.envelope_json if isinstance(row.envelope_json, dict) else {}
    expected = event.safe_envelope()
    if envelope.get("text") != expected.get("text"):
        raise VkClientIngressIdempotencyConflict()
    if envelope.get("event_type") != expected.get("event_type"):
        raise VkClientIngressIdempotencyConflict()


def _assert_vk_reply_duplicate_matches(
    row: IngressEvent,
    event: VkClientMessageReplyIngressEvent,
) -> None:
    if row.channel != IngressChannel.VK.value:
        raise VkClientIngressIdempotencyConflict()
    if row.event_type != IngressEventType.VK_CLIENT_MESSAGE_REPLY.value:
        raise VkClientIngressIdempotencyConflict()
    if row.external_event_id != event.external_event_id:
        raise VkClientIngressIdempotencyConflict()
    if row.external_conversation_id != event.external_conversation_id:
        raise VkClientIngressIdempotencyConflict()

    envelope = row.envelope_json if isinstance(row.envelope_json, dict) else {}
    expected = event.safe_envelope()
    for key in (
        "provider_message_id",
        "conversation_message_id",
        "group_id",
        "peer_id",
        "event_type",
    ):
        if envelope.get(key) != expected.get(key):
            raise VkClientIngressIdempotencyConflict()
