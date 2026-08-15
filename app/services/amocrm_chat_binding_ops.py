"""Offline amoCRM Chat binding seed (AMO-PROD-ENABLEMENT-OPS-01).

Explicit conversation_id + amocrm_chat_id + integration_conversation_id only.
Reuses binding repository constraints. No discovery, bulk, Chat HTTP, or CRM REST.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import session_scope
from app.repositories import amocrm_chat_bindings as binding_repo
from app.repositories import conversations as conversation_repo
from app.repositories.amocrm_chat_bindings import AmocrmChatBindingAmbiguousError

__all__ = (
    "AmoCrmChatBindingOpsOutcome",
    "AmoCrmChatBindingOpsResult",
    "seed_active_chat_binding",
)

_CHAT_ID_MAX = 128
_INTEG_CID_MAX = 128


class AmoCrmChatBindingOpsOutcome(str, Enum):
    SEEDED = "SEEDED"
    UPDATED = "UPDATED"
    ALREADY_PRESENT = "ALREADY_PRESENT"
    REFUSED = "REFUSED"


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmChatBindingOpsResult:
    outcome: AmoCrmChatBindingOpsOutcome
    created: bool = False
    error_code: str | None = None

    def __repr__(self) -> str:
        return (
            "AmoCrmChatBindingOpsResult("
            f"outcome={self.outcome!r}, "
            f"created={self.created!r}, "
            f"error_code={self.error_code!r})"
        )


def _require_id_token(value: object, *, code: str, max_len: int) -> str:
    if type(value) is not str or not value or not value.strip():
        raise ValueError(code)
    token = value.strip()
    if len(token) > max_len:
        raise ValueError(code)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in token):
        raise ValueError(code)
    if any(ch.isspace() for ch in token):
        raise ValueError(code)
    return token


async def seed_active_chat_binding(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    conversation_id: uuid.UUID,
    amocrm_chat_id: str,
    integration_conversation_id: str,
) -> AmoCrmChatBindingOpsResult:
    """Insert or confirm one ACTIVE chat binding. Fail closed on any conflict.

    NULL ``integration_conversation_id`` on an otherwise matching ACTIVE row may
    be filled once → ``UPDATED`` (never ``ALREADY_PRESENT``). Non-null integ
    repoint refuses with zero mutation.
    """

    try:
        chat_id = _require_id_token(
            amocrm_chat_id,
            code="AMOCRM_CHAT_ID_INVALID",
            max_len=_CHAT_ID_MAX,
        )
        integ_cid = _require_id_token(
            integration_conversation_id,
            code="INTEGRATION_CONVERSATION_ID_INVALID",
            max_len=_INTEG_CID_MAX,
        )
    except ValueError as exc:
        code = str(exc.args[0]) if exc.args else "BINDING_INPUT_INVALID"
        return AmoCrmChatBindingOpsResult(
            outcome=AmoCrmChatBindingOpsOutcome.REFUSED,
            error_code=code if type(code) is str else "BINDING_INPUT_INVALID",
        )

    async with session_scope(session_factory) as session:
        conversation = await conversation_repo.lock_for_update(
            session,
            conversation_id=conversation_id,
        )
        if conversation is None:
            return AmoCrmChatBindingOpsResult(
                outcome=AmoCrmChatBindingOpsOutcome.REFUSED,
                error_code="CONVERSATION_MISSING",
            )

        try:
            by_chat = await binding_repo.get_active_by_amocrm_chat_id(
                session,
                amocrm_chat_id=chat_id,
            )
            by_conv = await binding_repo.get_active_by_conversation_id(
                session,
                conversation_id=conversation_id,
            )
        except AmocrmChatBindingAmbiguousError:
            return AmoCrmChatBindingOpsResult(
                outcome=AmoCrmChatBindingOpsOutcome.REFUSED,
                error_code="BINDING_AMBIGUOUS",
            )

        if by_chat is not None and by_chat.conversation_id != conversation_id:
            return AmoCrmChatBindingOpsResult(
                outcome=AmoCrmChatBindingOpsOutcome.REFUSED,
                error_code="BINDING_CHAT_CONFLICT",
            )
        if by_conv is not None and by_conv.amocrm_chat_id != chat_id:
            return AmoCrmChatBindingOpsResult(
                outcome=AmoCrmChatBindingOpsOutcome.REFUSED,
                error_code="BINDING_CONVERSATION_CONFLICT",
            )

        existing = by_chat if by_chat is not None else by_conv
        if existing is not None:
            if (
                existing.integration_conversation_id is not None
                and existing.integration_conversation_id != integ_cid
            ):
                return AmoCrmChatBindingOpsResult(
                    outcome=AmoCrmChatBindingOpsOutcome.REFUSED,
                    error_code="BINDING_INTEGRATION_CONVERSATION_CONFLICT",
                )
            if (
                existing.conversation_id == conversation_id
                and existing.amocrm_chat_id == chat_id
                and existing.integration_conversation_id == integ_cid
            ):
                return AmoCrmChatBindingOpsResult(
                    outcome=AmoCrmChatBindingOpsOutcome.ALREADY_PRESENT,
                    created=False,
                )
            # Explicit one-time fill: same conversation+chat, integ still NULL.
            if (
                existing.conversation_id == conversation_id
                and existing.amocrm_chat_id == chat_id
                and existing.integration_conversation_id is None
            ):
                try:
                    updated = await binding_repo.capture_integration_conversation_id(
                        session,
                        binding_id=existing.id,
                        integration_conversation_id=integ_cid,
                    )
                except AmocrmChatBindingAmbiguousError as exc:
                    code = str(exc.args[0]) if exc.args else "BINDING_AMBIGUOUS"
                    return AmoCrmChatBindingOpsResult(
                        outcome=AmoCrmChatBindingOpsOutcome.REFUSED,
                        error_code=(
                            code if type(code) is str else "BINDING_AMBIGUOUS"
                        ),
                    )
                except ValueError as exc:
                    code = str(exc.args[0]) if exc.args else "BINDING_INPUT_INVALID"
                    return AmoCrmChatBindingOpsResult(
                        outcome=AmoCrmChatBindingOpsOutcome.REFUSED,
                        error_code=(
                            code if type(code) is str else "BINDING_INPUT_INVALID"
                        ),
                    )
                except RuntimeError as exc:
                    code = str(exc.args[0]) if exc.args else "BINDING_LOOKUP_FAILED"
                    return AmoCrmChatBindingOpsResult(
                        outcome=AmoCrmChatBindingOpsOutcome.REFUSED,
                        error_code=(
                            code if type(code) is str else "BINDING_LOOKUP_FAILED"
                        ),
                    )
                if updated.integration_conversation_id != integ_cid:
                    return AmoCrmChatBindingOpsResult(
                        outcome=AmoCrmChatBindingOpsOutcome.REFUSED,
                        error_code="BINDING_MISMATCH_AFTER_WRITE",
                    )
                return AmoCrmChatBindingOpsResult(
                    outcome=AmoCrmChatBindingOpsOutcome.UPDATED,
                    created=False,
                )

        try:
            row, created = await binding_repo.insert_active_if_absent(
                session,
                conversation_id=conversation_id,
                amocrm_chat_id=chat_id,
                integration_conversation_id=integ_cid,
            )
        except AmocrmChatBindingAmbiguousError as exc:
            code = str(exc.args[0]) if exc.args else "BINDING_AMBIGUOUS"
            return AmoCrmChatBindingOpsResult(
                outcome=AmoCrmChatBindingOpsOutcome.REFUSED,
                error_code=code if type(code) is str else "BINDING_AMBIGUOUS",
            )
        except ValueError as exc:
            code = str(exc.args[0]) if exc.args else "BINDING_INPUT_INVALID"
            return AmoCrmChatBindingOpsResult(
                outcome=AmoCrmChatBindingOpsOutcome.REFUSED,
                error_code=code if type(code) is str else "BINDING_INPUT_INVALID",
            )
        except RuntimeError as exc:
            code = str(exc.args[0]) if exc.args else "BINDING_LOOKUP_FAILED"
            return AmoCrmChatBindingOpsResult(
                outcome=AmoCrmChatBindingOpsOutcome.REFUSED,
                error_code=code if type(code) is str else "BINDING_LOOKUP_FAILED",
            )

        if (
            row.conversation_id != conversation_id
            or row.amocrm_chat_id != chat_id
            or row.integration_conversation_id != integ_cid
        ):
            return AmoCrmChatBindingOpsResult(
                outcome=AmoCrmChatBindingOpsOutcome.REFUSED,
                error_code="BINDING_MISMATCH_AFTER_WRITE",
            )

        if created:
            return AmoCrmChatBindingOpsResult(
                outcome=AmoCrmChatBindingOpsOutcome.SEEDED,
                created=True,
            )
        return AmoCrmChatBindingOpsResult(
            outcome=AmoCrmChatBindingOpsOutcome.ALREADY_PRESENT,
            created=False,
        )
