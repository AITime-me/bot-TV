"""AI-DIALOGUE-02 context budget — relevance selection + bounded compile regressions."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
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
    ServiceResolutionStatus,
    build_knowledge_selection_hint,
    resolve_live_fact_services,
)
from app.core.shadow_draft_eval_scenarios import LIVE_EVAL_SCENARIOS
from app.core.shadow_draft_prompt import (
    compile_shadow_draft_messages,
    measure_shadow_draft_prompt,
)
from app.core.shadow_draft_types import (
    ShadowDraftDisposition,
    ShadowDraftReasonCode,
)
from app.core.text_generation_port import TextGenerationMessage, TextGenerationResult
from app.core.yandex_gpt_http import YandexGptHttpError
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

_REPO = Path(__file__).resolve().parents[1]
_CHECKSUM = "c" * 64
_PUB_ID = "33333333-3333-4333-8333-333333333333"
_SERVICE_A = "11111111-1111-4111-8111-111111111111"
_SERVICE_B = "22222222-2222-4222-8222-222222222222"
_TARGET = "Чистка лица"


def _settings_envelope() -> dict[str, Any]:
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
                "mainInstruction": "Помощник менеджера студии «Твоё время»",
                "knowledgeBaseNote": None,
                "handoffRules": "При нехватке данных — менеджеру",
                "taggingRules": None,
                "safetyRules": "Без медицинской диагностики",
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


def _knowledge_entries() -> list[dict[str, Any]]:
    return [
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
        {
            "key": "safety.general",
            "category": "SAFETY_INFORMATION",
            "title": "Безопасность",
            "content": "Не ставим диагноз.",
            "tags": ["safety"],
            "serviceId": None,
        },
    ]


def _knowledge_envelope() -> dict[str, Any]:
    return {
        "ok": True,
        "schemaVersion": 1,
        "knowledgePublicationId": _PUB_ID,
        "version": 1,
        "checksum": _CHECKSUM,
        "publishedAt": "2026-08-01T12:00:00.000Z",
        "entries": _knowledge_entries(),
    }


def _service_dict(
    *,
    service_id: str,
    name: str,
    price: str = "9999.00",
    active: bool = True,
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
        "isActive": active,
        "isOnlineBookingEnabled": False,
    }


def _live_facts_many(*, count: int = 110, target_name: str = _TARGET) -> dict[str, Any]:
    payload = copy.deepcopy(ONLINE_ZAPIS_LIVE_FACTS_V1_REPRESENTATIVE)
    services = [
        _service_dict(service_id=_SERVICE_A, name=target_name, price="2000"),
    ]
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
        },
        {
            "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "name": "Мастер Другой",
            "isActive": True,
            "isOnlineBookingEnabled": True,
            "serviceIds": [services[1]["id"]],
        },
    ]
    return payload


def _context(
    *,
    client_text: str,
    live_facts: dict[str, Any] | None = None,
    knowledge_hint=None,
) -> Any:
    settings = parse_settings_publication_v1(_settings_envelope())
    knowledge = parse_knowledge_publication_v1(_knowledge_envelope())
    lf = parse_live_facts_response_v1(
        live_facts if live_facts is not None else _live_facts_many()
    )
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
    hint = knowledge_hint
    if hint is None:
        hint = build_knowledge_selection_hint(
            conversation_text=client_text,
            live_facts=lf,
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
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[TextGenerationMessage, ...]] = []

    def generate(self, messages) -> TextGenerationResult:
        self.calls.append(tuple(messages))
        if self.error is not None:
            raise self.error
        return TextGenerationResult(text="Черновик по релевантным фактам.")


def test_110_services_relevant_only_and_under_budget() -> None:
    ctx = _context(client_text=f"Сколько стоит {_TARGET}?")
    metrics = measure_shadow_draft_prompt(ctx)
    messages = compile_shadow_draft_messages(ctx)
    system = messages[0].text

    assert metrics.live_services_included == 1
    assert metrics.within_budget is True
    assert metrics.total_chars <= SHADOW_DRAFT_COMPILED_CHAR_BUDGET
    assert metrics.total_chars <= YANDEX_PROVIDER_MESSAGE_CHAR_CEILING
    assert metrics.service_resolution == ServiceResolutionStatus.UNIQUE.value
    assert "price_from=2000" in system
    assert "price_from=1001.00" not in system
    assert "service_catalog_names_only" not in system


def test_unrelated_service_prices_absent() -> None:
    ctx = _context(client_text=f"Сколько стоит {_TARGET}?")
    system = compile_shadow_draft_messages(ctx)[0].text
    for i in range(2, 20):
        assert f"price_from={1000 + i}.00" not in system


def test_assigned_masters_only_for_target_service() -> None:
    ctx = _context(client_text=f"Кто выполняет {_TARGET}?")
    system = compile_shadow_draft_messages(ctx)[0].text
    assert "Мастер Чистка" in system
    assert "Мастер Другой" not in system


def test_ambiguous_service_no_arbitrary_first_pick() -> None:
    payload = _live_facts_many(count=5)
    payload["services"] = [
        _service_dict(
            service_id="00000001-1111-4111-8111-111111111111",
            name="Чистка лица классическая",
            price="1000",
        ),
        _service_dict(
            service_id="00000002-1111-4111-8111-111111111111",
            name="Чистка лица глубокая",
            price="2000",
        ),
    ]
    lf = parse_live_facts_response_v1(payload)
    client_text = (
        "Сколько стоит чистка лица классическая и чистка лица глубокая?"
    )
    resolution = resolve_live_fact_services(client_text, lf.services)
    assert resolution.status is ServiceResolutionStatus.AMBIGUOUS
    assert len(resolution.service_ids) == 2

    ctx = _context(
        client_text=client_text,
        live_facts=payload,
    )
    system = compile_shadow_draft_messages(ctx)[0].text
    assert "service_catalog_names_only" in system
    assert "price_from=1000" not in system
    assert "price_from=2000" not in system


def test_unknown_service_no_detailed_110_dump() -> None:
    ctx = _context(client_text="Сколько стоит услуга НесуществующаяXYZ?")
    metrics = measure_shadow_draft_prompt(ctx)
    system = compile_shadow_draft_messages(ctx)[0].text
    assert metrics.service_resolution == ServiceResolutionStatus.NONE.value
    assert "service_catalog_names_only" in system
    assert "price_from=9999.00" not in system
    assert metrics.total_chars <= SHADOW_DRAFT_COMPILED_CHAR_BUDGET


def test_kb_relevance_excludes_unrelated_entries() -> None:
    ctx = _context(client_text=f"Как подготовиться к {_TARGET}?")
    system = compile_shadow_draft_messages(ctx)[0].text
    assert "prep.cleaning" in system or "Подготовка к чистке" in system
    assert "aftercare.beta" not in system
    assert "Уход после Бета" not in system


def test_config_invalid_before_transport_not_provider_called() -> None:
    class _ConfigInvalidPort:
        def generate(self, messages):
            raise YandexGptHttpError("CONFIG_INVALID")

    service = ShadowDraftGenerationService(
        port=_ConfigInvalidPort(), shadow_feature_enabled=True
    )
    ctx = _context(client_text=f"Сколько стоит {_TARGET}?")
    reply = service.generate_from_context(ctx, generation_allowed=True)
    assert reply.disposition is ShadowDraftDisposition.PROVIDER_ERROR
    assert reply.reason_code is ShadowDraftReasonCode.PROVIDER_CONFIG_INVALID
    assert reply.generation_metadata["provider_transport_called"] is False


def test_success_sets_provider_transport_called() -> None:
    port = _FakePort()
    service = ShadowDraftGenerationService(port=port, shadow_feature_enabled=True)
    ctx = _context(client_text=f"Сколько стоит {_TARGET}?")
    reply = service.generate_from_context(ctx, generation_allowed=True)
    assert reply.disposition is ShadowDraftDisposition.REPLY
    assert reply.generation_metadata["provider_transport_called"] is True
    assert len(port.calls) == 1


def test_all_17_eval_scenarios_compile_within_budget() -> None:
    sources = ShadowDraftEvalPublishedSources(
        settings=parse_settings_publication_v1(_settings_envelope()),
        knowledge=parse_knowledge_publication_v1(_knowledge_envelope()),
        live_facts=parse_live_facts_response_v1(_live_facts_many()),
        settings_readiness=ControlPlaneKindReadiness.READY_FRESH,
        knowledge_readiness=ControlPlaneKindReadiness.READY_FRESH,
    )
    inventory: list[dict[str, object]] = []
    for scenario in LIVE_EVAL_SCENARIOS:
        ctx = build_synthetic_eval_context(sources=sources, scenario=scenario)
        metrics = measure_shadow_draft_prompt(ctx)
        inventory.append({"scenarioId": scenario.id, **metrics.as_dict()})
        assert metrics.within_budget is True, scenario.id
        assert metrics.total_chars <= YANDEX_PROVIDER_MESSAGE_CHAR_CEILING, scenario.id

    assert len(inventory) == 17
    blob = json.dumps(inventory, ensure_ascii=False)
    assert "UNTRUSTED_CLIENT" not in blob


def test_eval_run_no_provider_config_invalid_from_prompt_size() -> None:
    port = _FakePort()
    service = build_eval_generation_service(
        port=port,
        environ={"YANDEX_SHADOW_DRAFT_ENABLED": "true"},
    )
    sources = ShadowDraftEvalPublishedSources(
        settings=parse_settings_publication_v1(_settings_envelope()),
        knowledge=parse_knowledge_publication_v1(_knowledge_envelope()),
        live_facts=parse_live_facts_response_v1(_live_facts_many()),
    )
    report = run_shadow_draft_eval(
        sources=sources,
        service=service,
        allow_live_yandex=True,
        environ={"YANDEX_SHADOW_DRAFT_ENABLED": "true"},
    )
    assert report.aggregate.total == 17
    for item in report.scenarios:
        assert item.reason_code != "PROVIDER_CONFIG_INVALID", item.scenario_id
        assert item.provider_called is True, item.scenario_id
    assert len(port.calls) == 17


def test_docker_allowlist_includes_context_selection() -> None:
    from tests.docker_runtime_allowlist import (
        AI_DIALOGUE_02_DOCKER_RUNTIME_PATHS,
        dockerignore_lines,
        is_included_in_docker_build_context,
    )

    lines = dockerignore_lines(_REPO)
    rel = "app/core/shadow_draft_context_selection.py"
    assert rel in AI_DIALOGUE_02_DOCKER_RUNTIME_PATHS
    assert is_included_in_docker_build_context(rel, lines) is True
    assert (_REPO / rel).is_file()
