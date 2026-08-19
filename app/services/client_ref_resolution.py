"""Read-only clientRef resolver for bot-TV → online-zapis-tv calls.

This module is intentionally "fail-closed":
- it never guesses client identity from phone/name/amocrm entities;
- it never performs CRM/online-zapis HTTP I/O;
- it never mutates canonical identities or external identity links.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_ref_resolution import (
    ClientRefResolutionOutcome,
    ClientRefResolutionResult,
)
from app.core.identity_resolution import CanonicalIdentityStatus
from app.models.canonical_identity import CanonicalIdentity
from app.models.conversation import Conversation

__all__ = ("ClientRefResolverService",)


def _as_uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


class ClientRefResolverService:
    """Resolve clientRef only from conversation's attached ACTIVE canonical."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_for_conversation(
        self,
        *,
        conversation_id: object,
    ) -> ClientRefResolutionResult:
        cid = _as_uuid(conversation_id)
        if cid is None:
            return ClientRefResolutionResult(
                outcome=ClientRefResolutionOutcome.INVALID_INPUT,
                error_code="CONVERSATION_ID_INVALID",
            )

        conversation = await self._session.scalar(
            select(Conversation).where(Conversation.id == cid)
        )
        if conversation is None:
            # Graph incomplete: conversation missing; never guess.
            return ClientRefResolutionResult(
                outcome=ClientRefResolutionOutcome.REFUSED,
                reason_code="CONVERSATION_MISSING",
            )

        canonical_id = conversation.canonical_identity_id
        if canonical_id is None:
            # Safe absence: no attached identity yet.
            return ClientRefResolutionResult(
                outcome=ClientRefResolutionOutcome.NOT_FOUND,
            )

        canonical = await self._session.scalar(
            select(CanonicalIdentity).where(CanonicalIdentity.id == canonical_id)
        )
        if canonical is None:
            return ClientRefResolutionResult(
                outcome=ClientRefResolutionOutcome.REFUSED,
                reason_code="CANONICAL_MISSING",
            )
        if canonical.status != CanonicalIdentityStatus.ACTIVE.value:
            return ClientRefResolutionResult(
                outcome=ClientRefResolutionOutcome.REFUSED,
                reason_code="CANONICAL_NOT_ACTIVE",
            )

        # Stage-01 encoding contract: canonical UUID string itself.
        client_ref = str(canonical.id)
        return ClientRefResolutionResult(
            outcome=ClientRefResolutionOutcome.FOUND,
            client_ref=client_ref,
        )

