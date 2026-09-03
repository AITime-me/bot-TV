"""Shadow draft generation gate (AI-DIALOGUE-02).

Fail-closed: any missing/stale/invalid required source denies generation and
prevents YandexGPT calls. Independent of BOT_MODE=AUTO and client delivery.

Optional explicit override ``allow_under_emergency_lock`` may ignore
EMERGENCY_LOCK as the *sole* shadow blocker. It never authorizes outbound,
CRM, booking, or client delivery, and never mutates RuntimeSafetyLayer /
RuntimeContextBuilder readiness semantics.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.control_plane_types import ControlPlaneKindReadiness
from app.core.runtime_context_types import (
    RuntimeContextBuildResult,
    RuntimeContextReadiness,
    RuntimeContextReason,
    TeyaRuntimeContext,
)
from app.core.shadow_draft_types import ShadowDraftReasonCode

# Builder reasons that are consequences of EMERGENCY_LOCK alone.
_SOLE_LOCK_IGNORABLE_REASONS: frozenset[RuntimeContextReason] = frozenset(
    {
        RuntimeContextReason.EMERGENCY_LOCK_ACTIVE,
        RuntimeContextReason.GENERATION_DISABLED_STAGE,
    }
)

# Any of these still deny shadow even when allow-under-lock is on.
_DATA_OR_DIALOG_BLOCKING_REASONS: frozenset[RuntimeContextReason] = frozenset(
    {
        RuntimeContextReason.SETTINGS_NOT_READY,
        RuntimeContextReason.KNOWLEDGE_NOT_READY,
        RuntimeContextReason.LIVE_FACTS_UNAVAILABLE,
        RuntimeContextReason.LIVE_FACTS_INVALID,
        RuntimeContextReason.LIVE_FACTS_AUTH_ERROR,
        RuntimeContextReason.LIVE_FACTS_CONTRACT_ERROR,
        RuntimeContextReason.HISTORY_UNAVAILABLE,
        RuntimeContextReason.SAFETY_UNREADABLE,
        RuntimeContextReason.CONVERSATION_UNAVAILABLE,
        RuntimeContextReason.HANDOFF_ACTIVE,
    }
)


@dataclass(frozen=True, slots=True)
class ShadowDraftGateDecision:
    allowed: bool
    reason_code: ShadowDraftReasonCode
    deny_reasons: tuple[ShadowDraftReasonCode, ...]


def _readiness_usable(value: ControlPlaneKindReadiness | None) -> bool:
    """Shadow generation requires fresh LKG only.

    Control-plane READY_STALE remains usable for other consumers, but must not
    authorize YandexGPT shadow drafts.
    """

    return value is ControlPlaneKindReadiness.READY_FRESH


def _sole_emergency_lock_override_eligible(
    *,
    context: TeyaRuntimeContext,
    allow_under_emergency_lock: bool,
    build_reasons: tuple[RuntimeContextReason, ...] | None,
) -> bool:
    """True only when EMERGENCY_LOCK is the sole shadow safety/readiness blocker."""

    if not allow_under_emergency_lock:
        return False
    safety = context.safety
    if not safety.emergency_lock:
        return False
    if safety.handoff_active or safety.manager_takeover_active:
        return False
    if context.conversation is None:
        return False
    if build_reasons is not None:
        other = set(build_reasons) - _SOLE_LOCK_IGNORABLE_REASONS
        if other & _DATA_OR_DIALOG_BLOCKING_REASONS:
            return False
    return True


def evaluate_shadow_draft_gate(
    *,
    context: TeyaRuntimeContext | None,
    generation_allowed: bool,
    provider_configured: bool,
    shadow_feature_enabled: bool,
    readiness: RuntimeContextReadiness | None = None,
    allow_under_emergency_lock: bool = False,
    build_reasons: tuple[RuntimeContextReason, ...] | None = None,
) -> ShadowDraftGateDecision:
    """Return whether YandexGPT may be called for an internal shadow draft."""

    denies: list[ShadowDraftReasonCode] = []

    if not shadow_feature_enabled:
        denies.append(ShadowDraftReasonCode.SHADOW_FEATURE_DISABLED)
    if not provider_configured:
        denies.append(ShadowDraftReasonCode.PROVIDER_NOT_CONFIGURED)

    if context is None:
        if not generation_allowed:
            denies.append(ShadowDraftReasonCode.GENERATION_NOT_ALLOWED)
        denies.append(ShadowDraftReasonCode.CONTEXT_NOT_READY)
        return ShadowDraftGateDecision(
            allowed=False,
            reason_code=denies[0],
            deny_reasons=tuple(denies),
        )

    sole_lock_override = _sole_emergency_lock_override_eligible(
        context=context,
        allow_under_emergency_lock=allow_under_emergency_lock,
        build_reasons=build_reasons,
    )

    if not generation_allowed and not sole_lock_override:
        denies.append(ShadowDraftReasonCode.GENERATION_NOT_ALLOWED)

    if readiness is RuntimeContextReadiness.NOT_READY and not sole_lock_override:
        denies.append(ShadowDraftReasonCode.CONTEXT_NOT_READY)

    safety = context.safety
    if safety.emergency_lock and not sole_lock_override:
        denies.append(ShadowDraftReasonCode.EMERGENCY_LOCK)
    if safety.manager_takeover_active:
        denies.append(ShadowDraftReasonCode.MANAGER_TAKEOVER)
    if safety.handoff_active:
        denies.append(ShadowDraftReasonCode.HANDOFF_ACTIVE)
    if not safety.generation_allowed and not sole_lock_override:
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
    allow_under_emergency_lock: bool = False,
) -> ShadowDraftGateDecision:
    return evaluate_shadow_draft_gate(
        context=build.context,
        generation_allowed=build.generation_allowed,
        provider_configured=provider_configured,
        shadow_feature_enabled=shadow_feature_enabled,
        readiness=build.readiness,
        allow_under_emergency_lock=allow_under_emergency_lock,
        build_reasons=build.reasons,
    )
