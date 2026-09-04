"""Typed shadow draft reply contract (AI-DIALOGUE-02).

Internal quality-check result only. Never creates outbound, CRM, booking, or
client delivery. Text never appears in repr/logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Mapping
from uuid import UUID


class ShadowDraftDisposition(StrEnum):
    REPLY = "REPLY"
    HANDOFF = "HANDOFF"
    DENIED = "DENIED"
    PROVIDER_ERROR = "PROVIDER_ERROR"


class ShadowDraftReasonCode(StrEnum):
    OK = "OK"
    GATE_DENIED = "GATE_DENIED"
    SETTINGS_NOT_USABLE = "SETTINGS_NOT_USABLE"
    KNOWLEDGE_NOT_USABLE = "KNOWLEDGE_NOT_USABLE"
    LIVE_FACTS_NOT_USABLE = "LIVE_FACTS_NOT_USABLE"
    GENERATION_NOT_ALLOWED = "GENERATION_NOT_ALLOWED"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    SHADOW_FEATURE_DISABLED = "SHADOW_FEATURE_DISABLED"
    HANDOFF_ACTIVE = "HANDOFF_ACTIVE"
    MANAGER_TAKEOVER = "MANAGER_TAKEOVER"
    EMERGENCY_LOCK = "EMERGENCY_LOCK"
    CONTEXT_NOT_READY = "CONTEXT_NOT_READY"
    PROMPT_BUDGET_EXCEEDED = "PROMPT_BUDGET_EXCEEDED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_TRANSPORT_ERROR = "PROVIDER_TRANSPORT_ERROR"
    PROVIDER_REMOTE_REJECTED = "PROVIDER_REMOTE_REJECTED"
    PROVIDER_RESPONSE_INVALID = "PROVIDER_RESPONSE_INVALID"
    PROVIDER_RESPONSE_TOO_LARGE = "PROVIDER_RESPONSE_TOO_LARGE"
    PROVIDER_EMPTY = "PROVIDER_EMPTY"
    PROVIDER_CONFIG_INVALID = "PROVIDER_CONFIG_INVALID"
    PROVIDER_ERROR = "PROVIDER_ERROR"


_ALLOWED_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "provider",
        "text_len",
        "message_count",
        "error_code",
        "shadow",
        "model_configured",
        "provider_transport_called",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class ShadowDraftProvenanceSummary:
    """Safe provenance — ids/checksums/keys only, never bodies or dialog."""

    settings_publication_id: str | None
    settings_checksum: str | None
    knowledge_publication_id: str | None
    knowledge_checksum: str | None
    selected_knowledge_keys: tuple[str, ...]
    live_facts_service_count: int | None
    live_facts_master_count: int | None
    history_turn_count: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "settingsPublicationId": self.settings_publication_id,
            "settingsChecksum": self.settings_checksum,
            "knowledgePublicationId": self.knowledge_publication_id,
            "knowledgeChecksum": self.knowledge_checksum,
            "selectedKnowledgeKeys": list(self.selected_knowledge_keys),
            "liveFactsServiceCount": self.live_facts_service_count,
            "liveFactsMasterCount": self.live_facts_master_count,
            "historyTurnCount": self.history_turn_count,
        }

    def __repr__(self) -> str:
        return (
            "ShadowDraftProvenanceSummary("
            f"settings={self.settings_publication_id!r}, "
            f"knowledge={self.knowledge_publication_id!r}, "
            f"kb_keys={len(self.selected_knowledge_keys)!r}, "
            f"turns={self.history_turn_count!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ShadowAssistantTurn:
    """Shadow-only virtual assistant turn for multi-turn QA continuity.

    Not a shared RuntimeConversationTurn. Never written to inbox/manager/outbox.
    """

    conversation_event_seq: int
    inbox_message_id: UUID
    text: str

    def __post_init__(self) -> None:
        if type(self.conversation_event_seq) is not int or self.conversation_event_seq < 1:
            raise ValueError("conversation_event_seq invalid")
        if not isinstance(self.inbox_message_id, UUID):
            raise ValueError("inbox_message_id invalid")
        if type(self.text) is not str or not self.text:
            raise ValueError("text invalid")

    def __repr__(self) -> str:
        return (
            "ShadowAssistantTurn("
            f"conversation_event_seq={self.conversation_event_seq!r}, "
            f"inbox_message_id={self.inbox_message_id!r}, "
            f"text_len={len(self.text)!r}, "
            "text=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ShadowDraftReply:
    """Internal shadow draft. Never an outbound payload."""

    text: str | None
    disposition: ShadowDraftDisposition
    handoff_required: bool
    reason_code: ShadowDraftReasonCode
    provenance: ShadowDraftProvenanceSummary
    generation_metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, ShadowDraftDisposition):
            raise ValueError("disposition invalid")
        if not isinstance(self.reason_code, ShadowDraftReasonCode):
            raise ValueError("reason_code invalid")
        if type(self.handoff_required) is not bool:
            raise ValueError("handoff_required invalid")
        if self.text is not None and type(self.text) is not str:
            raise ValueError("text invalid")
        meta = dict(self.generation_metadata)
        for key in meta:
            if key not in _ALLOWED_METADATA_KEYS:
                raise ValueError("generation_metadata key invalid")
        object.__setattr__(self, "generation_metadata", meta)

    def diagnostic_summary(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "handoffRequired": self.handoff_required,
            "reasonCode": self.reason_code.value,
            "hasText": self.text is not None and bool(self.text.strip()),
            "textLen": len(self.text) if self.text is not None else 0,
            "provenance": self.provenance.as_dict(),
            "generationMetadata": dict(self.generation_metadata),
        }

    def __repr__(self) -> str:
        return (
            "ShadowDraftReply("
            f"disposition={self.disposition.value!r}, "
            f"reason_code={self.reason_code.value!r}, "
            f"handoff_required={self.handoff_required!r}, "
            f"text_len={len(self.text) if self.text else 0!r})"
        )
