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

# Minimum stem length for morphology-tolerant Russian token match.
_MIN_MORPH_STEM_LEN: int = 4

# Single-token query-subset match requires this length (conservative false-positive guard).
_MIN_DISTINCTIVE_QUERY_TOKEN_LEN: int = 6

# Minimum latin-skeleton length for cross-script token equivalence.
_MIN_CROSS_SCRIPT_SKELETON_LEN: int = 6

# Deterministic Cyrillic → Latin skeleton (no brand alias dictionary).
_CYRILLIC_TO_LATIN_SKELETON: dict[str, str] = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "i",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "c",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}

# Deterministic Russian inflection suffixes (longest first). No dictionary aliases.
_RUSSIAN_INFLECTION_SUFFIXES: tuple[str, ...] = (
    "иями",
    "ями",
    "ами",
    "ого",
    "его",
    "ому",
    "ему",
    "ией",
    "иям",
    "иях",
    "ую",
    "юю",
    "ая",
    "яя",
    "ое",
    "ее",
    "ие",
    "ые",
    "ом",
    "ем",
    "ам",
    "ям",
    "ах",
    "ях",
    "ов",
    "ев",
    "ей",
    "ий",
    "ый",
    "ой",
    "ю",
    "у",
    "а",
    "е",
    "и",
    "о",
    "ы",
    "я",
)

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
        "стоит",
        "стоимость",
        "цена",
        "цены",
        "кто",
        "делает",
        "делать",
        "где",
        "когда",
        "про",
        "расскажите",
        "расскажи",
        "подготовиться",
        "после",
        "нельзя",
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
    """Latest non-empty client turn (for keywords/categories)."""

    if context.conversation is None:
        return ""
    turns = client_turn_texts_newest_first(context.conversation.turns)
    return turns[0] if turns else ""


def conversation_client_text_from_turns(
    turns: Sequence[object],
) -> str:
    """Latest non-empty client turn text."""

    newest = client_turn_texts_newest_first(turns)
    return newest[0] if newest else ""


def client_turn_texts_newest_first(
    turns: Sequence[object],
) -> tuple[str, ...]:
    """Non-empty client turn texts, newest first."""

    ordered: list[str] = []
    for turn in turns:
        role = getattr(turn, "role", None)
        if role is ConversationTurnRole.CLIENT:
            text = getattr(turn, "text", "")
            if type(text) is str and text.strip():
                ordered.append(text.strip())
    ordered.reverse()
    return tuple(ordered)


def _inflection_stem(token: str) -> str:
    """Deterministic morphology-tolerant stem for Russian tokens (no LLM/dictionary)."""

    if len(token) <= _MIN_MORPH_STEM_LEN:
        return token
    for suffix in _RUSSIAN_INFLECTION_SUFFIXES:
        if not token.endswith(suffix):
            continue
        stem = token[: -len(suffix)]
        if len(stem) >= _MIN_MORPH_STEM_LEN:
            return stem
    if len(token) > _MIN_MORPH_STEM_LEN:
        candidate = token[:-1]
        if len(candidate) >= _MIN_MORPH_STEM_LEN:
            return candidate
    return token


def _latin_script_skeleton(token: str) -> str:
    """Fold a token to a latin letter skeleton for conservative cross-script equality.

    Cyrillic letters are mapped via a fixed table. Latin ``x`` expands to ``ks``
    (conventional Russian brand spelling). No brand-specific aliases.
    """

    folded = token.casefold()
    parts: list[str] = []
    for char in folded:
        mapped = _CYRILLIC_TO_LATIN_SKELETON.get(char)
        if mapped is not None:
            parts.append(mapped)
        elif "a" <= char <= "z" or char.isdigit():
            parts.append(char)
    skeleton = "".join(parts)
    return skeleton.replace("x", "ks")


def _cross_script_token_equivalent(left: str, right: str) -> bool:
    """True when tokens share the same distinctive latin skeleton (or stems)."""

    left_skel = _latin_script_skeleton(left)
    right_skel = _latin_script_skeleton(right)
    if (
        len(left_skel) < _MIN_CROSS_SCRIPT_SKELETON_LEN
        or len(right_skel) < _MIN_CROSS_SCRIPT_SKELETON_LEN
    ):
        return False
    if left_skel == right_skel:
        return True
    left_stem_skel = _latin_script_skeleton(_inflection_stem(left))
    right_stem_skel = _latin_script_skeleton(_inflection_stem(right))
    if (
        len(left_stem_skel) < _MIN_CROSS_SCRIPT_SKELETON_LEN
        or len(right_stem_skel) < _MIN_CROSS_SCRIPT_SKELETON_LEN
    ):
        return False
    return left_stem_skel == right_stem_skel


def _significant_name_tokens(service_name: str) -> tuple[str, ...]:
    normalized = normalize_match_text(service_name)
    if not normalized:
        return ()
    tokens: list[str] = []
    for raw in normalized.split():
        if len(raw) < 3 or raw in _STOP_WORDS:
            continue
        tokens.append(raw)
    return tuple(tokens)


def _token_stem_matches(name_token: str, text_tokens: Sequence[str]) -> bool:
    if name_token in text_tokens:
        return True
    name_stem = _inflection_stem(name_token)
    if len(name_stem) < _MIN_MORPH_STEM_LEN:
        return name_token in text_tokens or any(
            _cross_script_token_equivalent(name_token, text_token)
            for text_token in text_tokens
        )
    for text_token in text_tokens:
        if text_token == name_token:
            return True
        if _inflection_stem(text_token) == name_stem:
            return True
        if _cross_script_token_equivalent(name_token, text_token):
            return True
    return False


def _query_significant_tokens(normalized_text: str) -> tuple[str, ...]:
    """Significant tokens from a client phrase for query-subset matching."""

    if not normalized_text:
        return ()
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in normalized_text.split():
        if len(raw) < 3 or raw in _STOP_WORDS:
            continue
        if raw not in seen:
            seen.add(raw)
            tokens.append(raw)
    return tuple(tokens)


def _service_name_tokens(service_name: str) -> tuple[str, ...]:
    """Significant tokens from canonical service name (incl. slash-separated forms)."""

    tokens: list[str] = []
    seen: set[str] = set()
    for segment in service_name.split("/"):
        normalized = normalize_match_text(segment)
        for raw in normalized.split():
            if len(raw) < 3 or raw in _STOP_WORDS:
                continue
            if raw not in seen:
                seen.add(raw)
                tokens.append(raw)
    return tuple(tokens)


def _query_token_match_score(
    service_name: str,
    query_tokens: Sequence[str],
) -> int:
    """Count of identity query tokens that stem-match the canonical service name."""

    if not query_tokens:
        return 0
    service_tokens = _service_name_tokens(service_name)
    if not service_tokens:
        return 0
    return sum(
        1
        for query_token in query_tokens
        if _token_stem_matches(query_token, service_tokens)
    )


def _filter_identity_query_tokens(
    query_tokens: Sequence[str],
    services: Sequence[LiveFactsServiceV1],
) -> tuple[str, ...]:
    """Keep client tokens that stem-match at least one active service-name token."""

    if not query_tokens:
        return ()
    catalog_tokens: list[str] = []
    seen: set[str] = set()
    for service in services:
        if not service.is_active:
            continue
        for token in _service_name_tokens(service.name):
            if token not in seen:
                seen.add(token)
                catalog_tokens.append(token)
    if not catalog_tokens:
        return ()
    return tuple(
        query_token
        for query_token in query_tokens
        if _token_stem_matches(query_token, catalog_tokens)
    )


def _collect_query_subset_matches(
    services: Sequence[LiveFactsServiceV1],
    normalized_text: str,
) -> list[LiveFactsServiceV1]:
    """Conservative subset match: strongest unique evidence wins.

    Extra catalog-matching words in the client phrase must not erase a stronger
    unique service match. Equal top scores stay AMBIGUOUS (caller fail-closes).
    """

    query_tokens = _filter_identity_query_tokens(
        _query_significant_tokens(normalized_text),
        services,
    )
    if not query_tokens:
        return []

    scored: list[tuple[int, LiveFactsServiceV1]] = []
    for service in services:
        if not service.is_active:
            continue
        score = _query_token_match_score(service.name, query_tokens)
        if score <= 0:
            continue
        if score == 1:
            matching = [
                query_token
                for query_token in query_tokens
                if _token_stem_matches(
                    query_token, _service_name_tokens(service.name)
                )
            ]
            if (
                len(matching) != 1
                or len(matching[0]) < _MIN_DISTINCTIVE_QUERY_TOKEN_LEN
            ):
                continue
        scored.append((score, service))

    if not scored:
        return []

    max_score = max(score for score, _ in scored)
    return [service for score, service in scored if score == max_score]


def _service_name_morphology_match(service_name: str, normalized_text: str) -> bool:
    name_tokens = _significant_name_tokens(service_name)
    if not name_tokens:
        return False
    text_tokens = tuple(normalize_match_text(normalized_text).split())
    if not text_tokens:
        return False
    return all(_token_stem_matches(token, text_tokens) for token in name_tokens)


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


def _collect_tier_matches(
    services: Sequence[LiveFactsServiceV1],
    normalized: str,
    text_cf: str,
    *,
    tier: str,
) -> list[LiveFactsServiceV1]:
    matches: list[LiveFactsServiceV1] = []
    for service in services:
        if tier == "exact" and _service_name_in_text(service.name, normalized):
            matches.append(service)
        elif tier == "substring":
            name_cf = service.name.casefold().strip()
            if name_cf and name_cf in text_cf:
                matches.append(service)
        elif tier == "morphology":
            # Single-token canonical names are too ambiguous for morphology alone.
            if len(_significant_name_tokens(service.name)) < 2:
                continue
            if _service_name_morphology_match(service.name, normalized):
                matches.append(service)
    return matches


def _collect_resolution_matches(
    services: Sequence[LiveFactsServiceV1],
    normalized: str,
    text_cf: str,
) -> list[LiveFactsServiceV1]:
    for tier in ("exact", "substring", "morphology"):
        matches = _collect_tier_matches(
            services, normalized, text_cf, tier=tier
        )
        if matches:
            return matches
    return _collect_query_subset_matches(services, normalized)


def _resolution_from_matches(
    matches: Sequence[LiveFactsServiceV1],
) -> ServiceResolutionResult | None:
    if len(matches) == 1:
        return ServiceResolutionResult(
            status=ServiceResolutionStatus.UNIQUE,
            service_ids=(matches[0].id,),
            match_count=1,
        )
    if len(matches) > 1:
        return ServiceResolutionResult(
            status=ServiceResolutionStatus.AMBIGUOUS,
            service_ids=tuple(s.id for s in matches),
            match_count=len(matches),
        )
    return None


def resolve_live_fact_services(
    text: str,
    services: Sequence[LiveFactsServiceV1],
) -> ServiceResolutionResult:
    """Resolve relevant service(s) from a single client turn.

    Priority: exact/full name → substring → morphology → query-token subset.
    Never silently picks the first service when multiple match.
    """

    if not text.strip() or not services:
        return ServiceResolutionResult(
            status=ServiceResolutionStatus.NONE,
            match_count=0,
        )

    normalized = normalize_match_text(text)
    text_cf = text.casefold()

    matches = _collect_resolution_matches(services, normalized, text_cf)
    resolved = _resolution_from_matches(matches)
    if resolved is not None:
        return resolved

    return ServiceResolutionResult(
        status=ServiceResolutionStatus.NONE,
        match_count=0,
    )


def resolve_live_fact_services_from_client_turns(
    client_turns_newest_first: Sequence[str],
    services: Sequence[LiveFactsServiceV1],
) -> ServiceResolutionResult:
    """Latest client turn first; unique-only fallback to earlier turns.

    - Latest UNIQUE → use it.
    - Latest AMBIGUOUS → fail closed (no history bleed).
    - Latest NONE → try older turns; each must resolve UNIQUE to adopt.
    """

    if not services:
        return ServiceResolutionResult(
            status=ServiceResolutionStatus.NONE,
            match_count=0,
        )

    turns = tuple(
        t.strip()
        for t in client_turns_newest_first
        if type(t) is str and t.strip()
    )
    if not turns:
        return ServiceResolutionResult(
            status=ServiceResolutionStatus.NONE,
            match_count=0,
        )

    for turn in turns:
        result = resolve_live_fact_services(turn, services)
        if result.status is ServiceResolutionStatus.UNIQUE:
            return result
        if result.status is ServiceResolutionStatus.AMBIGUOUS:
            return result

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
    client_turns_newest_first: Sequence[str] | None = None,
) -> KnowledgeSelectionHint:
    """Build deterministic KB hint from conversation + optional structured hint.

    ``structured_service_hint`` (eval scenario metadata) is merged as an extra
    keyword signal — it does not bypass resolution architecture.
    """

    turns = client_turns_newest_first
    if turns is None and conversation_text.strip():
        turns = (conversation_text.strip(),)

    service_ids: tuple[str, ...] = ()
    if live_facts is not None and turns:
        resolution = resolve_live_fact_services_from_client_turns(
            turns, live_facts.services
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
    if context.conversation is None:
        return None
    turns = client_turn_texts_newest_first(context.conversation.turns)
    resolution = resolve_live_fact_services_from_client_turns(
        turns, context.live_facts.facts.services
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
