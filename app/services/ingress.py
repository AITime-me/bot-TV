from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import session_scope
from app.models.ingress import IngressEvent, IngressEventType, IngressStatus
from app.repositories import ingress as ingress_repo
from app.repositories.ingress import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_DELAY_SECONDS,
    IngressClaim,
    StaleIngressLeaseError,
)
from app.schemas.inbound import SyntheticInboundEvent
from app.schemas.ingress import SyntheticIngressEvent
from app.services.inbound import InboundService


class IngressPersistError(RuntimeError):
    """Fail-closed: durable ingress could not be committed. Never ACK the source."""


@dataclass(frozen=True, repr=False)
class IngressAck:
    """Source-facing acknowledgement. Issued only after a successful commit."""

    accepted: bool
    duplicate: bool
    event_id: uuid.UUID
    status: str
    correlation_id: uuid.UUID

    def __repr__(self) -> str:
        return (
            f"IngressAck(accepted={self.accepted!r}, duplicate={self.duplicate!r}, "
            f"event_id={self.event_id!r}, status={self.status!r}, "
            f"correlation_id={self.correlation_id!r})"
        )


@dataclass(frozen=True, repr=False)
class IngressProcessResult:
    event_id: uuid.UUID
    status: str
    duplicate_business: bool
    inbox_id: uuid.UUID | None
    outbox_id: uuid.UUID | None

    def __repr__(self) -> str:
        return (
            f"IngressProcessResult(event_id={self.event_id!r}, "
            f"status={self.status!r}, "
            f"duplicate_business={self.duplicate_business!r}, "
            f"inbox_id={self.inbox_id!r}, outbox_id={self.outbox_id!r})"
        )


class SyntheticIngressAdapter:
    """Synthetic-only durable ingress adapter.

    Persists the provider event, commits, then returns an ACK. Does not open
    public webhooks, call AI, or send outbound messages. Real channel adapters
    are out of scope for BOT-CORE-INGRESS-01B.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def accept(self, event: SyntheticIngressEvent) -> IngressAck:
        """Persist first; ACK only after commit succeeds."""
        if event.channel != "synthetic":
            raise ValueError("UNSUPPORTED_CHANNEL")

        correlation_id = event.correlation_id_or_new()
        try:
            async with session_scope(self._session_factory) as session:
                row, created = await ingress_repo.insert_if_absent(
                    session,
                    channel=event.channel_enum(),
                    external_event_id=event.external_event_id,
                    external_conversation_id=event.external_conversation_id,
                    event_type=IngressEventType(event.event_type),
                    envelope_json=event.safe_envelope(),
                    correlation_id=correlation_id,
                )
                ack = IngressAck(
                    accepted=True,
                    duplicate=not created,
                    event_id=row.id,
                    status=row.status,
                    correlation_id=row.correlation_id,
                )
        except Exception as exc:
            # Do not chain the driver exception: it may embed DATABASE_URL.
            raise IngressPersistError(
                f"INGRESS_PERSIST_FAILED ({type(exc).__name__})"
            ) from None

        return ack


class IngressWorker:
    """Claims leased ingress events and runs foundation inbound persistence.

    Crash after commit / before process leaves the row in RECEIVED (or an
    expired PROCESSING lease), so another worker can safely continue.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        worker_id: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._retry_delay_seconds = retry_delay_seconds

    async def claim_one(self) -> IngressClaim | None:
        async with session_scope(self._session_factory) as session:
            return await ingress_repo.claim_next(
                session,
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
                max_attempts=self._max_attempts,
            )

    async def process_claimed(self, claim: IngressClaim) -> IngressProcessResult:
        """Apply foundation inbound persistence under the held lease."""
        inbound = _envelope_to_inbound(claim)
        try:
            async with session_scope(self._session_factory) as session:
                accept = await InboundService(session).accept(inbound)
                event = await ingress_repo.complete_with_lease(
                    session,
                    event_id=claim.event_id,
                    lease_token=claim.lease_token,
                    lease_version=claim.lease_version,
                )
                return IngressProcessResult(
                    event_id=event.id,
                    status=event.status,
                    duplicate_business=accept.duplicate,
                    inbox_id=accept.inbox.id,
                    outbox_id=accept.outbox.id,
                )
        except StaleIngressLeaseError:
            raise
        except Exception as exc:
            await self.fail_claimed(claim, error_code=type(exc).__name__)
            raise

    async def fail_claimed(
        self,
        claim: IngressClaim,
        *,
        error_code: str,
    ) -> IngressEvent:
        async with session_scope(self._session_factory) as session:
            return await ingress_repo.fail_with_lease(
                session,
                event_id=claim.event_id,
                lease_token=claim.lease_token,
                lease_version=claim.lease_version,
                error_code=error_code,
                max_attempts=self._max_attempts,
                retry_delay_seconds=self._retry_delay_seconds,
            )


def _envelope_to_inbound(claim: IngressClaim) -> SyntheticInboundEvent:
    envelope = claim.envelope_json
    text = envelope.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("INGRESS_ENVELOPE_INVALID")
    # external_message_id reuses the durable provider event id so inbox
    # uniqueness aligns with ingress uniqueness for synthetic traffic.
    return SyntheticInboundEvent(
        channel="synthetic",
        external_conversation_id=claim.external_conversation_id,
        external_message_id=claim.external_event_id,
        text=text,
    )


def assert_no_client_outbound_path() -> None:
    """Static guard: ingress modules must not grow a client sender."""
    forbidden = ("send_message", "publish_to_channel", "transport_send")
    source = Path(__file__).read_text(encoding="utf-8")
    for name in forbidden:
        if f"def {name}" in source or f".{name}(" in source:
            raise AssertionError(f"forbidden outbound symbol present: {name}")
