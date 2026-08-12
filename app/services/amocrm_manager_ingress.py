"""Durable amoCRM manager ingress adapter (AMO-01A).

Persists webhook receipt only. Does not apply manager FSM, call outbound HTTP,
or touch booking/AI/VK/MAX adapters.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import session_scope
from app.models.ingress import IngressChannel, IngressEvent, IngressEventType
from app.repositories import ingress as ingress_repo
from app.repositories.ingress import DEFAULT_MAX_ATTEMPTS
from app.schemas.amocrm_manager_ingress import AmoCrmManagerIngressEvent
from app.services.ingress import IngressAck, IngressPersistError


class IngressIdempotencyConflict(RuntimeError):
    """Same ingress key reused with a different conversation/envelope body."""

    def __init__(self) -> None:
        super().__init__("INGRESS_IDEMPOTENCY_CONFLICT")


class AmoCrmManagerIngressAdapter:
    """amoCRM manager durable ingress adapter — persist then ACK."""

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

    async def accept(self, event: AmoCrmManagerIngressEvent) -> IngressAck:
        """Persist first; ACK only after commit succeeds."""

        if event.channel != IngressChannel.AMOCRM.value:
            raise ValueError("UNSUPPORTED_CHANNEL")
        if event.event_type != IngressEventType.AMOCRM_MANAGER_MESSAGE.value:
            raise ValueError("UNSUPPORTED_EVENT_TYPE")

        correlation_id = event.correlation_id_or_new()
        try:
            async with session_scope(self._session_factory) as session:
                row, created = await ingress_repo.insert_if_absent(
                    session,
                    channel=IngressChannel.AMOCRM,
                    external_event_id=event.external_message_id,
                    external_conversation_id=event.amocrm_chat_id,
                    event_type=IngressEventType.AMOCRM_MANAGER_MESSAGE,
                    envelope_json=event.safe_envelope(),
                    correlation_id=correlation_id,
                    max_attempts=self._max_attempts,
                )
                if not created:
                    _assert_amocrm_duplicate_matches(row, event)
                ack = IngressAck(
                    accepted=True,
                    duplicate=not created,
                    event_id=row.id,
                    status=row.status,
                    correlation_id=row.correlation_id,
                )
        except IngressIdempotencyConflict:
            raise
        except Exception as exc:
            raise IngressPersistError(
                f"INGRESS_PERSIST_FAILED ({type(exc).__name__})"
            ) from None

        return ack


def _assert_amocrm_duplicate_matches(
    row: IngressEvent,
    event: AmoCrmManagerIngressEvent,
) -> None:
    """Identical key + payload → OK; altered chat/body → explicit conflict."""

    if row.channel != IngressChannel.AMOCRM.value:
        raise IngressIdempotencyConflict()
    if row.event_type != IngressEventType.AMOCRM_MANAGER_MESSAGE.value:
        raise IngressIdempotencyConflict()
    if row.external_event_id != event.external_message_id:
        raise IngressIdempotencyConflict()
    if row.external_conversation_id != event.amocrm_chat_id:
        raise IngressIdempotencyConflict()

    envelope = row.envelope_json if isinstance(row.envelope_json, dict) else {}
    expected = event.safe_envelope()
    for key in (
        "amocrm_chat_id",
        "amocrm_message_id",
        "external_message_id",
        "provider_sequence",
        "text",
    ):
        if envelope.get(key) != expected.get(key):
            raise IngressIdempotencyConflict()
