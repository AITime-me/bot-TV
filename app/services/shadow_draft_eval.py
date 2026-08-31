"""Operator shadow-draft evaluation harness (AI-EVAL-01).

Fetches published Settings + ACTIVE Knowledge + current Live Facts via existing
S2S clients, builds SYNTHETIC RuntimeContext (isolated eval safety), calls
ShadowDraftGenerationService / TextGenerationPort, scores deterministically.

No DB conversation reads. No outbound. No durable persistence / migration.
Production EMERGENCY_LOCK / BOT_MODE are never mutated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence
from uuid import UUID, uuid5

from app.config import BotMode
from app.core.booking_eligibility_http import BookingEligibilityHttpConfig
from app.core.control_plane_http import (
    ControlPlaneFetchCode,
    ControlPlaneHttpClient,
)
from app.core.control_plane_types import (
    ControlPlaneKindReadiness,
    KnowledgePublicationV1,
    SettingsPublicationV1,
)
from app.core.live_facts_http import LiveFactsFetchCode, LiveFactsHttpClient
from app.core.live_facts_types import LiveFactsPayloadV1
from app.core.runtime_context_assemble import (
    assemble_runtime_context,
    build_conversation_layer_from_turns,
    map_history_author,
)
from app.core.shadow_draft_context_selection import (
    build_knowledge_selection_hint,
    client_turn_texts_newest_first,
)
from app.core.runtime_context_types import (
    RuntimeContextBuildResult,
    RuntimeContextReadiness,
    TeyaRuntimeContext,
)
from app.core.s2s_http_transport import S2sHttpTransport
from app.core.shadow_draft_eval_scenarios import LIVE_EVAL_SCENARIOS
from app.core.shadow_draft_eval_scoring import score_shadow_draft_eval
from app.core.shadow_draft_eval_types import (
    ShadowDraftEvalAggregate,
    ShadowDraftEvalReport,
    ShadowDraftEvalScenario,
    ShadowDraftEvalScenarioResult,
    ShadowDraftEvalSourceProof,
    ShadowDraftEvalVerdict,
    assert_synthetic_conversation_id,
    redact_mapping_secrets,
)
from app.core.shadow_draft_types import ShadowDraftReply
from app.core.text_generation_port import TextGenerationPort
from app.services.shadow_draft_generation import (
    ShadowDraftGenerationService,
    build_shadow_draft_generation_service,
    is_yandex_shadow_draft_enabled,
)

# Stable namespace — all synthetic eval conversation ids are uuid5 under this.
SHADOW_DRAFT_EVAL_NAMESPACE = UUID("a1e70101-4e01-4000-8000-0000a1e70101")

_EVAL_SAFETY_NOTE = (
    "Isolated synthetic-eval context: BotMode.OFF, emergency_lock=False local "
    "to this harness only. Does not read or change production EMERGENCY_LOCK / "
    "BOT_MODE / worker. No client delivery. No real conversation ids."
)


def synthetic_conversation_id(scenario_id: str) -> UUID:
    if type(scenario_id) is not str or not scenario_id.strip():
        raise ValueError("SCENARIO_ID_INVALID")
    return uuid5(SHADOW_DRAFT_EVAL_NAMESPACE, scenario_id.strip())


def is_synthetic_eval_conversation_id(conversation_id: UUID) -> bool:
    """True iff id equals uuid5(namespace, scenario.id) for a known scenario."""

    if type(conversation_id) is not UUID:
        return False
    for scenario in LIVE_EVAL_SCENARIOS:
        if conversation_id == synthetic_conversation_id(scenario.id):
            return True
    return False


@dataclass(frozen=True, slots=True)
class ShadowDraftEvalPublishedSources:
    settings: SettingsPublicationV1
    knowledge: KnowledgePublicationV1
    live_facts: LiveFactsPayloadV1
    settings_readiness: ControlPlaneKindReadiness = (
        ControlPlaneKindReadiness.READY_FRESH
    )
    knowledge_readiness: ControlPlaneKindReadiness = (
        ControlPlaneKindReadiness.READY_FRESH
    )

    def source_proof(self) -> ShadowDraftEvalSourceProof:
        return ShadowDraftEvalSourceProof(
            settings_publication_id=self.settings.publication_id,
            settings_version=self.settings.version,
            settings_checksum=self.settings.checksum,
            knowledge_publication_id=self.knowledge.knowledge_publication_id,
            knowledge_version=self.knowledge.version,
            knowledge_checksum=self.knowledge.checksum,
            knowledge_entry_count=len(self.knowledge.entries),
            live_facts_schema_version=self.live_facts.schema_version,
            live_facts_service_count=len(self.live_facts.services),
            live_facts_master_count=len(self.live_facts.masters),
            live_facts_generated_at=self.live_facts.generated_at.isoformat(),
        )


class ShadowDraftEvalError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "EVAL_ERROR"

    def __str__(self) -> str:
        return self.code


def fetch_published_eval_sources(
    *,
    config: BookingEligibilityHttpConfig,
    transport: S2sHttpTransport,
) -> ShadowDraftEvalPublishedSources:
    """Fetch ACTIVE Settings/Knowledge + current Live Facts. No DB. No secrets log."""

    if type(config) is not BookingEligibilityHttpConfig:
        raise ShadowDraftEvalError("S2S_CONFIG_INVALID")
    if transport is None:
        raise ShadowDraftEvalError("S2S_TRANSPORT_INVALID")

    cp = ControlPlaneHttpClient(config, transport)
    lf = LiveFactsHttpClient(config, transport)

    settings_result = cp.fetch_settings()
    if (
        settings_result.code is not ControlPlaneFetchCode.OK
        or settings_result.publication is None
    ):
        raise ShadowDraftEvalError(f"SETTINGS_FETCH_{settings_result.code.value}")

    knowledge_result = cp.fetch_knowledge()
    if (
        knowledge_result.code is not ControlPlaneFetchCode.OK
        or knowledge_result.publication is None
    ):
        raise ShadowDraftEvalError(f"KNOWLEDGE_FETCH_{knowledge_result.code.value}")

    live_result = lf.fetch()
    if live_result.code is not LiveFactsFetchCode.OK or live_result.payload is None:
        raise ShadowDraftEvalError(f"LIVE_FACTS_FETCH_{live_result.code.value}")

    return ShadowDraftEvalPublishedSources(
        settings=settings_result.publication,
        knowledge=knowledge_result.publication,
        live_facts=live_result.payload,
        settings_readiness=ControlPlaneKindReadiness.READY_FRESH,
        knowledge_readiness=ControlPlaneKindReadiness.READY_FRESH,
    )


def build_synthetic_eval_context(
    *,
    sources: ShadowDraftEvalPublishedSources,
    scenario: ShadowDraftEvalScenario,
    conversation_id: UUID | None = None,
    settings_readiness: ControlPlaneKindReadiness | None = None,
    knowledge_readiness: ControlPlaneKindReadiness | None = None,
    include_live_facts: bool = True,
    emergency_lock: bool = False,
) -> TeyaRuntimeContext:
    """Assemble isolated RuntimeContext from published sources + synthetic turns.

    ``emergency_lock`` defaults False for local eval isolation and must never
    be used to rewrite production env. Real client conversation ids are rejected.
    """

    cid = (
        conversation_id
        if conversation_id is not None
        else synthetic_conversation_id(scenario.id)
    )
    allowed = (synthetic_conversation_id(scenario.id),)
    assert_synthetic_conversation_id(cid, allowed=allowed)

    turns = [
        map_history_author(
            author="client",
            conversation_event_seq=1,
            text=scenario.client_text,
            occurred_at=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
        )
    ]
    if scenario.client_followup:
        turns.append(
            map_history_author(
                author="client",
                conversation_event_seq=2,
                text=scenario.client_followup,
                occurred_at=datetime(2026, 8, 30, 12, 1, tzinfo=timezone.utc),
            )
        )
    conversation = build_conversation_layer_from_turns(
        conversation_id=cid,
        event_seq_hwm=len(turns),
        turns=turns,
    )

    client_turns_nf = client_turn_texts_newest_first(conversation.turns)
    conversation_text = client_turns_nf[0] if client_turns_nf else scenario.client_text
    knowledge_hint = build_knowledge_selection_hint(
        conversation_text=conversation_text,
        live_facts=sources.live_facts if include_live_facts else None,
        structured_service_hint=scenario.service_name_contains,
        client_turns_newest_first=client_turns_nf,
    )

    return assemble_runtime_context(
        bot_mode=BotMode.OFF,
        emergency_lock=emergency_lock,
        settings_publication=sources.settings,
        settings_readiness=(
            settings_readiness
            if settings_readiness is not None
            else sources.settings_readiness
        ),
        knowledge_publication=sources.knowledge,
        knowledge_readiness=(
            knowledge_readiness
            if knowledge_readiness is not None
            else sources.knowledge_readiness
        ),
        live_facts=sources.live_facts if include_live_facts else None,
        conversation=conversation,
        handoff_state="BOT_ACTIVE",
        ownership="BOT",
        conversation_status="OPEN",
        manager_takeover_at_present=False,
        knowledge_hint=knowledge_hint,
    )


def _build_result(context: TeyaRuntimeContext) -> RuntimeContextBuildResult:
    """Map assembled context into a build result for the shadow gate.

    Source usability (fresh settings/KB, live facts present) is enforced by
    ``evaluate_shadow_draft_gate``, not by collapsing everything into
    ``generation_allowed=False`` here — so reason codes stay precise for eval.
    Local eval safety remains BotMode.OFF + emergency_lock=False.
    """

    return RuntimeContextBuildResult(
        readiness=RuntimeContextReadiness.READY,
        reasons=(),
        generation_allowed=context.safety.generation_allowed,
        context=context,
    )


def require_live_yandex_eval_allowed(
    *,
    allow_live_yandex: bool,
    environ: Mapping[str, str] | None,
) -> None:
    if not allow_live_yandex:
        raise ShadowDraftEvalError("LIVE_YANDEX_FLAG_REQUIRED")
    if not is_yandex_shadow_draft_enabled(environ):
        raise ShadowDraftEvalError("YANDEX_SHADOW_DRAFT_ENABLED_REQUIRED")


def run_shadow_draft_eval(
    *,
    sources: ShadowDraftEvalPublishedSources,
    service: ShadowDraftGenerationService,
    scenarios: Sequence[ShadowDraftEvalScenario] | None = None,
    allow_live_yandex: bool = False,
    environ: Mapping[str, str] | None = None,
    settings_readiness_override: ControlPlaneKindReadiness | None = None,
    knowledge_readiness_override: ControlPlaneKindReadiness | None = None,
    include_live_facts: bool = True,
) -> ShadowDraftEvalReport:
    """Run synthetic scenarios against ShadowDraftGenerationService.

    When ``service.port`` is a live Yandex client, ``allow_live_yandex`` and
    ``YANDEX_SHADOW_DRAFT_ENABLED=true`` are required. Fake ports in unit tests
    may set ``allow_live_yandex=True`` with a test environ mapping.
    """

    require_live_yandex_eval_allowed(
        allow_live_yandex=allow_live_yandex,
        environ=environ,
    )

    selected = tuple(scenarios) if scenarios is not None else LIVE_EVAL_SCENARIOS
    results: list[ShadowDraftEvalScenarioResult] = []

    for scenario in selected:
        context = build_synthetic_eval_context(
            sources=sources,
            scenario=scenario,
            settings_readiness=settings_readiness_override,
            knowledge_readiness=knowledge_readiness_override,
            include_live_facts=include_live_facts,
            emergency_lock=False,
        )
        build = _build_result(context)
        # Count provider call via disposition/metadata after generate.
        before_configured = service.provider_configured
        reply = service.generate_from_build(build)
        provider_called = _infer_provider_called(reply, before_configured)

        checks, verdict = score_shadow_draft_eval(
            scenario=scenario,
            reply=reply,
            provider_called=provider_called,
            live_facts=sources.live_facts if include_live_facts else None,
        )
        # Gate denials that are intentional fail-closed (stale / missing LF)
        # score as DENIED verdict from disposition; adjust expected provider.
        if reply.disposition.value == "DENIED" and not scenario.expect_provider_called:
            verdict = ShadowDraftEvalVerdict.DENIED

        results.append(
            ShadowDraftEvalScenarioResult(
                scenario_id=scenario.id,
                synthetic_conversation_id=str(
                    synthetic_conversation_id(scenario.id)
                ),
                question=scenario.client_text,
                answer=reply.text,
                disposition=reply.disposition.value,
                handoff_required=reply.handoff_required,
                reason_code=reply.reason_code.value,
                provider_called=provider_called,
                selected_knowledge_keys=tuple(
                    reply.provenance.selected_knowledge_keys
                ),
                checks=checks,
                verdict=verdict,
            )
        )

    aggregate = _aggregate(results)
    report = ShadowDraftEvalReport(
        source_proof=sources.source_proof(),
        scenarios=tuple(results),
        aggregate=aggregate,
        eval_safety_note=_EVAL_SAFETY_NOTE,
        raw_prompt_included=False,
    )
    # Defensive: ensure no secret keys sneak into serialized form.
    redact_mapping_secrets(report.as_dict())
    return report


def _infer_provider_called(
    reply: ShadowDraftReply,
    provider_configured: bool,
) -> bool:
    meta = reply.generation_metadata
    if "provider_transport_called" in meta:
        return meta.get("provider_transport_called") is True
    if not provider_configured:
        return False
    return False


def _aggregate(
    results: Sequence[ShadowDraftEvalScenarioResult],
) -> ShadowDraftEvalAggregate:
    passed = sum(1 for r in results if r.verdict is ShadowDraftEvalVerdict.PASS)
    failed = sum(1 for r in results if r.verdict is ShadowDraftEvalVerdict.FAIL)
    handoff = sum(1 for r in results if r.verdict is ShadowDraftEvalVerdict.HANDOFF)
    denied = sum(1 for r in results if r.verdict is ShadowDraftEvalVerdict.DENIED)
    provider_errors = sum(
        1 for r in results if r.verdict is ShadowDraftEvalVerdict.PROVIDER_ERROR
    )
    return ShadowDraftEvalAggregate(
        total=len(results),
        passed=passed,
        failed=failed,
        handoff=handoff,
        denied=denied,
        provider_errors=provider_errors,
    )


def build_eval_generation_service(
    *,
    port: TextGenerationPort | None,
    environ: Mapping[str, str] | None = None,
) -> ShadowDraftGenerationService:
    return build_shadow_draft_generation_service(port=port, environ=environ)


def format_eval_report_markdown(report: ShadowDraftEvalReport) -> str:
    """Human-readable redacted report (answers allowed; no secrets/raw prompt)."""

    lines: list[str] = [
        "# Shadow draft evaluation report (AI-EVAL-01)",
        "",
        report.eval_safety_note,
        "",
        "## Source proof",
        "",
    ]
    proof = report.source_proof.as_dict()
    for key, value in proof.items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Aggregate", ""])
    for key, value in report.aggregate.as_dict().items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Scenarios", ""])
    for item in report.scenarios:
        lines.append(f"### {item.scenario_id} — {item.verdict.value}")
        lines.append(f"- question: {item.question}")
        lines.append(f"- answer: {item.answer if item.answer is not None else ''}")
        lines.append(f"- disposition: {item.disposition}")
        lines.append(f"- handoff: {item.handoff_required}")
        lines.append(f"- reasonCode: {item.reason_code}")
        lines.append(f"- providerCalled: {item.provider_called}")
        lines.append(
            "- selectedKnowledgeKeys: "
            + ", ".join(item.selected_knowledge_keys)
        )
        lines.append("- checks:")
        for check in item.checks:
            mark = "PASS" if check.passed else "FAIL"
            detail = f" ({check.detail})" if check.detail else ""
            lines.append(f"  - [{mark}] {check.name}{detail}")
        lines.append("")
    lines.append("rawPromptIncluded: false")
    return "\n".join(lines)
