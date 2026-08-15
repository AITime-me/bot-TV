"""Repository helpers for amocrm_chat_bindings (AMO-01A).

Lock order note: resolve reads are non-locking SELECTs. When creating a binding
in the same transaction as conversation work, lock conversations first, then
insert here (before manager_messages / ingress completion).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.amocrm_chat_binding import (
    AmocrmChatBinding,
    AmocrmChatBindingStatus,
)


class AmocrmChatBindingAmbiguousError(RuntimeError):
    """More than one ACTIVE binding matched — fail closed, never guess."""


async def get_active_by_amocrm_chat_id(
    session: AsyncSession,
    *,
    amocrm_chat_id: str,
) -> AmocrmChatBinding | None:
    """Return the single ACTIVE binding or None.

    Raises AmocrmChatBindingAmbiguousError if more than one ACTIVE row matches
    (should be unreachable under uq_amocrm_chat_bindings_amocrm_chat_id).
    """

    stmt = select(AmocrmChatBinding).where(
        AmocrmChatBinding.amocrm_chat_id == amocrm_chat_id,
        AmocrmChatBinding.status == AmocrmChatBindingStatus.ACTIVE.value,
    )
    rows = list(await session.scalars(stmt))
    if len(rows) > 1:
        raise AmocrmChatBindingAmbiguousError("BINDING_AMBIGUOUS")
    if not rows:
        return None
    return rows[0]


async def get_active_by_conversation_id(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
) -> AmocrmChatBinding | None:
    """Return the single ACTIVE binding for a conversation or None.

    Raises AmocrmChatBindingAmbiguousError if more than one ACTIVE row matches
    (should be unreachable under uq_amocrm_chat_bindings_conversation_id).
    """

    stmt = select(AmocrmChatBinding).where(
        AmocrmChatBinding.conversation_id == conversation_id,
        AmocrmChatBinding.status == AmocrmChatBindingStatus.ACTIVE.value,
    )
    rows = list(await session.scalars(stmt))
    if len(rows) > 1:
        raise AmocrmChatBindingAmbiguousError("BINDING_AMBIGUOUS")
    if not rows:
        return None
    return rows[0]


async def insert_active_if_absent(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    amocrm_chat_id: str,
    integration_conversation_id: str | None = None,
) -> tuple[AmocrmChatBinding, bool]:
    """Idempotently insert an ACTIVE binding. Does not commit."""

    existing = await get_active_by_amocrm_chat_id(
        session,
        amocrm_chat_id=amocrm_chat_id,
    )
    if existing is not None:
        if existing.conversation_id != conversation_id:
            raise AmocrmChatBindingAmbiguousError("BINDING_AMBIGUOUS")
        if integration_conversation_id:
            await capture_integration_conversation_id(
                session,
                binding_id=existing.id,
                integration_conversation_id=integration_conversation_id,
            )
            refreshed = await get_active_by_amocrm_chat_id(
                session,
                amocrm_chat_id=amocrm_chat_id,
            )
            if refreshed is None:
                raise RuntimeError("BINDING_LOOKUP_FAILED")
            return refreshed, False
        return existing, False

    new_id = uuid.uuid4()
    stmt = (
        insert(AmocrmChatBinding)
        .values(
            id=new_id,
            conversation_id=conversation_id,
            amocrm_chat_id=amocrm_chat_id,
            integration_conversation_id=integration_conversation_id,
            status=AmocrmChatBindingStatus.ACTIVE.value,
        )
        .on_conflict_do_nothing(
            constraint="uq_amocrm_chat_bindings_amocrm_chat_id",
        )
        .returning(AmocrmChatBinding.id)
    )
    inserted = await session.scalar(stmt)
    row = await get_active_by_amocrm_chat_id(
        session,
        amocrm_chat_id=amocrm_chat_id,
    )
    if row is None:
        # Conflict on chat_id with REVOKED or conversation unique — fail closed.
        raise RuntimeError("BINDING_LOOKUP_FAILED")
    if row.conversation_id != conversation_id:
        raise AmocrmChatBindingAmbiguousError("BINDING_AMBIGUOUS")
    if integration_conversation_id and not row.integration_conversation_id:
        await capture_integration_conversation_id(
            session,
            binding_id=row.id,
            integration_conversation_id=integration_conversation_id,
        )
        row = await get_active_by_amocrm_chat_id(
            session,
            amocrm_chat_id=amocrm_chat_id,
        )
        if row is None:
            raise RuntimeError("BINDING_LOOKUP_FAILED")
    return row, inserted is not None


async def capture_integration_conversation_id(
    session: AsyncSession,
    *,
    binding_id: uuid.UUID,
    integration_conversation_id: str,
) -> AmocrmChatBinding:
    """Set/refresh integration_conversation_id from Chat webhook when present."""

    if type(integration_conversation_id) is not str or not integration_conversation_id.strip():
        raise ValueError("INTEGRATION_CONVERSATION_ID_INVALID")
    value = integration_conversation_id.strip()
    if len(value) > 128:
        raise ValueError("INTEGRATION_CONVERSATION_ID_INVALID")

    row = await session.get(AmocrmChatBinding, binding_id)
    if row is None:
        raise RuntimeError("BINDING_LOOKUP_FAILED")
    if row.integration_conversation_id == value:
        return row
    if (
        row.integration_conversation_id is not None
        and row.integration_conversation_id != value
    ):
        # Do not silently remap a known integration conversation.
        raise AmocrmChatBindingAmbiguousError("BINDING_INTEGRATION_CONVERSATION_CONFLICT")

    row.integration_conversation_id = value
    await session.flush()
    return row
