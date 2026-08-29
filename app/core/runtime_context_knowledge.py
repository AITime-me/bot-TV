"""Deterministic non-LLM knowledge selection for runtime context.

Selection uses only structured metadata: serviceId, category, tags, and
stable key/title keyword tokens. No embeddings. No vector DB. Never invents
entries. Ordering and limits are deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.control_plane_types import (
    ControlPlaneKindReadiness,
    KnowledgeCategory,
    KnowledgeEntryV1,
)
from app.core.runtime_context_types import (
    HARD_MAX_SELECTED_KNOWLEDGE_ENTRIES,
    KnowledgeCoverage,
    RuntimeKnowledgeLayer,
    RuntimeSelectedKnowledgeEntry,
    TrustBoundary,
    knowledge_entry_to_selected,
)


@dataclass(frozen=True, slots=True)
class KnowledgeSelectionHint:
    """Optional structured signals — never free-form LLM retrieval."""

    service_ids: tuple[str, ...] = ()
    categories: tuple[KnowledgeCategory, ...] = ()
    tags: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


def _normalize_token(value: str) -> str:
    return value.strip().lower()


def _keyword_hit(entry: KnowledgeEntryV1, keywords: tuple[str, ...]) -> bool:
    if not keywords:
        return False
    haystack = f"{entry.key} {entry.title}".lower()
    for raw in keywords:
        token = _normalize_token(raw)
        if token and token in haystack:
            return True
    return False


def _score_entry(
    entry: KnowledgeEntryV1,
    hint: KnowledgeSelectionHint,
) -> int | None:
    """Return priority score (higher = better) or None if filtered out.

    Without any hint signals, every entry is a candidate (score 0).
    With signals, only matching entries are candidates.
    """

    service_ids = frozenset(hint.service_ids)
    categories = frozenset(hint.categories)
    tags = frozenset(_normalize_token(t) for t in hint.tags if t.strip())
    keywords = hint.keywords
    has_signal = bool(service_ids or categories or tags or keywords)

    score = 0
    matched = False

    if service_ids:
        if entry.service_id is not None and entry.service_id in service_ids:
            score += 100
            matched = True
        elif entry.service_id is not None:
            # Explicit other-service entries stay out when service filter active.
            return None

    if categories:
        if entry.category in categories:
            score += 40
            matched = True
        else:
            return None

    if tags:
        entry_tags = frozenset(_normalize_token(t) for t in entry.tags)
        overlap = entry_tags & tags
        if overlap:
            score += 20 * len(overlap)
            matched = True
        else:
            return None

    if keywords:
        if _keyword_hit(entry, keywords):
            score += 10
            matched = True
        elif has_signal and not (service_ids or categories or tags):
            return None

    if has_signal and not matched:
        return None
    return score


def select_knowledge_entries(
    entries: tuple[KnowledgeEntryV1, ...],
    *,
    hint: KnowledgeSelectionHint | None = None,
    max_entries: int = HARD_MAX_SELECTED_KNOWLEDGE_ENTRIES,
) -> tuple[tuple[RuntimeSelectedKnowledgeEntry, ...], KnowledgeCoverage]:
    if max_entries < 1:
        raise ValueError("max_entries must be positive")
    ceiling = min(max_entries, HARD_MAX_SELECTED_KNOWLEDGE_ENTRIES)
    selection_hint = hint if hint is not None else KnowledgeSelectionHint()

    if not entries:
        return (), KnowledgeCoverage.MISSING

    scored: list[tuple[int, str, KnowledgeEntryV1]] = []
    for entry in entries:
        score = _score_entry(entry, selection_hint)
        if score is None:
            continue
        scored.append((score, entry.key, entry))

    if not scored:
        has_signal = bool(
            selection_hint.service_ids
            or selection_hint.categories
            or selection_hint.tags
            or selection_hint.keywords
        )
        if has_signal:
            return (), KnowledgeCoverage.MISSING
        return (), KnowledgeCoverage.MISSING

    # Deterministic: score desc, then stable key asc.
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected_raw = [item[2] for item in scored[:ceiling]]
    selected = tuple(knowledge_entry_to_selected(e) for e in selected_raw)

    has_signal = bool(
        selection_hint.service_ids
        or selection_hint.categories
        or selection_hint.tags
        or selection_hint.keywords
    )
    if has_signal and len(selected) < len(scored):
        coverage = KnowledgeCoverage.PARTIAL
    elif has_signal:
        coverage = KnowledgeCoverage.AVAILABLE
    elif len(selected) < len(entries):
        coverage = KnowledgeCoverage.PARTIAL
    else:
        coverage = KnowledgeCoverage.AVAILABLE

    return selected, coverage


def build_knowledge_layer(
    *,
    knowledge_publication_id: str,
    version: int,
    checksum: str,
    knowledge_readiness: ControlPlaneKindReadiness,
    entries: tuple[KnowledgeEntryV1, ...],
    hint: KnowledgeSelectionHint | None = None,
    max_entries: int = HARD_MAX_SELECTED_KNOWLEDGE_ENTRIES,
) -> RuntimeKnowledgeLayer:
    selected, coverage = select_knowledge_entries(
        entries, hint=hint, max_entries=max_entries
    )
    return RuntimeKnowledgeLayer(
        trust=TrustBoundary.TRUSTED_MANAGED_KB,
        knowledge_publication_id=knowledge_publication_id,
        version=version,
        checksum=checksum,
        knowledge_readiness=knowledge_readiness,
        coverage=coverage,
        selected=selected,
        selected_keys=tuple(entry.key for entry in selected),
        total_published_entries=len(entries),
    )
