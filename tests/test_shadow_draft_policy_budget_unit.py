"""AI-DIALOGUE-02 policy budget — generation projection + real-sized policy regressions."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import uuid4

from app.config import BotMode
from app.core.control_plane_types import (
    ControlPlaneKindReadiness,
    parse_knowledge_publication_v1,
    parse_settings_publication_v1,
)
from app.core.live_facts_types import parse_live_facts_response_v1
from app.core.runtime_context_assemble import (
    assemble_runtime_context,
    build_conversation_layer_from_turns,
    map_history_author,
)
from app.core.shadow_draft_context_selection import (
    SHADOW_DRAFT_COMPILED_CHAR_BUDGET,
    YANDEX_PROVIDER_MESSAGE_CHAR_CEILING,
    build_knowledge_selection_hint,
)
from app.core.shadow_draft_eval_scenarios import LIVE_EVAL_SCENARIOS
from app.core.shadow_draft_prompt import (
    administrative_content_policy_chars,
    compile_shadow_draft_messages,
    measure_generation_policy,
    measure_shadow_draft_prompt,
    project_generation_policy,
)
from app.core.shadow_draft_types import (
    ShadowDraftDisposition,
    ShadowDraftReasonCode,
)
from app.core.text_generation_port import TextGenerationMessage, TextGenerationResult
from app.services.shadow_draft_eval import (
    ShadowDraftEvalPublishedSources,
    build_eval_generation_service,
    build_synthetic_eval_context,
    run_shadow_draft_eval,
)
from app.services.shadow_draft_generation import ShadowDraftGenerationService
from tests.fixtures.online_zapis_live_facts_v1 import (
    ONLINE_ZAPIS_LIVE_FACTS_V1_REPRESENTATIVE,
)

_CHECKSUM = "d" * 64
_PUB_ID = "44444444-4444-4444-8444-444444444444"
_SERVICE_A = "11111111-1111-4111-8111-111111111111"
_SERVICE_B = "22222222-2222-4222-8222-222222222222"
_TARGET = "Чистка лица"

# Proven production-size field lengths (AI-DIALOGUE-02-POLICY-BUDGET-02).
_MAIN_LEN = 6548
_KB_NOTE_LEN = 1501
_HANDOFF_LEN = 930
_TAGGING_LEN = 616
_SAFETY_LEN = 1060


def _pad(prefix: str, length: int) -> str:
    body = f"{prefix}|"
    if length <= len(body):
        return body[:length]
    return body + ("x" * (length - len(body)))


def _settings_envelope(
    *,
    main_len: int = _MAIN_LEN,
    kb_note_len: int = _KB_NOTE_LEN,
    handoff_len: int = _HANDOFF_LEN,
    tagging_len: int = _TAGGING_LEN,
    safety_len: int = _SAFETY_LEN,
) -> dict[str, Any]:
    return {
        "ok": True,
        "schemaVersion": 1,
        "publicationId": _PUB_ID,
        "version": 1,
        "checksum": _CHECKSUM,
        "publishedAt": "2026-08-01T12:00:00.000Z",
        "sourceUpdatedAt": "2026-08-01T11:00:00.000Z",
        "settings": {
            "schemaVersion": 1,
            "desiredAdminState": {
                "isEnabled": False,
                "mode": "OFF",
                "responseMode": "DRAFT",
            },
            "provider": "YANDEX",
            "channels": {
                "siteWidget": False,
                "vk": False,
                "max": False,
                "telegram": False,
                "whatsapp": False,
            },
            "contentPolicy": {
                "mainInstruction": _pad("MAIN", main_len),
                "knowledgeBaseNote": _pad("KBNOTE", kb_note_len),
                "handoffRules": _pad("HANDOFF", handoff_len),
                "taggingRules": _pad("TAGGING", tagging_len),
                "safetyRules": _pad("SAFETY", safety_len),
            },
            "limits": {
                "maxMessagesPerClient": 20,
                "maxDailyMessages": 200,
                "logRetentionDays": 30,
                "errorLogRetentionDays": 90,
                "maxStoredBotEvents": 5000,
            },
            "operationalSafety": {
                "emergencyLockOwnedByBotCoreEnv": True,
                "effectiveRuntimeModeOwnedByBotCoreEnv": True,
            },
        },
    }


def _knowledge_envelope() -> dict[str, Any]:
    return {
        "ok": True,
        "schemaVersion": 1,
        "knowledgePublicationId": _PUB_ID,
        "version": 1,
        "checksum": _CHECKSUM,
        "publishedAt": "2026-08-01T12:00:00.000Z",
        "entries": [
            {
                "key": "procedure.cleaning",
                "category": "PROCEDURE_EXPLANATION",
                "title": "Чистка лица",
                "content": "Объяснение чистки лица без индивидуальной схемы.",
                "tags": ["cleaning"],
                "serviceId": _SERVICE_A,
            },
            {
                "key": "prep.cleaning",
                "category": "PREPARATION",
                "title": "Подготовка к чистке",
                "content": "Перед процедурой не наносить кремы.",
                "tags": ["prep"],
                "serviceId": _SERVICE_A,
            },
            {
                "key": "aftercare.beta",
                "category": "AFTERCARE",
                "title": "Уход после Бета",
                "content": "После процедуры Бета — только SPF.",
                "tags": ["beta"],
                "serviceId": _SERVICE_B,
            },
            {
                "key": "faq.relatox_units",
                "category": "FAQ",
                "title": "Relatox",
                "content": "Ориентир единиц только справочно.",
                "tags": ["relatox"],
                "serviceId": None,
            },
        ],
    }


def _service_dict(
    *,
    service_id: str,
    name: str,
    price: str = "9999.00",
) -> dict[str, Any]:
    return {
        "id": service_id,
        "name": name,
        "category": "cat",
        "priceFrom": price,
        "priceTo": price,
        "currency": "RUB",
        "durationMinutes": 60,
        "bookingMode": "MANAGER_ONLY",
        "isActive": True,
        "isOnlineBookingEnabled": False,
    }


def _live_facts_many(*, count: int = 110) -> dict[str, Any]:
    payload = copy.deepcopy(ONLINE_ZAPIS_LIVE_FACTS_V1_REPRESENTATIVE)
    services = [_service_dict(service_id=_SERVICE_A, name=_TARGET, price="2000")]
    for i in range(1, count):
        services.append(
            _service_dict(
                service_id=f"{i + 10:08d}-1111-4111-8111-111111111111",
                name=f"Услуга {i}",
                price=f"{1000 + i}.00",
            )
        )
    payload["services"] = services
    payload["masters"] = [
        {
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "name": "Мастер Чистка",
            "isActive": True,
            "isOnlineBookingEnabled": True,
            "serviceIds": [_SERVICE_A],
        }
    ]
    return payload


def _sources() -> ShadowDraftEvalPublishedSources:
    return ShadowDraftEvalPublishedSources(
        settings=parse_settings_publication_v1(_settings_envelope()),
        knowledge=parse_knowledge_publication_v1(_knowledge_envelope()),
        live_facts=parse_live_facts_response_v1(_live_facts_many()),
        settings_readiness=ControlPlaneKindReadiness.READY_FRESH,
        knowledge_readiness=ControlPlaneKindReadiness.READY_FRESH,
    )


def _context(*, client_text: str) -> Any:
    settings = parse_settings_publication_v1(_settings_envelope())
    knowledge = parse_knowledge_publication_v1(_knowledge_envelope())
    lf = parse_live_facts_response_v1(_live_facts_many())
    turns = (
        map_history_author(
            author="client",
            conversation_event_seq=1,
            text=client_text,
            occurred_at=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
        ),
    )
    conversation = build_conversation_layer_from_turns(
        conversation_id=uuid4(),
        event_seq_hwm=1,
        turns=turns,
    )
    hint = build_knowledge_selection_hint(
        conversation_text=client_text,
        live_facts=lf,
        client_turns_newest_first=(client_text,),
    )
    return assemble_runtime_context(
        bot_mode=BotMode.OFF,
        emergency_lock=False,
        settings_publication=settings,
        settings_readiness=ControlPlaneKindReadiness.READY_FRESH,
        knowledge_publication=knowledge,
        knowledge_readiness=ControlPlaneKindReadiness.READY_FRESH,
        live_facts=lf,
        conversation=conversation,
        handoff_state="BOT_ACTIVE",
        ownership="BOT",
        conversation_status="OPEN",
        manager_takeover_at_present=False,
        knowledge_hint=hint,
    )


class _FakePort:
    def __init__(self) -> None:
        self.calls: list[tuple[TextGenerationMessage, ...]] = []

    def generate(self, messages: Sequence[TextGenerationMessage]) -> TextGenerationResult:
        self.calls.append(tuple(messages))
        return TextGenerationResult(text="Черновик по релевантным фактам.")


def test_administrative_policy_exceeds_budget_generation_projection_fits() -> None:
    settings = parse_settings_publication_v1(_settings_envelope())
    policy = settings.settings.content_policy
    admin_chars = administrative_content_policy_chars(policy)
    assert admin_chars == (
        _MAIN_LEN + _KB_NOTE_LEN + _HANDOFF_LEN + _TAGGING_LEN + _SAFETY_LEN
    )
    assert admin_chars > SHADOW_DRAFT_COMPILED_CHAR_BUDGET

    metrics = measure_generation_policy(
        policy=policy,
        provider=settings.settings.provider,
        response_mode=settings.settings.desired_admin_state.response_mode,
    )
    assert metrics.main_instruction_chars == _MAIN_LEN
    assert metrics.handoff_rules_chars == _HANDOFF_LEN
    assert metrics.safety_rules_chars == _SAFETY_LEN
    assert metrics.excluded_knowledge_base_note_chars == _KB_NOTE_LEN
    assert metrics.excluded_tagging_rules_chars == _TAGGING_LEN
    assert metrics.administrative_content_policy_chars == admin_chars
    projected_fields = (
        metrics.main_instruction_chars
        + metrics.handoff_rules_chars
        + metrics.safety_rules_chars
    )
    assert projected_fields == _MAIN_LEN + _HANDOFF_LEN + _SAFETY_LEN
    assert projected_fields < SHADOW_DRAFT_COMPILED_CHAR_BUDGET
    assert metrics.generation_policy_total_chars < SHADOW_DRAFT_COMPILED_CHAR_BUDGET


def test_generation_projection_excludes_tagging_and_kb_note() -> None:
    settings = parse_settings_publication_v1(_settings_envelope())
    projection = project_generation_policy(
        policy=settings.settings.content_policy,
        provider="YANDEX",
        response_mode="DRAFT",
    )
    ctx = _context(client_text=f"Сколько стоит {_TARGET}?")
    system = compile_shadow_draft_messages(ctx)[0].text
    assert "knowledge_base_note=" not in system
    assert "tagging_rules=" not in system
    assert "KBNOTE|" not in system
    assert "TAGGING|" not in system
    assert "main_instruction=" in system
    assert "safety_rules=" in system
    assert "handoff_rules=" in system
    assert "MAIN|" in system
    assert "SAFETY|" in system
    assert "HANDOFF|" in system
    assert projection.main_instruction is not None
    assert settings.settings.content_policy.knowledge_base_note is not None
    assert settings.settings.content_policy.tagging_rules is not None


def test_no_duplicated_large_hardcoded_behavior_block() -> None:
    ctx = _context(client_text=f"Сколько стоит {_TARGET}?")
    system = compile_shadow_draft_messages(ctx)[0].text
    assert "Поведенческие правила Теи" not in system
    assert "IMMUTABLE TRUST GUARD" in system
    assert "LIVE FACTS >" in system


def test_real_policy_relevant_service_compiles_under_budget() -> None:
    ctx = _context(client_text=f"Сколько стоит {_TARGET}?")
    metrics = measure_shadow_draft_prompt(ctx)
    messages = compile_shadow_draft_messages(ctx)
    system = messages[0].text

    assert metrics.within_budget is True
    assert metrics.total_chars <= SHADOW_DRAFT_COMPILED_CHAR_BUDGET
    assert metrics.total_chars <= YANDEX_PROVIDER_MESSAGE_CHAR_CEILING
    assert metrics.service_resolution == "UNIQUE"
    assert metrics.live_services_included == 1
    assert "price_from=2000" in system
    assert "price_from=1001.00" not in system
    assert "knowledge_base_note=" not in system
    assert "tagging_rules=" not in system

    port = _FakePort()
    service = ShadowDraftGenerationService(port=port, shadow_feature_enabled=True)
    reply = service.generate_from_context(ctx, generation_allowed=True)
    assert reply.disposition is ShadowDraftDisposition.REPLY
    assert reply.reason_code is ShadowDraftReasonCode.OK
    assert reply.generation_metadata.get("provider_transport_called") is True
    assert len(port.calls) == 1


def test_all_17_scenarios_with_real_policy_compile_no_budget_deny() -> None:
    sources = _sources()
    port = _FakePort()
    service = build_eval_generation_service(
        port=port,
        environ={"YANDEX_SHADOW_DRAFT_ENABLED": "true"},
    )
    report = run_shadow_draft_eval(
        sources=sources,
        service=service,
        allow_live_yandex=True,
        environ={"YANDEX_SHADOW_DRAFT_ENABLED": "true"},
    )
    inventory: list[dict[str, object]] = []
    for scenario, result in zip(LIVE_EVAL_SCENARIOS, report.scenarios, strict=True):
        ctx = build_synthetic_eval_context(sources=sources, scenario=scenario)
        metrics = measure_shadow_draft_prompt(ctx)
        inventory.append(
            {
                "scenario": scenario.id,
                "policyChars": metrics.policy_chars,
                "liveFactChars": metrics.live_fact_chars,
                "kbChars": metrics.kb_chars,
                "dialogChars": metrics.dialog_chars,
                "totalChars": metrics.total_chars,
                "resolution": metrics.service_resolution,
                "providerCalled": result.provider_called,
                "reasonCode": result.reason_code,
            }
        )
        assert metrics.within_budget is True, scenario.id
        assert metrics.total_chars <= SHADOW_DRAFT_COMPILED_CHAR_BUDGET, scenario.id
        assert result.reason_code != "PROMPT_BUDGET_EXCEEDED", scenario.id
        assert result.reason_code != "PROVIDER_CONFIG_INVALID", scenario.id
        assert result.provider_called is True, scenario.id

    assert len(inventory) == 17
    blob = json.dumps(inventory, ensure_ascii=False)
    assert "MAIN|" not in blob
    assert "UNTRUSTED_CLIENT" not in blob
    assert len(port.calls) == 17
