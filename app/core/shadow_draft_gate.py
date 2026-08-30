"""Shadow draft generation gate (AI-DIALOGUE-02).

Fail-closed: any missing/stale/invalid required source denies generation and
prevents YandexGPT calls. Independent of BOT_MODE=AUTO and client delivery.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.control_plane_types import ControlPlaneKindReadiness
from app.core.runtime_context_types import (
    RuntimeContextBuildResult,
    RuntimeContextReadiness,
    TeyaRuntimeContext,
)
from app.core.shadow_draft_types import ShadowDraftReasonCode


@dataclass(frozen=True, slots=True)
class ShadowDraftGateDecision:
    allowed: bool
    reason_code: ShadowDraftReasonCode
    deny_reasons: tuple[ShadowDraftReasonCode, ...]


def _readiness_usable(value: ControlPlaneKindReadiness | None) -> bool:
    return value in {
        ControlPlaneKindReadiness.READY_FRESH,
        ControlPlaneKindReadiness.READY_STALE,
    }


def evaluate_shadow_draft_gate(
    *,
    context: TeyaRuntimeContext | None,
    generation_allowed: bool,
    provider_configured: bool,
    shadow_feature_enabled: bool,
    readiness: RuntimeContextReadiness | None = None,
) -> ShadowDraftGateDecision:
    """Return whether YandexGPT may be called for an internal shadow draft."""

    denies: list[ShadowDraftReasonCode] = []

    if not shadow_feature_enabled:
        denies.append(ShadowDraftReasonCode.SHADOW_FEATURE_DISABLED)
    if not provider_configured:
        denies.append(ShadowDraftReasonCode.PROVIDER_NOT_CONFIGURED)
    if not generation_allowed:
        denies.append(ShadowDraftReasonCode.GENERATION_NOT_ALLOWED)
    if context is None:
        denies.append(ShadowDraftReasonCode.CONTEXT_NOT_READY)
        return ShadowDraftGateDecision(
            allowed=False,
            reason_code=denies[0],
            deny_reasons=tuple(denies),
        )

    if readiness is RuntimeContextReadiness.NOT_READY:
        denies.append(ShadowDraftReasonCode.CONTEXT_NOT_READY)

    safety = context.safety
    if safety.emergency_lock:
        denies.append(ShadowDraftReasonCode.EMERGENCY_LOCK)
    if safety.manager_takeover_active:
        denies.append(ShadowDraftReasonCode.MANAGER_TAKEOVER)
    if safety.handoff_active:
        denies.append(ShadowDraftReasonCode.HANDOFF_ACTIVE)
    if not safety.generation_allowed:
        denies.append(ShadowDraftReasonCode.GENERATION_NOT_ALLOWED)

    settings = context.settings
    if (
        settings is None
        or not _readiness_usable(settings.settings_readiness)
        or not _readiness_usable(context.provenance.settings_readiness)
    ):
        denies.append(ShadowDraftReasonCode.SETTINGS_NOT_USABLE)

    knowledge = context.knowledge
    if (
        knowledge is None
        or not _readiness_usable(knowledge.knowledge_readiness)
        or not _readiness_usable(context.provenance.knowledge_readiness)
    ):
        denies.append(ShadowDraftReasonCode.KNOWLEDGE_NOT_USABLE)

    if context.live_facts is None or context.provenance.live_facts_generated_at is None:
        denies.append(ShadowDraftReasonCode.LIVE_FACTS_NOT_USABLE)

    # Deduplicate while preserving order.
    seen: set[ShadowDraftReasonCode] = set()
    ordered: list[ShadowDraftReasonCode] = []
    for code in denies:
        if code not in seen:
            seen.add(code)
            ordered.append(code)

    if ordered:
        return ShadowDraftGateDecision(
            allowed=False,
            reason_code=ordered[0],
            deny_reasons=tuple(ordered),
        )
    return ShadowDraftGateDecision(
        allowed=True,
        reason_code=ShadowDraftReasonCode.OK,
        deny_reasons=(),
    )


def evaluate_shadow_draft_gate_from_build(
    build: RuntimeContextBuildResult,
    *,
    provider_configured: bool,
    shadow_feature_enabled: bool,
) -> ShadowDraftGateDecision:
    return evaluate_shadow_draft_gate(
        context=build.context,
        generation_allowed=build.generation_allowed,
        provider_configured=provider_configured,
        shadow_feature_enabled=shadow_feature_enabled,
        readiness=build.readiness,
    )
