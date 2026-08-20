from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import session_scope
from app.models.conversation import Conversation
from app.models.ingress import IngressEvent, IngressEventType
from app.repositories import amocrm_chat_bindings as binding_repo
from app.repositories import amocrm_message_projections as projection_repo
from app.repositories import ingress as ingress_repo
from app.repositories.amocrm_chat_bindings import AmocrmChatBindingAmbiguousError
from app.repositories.ingress import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_DELAY_SECONDS,
    IngressClaim,
    StaleIngressLeaseError,
)
from app.schemas.booking_input import SyntheticBookingInput
from app.schemas.self_booking_confirm_action import SyntheticConfirmSelectedSlotAction
from app.schemas.inbound import SyntheticInboundEvent
from app.schemas.ingress import SyntheticIngressEvent
from app.schemas.manager_message import SyntheticManagerMessageEvent
from app.services.inbound import InboundService
from app.services.manager_messages import apply_manager_message_in_session


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
                    max_attempts=self._max_attempts,
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
        retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
        handoff_pause_seconds: int = 15 * 60,
    ) -> None:
        if not 10 * 60 <= handoff_pause_seconds <= 15 * 60:
            raise ValueError("handoff_pause_seconds must be between 600 and 900")
        self._session_factory = session_factory
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._handoff_pause_seconds = handoff_pause_seconds

    async def claim_one(self) -> IngressClaim | None:
        async with session_scope(self._session_factory) as session:
            return await ingress_repo.claim_next(
                session,
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
            )

    async def process_claimed(self, claim: IngressClaim) -> IngressProcessResult:
        """Apply foundation inbound or amoCRM manager path under the held lease."""
        # Fail closed: never route mismatched channel/event pairs into synthetic
        # client inbound (AMO-01A / M1).
        if claim.channel == "amocrm":
            if claim.event_type != IngressEventType.AMOCRM_MANAGER_MESSAGE.value:
                await self.fail_claimed(
                    claim,
                    error_code="INGRESS_CHANNEL_EVENT_MISMATCH",
                )
                raise ValueError("INGRESS_CHANNEL_EVENT_MISMATCH")
            return await self._process_amocrm_manager(claim)
        if (
            claim.channel != "synthetic"
            or claim.event_type != IngressEventType.SYNTHETIC_MESSAGE.value
        ):
            await self.fail_claimed(
                claim,
                error_code="INGRESS_CHANNEL_EVENT_MISMATCH",
            )
            raise ValueError("INGRESS_CHANNEL_EVENT_MISMATCH")

        inbound = _envelope_to_inbound(claim)
        try:
            async with session_scope(self._session_factory) as session:
                accept = await InboundService(
                    session,
                    handoff_pause_seconds=self._handoff_pause_seconds,
                ).accept(inbound)
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

    async def _process_amocrm_manager(self, claim: IngressClaim) -> IngressProcessResult:
        """Resolve binding then apply existing manager-message FSM. No outbound."""

        try:
            async with session_scope(self._session_factory) as session:
                envelope = claim.envelope_json
                amocrm_chat_id = envelope.get("amocrm_chat_id")
                amocrm_message_id = envelope.get("amocrm_message_id")
                external_message_id = envelope.get("external_message_id")
                provider_sequence = envelope.get("provider_sequence")
                text = envelope.get("text")
                if (
                    not isinstance(amocrm_chat_id, str)
                    or not amocrm_chat_id
                    or not isinstance(amocrm_message_id, str)
                    or not amocrm_message_id
                    or not isinstance(external_message_id, str)
                    or not external_message_id
                    or not isinstance(provider_sequence, int)
                    or not isinstance(text, str)
                    or not text
                ):
                    raise ValueError("INGRESS_ENVELOPE_INVALID")
                # Ingress row key must match namespaced manager key (H1/H2).
                if claim.external_event_id != external_message_id:
                    raise ValueError("INGRESS_ENVELOPE_INVALID")
                if claim.external_conversation_id != amocrm_chat_id:
                    raise ValueError("INGRESS_ENVELOPE_INVALID")

                # Echo suppress: any projection already carrying this amo msgid
                # (including PROCESSING after HTTP success, before PROJECTED).
                projected = await projection_repo.get_projected_by_amocrm_message_id(
                    session,
                    amocrm_message_id=amocrm_message_id,
                )
                if projected is not None:
                    conversation_client_id = envelope.get("conversation_client_id")
                    if (
                        isinstance(conversation_client_id, str)
                        and conversation_client_id
                    ):
                        try:
                            binding = await binding_repo.get_active_by_amocrm_chat_id(
                                session,
                                amocrm_chat_id=amocrm_chat_id,
                            )
                        except AmocrmChatBindingAmbiguousError:
                            binding = None
                        if binding is not None:
                            try:
                                await binding_repo.capture_integration_conversation_id(
                                    session,
                                    binding_id=binding.id,
                                    integration_conversation_id=conversation_client_id,
                                )
                            except (AmocrmChatBindingAmbiguousError, ValueError):
                                pass
                    event = await ingress_repo.complete_with_lease(
                        session,
                        event_id=claim.event_id,
                        lease_token=claim.lease_token,
                        lease_version=claim.lease_version,
                    )
                    return IngressProcessResult(
                        event_id=event.id,
                        status=event.status,
                        duplicate_business=True,
                        inbox_id=None,
                        outbox_id=None,
                    )

                try:
                    binding = await binding_repo.get_active_by_amocrm_chat_id(
                        session,
                        amocrm_chat_id=amocrm_chat_id,
                    )
                except AmocrmChatBindingAmbiguousError:
                    raise ValueError("BINDING_AMBIGUOUS") from None
                if binding is None:
                    raise ValueError("BINDING_UNKNOWN")

                conversation_client_id = envelope.get("conversation_client_id")
                if isinstance(conversation_client_id, str) and conversation_client_id:
                    try:
                        await binding_repo.capture_integration_conversation_id(
                            session,
                            binding_id=binding.id,
                            integration_conversation_id=conversation_client_id,
                        )
                    except AmocrmChatBindingAmbiguousError:
                        raise ValueError("BINDING_INTEGRATION_CONVERSATION_CONFLICT") from None

                conversation = await session.get(Conversation, binding.conversation_id)
                if conversation is None:
                    raise ValueError("BINDING_CONVERSATION_MISSING")

                # Namespaced external_message_id keeps AMO provenance out of the
                # raw synthetic manager id namespace (H2).
                manager_event = SyntheticManagerMessageEvent(
                    channel="synthetic",
                    external_conversation_id=conversation.external_conversation_id,
                    external_message_id=external_message_id,
                    provider_sequence=provider_sequence,
                    text=text,
                )
                apply = await apply_manager_message_in_session(
                    session,
                    event=manager_event,
                    handoff_pause_seconds=self._handoff_pause_seconds,
                    conversation_id=conversation.id,
                )
                event = await ingress_repo.complete_with_lease(
                    session,
                    event_id=claim.event_id,
                    lease_token=claim.lease_token,
                    lease_version=claim.lease_version,
                )
                return IngressProcessResult(
                    event_id=event.id,
                    status=event.status,
                    duplicate_business=apply.duplicate,
                    inbox_id=None,
                    outbox_id=None,
                )
        except StaleIngressLeaseError:
            raise
        except Exception as exc:
            code = type(exc).__name__
            if isinstance(exc, ValueError) and exc.args:
                arg0 = exc.args[0]
                if isinstance(arg0, str) and arg0.isupper() and " " not in arg0:
                    code = arg0
            await self.fail_claimed(claim, error_code=code)
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
                retry_delay_seconds=self._retry_delay_seconds,
            )


def _envelope_to_inbound(claim: IngressClaim) -> SyntheticInboundEvent:
    envelope = claim.envelope_json
    text = envelope.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("INGRESS_ENVELOPE_INVALID")
    booking_raw = envelope.get("booking")
    booking = None
    if booking_raw is not None:
        try:
            booking = SyntheticBookingInput.model_validate(booking_raw)
        except Exception as exc:
            raise ValueError("INGRESS_BOOKING_INVALID") from exc
    action_raw = envelope.get("action")
    action = None
    if action_raw is not None:
        try:
            action = SyntheticConfirmSelectedSlotAction.model_validate(action_raw)
        except Exception as exc:
            raise ValueError("INGRESS_ACTION_INVALID") from exc
    # external_message_id reuses the durable provider event id so inbox
    # uniqueness aligns with ingress uniqueness for synthetic traffic.
    # channel / external ids come from the durable claim envelope, not action body.
    return SyntheticInboundEvent(
        channel="synthetic",
        external_conversation_id=claim.external_conversation_id,
        external_message_id=claim.external_event_id,
        text=text,
        booking=booking,
        action=action,
    )


def assert_no_client_outbound_path() -> None:
    """Static guard: ingress modules must not grow a client sender."""
    forbidden = ("send_message", "publish_to_channel", "transport_send")
    source = Path(__file__).read_text(encoding="utf-8")
    for name in forbidden:
        if f"def {name}" in source or f".{name}(" in source:
            raise AssertionError(f"forbidden outbound symbol present: {name}")
