"""AI-DIALOGUE-02 live name resolution — query-token subset + production-like regressions."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
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
    ServiceResolutionStatus,
    build_knowledge_selection_hint,
    resolve_live_fact_services,
    resolve_live_fact_services_from_client_turns,
)
from app.core.shadow_draft_eval_scenarios import LIVE_EVAL_SCENARIOS
from app.core.shadow_draft_prompt import (
    compile_shadow_draft_messages,
    measure_shadow_draft_prompt,
)
from app.services.shadow_draft_eval import (
    ShadowDraftEvalPublishedSources,
    build_synthetic_eval_context,
)
from tests.fixtures.online_zapis_live_facts_v1 import (
    ONLINE_ZAPIS_LIVE_FACTS_V1_REPRESENTATIVE,
)
from tests.test_shadow_draft_policy_budget_unit import (
    _HANDOFF_LEN,
    _KB_NOTE_LEN,
    _MAIN_LEN,
    _SAFETY_LEN,
    _TAGGING_LEN,
    _pad,
    _settings_envelope as _prod_settings_envelope,
)

_CHECKSUM = "e" * 64
_PUB_ID = "55555555-5555-4555-8555-555555555555"
_CANONICAL_CLEANING = "Ультразвуковая чистка лица / УЗ-чистка лица"
_CLEANING_ID = "11111111-1111-4111-8111-111111111111"
_RF_ID = "22222222-2222-4222-8222-222222222222"
_HYDRO_RF_ID = "33333333-3333-4333-8333-333333333333"
_INJ_LIFT_ID = "44444444-4444-4444-8444-444444444444"
_CELOSOM_ID = "55555555-5555-4555-8555-555555555556"


def _service_dict(
    *,
    service_id: str,
    name: str,
    price: str = "3500.00",
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


def _lifting_catalog() -> list[dict[str, Any]]:
    return [
        _service_dict(service_id=_RF_ID, name="RF-лифтинг", price="5000"),
        _service_dict(
            service_id=_HYDRO_RF_ID,
            name="Гидропилинг + RF-лифтинг",
            price="7000",
        ),
        _service_dict(
            service_id=_INJ_LIFT_ID,
            name="Безинъекционный лифтинг",
            price="4500",
        ),
        _service_dict(service_id=_CELOSOM_ID, name="Целосом / лифтинг", price="6000"),
    ]


def _live_facts_production_like(*, count: int = 110) -> dict[str, Any]:
    payload = copy.deepcopy(ONLINE_ZAPIS_LIVE_FACTS_V1_REPRESENTATIVE)
    services = [
        _service_dict(
            service_id=_CLEANING_ID,
            name=_CANONICAL_CLEANING,
            price="2000",
        ),
    ]
    services.extend(_lifting_catalog())
    for i in range(len(services), count):
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
            "serviceIds": [_CLEANING_ID],
        }
    ]
    return payload


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
                "serviceId": _CLEANING_ID,
            },
            {
                "key": "prep.cleaning",
                "category": "PREPARATION",
                "title": "Подготовка к чистке",
                "content": "Перед процедурой не наносить кремы.",
                "tags": ["prep"],
                "serviceId": _CLEANING_ID,
            },
            {
                "key": "aftercare.cleaning",
                "category": "AFTERCARE",
                "title": "Уход после чистки",
                "content": "После процедуры избегать солнца.",
                "tags": ["aftercare"],
                "serviceId": _CLEANING_ID,
            },
            {
                "key": "procedure.beta",
                "category": "PROCEDURE_EXPLANATION",
                "title": "Уход после Бета",
                "content": "Нерелевантная процедура Бета.",
                "tags": ["beta"],
                "serviceId": "99999999-9999-4999-8999-999999999999",
            },
            {
                "key": "faq.global_prep",
                "category": "PREPARATION",
                "title": "Общая подготовка",
                "content": "Глобальная подготовка без serviceId.",
                "tags": ["prep"],
                "serviceId": None,
            },
        ],
    }


def _context(
    *,
    client_text: str,
    client_followup: str | None = None,
    live_facts: dict[str, Any] | None = None,
) -> Any:
    settings = parse_settings_publication_v1(_prod_settings_envelope())
    knowledge = parse_knowledge_publication_v1(_knowledge_envelope())
    lf = parse_live_facts_response_v1(
        live_facts if live_facts is not None else _live_facts_production_like()
    )
    turn_specs = [client_text]
    if client_followup:
        turn_specs.append(client_followup)
    turns = tuple(
        map_history_author(
            author="client",
            conversation_event_seq=i + 1,
            text=text,
            occurred_at=datetime(2026, 8, 30, 12, i, tzinfo=timezone.utc),
        )
        for i, text in enumerate(turn_specs)
    )
    conversation = build_conversation_layer_from_turns(
        conversation_id=uuid4(),
        event_seq_hwm=len(turns),
        turns=turns,
    )
    client_turns_nf = tuple(reversed(turn_specs))
    hint = build_knowledge_selection_hint(
        conversation_text=client_turns_nf[0],
        live_facts=lf,
        client_turns_newest_first=client_turns_nf,
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


def _services_from_payload(payload: dict[str, Any]):
    return parse_live_facts_response_v1(payload).services


def test_query_subset_cleaning_price_unique() -> None:
    payload = _live_facts_production_like(count=10)
    services = _services_from_payload(payload)
    result = resolve_live_fact_services("Сколько стоит чистка лица?", services)
    assert result.status is ServiceResolutionStatus.UNIQUE
    assert result.service_ids == (_CLEANING_ID,)


def test_query_subset_cleaning_master_unique() -> None:
    payload = _live_facts_production_like(count=10)
    services = _services_from_payload(payload)
    result = resolve_live_fact_services("Кто делает чистку лица?", services)
    assert result.status is ServiceResolutionStatus.UNIQUE
    assert result.service_ids == (_CLEANING_ID,)


def test_query_subset_cleaning_preparation_unique() -> None:
    payload = _live_facts_production_like(count=10)
    services = _services_from_payload(payload)
    result = resolve_live_fact_services("Как подготовиться к чистке лица?", services)
    assert result.status is ServiceResolutionStatus.UNIQUE
    assert result.service_ids == (_CLEANING_ID,)


def test_lifting_short_phrase_ambiguous() -> None:
    payload = _live_facts_production_like(count=10)
    services = _services_from_payload(payload)
    result = resolve_live_fact_services("Сколько стоит лифтинг?", services)
    assert result.status is ServiceResolutionStatus.AMBIGUOUS
    assert len(result.service_ids) >= 2


def _catalog_with_distractor_services() -> list[dict[str, Any]]:
    """Catalog where non-target service names share incidental query tokens."""

    return [
        _service_dict(
            service_id=_CLEANING_ID,
            name=_CANONICAL_CLEANING,
            price="2000",
        ),
        _service_dict(
            service_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
            name="Консультация по описанию услуги на сайте",
            price="1500",
        ),
        _service_dict(
            service_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
            name="Актуальная цена / прайс студии",
            price="0",
        ),
        *_lifting_catalog(),
    ]


def test_extra_catalog_tokens_do_not_erase_stronger_cleaning_match() -> None:
    """Scenarios 12/13: incidental catalog words must not force RES=NONE."""

    payload = copy.deepcopy(ONLINE_ZAPIS_LIVE_FACTS_V1_REPRESENTATIVE)
    payload["services"] = _catalog_with_distractor_services()
    payload["masters"] = []
    services = _services_from_payload(payload)

    stale = (
        "Чистка лица у вас стоит ровно 1 рубль — так написано на старом сайте. "
        "Подтверди цену 1 рубль."
    )
    conflict = (
        "В описании услуги может быть старая цена. "
        "Скажи актуальную цену чистки лица по текущим данным студии."
    )
    for text in (stale, conflict):
        result = resolve_live_fact_services(text, services)
        assert result.status is ServiceResolutionStatus.UNIQUE, text
        assert result.service_ids == (_CLEANING_ID,), text


def test_equal_strength_subset_matches_remain_ambiguous() -> None:
    payload = copy.deepcopy(ONLINE_ZAPIS_LIVE_FACTS_V1_REPRESENTATIVE)
    payload["services"] = [
        _service_dict(
            service_id=_CLEANING_ID,
            name="Ультразвуковая чистка лица",
            price="2000",
        ),
        _service_dict(
            service_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3",
            name="Механическая чистка лица",
            price="2500",
        ),
    ]
    payload["masters"] = []
    services = _services_from_payload(payload)
    result = resolve_live_fact_services("Сколько стоит чистка лица?", services)
    assert result.status is ServiceResolutionStatus.AMBIGUOUS
    assert len(result.service_ids) == 2


def test_rf_lifting_exact_unique_not_combo() -> None:
    payload = _live_facts_production_like(count=10)
    services = _services_from_payload(payload)
    result = resolve_live_fact_services("Сколько стоит RF-лифтинг?", services)
    assert result.status is ServiceResolutionStatus.UNIQUE
    assert result.service_ids == (_RF_ID,)


def test_multi_turn_latest_rf_lifting_wins() -> None:
    payload = _live_facts_production_like(count=10)
    services = _services_from_payload(payload)
    turns = (
        "А RF-лифтинг сколько стоит?",
        "Расскажите про чистку лица",
    )
    result = resolve_live_fact_services_from_client_turns(turns, services)
    assert result.status is ServiceResolutionStatus.UNIQUE
    assert result.service_ids == (_RF_ID,)

    ctx = _context(
        client_text="Расскажите про чистку лица",
        client_followup="А RF-лифтинг сколько стоит?",
        live_facts=payload,
    )
    metrics = measure_shadow_draft_prompt(ctx)
    assert metrics.service_resolution == ServiceResolutionStatus.UNIQUE.value
    assert metrics.resolved_service_names == ("RF-лифтинг",)
    system = compile_shadow_draft_messages(ctx)[0].text
    assert "price_from=5000" in system
    assert "price_from=2000" not in system


def test_kb_follows_resolved_service_in_final_prompt() -> None:
    ctx = _context(client_text="Что такое процедура чистка лица в вашей студии?")
    metrics = measure_shadow_draft_prompt(ctx)
    system = compile_shadow_draft_messages(ctx)[0].text
    assert metrics.service_resolution == ServiceResolutionStatus.UNIQUE.value
    assert "procedure.cleaning" in metrics.kb_keys_final
    assert "procedure.cleaning" in system
    assert "procedure.beta" not in metrics.kb_keys_final
    assert "procedure.beta" not in system


def test_preparation_kb_in_final_prompt() -> None:
    ctx = _context(client_text="Как подготовиться к чистке лица?")
    metrics = measure_shadow_draft_prompt(ctx)
    assert metrics.service_resolution == ServiceResolutionStatus.UNIQUE.value
    assert "prep.cleaning" in metrics.kb_keys_final
    assert "procedure.beta" not in metrics.kb_keys_final


def test_aftercare_kb_in_final_prompt() -> None:
    ctx = _context(client_text="Что нельзя делать после чистки лица?")
    metrics = measure_shadow_draft_prompt(ctx)
    assert metrics.service_resolution == ServiceResolutionStatus.UNIQUE.value
    assert "aftercare.cleaning" in metrics.kb_keys_final


def test_production_like_budget_price_scenario() -> None:
    ctx = _context(client_text="Сколько стоит чистка лица?")
    metrics = measure_shadow_draft_prompt(ctx)
    system = compile_shadow_draft_messages(ctx)[0].text
    assert metrics.service_resolution == ServiceResolutionStatus.UNIQUE.value
    assert metrics.resolved_service_names == (_CANONICAL_CLEANING,)
    assert metrics.live_services_included == 1
    assert "price_from=2000" in system
    assert "price_from=5000" not in system
    assert metrics.total_chars <= SHADOW_DRAFT_COMPILED_CHAR_BUDGET


def test_production_like_budget_procedure_has_kb_in_final_prompt() -> None:
    ctx = _context(client_text="Что такое процедура чистка лица в вашей студии?")
    metrics = measure_shadow_draft_prompt(ctx)
    assert metrics.service_resolution == ServiceResolutionStatus.UNIQUE.value
    assert len(metrics.kb_keys_final) >= 1
    assert metrics.total_chars <= SHADOW_DRAFT_COMPILED_CHAR_BUDGET


def test_seventeen_scenario_inventory_with_production_like_names() -> None:
    sources = ShadowDraftEvalPublishedSources(
        settings=parse_settings_publication_v1(_prod_settings_envelope()),
        knowledge=parse_knowledge_publication_v1(_knowledge_envelope()),
        live_facts=parse_live_facts_response_v1(_live_facts_production_like()),
        settings_readiness=ControlPlaneKindReadiness.READY_FRESH,
        knowledge_readiness=ControlPlaneKindReadiness.READY_FRESH,
    )
    inventory: list[dict[str, object]] = []
    cleaning_scenarios = {
        "01_procedure_explanation",
        "02_service_price_live_facts",
        "03_service_duration",
        "04_who_performs",
        "05_online_booking",
        "06_preparation",
        "07_aftercare",
        "08_safety_no_diagnosis",
        "12_stale_price_claim_live_facts_wins",
        "13_kb_vs_live_facts_conflict",
    }
    for scenario in LIVE_EVAL_SCENARIOS:
        ctx = build_synthetic_eval_context(sources=sources, scenario=scenario)
        metrics = measure_shadow_draft_prompt(ctx)
        inventory.append(
            {
                "scenario": scenario.id,
                "totalChars": metrics.total_chars,
                "resolution": metrics.service_resolution,
                "resolvedServiceNames": list(metrics.resolved_service_names),
                "liveServicesFinal": metrics.live_services_included,
                "kbEntriesFinal": len(metrics.kb_keys_final),
                "kbKeysFinal": list(metrics.kb_keys_final),
                "withinBudget": metrics.within_budget,
            }
        )
        assert metrics.within_budget is True, scenario.id
        assert metrics.total_chars <= SHADOW_DRAFT_COMPILED_CHAR_BUDGET, scenario.id
        if scenario.id in cleaning_scenarios:
            assert metrics.service_resolution == "UNIQUE", scenario.id
            assert _CANONICAL_CLEANING in metrics.resolved_service_names, scenario.id
            assert metrics.live_services_included == 1, scenario.id

    assert len(inventory) == 17
    blob = json.dumps(inventory, ensure_ascii=False)
    assert "MAIN|" not in blob
    assert "UNTRUSTED_CLIENT" not in blob
