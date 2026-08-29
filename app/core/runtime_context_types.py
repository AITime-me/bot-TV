"""Structured Teya runtime context model (AI-DIALOGUE-01).

Layers stay separate. No final prompt string. generationAllowed is always
false in this foundation stage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from app.config import BotMode
from app.core.control_plane_types import (
    ControlPlaneKindReadiness,
    KnowledgeCategory,
    KnowledgeEntryV1,
    SettingsPublicationV1,
)
from app.core.live_facts_remote import LIVE_FACTS_OWNERSHIP_INVARIANT
from app.core.live_facts_types import LiveFactsPayloadV1

RUNTIME_CONTEXT_SCHEMA_VERSION: Final[int] = 1

# Hard local safety ceilings for conversation history (not publishable higher).
HARD_MAX_HISTORY_TURNS: Final[int] = 40
HARD_MAX_HISTORY_CHARS: Final[int] = 12_000

# Deterministic knowledge selection ceilings.
HARD_MAX_SELECTED_KNOWLEDGE_ENTRIES: Final[int] = 32


class TrustBoundary(StrEnum):
    TRUSTED_SYSTEM = "TRUSTED_SYSTEM"
    TRUSTED_PUBLISHED_POLICY = "TRUSTED_PUBLISHED_POLICY"
    TRUSTED_LIVE_FACTS = "TRUSTED_LIVE_FACTS"
    TRUSTED_MANAGED_KB = "TRUSTED_MANAGED_KB"
    UNTRUSTED_CONVERSATION = "UNTRUSTED_CONVERSATION"
    MANAGER_AUTHORED = "MANAGER_AUTHORED"


class KnowledgeCoverage(StrEnum):
    MISSING = "MISSING"
    PARTIAL = "PARTIAL"
    AVAILABLE = "AVAILABLE"


class RuntimeContextReadiness(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"


class RuntimeContextReason(StrEnum):
    SETTINGS_NOT_READY = "SETTINGS_NOT_READY"
    KNOWLEDGE_NOT_READY = "KNOWLEDGE_NOT_READY"
    LIVE_FACTS_UNAVAILABLE = "LIVE_FACTS_UNAVAILABLE"
    LIVE_FACTS_INVALID = "LIVE_FACTS_INVALID"
    LIVE_FACTS_AUTH_ERROR = "LIVE_FACTS_AUTH_ERROR"
    LIVE_FACTS_CONTRACT_ERROR = "LIVE_FACTS_CONTRACT_ERROR"
    HISTORY_UNAVAILABLE = "HISTORY_UNAVAILABLE"
    SAFETY_UNREADABLE = "SAFETY_UNREADABLE"
    CONVERSATION_UNAVAILABLE = "CONVERSATION_UNAVAILABLE"
    EMERGENCY_LOCK_ACTIVE = "EMERGENCY_LOCK_ACTIVE"
    HANDOFF_ACTIVE = "HANDOFF_ACTIVE"
    GENERATION_DISABLED_STAGE = "GENERATION_DISABLED_STAGE"


class ConversationTurnRole(StrEnum):
    CLIENT = "CLIENT"
    MANAGER = "MANAGER"


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeSafetyLayer:
    """Local operational safety — never overridden by published desiredAdminState."""

    trust: TrustBoundary
    bot_mode: BotMode
    emergency_lock: bool
    handoff_state: str | None
    ownership: str | None
    conversation_status: str | None
    manager_takeover_active: bool
    handoff_active: bool
    generation_allowed: bool

    def __repr__(self) -> str:
        return (
            "RuntimeSafetyLayer("
            f"bot_mode={self.bot_mode.value!r}, "
            f"emergency_lock={self.emergency_lock!r}, "
            f"handoff_active={self.handoff_active!r}, "
            f"manager_takeover_active={self.manager_takeover_active!r}, "
            f"generation_allowed={self.generation_allowed!r})"
        )


@dataclass(frozen=True, slots=True)
class RuntimeSettingsLayer:
    trust: TrustBoundary
    publication: SettingsPublicationV1
    settings_readiness: ControlPlaneKindReadiness
    # desiredAdminState is retained for future policy metadata only.
    desired_admin_mode: str
    desired_admin_enabled: bool
    provider: str
    response_mode: str


@dataclass(frozen=True, slots=True)
class RuntimeLiveFactsLayer:
    trust: TrustBoundary
    facts: LiveFactsPayloadV1
    ownership_invariant: str


@dataclass(frozen=True, slots=True)
class RuntimeSelectedKnowledgeEntry:
    trust: TrustBoundary
    key: str
    category: KnowledgeCategory
    title: str
    content: str
    tags: tuple[str, ...]
    service_id: str | None


@dataclass(frozen=True, slots=True)
class RuntimeKnowledgeLayer:
    trust: TrustBoundary
    knowledge_publication_id: str
    version: int
    checksum: str
    knowledge_readiness: ControlPlaneKindReadiness
    coverage: KnowledgeCoverage
    selected: tuple[RuntimeSelectedKnowledgeEntry, ...]
    selected_keys: tuple[str, ...]
    total_published_entries: int


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeConversationTurn:
    trust: TrustBoundary
    role: ConversationTurnRole
    conversation_event_seq: int
    occurred_at: datetime | None
    text: str

    def __repr__(self) -> str:
        return (
            "RuntimeConversationTurn("
            f"trust={self.trust.value!r}, "
            f"role={self.role.value!r}, "
            f"conversation_event_seq={self.conversation_event_seq!r}, "
            "text=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeConversationLayer:
    trust_note: str
    conversation_id: UUID
    event_seq_hwm: int
    turns: tuple[RuntimeConversationTurn, ...]
    turn_count: int
    total_chars: int
    max_turns_ceiling: int
    max_chars_ceiling: int

    def __repr__(self) -> str:
        return (
            "RuntimeConversationLayer("
            f"turn_count={self.turn_count!r}, "
            f"total_chars={self.total_chars!r}, "
            f"event_seq_hwm={self.event_seq_hwm!r})"
        )


@dataclass(frozen=True, slots=True)
class RuntimeContextProvenance:
    settings_publication_id: str | None
    settings_checksum: str | None
    settings_readiness: ControlPlaneKindReadiness | None
    knowledge_publication_id: str | None
    knowledge_checksum: str | None
    knowledge_readiness: ControlPlaneKindReadiness | None
    live_facts_generated_at: datetime | None
    live_facts_service_count: int | None
    live_facts_master_count: int | None
    selected_knowledge_keys: tuple[str, ...]
    history_turn_count: int | None
    ownership_invariant: str


@dataclass(frozen=True, slots=True, repr=False)
class TeyaRuntimeContext:
    context_schema_version: int
    built_at: datetime
    safety: RuntimeSafetyLayer
    settings: RuntimeSettingsLayer | None
    live_facts: RuntimeLiveFactsLayer | None
    knowledge: RuntimeKnowledgeLayer | None
    conversation: RuntimeConversationLayer | None
    provenance: RuntimeContextProvenance

    def __repr__(self) -> str:
        return (
            "TeyaRuntimeContext("
            f"schema={self.context_schema_version!r}, "
            f"built_at={self.built_at.isoformat()!r}, "
            f"generation_allowed={self.safety.generation_allowed!r}, "
            f"has_settings={self.settings is not None!r}, "
            f"has_live_facts={self.live_facts is not None!r}, "
            f"has_knowledge={self.knowledge is not None!r}, "
            f"has_conversation={self.conversation is not None!r})"
        )

    def diagnostic_summary(self) -> dict[str, object]:
        """Safe internal/debug representation — no secrets, PII, or full bodies."""

        return {
            "contextSchemaVersion": self.context_schema_version,
            "builtAt": self.built_at.isoformat(),
            "generationAllowed": self.safety.generation_allowed,
            "botMode": self.safety.bot_mode.value,
            "emergencyLock": self.safety.emergency_lock,
            "handoffActive": self.safety.handoff_active,
            "managerTakeoverActive": self.safety.manager_takeover_active,
            "settingsPublicationId": self.provenance.settings_publication_id,
            "settingsChecksum": self.provenance.settings_checksum,
            "settingsReadiness": (
                self.provenance.settings_readiness.value
                if self.provenance.settings_readiness is not None
                else None
            ),
            "knowledgePublicationId": self.provenance.knowledge_publication_id,
            "knowledgeChecksum": self.provenance.knowledge_checksum,
            "knowledgeReadiness": (
                self.provenance.knowledge_readiness.value
                if self.provenance.knowledge_readiness is not None
                else None
            ),
            "selectedKnowledgeKeys": list(self.provenance.selected_knowledge_keys),
            "knowledgeCoverage": (
                self.knowledge.coverage.value if self.knowledge is not None else None
            ),
            "liveFactsGeneratedAt": (
                self.provenance.live_facts_generated_at.isoformat()
                if self.provenance.live_facts_generated_at is not None
                else None
            ),
            "liveFactsServiceCount": self.provenance.live_facts_service_count,
            "liveFactsMasterCount": self.provenance.live_facts_master_count,
            "historyTurnCount": self.provenance.history_turn_count,
            "ownershipInvariant": self.provenance.ownership_invariant,
        }


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeContextBuildResult:
    readiness: RuntimeContextReadiness
    reasons: tuple[RuntimeContextReason, ...]
    generation_allowed: bool
    context: TeyaRuntimeContext | None

    def __repr__(self) -> str:
        return (
            "RuntimeContextBuildResult("
            f"readiness={self.readiness.value!r}, "
            f"reasons={[r.value for r in self.reasons]!r}, "
            f"generation_allowed={self.generation_allowed!r}, "
            f"has_context={self.context is not None!r})"
        )

    def diagnostic_summary(self) -> dict[str, object]:
        base: dict[str, object] = {
            "readiness": self.readiness.value,
            "reasons": [r.value for r in self.reasons],
            "generationAllowed": self.generation_allowed,
        }
        if self.context is not None:
            base["context"] = self.context.diagnostic_summary()
        return base


def knowledge_entry_to_selected(
    entry: KnowledgeEntryV1,
) -> RuntimeSelectedKnowledgeEntry:
    return RuntimeSelectedKnowledgeEntry(
        trust=TrustBoundary.TRUSTED_MANAGED_KB,
        key=entry.key,
        category=entry.category,
        title=entry.title,
        content=entry.content,
        tags=entry.tags,
        service_id=entry.service_id,
    )


DEFAULT_OWNERSHIP_INVARIANT: Final[str] = LIVE_FACTS_OWNERSHIP_INVARIANT
