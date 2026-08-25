"""ConversationLocator stub for Teya contact-route resolution.

Channel is synthetic-only today: absence of a text dialog → PHONE_ONLY.
Never sends outbound messages. Never calls OutboundArbiter.
"""

from __future__ import annotations

import uuid
from typing import Protocol, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.teya_request_types import (
    ContactRouteOutcome,
    ContactRouteResolution,
    TransportCapability,
)
from app.models.conversation import Channel, Conversation


class ConversationQueryPort(Protocol):
    async def list_by_canonical_identity(
        self, *, canonical_identity_id: uuid.UUID
    ) -> Sequence[Conversation]: ...


class SqlConversationQuery:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_canonical_identity(
        self, *, canonical_identity_id: uuid.UUID
    ) -> Sequence[Conversation]:
        stmt = select(Conversation).where(
            Conversation.canonical_identity_id == canonical_identity_id
        )
        result = await self._session.scalars(stmt)
        return list(result.all())


class ConversationLocator:
    """Resolve whether a text channel dialog exists for a canonical identity."""

    def __init__(
        self,
        *,
        conversations: ConversationQueryPort | None = None,
    ) -> None:
        self._conversations = conversations

    async def resolve(
        self,
        *,
        canonical_identity_id: uuid.UUID | None,
    ) -> ContactRouteResolution:
        if canonical_identity_id is None:
            return ContactRouteResolution(
                outcome=ContactRouteOutcome.PHONE_ONLY,
                capabilities=TransportCapability.NONE,
                reason_code="NO_CANONICAL_IDENTITY",
            )
        if self._conversations is None:
            return ContactRouteResolution(
                outcome=ContactRouteOutcome.PHONE_ONLY,
                capabilities=TransportCapability.NONE,
                reason_code="LOCATOR_UNBOUND",
            )
        rows = await self._conversations.list_by_canonical_identity(
            canonical_identity_id=canonical_identity_id
        )
        text_capable = [
            row
            for row in rows
            if getattr(row, "channel", None) not in {None, Channel.SYNTHETIC.value, "synthetic"}
        ]
        if not text_capable:
            # Synthetic-only world → PHONE_ONLY is a success business outcome.
            return ContactRouteResolution(
                outcome=ContactRouteOutcome.PHONE_ONLY,
                conversation_id=uuid.UUID(str(rows[0].id)) if rows else None,
                capabilities=TransportCapability.NONE,
                reason_code="SYNTHETIC_ONLY_OR_NO_TEXT_DIALOG",
            )
        if len(text_capable) > 1:
            return ContactRouteResolution(
                outcome=ContactRouteOutcome.AMBIGUOUS_CHANNEL,
                capabilities=TransportCapability.TEXT_INBOUND,
                reason_code="MULTIPLE_TEXT_DIALOGS",
            )
        return ContactRouteResolution(
            outcome=ContactRouteOutcome.TEXT_CHANNEL_AVAILABLE,
            conversation_id=uuid.UUID(str(text_capable[0].id)),
            capabilities=TransportCapability.TEXT_INBOUND,
            reason_code="TEXT_DIALOG_FOUND",
        )
