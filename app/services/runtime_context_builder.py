"""Runtime context builder — acquisition orchestration (AI-DIALOGUE-01).

Separates:
A. data acquisition (CP cache, live-facts HTTP, dialog history)
B. strict validation (already done by parsers/clients)
C. deterministic pure assembly

Does NOT call text-generation providers, outbound, or change BOT_MODE /
EMERGENCY_LOCK. Live-facts are never written to control_plane_snapshots.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import BotMode, Settings
from app.core.control_plane_types import (
    ControlPlaneKindReadiness,
    ControlPlaneOverallReadiness,
    ControlPlaneSnapshotKind,
    ControlPlaneSnapshotState,
    KnowledgePublicationV1,
    SettingsPublicationV1,
)
from app.core.live_facts_http import LiveFactsFetchCode, LiveFactsFetchResult
from app.core.live_facts_types import LiveFactsPayloadV1
from app.core.runtime_context_assemble import (
    assemble_runtime_context,
    build_conversation_layer_from_turns,
    map_history_author,
)
from app.core.runtime_context_knowledge import KnowledgeSelectionHint
from app.core.runtime_context_types import (
    HARD_MAX_HISTORY_CHARS,
    HARD_MAX_HISTORY_TURNS,
    RuntimeContextBuildResult,
    RuntimeContextReadiness,
    RuntimeContextReason,
)
from app.core.shadow_draft_context_selection import (
    build_knowledge_selection_hint,
    conversation_client_text_from_turns,
)
from app.db.session import session_scope
from app.models.conversation import Conversation
from app.repositories import control_plane_snapshots as snapshot_repo
from app.services.control_plane_snapshot_service import (
    ControlPlaneSnapshotService,
    load_knowledge_from_row,
    load_settings_from_row,
)
from app.services.dialog_context import DialogContextService

logger = logging.getLogger(__name__)


class _LiveFactsRemote(Protocol):
    def fetch(self) -> LiveFactsFetchResult: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _log_diag(event: str) -> None:
    try:
        logger.info("runtime_context event=%s", event)
    except Exception:
        return


@dataclass(frozen=True, slots=True)
class _AcquiredLayers:
    settings_publication: SettingsPublicationV1 | None
    settings_readiness: ControlPlaneKindReadiness | None
    knowledge_publication: KnowledgePublicationV1 | None
    knowledge_readiness: ControlPlaneKindReadiness | None
    live_facts: LiveFactsPayloadV1 | None
    live_facts_code: LiveFactsFetchCode | None
    conversation_row: Conversation | None
    conversation_layer: object | None
    history_ok: bool


@dataclass
class RuntimeContextBuilder:
    """Build structured runtime context for a conversation.

    generationAllowed authorizes internal shadow drafts only when the context
    is READY and local safety permits — never client delivery / outbound.
    """

    session_factory: async_sessionmaker[AsyncSession]
    local_settings: Settings
    control_plane: ControlPlaneSnapshotService
    live_facts_remote: _LiveFactsRemote | None

    async def build_for_conversation(
        self,
        conversation_id: UUID,
        *,
        knowledge_hint: KnowledgeSelectionHint | None = None,
        history_max_turns: int = HARD_MAX_HISTORY_TURNS,
        history_max_chars: int = HARD_MAX_HISTORY_CHARS,
    ) -> RuntimeContextBuildResult:
        reasons: list[RuntimeContextReason] = []
        acquired = await self._acquire(
            conversation_id,
            history_max_turns=history_max_turns,
            history_max_chars=history_max_chars,
        )

        if not isinstance(self.local_settings.bot_mode, BotMode):
            reasons.append(RuntimeContextReason.SAFETY_UNREADABLE)
        if type(self.local_settings.emergency_lock) is not bool:
            reasons.append(RuntimeContextReason.SAFETY_UNREADABLE)

        if acquired.settings_publication is None:
            reasons.append(RuntimeContextReason.SETTINGS_NOT_READY)
        if acquired.knowledge_publication is None:
            reasons.append(RuntimeContextReason.KNOWLEDGE_NOT_READY)

        if acquired.live_facts is None:
            code = acquired.live_facts_code
            if code is LiveFactsFetchCode.AUTH_ERROR:
                reasons.append(RuntimeContextReason.LIVE_FACTS_AUTH_ERROR)
            elif code is LiveFactsFetchCode.CONTRACT_ERROR:
                reasons.append(RuntimeContextReason.LIVE_FACTS_CONTRACT_ERROR)
            elif code is LiveFactsFetchCode.RESPONSE_INVALID:
                reasons.append(RuntimeContextReason.LIVE_FACTS_INVALID)
            else:
                reasons.append(RuntimeContextReason.LIVE_FACTS_UNAVAILABLE)

        if not acquired.history_ok:
            reasons.append(RuntimeContextReason.HISTORY_UNAVAILABLE)
        if acquired.conversation_row is None and acquired.history_ok is False:
            if RuntimeContextReason.CONVERSATION_UNAVAILABLE not in reasons:
                reasons.append(RuntimeContextReason.CONVERSATION_UNAVAILABLE)

        emergency = (
            self.local_settings.emergency_lock is True
            if type(self.local_settings.emergency_lock) is bool
            else True
        )
        if emergency:
            reasons.append(RuntimeContextReason.EMERGENCY_LOCK_ACTIVE)

        row = acquired.conversation_row
        handoff_state = row.handoff_state if row is not None else None
        ownership = row.ownership if row is not None else None
        status = row.status if row is not None else None
        takeover = row.manager_takeover_at is not None if row is not None else False
        handoff_active = False
        if handoff_state is not None and handoff_state != "BOT_ACTIVE":
            handoff_active = True
        if status == "HANDOFF" or ownership == "MANAGER" or takeover:
            handoff_active = True
        if handoff_active:
            reasons.append(RuntimeContextReason.HANDOFF_ACTIVE)

        bot_mode = (
            self.local_settings.bot_mode
            if isinstance(self.local_settings.bot_mode, BotMode)
            else BotMode.OFF
        )

        effective_hint = knowledge_hint
        if (
            effective_hint is None
            and acquired.conversation_layer is not None
            and acquired.live_facts is not None
        ):
            conv_text = conversation_client_text_from_turns(
                acquired.conversation_layer.turns  # type: ignore[attr-defined]
            )
            effective_hint = build_knowledge_selection_hint(
                conversation_text=conv_text,
                live_facts=acquired.live_facts,
            )

        context = assemble_runtime_context(
            bot_mode=bot_mode,
            emergency_lock=emergency,
            settings_publication=acquired.settings_publication,
            settings_readiness=acquired.settings_readiness,
            knowledge_publication=acquired.knowledge_publication,
            knowledge_readiness=acquired.knowledge_readiness,
            live_facts=acquired.live_facts,
            conversation=acquired.conversation_layer,  # type: ignore[arg-type]
            handoff_state=handoff_state,
            ownership=ownership,
            conversation_status=status,
            manager_takeover_at_present=takeover,
            knowledge_hint=effective_hint,
            built_at=_utc_now(),
        )

        # desiredAdminState must never mutate effective BOT_MODE.
        if context.settings is not None:
            if context.safety.bot_mode is not bot_mode:
                reasons.append(RuntimeContextReason.SAFETY_UNREADABLE)

        data_blocking = {
            RuntimeContextReason.SETTINGS_NOT_READY,
            RuntimeContextReason.KNOWLEDGE_NOT_READY,
            RuntimeContextReason.LIVE_FACTS_UNAVAILABLE,
            RuntimeContextReason.LIVE_FACTS_INVALID,
            RuntimeContextReason.LIVE_FACTS_AUTH_ERROR,
            RuntimeContextReason.LIVE_FACTS_CONTRACT_ERROR,
            RuntimeContextReason.HISTORY_UNAVAILABLE,
            RuntimeContextReason.SAFETY_UNREADABLE,
            RuntimeContextReason.CONVERSATION_UNAVAILABLE,
            RuntimeContextReason.EMERGENCY_LOCK_ACTIVE,
            RuntimeContextReason.HANDOFF_ACTIVE,
        }
        blocking = [r for r in reasons if r in data_blocking]
        readiness = (
            RuntimeContextReadiness.READY
            if not blocking
            else RuntimeContextReadiness.NOT_READY
        )

        # Shadow generation eligibility (AI-DIALOGUE-02): READY context +
        # local safety. Client delivery remains separately denied forever by
        # outbound_policy / arbiter — not by this flag alone.
        generation_allowed = (
            readiness is RuntimeContextReadiness.READY
            and context.safety.generation_allowed
        )
        if not generation_allowed and RuntimeContextReason.GENERATION_DISABLED_STAGE not in reasons:
            if readiness is RuntimeContextReadiness.READY and not context.safety.generation_allowed:
                reasons.append(RuntimeContextReason.GENERATION_DISABLED_STAGE)

        # Deduplicate while preserving order.
        seen: set[RuntimeContextReason] = set()
        ordered: list[RuntimeContextReason] = []
        for reason in reasons:
            if reason not in seen:
                seen.add(reason)
                ordered.append(reason)

        result = RuntimeContextBuildResult(
            readiness=readiness,
            reasons=tuple(ordered),
            generation_allowed=generation_allowed,
            context=context,
        )
        _log_diag(f"build readiness={readiness.value}")
        return result

    async def _acquire(
        self,
        conversation_id: UUID,
        *,
        history_max_turns: int,
        history_max_chars: int,
    ) -> _AcquiredLayers:
        cp_state = await self._load_cp_state()
        settings_pub, settings_ready = await self._load_settings_publication(cp_state)
        knowledge_pub, knowledge_ready = await self._load_knowledge_publication(
            cp_state
        )
        live_facts, live_code = self._fetch_live_facts()

        conversation_layer = None
        conversation_row: Conversation | None = None
        history_ok = False
        try:
            async with session_scope(self.session_factory) as session:
                conversation_row = await session.get(Conversation, conversation_id)
                if conversation_row is None:
                    return _AcquiredLayers(
                        settings_publication=settings_pub,
                        settings_readiness=settings_ready,
                        knowledge_publication=knowledge_pub,
                        knowledge_readiness=knowledge_ready,
                        live_facts=live_facts,
                        live_facts_code=live_code,
                        conversation_row=None,
                        conversation_layer=None,
                        history_ok=False,
                    )
                # Detach scalar fields before session ends.
                handoff_state = conversation_row.handoff_state
                ownership = conversation_row.ownership
                status = conversation_row.status
                takeover_at = conversation_row.manager_takeover_at
                dialog = await DialogContextService(session).load(
                    conversation_id=conversation_id
                )
                turns = tuple(
                    map_history_author(
                        author=message.author,
                        conversation_event_seq=message.conversation_event_seq,
                        text=message.text,
                        occurred_at=getattr(message, "occurred_at", None),
                    )
                    for message in dialog.messages
                )
                conversation_layer = build_conversation_layer_from_turns(
                    conversation_id=dialog.conversation_id,
                    event_seq_hwm=dialog.event_seq_hwm,
                    turns=turns,
                    max_turns=history_max_turns,
                    max_chars=history_max_chars,
                )
                # Rebuild a lightweight detached snapshot for safety fields.
                conversation_row = _DetachedConversation(
                    handoff_state=handoff_state,
                    ownership=ownership,
                    status=status,
                    manager_takeover_at=takeover_at,
                )  # type: ignore[assignment]
                history_ok = True
        except Exception:
            _log_diag("history_unavailable")
            history_ok = False
            conversation_layer = None

        return _AcquiredLayers(
            settings_publication=settings_pub,
            settings_readiness=settings_ready,
            knowledge_publication=knowledge_pub,
            knowledge_readiness=knowledge_ready,
            live_facts=live_facts,
            live_facts_code=live_code,
            conversation_row=conversation_row,
            conversation_layer=conversation_layer,
            history_ok=history_ok,
        )

    async def _load_cp_state(self) -> ControlPlaneSnapshotState:
        try:
            state = self.control_plane.get_state()
            if (
                state.overall is ControlPlaneOverallReadiness.READY
                or state.settings.usable
                or state.knowledge.usable
            ):
                return state
            return await self.control_plane.load_state_from_cache()
        except Exception:
            return await self.control_plane.load_state_from_cache()

    async def _load_settings_publication(
        self, state: ControlPlaneSnapshotState
    ) -> tuple[SettingsPublicationV1 | None, ControlPlaneKindReadiness | None]:
        if not state.settings.usable:
            return None, state.settings.readiness
        async with session_scope(self.session_factory) as session:
            row = await snapshot_repo.get_by_kind(
                session, kind=ControlPlaneSnapshotKind.SETTINGS
            )
            if row is None:
                return None, ControlPlaneKindReadiness.NOT_READY
            publication = load_settings_from_row(row)
            if publication is None:
                return None, ControlPlaneKindReadiness.INVALID
            return publication, state.settings.readiness

    async def _load_knowledge_publication(
        self, state: ControlPlaneSnapshotState
    ) -> tuple[KnowledgePublicationV1 | None, ControlPlaneKindReadiness | None]:
        if not state.knowledge.usable:
            return None, state.knowledge.readiness
        async with session_scope(self.session_factory) as session:
            row = await snapshot_repo.get_by_kind(
                session, kind=ControlPlaneSnapshotKind.KNOWLEDGE
            )
            if row is None:
                return None, ControlPlaneKindReadiness.NOT_READY
            publication = load_knowledge_from_row(row)
            if publication is None:
                return None, ControlPlaneKindReadiness.INVALID
            return publication, state.knowledge.readiness

    def _fetch_live_facts(
        self,
    ) -> tuple[LiveFactsPayloadV1 | None, LiveFactsFetchCode | None]:
        if self.live_facts_remote is None:
            return None, LiveFactsFetchCode.UNAVAILABLE
        try:
            result = self.live_facts_remote.fetch()
        except Exception:
            return None, LiveFactsFetchCode.UNAVAILABLE
        if result.code is LiveFactsFetchCode.OK and result.payload is not None:
            return result.payload, result.code
        return None, result.code


@dataclass(frozen=True, slots=True)
class _DetachedConversation:
    handoff_state: str
    ownership: str
    status: str
    manager_takeover_at: datetime | None
