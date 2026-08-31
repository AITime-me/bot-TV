"""Deterministic relevance selection for shadow-draft prompts (AI-DIALOGUE-02).

Resolves Live Facts services and Managed KB hints from trusted conversation
context only. No LLM classifiers. Shared by production compile and eval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

from app.core.control_plane_types import KnowledgeCategory, KnowledgeEntryV1
from app.core.live_facts_types import (
    LiveFactsMasterV1,
    LiveFactsPayloadV1,
    LiveFactsServiceV1,
    LiveFactsStudioV1,
)
from app.core.runtime_context_knowledge import KnowledgeSelectionHint
from app.core.runtime_context_types import (
    ConversationTurnRole,
    TeyaRuntimeContext,
)

# Safe margin under YandexGptHttpClient._MAX_MESSAGE_CHARS (12_000).
SHADOW_DRAFT_COMPILED_CHAR_BUDGET: int = 10_500
YANDEX_PROVIDER_MESSAGE_CHAR_CEILING: int = 12_000

# Compact names-only catalog when service cannot be resolved uniquely.
_MAX_CATALOG_NAME_LINES: int = 40

_NORMALIZE_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")

_STOP_WORDS = frozenset(
    {
        "что",
        "как",
        "можно",
        "ли",
        "у",
        "вас",
        "в",
        "на",
        "по",
        "за",
        "или",
        "это",
        "the",
        "and",
        "you",
        "your",
        "для",
        "меня",
        "мне",
        "сколько",
        "кто",
        "где",
        "когда",
        "про",
        "при",
        "не",
        "да",
        "нет",
        "ты",
        "вы",
        "наш",
        "наша",
        "наше",
        "студии",
        "студия",
        "процедура",
        "процедуру",
        "услуга",
        "услуги",
    }
)


class ServiceResolutionStatus(StrEnum):
    UNIQUE = "UNIQUE"
    NONE = "NONE"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class ServiceResolutionResult:
    status: ServiceResolutionStatus
    service_ids: tuple[str, ...] = ()
    match_count: int = 0


@dataclass(frozen=True, slots=True)
class SelectedLiveFactsSlice:
    """Subset of Live Facts for prompt serialization."""

    services: tuple[LiveFactsServiceV1, ...]
    masters: tuple[LiveFactsMasterV1, ...]
    catalog_names_only: bool
    resolution: ServiceResolutionResult


@dataclass(frozen=True, slots=True)
class ShadowDraftPromptMetrics:
    message_count: int
    system_chars: int
    dialog_chars: int
    total_chars: int
    selected_kb_count: int
    live_services_included: int
    live_masters_included: int
    service_resolution: str
    within_budget: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "messageCount": self.message_count,
            "systemChars": self.system_chars,
            "dialogChars": self.dialog_chars,
            "totalChars": self.total_chars,
            "selectedKbCount": self.selected_kb_count,
            "liveServicesIncluded": self.live_services_included,
            "liveMastersIncluded": self.live_masters_included,
            "serviceResolution": self.service_resolution,
            "withinBudget": self.within_budget,
        }


def normalize_match_text(text: str) -> str:
    lowered = text.casefold()
    cleaned = _NORMALIZE_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", cleaned).strip()


def conversation_client_text(context: TeyaRuntimeContext) -> str:
    if context.conversation is None:
        return ""
    return conversation_client_text_from_turns(context.conversation.turns)


def conversation_client_text_from_turns(
    turns: Sequence[object],
) -> str:
    parts: list[str] = []
    for turn in turns:
        role = getattr(turn, "role", None)
        if role is ConversationTurnRole.CLIENT:
            text = getattr(turn, "text", "")
            if type(text) is str and text.strip():
                parts.append(text)
    return " ".join(parts).strip()


def _service_name_in_text(service_name: str, normalized_text: str) -> bool:
    name_norm = normalize_match_text(service_name)
    if not name_norm:
        return False
    if name_norm in normalized_text:
        return True
    # Word-boundary full name match inside normalized text.
    return re.search(
        rf"(?<!\w){re.escape(name_norm)}(?!\w)",
        normalized_text,
    ) is not None


def resolve_live_fact_services(
    text: str,
    services: Sequence[LiveFactsServiceV1],
) -> ServiceResolutionResult:
    """Resolve relevant service(s) from trusted conversation text.

    Exact/full service name match has priority. Substring match is used only
    when it yields exactly one candidate. Never silently picks the first
    service when multiple match.
    """

    if not text.strip() or not services:
        return ServiceResolutionResult(
            status=ServiceResolutionStatus.NONE,
            match_count=0,
        )

    normalized = normalize_match_text(text)
    text_cf = text.casefold()

    full_matches: list[LiveFactsServiceV1] = []
    for service in services:
        if _service_name_in_text(service.name, normalized):
            full_matches.append(service)

    if len(full_matches) == 1:
        return ServiceResolutionResult(
            status=ServiceResolutionStatus.UNIQUE,
            service_ids=(full_matches[0].id,),
            match_count=1,
        )
    if len(full_matches) > 1:
        return ServiceResolutionResult(
            status=ServiceResolutionStatus.AMBIGUOUS,
            service_ids=tuple(s.id for s in full_matches),
            match_count=len(full_matches),
        )

    substring_matches: list[LiveFactsServiceV1] = []
    for service in services:
        name_cf = service.name.casefold().strip()
        if name_cf and name_cf in text_cf:
            substring_matches.append(service)

    if len(substring_matches) == 1:
        return ServiceResolutionResult(
            status=ServiceResolutionStatus.UNIQUE,
            service_ids=(substring_matches[0].id,),
            match_count=1,
        )
    if len(substring_matches) > 1:
        return ServiceResolutionResult(
            status=ServiceResolutionStatus.AMBIGUOUS,
            service_ids=tuple(s.id for s in substring_matches),
            match_count=len(substring_matches),
        )

    return ServiceResolutionResult(
        status=ServiceResolutionStatus.NONE,
        match_count=0,
    )


def extract_client_keywords(text: str) -> tuple[str, ...]:
    normalized = normalize_match_text(text)
    if not normalized:
        return ()
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in normalized.split():
        if len(raw) < 3 or raw in _STOP_WORDS:
            continue
        if raw not in seen:
            seen.add(raw)
            tokens.append(raw)
    return tuple(tokens)


def build_knowledge_selection_hint(
    *,
    conversation_text: str,
    live_facts: LiveFactsPayloadV1 | None,
    structured_service_hint: str | None = None,
) -> KnowledgeSelectionHint:
    """Build deterministic KB hint from conversation + optional structured hint.

    ``structured_service_hint`` (eval scenario metadata) is merged as an extra
    keyword signal — it does not bypass resolution architecture.
    """

    resolution_source = conversation_text
    if structured_service_hint:
        resolution_source = f"{conversation_text} {structured_service_hint}"

    service_ids: tuple[str, ...] = ()
    if live_facts is not None:
        resolution = resolve_live_fact_services(
            resolution_source, live_facts.services
        )
        if resolution.status is ServiceResolutionStatus.UNIQUE:
            service_ids = resolution.service_ids

    keywords = extract_client_keywords(conversation_text)
    if structured_service_hint:
        hint_norm = normalize_match_text(structured_service_hint)
        extra = tuple(
            t for t in hint_norm.split() if t and t not in keywords
        )
        keywords = keywords + extra

    categories: tuple[KnowledgeCategory, ...] = ()
    lowered = conversation_text.casefold()
    cat_candidates: list[KnowledgeCategory] = []
    if any(w in lowered for w in ("подготов", "перед процедур")):
        cat_candidates.append(KnowledgeCategory.PREPARATION)
    if any(w in lowered for w in ("после", "aftercare", "уход")):
        cat_candidates.append(KnowledgeCategory.AFTERCARE)
    if any(w in lowered for w in ("противопоказ", "безопас", "диагноз")):
        cat_candidates.append(KnowledgeCategory.SAFETY_INFORMATION)
    if "relatox" in lowered:
        cat_candidates.append(KnowledgeCategory.FAQ)
    if any(w in lowered for w in ("что такое", "процедур", "объясн")):
        cat_candidates.append(KnowledgeCategory.PROCEDURE_EXPLANATION)
    if cat_candidates:
        categories = tuple(dict.fromkeys(cat_candidates))

    return KnowledgeSelectionHint(
        service_ids=service_ids,
        categories=categories,
        keywords=keywords,
    )


def select_live_facts_slice(
    live_facts: LiveFactsPayloadV1,
    resolution: ServiceResolutionResult,
) -> SelectedLiveFactsSlice:
    if resolution.status is ServiceResolutionStatus.UNIQUE:
        target_ids = frozenset(resolution.service_ids)
        services = tuple(s for s in live_facts.services if s.id in target_ids)
        masters = tuple(
            m
            for m in live_facts.masters
            if target_ids & frozenset(m.service_ids)
        )
        return SelectedLiveFactsSlice(
            services=services,
            masters=masters,
            catalog_names_only=False,
            resolution=resolution,
        )

    # Unresolved / ambiguous: compact names-only catalog, no per-service dynamics.
    active_services = tuple(s for s in live_facts.services if s.is_active)
    catalog = active_services[:_MAX_CATALOG_NAME_LINES]
    return SelectedLiveFactsSlice(
        services=catalog,
        masters=(),
        catalog_names_only=True,
        resolution=resolution,
    )


def resolve_and_select_live_facts(
    context: TeyaRuntimeContext,
) -> SelectedLiveFactsSlice | None:
    if context.live_facts is None:
        return None
    text = conversation_client_text(context)
    resolution = resolve_live_fact_services(
        text, context.live_facts.facts.services
    )
    return select_live_facts_slice(context.live_facts.facts, resolution)


def measure_prompt_messages(
    messages: Sequence[object],
    *,
    selected_kb_count: int,
    live_services_included: int,
    live_masters_included: int,
    service_resolution: str,
) -> ShadowDraftPromptMetrics:
    system_chars = 0
    dialog_chars = 0
    total = 0
    for msg in messages:
        text = getattr(msg, "text", "")
        role = getattr(msg, "role", "")
        if type(text) is not str:
            continue
        total += len(text)
        if role == "system":
            system_chars += len(text)
        else:
            dialog_chars += len(text)
    return ShadowDraftPromptMetrics(
        message_count=len(messages),
        system_chars=system_chars,
        dialog_chars=dialog_chars,
        total_chars=total,
        selected_kb_count=selected_kb_count,
        live_services_included=live_services_included,
        live_masters_included=live_masters_included,
        service_resolution=service_resolution,
        within_budget=total <= SHADOW_DRAFT_COMPILED_CHAR_BUDGET,
    )


def reselect_knowledge_entries(
    publication_entries: tuple[KnowledgeEntryV1, ...],
    hint: KnowledgeSelectionHint,
    *,
    max_entries: int,
):
    from app.core.runtime_context_knowledge import select_knowledge_entries

    return select_knowledge_entries(
        publication_entries, hint=hint, max_entries=max_entries
    )
