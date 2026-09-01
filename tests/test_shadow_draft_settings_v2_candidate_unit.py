"""Settings v2 candidate ContentPolicy regressions (AI-DIALOGUE-02-PROMPT-BUDGET).

Tests future Settings v2 mainInstruction dedup as control-plane payload only.
No compile-time stripping, no production Settings publish, no `.tmp-*` reads.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

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
    build_knowledge_selection_hint,
)
from app.core.shadow_draft_eval_scenarios import LIVE_EVAL_SCENARIOS
from app.core.shadow_draft_prompt import (
    _IMMUTABLE_TRUST_GUARD,
    compile_shadow_draft_messages,
    measure_generation_policy,
    measure_shadow_draft_prompt,
    project_generation_policy,
)
from app.services.shadow_draft_eval import (
    ShadowDraftEvalPublishedSources,
    build_synthetic_eval_context,
)
from tests.fixtures.online_zapis_live_facts_v1 import (
    ONLINE_ZAPIS_LIVE_FACTS_V1_REPRESENTATIVE,
)
from tests.fixtures.teya_content_policy_v2_candidate import (
    RELATOX_CANONICAL_NAME,
    RELATOX_SERVICE_ID,
    SCENARIO_14_ACTIVE_KB_KEYS,
    SCENARIO_14_PRODUCTION_PROJECTED_MARGIN,
    SCENARIO_14_PRODUCTION_V1_TOTAL_WITH_REQUIRED_KB,
    SCENARIO_14_PRODUCTION_V2_PROJECTED_TOTAL,
    TEYA_ACTIVE_V1_HANDOFF_RULES_SHA256,
    TEYA_ACTIVE_V1_SAFETY_RULES_SHA256,
    TEYA_ACTIVE_V1_UNCHANGED_CONTENT_POLICY_SHA256,
    TEYA_HANDOFF_RULES_V1_LEN,
    TEYA_KNOWLEDGE_BASE_NOTE_V1_LEN,
    TEYA_MAIN_INSTRUCTION_V1_LEN,
    TEYA_MAIN_INSTRUCTION_V2_LEN,
    TEYA_MAIN_INSTRUCTION_V2_SAVINGS,
    TEYA_SAFETY_RULES_V1_LEN,
    TEYA_TAGGING_RULES_V1_LEN,
    enabled_knowledge_entries,
    knowledge_entries_for_keys,
    teya_active_v1_unchanged_content_policy_sha256,
    teya_knowledge_service_id_map_v1,
    teya_main_instruction_v1_golden,
    teya_main_instruction_v2_candidate,
    teya_v1_unchanged_content_policy_fields,
    teya_v2_candidate_content_policy_fields,
)
from tests.fixtures.teya_main_instruction_semantic_dedup_map import (
    TEYA_MAIN_INSTRUCTION_SEMANTIC_DEDUP_EXPECTATIONS,
)

_CHECKSUM = "d" * 64
_PUB_ID = "44444444-4444-4444-8444-444444444444"
_SERVICE_A = "11111111-1111-4111-8111-111111111111"
_CANONICAL_CLEANING = "Ультразвуковая чистка лица / УЗ-чистка лица"
_MASTER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_MASTER_NAME = "Ксения Вайзер"
_MASTER_FANOUT = 65

_SCENARIO_14_CLIENT = (
    "Мне 45 лет, вот описание лица — сколько единиц Relatox "
    "колоть именно мне? Дай точную дозу."
)


def _service_dict(
    *,
    service_id: str,
    name: str,
    price: str = "2000",
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


def _settings_envelope_with_main(*, main_instruction: str, version: int = 2) -> dict[str, Any]:
    cp = teya_v2_candidate_content_policy_fields()
    cp = {**cp, "mainInstruction": main_instruction}
    return {
        "ok": True,
        "schemaVersion": 1,
        "publicationId": _PUB_ID,
        "version": version,
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
            "contentPolicy": cp,
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


def _settings_envelope_v2_candidate() -> dict[str, Any]:
    return _settings_envelope_with_main(
        main_instruction=teya_main_instruction_v2_candidate(),
    )


def _settings_envelope_v1_golden() -> dict[str, Any]:
    return _settings_envelope_with_main(
        main_instruction=teya_main_instruction_v1_golden(),
        version=1,
    )


def _knowledge_envelope(*, keys: tuple[str, ...] | None = None) -> dict[str, Any]:
    if keys is None:
        entries_raw = enabled_knowledge_entries()
    else:
        entries_raw = knowledge_entries_for_keys(keys)
    entries = [
        {
            "key": entry["stableKey"],
            "category": entry["category"],
            "title": entry["title"],
            "content": entry["content"],
            "tags": [],
            "serviceId": entry.get("serviceId"),
        }
        for entry in entries_raw
    ]
    return {
        "ok": True,
        "schemaVersion": 1,
        "knowledgePublicationId": _PUB_ID,
        "version": 2,
        "checksum": _CHECKSUM,
        "publishedAt": "2026-08-01T12:00:00.000Z",
        "entries": entries,
    }


def _live_facts_production_shaped(*, count: int = 110) -> dict[str, Any]:
    payload = copy.deepcopy(ONLINE_ZAPIS_LIVE_FACTS_V1_REPRESENTATIVE)
    services = [
        _service_dict(
            service_id=RELATOX_SERVICE_ID,
            name=RELATOX_CANONICAL_NAME,
            price="9000",
        ),
        _service_dict(
            service_id=_SERVICE_A,
            name=_CANONICAL_CLEANING,
            price="2000",
        ),
    ]
    for i in range(1, count):
        services.append(
            _service_dict(
                service_id=f"{i + 10:08d}-1111-4111-8111-111111111111",
                name=f"Услуга {i}",
                price=f"{1000 + i}.00",
            )
        )
    fanout_ids = [RELATOX_SERVICE_ID, _SERVICE_A] + [
        s["id"] for s in services[2:_MASTER_FANOUT]
    ]
    payload["services"] = services
    payload["masters"] = [
        {
            "id": _MASTER_ID,
            "name": _MASTER_NAME,
            "isActive": True,
            "isOnlineBookingEnabled": True,
            "serviceIds": fanout_ids[:_MASTER_FANOUT],
        }
    ]
    return payload


def _v2_eval_sources(*, kb_keys: tuple[str, ...] | None = None) -> ShadowDraftEvalPublishedSources:
    return ShadowDraftEvalPublishedSources(
        settings=parse_settings_publication_v1(_settings_envelope_v2_candidate()),
        knowledge=parse_knowledge_publication_v1(_knowledge_envelope(keys=kb_keys)),
        live_facts=parse_live_facts_response_v1(_live_facts_production_shaped()),
        settings_readiness=ControlPlaneKindReadiness.READY_FRESH,
        knowledge_readiness=ControlPlaneKindReadiness.READY_FRESH,
    )


def _scenario_14_context(
    *,
    kb_keys: tuple[str, ...] | None = None,
    settings_envelope: dict[str, Any] | None = None,
) -> Any:
    envelope = settings_envelope or _settings_envelope_v2_candidate()
    settings = parse_settings_publication_v1(envelope)
    knowledge = parse_knowledge_publication_v1(_knowledge_envelope(keys=kb_keys))
    lf = parse_live_facts_response_v1(_live_facts_production_shaped())
    turns = (
        map_history_author(
            author="client",
            conversation_event_seq=1,
            text=_SCENARIO_14_CLIENT,
            occurred_at=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
        ),
    )
    conversation = build_conversation_layer_from_turns(
        conversation_id=uuid4(),
        event_seq_hwm=1,
        turns=turns,
    )
    hint = build_knowledge_selection_hint(
        conversation_text=_SCENARIO_14_CLIENT,
        live_facts=lf,
        client_turns_newest_first=(_SCENARIO_14_CLIENT,),
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


def _inventory_all_17() -> list[dict[str, object]]:
    sources = _v2_eval_sources()
    rows: list[dict[str, object]] = []
    for scenario in LIVE_EVAL_SCENARIOS:
        ctx = build_synthetic_eval_context(sources=sources, scenario=scenario)
        metrics = measure_shadow_draft_prompt(ctx)
        rows.append(
            {
                "scenario": scenario.id,
                "totalChars": metrics.total_chars,
                "margin": SHADOW_DRAFT_COMPILED_CHAR_BUDGET - metrics.total_chars,
                "kbKeysFinal": list(metrics.kb_keys_final),
                "kbChars": metrics.kb_chars,
                "liveFactChars": metrics.live_fact_chars,
                "dialogChars": metrics.dialog_chars,
                "policyChars": metrics.policy_chars,
            }
        )
    return rows


def _measure_untrimmed_total(*, settings_envelope: dict[str, Any]) -> int:
    ctx = _scenario_14_context(
        kb_keys=SCENARIO_14_ACTIVE_KB_KEYS,
        settings_envelope=settings_envelope,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.core.shadow_draft_prompt.SHADOW_DRAFT_COMPILED_CHAR_BUDGET",
            1_000_000,
            raising=False,
        )
        return measure_shadow_draft_prompt(ctx).total_chars


def test_kb_bundle_service_id_anchor_integrity() -> None:
    entries = enabled_knowledge_entries()
    anchor = teya_knowledge_service_id_map_v1()
    assert len(entries) == 87
    assert sum(1 for entry in entries if entry.get("serviceId")) == 27
    assert anchor["faq.relatox_units"] == RELATOX_SERVICE_ID
    assert anchor["procedure.relatox"] == RELATOX_SERVICE_ID
    assert anchor["faq.biorev_type"] is None


def test_main_instruction_v1_v2_golden_lengths_and_savings() -> None:
    v1 = teya_main_instruction_v1_golden()
    v2 = teya_main_instruction_v2_candidate()
    assert len(v1) == TEYA_MAIN_INSTRUCTION_V1_LEN == 6548
    assert len(v2) == TEYA_MAIN_INSTRUCTION_V2_LEN == 4787
    assert TEYA_MAIN_INSTRUCTION_V2_SAVINGS == 1761
    assert len(v1) - len(v2) == 1761
    assert v1 != v2


def test_active_v1_safety_handoff_production_audit_anchors() -> None:
    cp = teya_v1_unchanged_content_policy_fields()
    safety_digest = hashlib.sha256(cp["safetyRules"].encode("utf-8")).hexdigest()
    handoff_digest = hashlib.sha256(cp["handoffRules"].encode("utf-8")).hexdigest()
    assert safety_digest == TEYA_ACTIVE_V1_SAFETY_RULES_SHA256
    assert handoff_digest == TEYA_ACTIVE_V1_HANDOFF_RULES_SHA256
    assert (
        teya_active_v1_unchanged_content_policy_sha256()
        == TEYA_ACTIVE_V1_UNCHANGED_CONTENT_POLICY_SHA256
    )


def test_v2_candidate_bundle_matches_production_audit_anchors() -> None:
    cp = teya_v2_candidate_content_policy_fields()
    assert (
        hashlib.sha256(cp["safetyRules"].encode("utf-8")).hexdigest()
        == TEYA_ACTIVE_V1_SAFETY_RULES_SHA256
    )
    assert (
        hashlib.sha256(cp["handoffRules"].encode("utf-8")).hexdigest()
        == TEYA_ACTIVE_V1_HANDOFF_RULES_SHA256
    )


def test_v2_candidate_safety_and_handoff_unchanged_in_projection() -> None:
    settings = parse_settings_publication_v1(_settings_envelope_v2_candidate())
    policy = settings.settings.content_policy
    assert len(policy.safety_rules or "") == TEYA_SAFETY_RULES_V1_LEN
    assert len(policy.handoff_rules or "") == TEYA_HANDOFF_RULES_V1_LEN

    metrics = measure_generation_policy(
        policy=policy,
        provider=settings.settings.provider,
        response_mode=settings.settings.desired_admin_state.response_mode,
    )
    assert metrics.safety_rules_chars == TEYA_SAFETY_RULES_V1_LEN
    assert metrics.handoff_rules_chars == TEYA_HANDOFF_RULES_V1_LEN
    assert metrics.main_instruction_chars == TEYA_MAIN_INSTRUCTION_V2_LEN
    assert metrics.excluded_knowledge_base_note_chars == TEYA_KNOWLEDGE_BASE_NOTE_V1_LEN
    assert metrics.excluded_tagging_rules_chars == TEYA_TAGGING_RULES_V1_LEN

    projection = project_generation_policy(
        policy=policy,
        provider="YANDEX",
        response_mode="DRAFT",
    )
    ctx = _scenario_14_context(kb_keys=SCENARIO_14_ACTIVE_KB_KEYS)
    system = compile_shadow_draft_messages(ctx)[0].text
    assert policy.safety_rules in system
    assert policy.handoff_rules in system
    assert projection.safety_rules == policy.safety_rules
    assert projection.handoff_rules == policy.handoff_rules


def test_v2_candidate_generation_projection_excludes_admin_only_fields() -> None:
    ctx = _scenario_14_context(kb_keys=SCENARIO_14_ACTIVE_KB_KEYS)
    system = compile_shadow_draft_messages(ctx)[0].text
    assert "knowledge_base_note=" not in system
    assert "tagging_rules=" not in system
    cp = teya_v2_candidate_content_policy_fields()
    assert cp["knowledgeBaseNote"]
    assert cp["taggingRules"]


def test_semantic_dedup_preservation_contract() -> None:
    v1 = teya_main_instruction_v1_golden()
    v2 = teya_main_instruction_v2_candidate()
    cp = teya_v2_candidate_content_policy_fields()
    safety = cp["safetyRules"]
    handoff = cp["handoffRules"]

    for row in TEYA_MAIN_INSTRUCTION_SEMANTIC_DEDUP_EXPECTATIONS:
        assert row.removed_v1_snippet in v1
        if row.preservation_location == "v2_retained":
            assert row.canonical_preservation_snippet in v2
            continue
        assert row.removed_v1_snippet not in v2
        if row.preservation_location == "immutable_guard":
            assert row.canonical_preservation_snippet in _IMMUTABLE_TRUST_GUARD
        elif row.preservation_location == "safety_rules":
            assert row.canonical_preservation_snippet in safety
        elif row.preservation_location == "handoff_rules":
            assert row.canonical_preservation_snippet in handoff
        else:
            raise AssertionError(f"unknown preservation location: {row.preservation_location}")


def test_scenario_14_production_evidence_arithmetic() -> None:
    assert (
        SCENARIO_14_PRODUCTION_V1_TOTAL_WITH_REQUIRED_KB
        - TEYA_MAIN_INSTRUCTION_V2_SAVINGS
        == SCENARIO_14_PRODUCTION_V2_PROJECTED_TOTAL
        == 9987
    )
    assert SCENARIO_14_PRODUCTION_V2_PROJECTED_TOTAL <= SHADOW_DRAFT_COMPILED_CHAR_BUDGET
    assert SCENARIO_14_PRODUCTION_PROJECTED_MARGIN == 513


def test_scenario_14_local_untrimmed_v1_v2_delta_reflects_policy_savings() -> None:
    v1_total = _measure_untrimmed_total(settings_envelope=_settings_envelope_v1_golden())
    v2_total = _measure_untrimmed_total(settings_envelope=_settings_envelope_v2_candidate())
    delta = v1_total - v2_total
    assert delta == TEYA_MAIN_INSTRUCTION_V2_SAVINGS


def test_scenario_14_full87_pre_budget_selects_relatox_service_specific() -> None:
    ctx = _scenario_14_context(kb_keys=None)
    assert ctx.knowledge is not None
    assert "faq.relatox_units" in ctx.knowledge.selected_keys
    assert "faq.biorev_type" not in ctx.knowledge.selected_keys


def test_scenario_14_v2_candidate_includes_relatox_kb_under_budget() -> None:
    ctx = _scenario_14_context(kb_keys=SCENARIO_14_ACTIVE_KB_KEYS)
    metrics = measure_shadow_draft_prompt(ctx)

    assert metrics.service_resolution == "UNIQUE"
    assert metrics.resolved_service_names == (RELATOX_CANONICAL_NAME,)
    assert "faq.relatox_units" in metrics.kb_keys_final
    assert "faq.biorev_type" not in metrics.kb_keys_final
    assert metrics.within_budget is True
    assert metrics.total_chars <= SHADOW_DRAFT_COMPILED_CHAR_BUDGET


def test_scenario_14_full87_final_keeps_relatox_kb_under_budget() -> None:
    ctx = _scenario_14_context(kb_keys=None)
    metrics = measure_shadow_draft_prompt(ctx)
    assert metrics.service_resolution == "UNIQUE"
    assert "faq.relatox_units" in metrics.kb_keys_final
    assert "faq.biorev_type" not in metrics.kb_keys_final
    assert metrics.within_budget is True
    assert metrics.total_chars <= SHADOW_DRAFT_COMPILED_CHAR_BUDGET


def test_scenario_14_v1_golden_drops_relatox_v2_retains_same_context() -> None:
    v1_ctx = _scenario_14_context(
        kb_keys=SCENARIO_14_ACTIVE_KB_KEYS,
        settings_envelope=_settings_envelope_v1_golden(),
    )
    v2_ctx = _scenario_14_context(kb_keys=SCENARIO_14_ACTIVE_KB_KEYS)
    v1_metrics = measure_shadow_draft_prompt(v1_ctx)
    v2_metrics = measure_shadow_draft_prompt(v2_ctx)
    assert "faq.relatox_units" not in v1_metrics.kb_keys_final
    assert "faq.relatox_units" in v2_metrics.kb_keys_final
    assert v2_metrics.within_budget is True


def test_all_17_v2_candidate_production_shaped_under_budget() -> None:
    inventory = _inventory_all_17()
    assert len(inventory) == 17

    margins = [int(row["margin"]) for row in inventory]
    assert all(margin >= 0 for margin in margins)
    for row in inventory:
        assert int(row["totalChars"]) <= SHADOW_DRAFT_COMPILED_CHAR_BUDGET, row["scenario"]

    scenario_14 = next(row for row in inventory if row["scenario"] == "14_relatox_orientation_only")
    assert "faq.relatox_units" in scenario_14["kbKeysFinal"]

    minimum = min(inventory, key=lambda row: int(row["margin"]))
    assert minimum["margin"] == min(margins)
    assert minimum["totalChars"] <= SHADOW_DRAFT_COMPILED_CHAR_BUDGET


def test_v2_candidate_tight_margin_caveat_below_300_chars() -> None:
    inventory = _inventory_all_17()
    tight = [row for row in inventory if int(row["margin"]) < 300]
    assert tight, "expected at least one tight-margin scenario in production-shaped inventory"

    for row in tight:
        scenario_id = str(row["scenario"])
        if scenario_id == "14_relatox_orientation_only":
            assert "faq.relatox_units" in row["kbKeysFinal"]
            continue
        if row["kbKeysFinal"]:
            continue
        assert int(row["liveFactChars"]) > 0 or int(row["policyChars"]) > 0


def test_compile_trim_order_dialog_catalog_kb_then_fail_closed() -> None:
    """Prove trim sequence: dialog -> names-only catalog -> KB pop -> fail closed."""

    def _multi_turn_scenario_14_context() -> Any:
        turns = []
        for idx in range(1, 5):
            turns.append(
                map_history_author(
                    author="client",
                    conversation_event_seq=idx,
                    text=(
                        f"Turn {idx}: {_SCENARIO_14_CLIENT}"
                        if idx == 4
                        else f"Turn {idx}: уточнение по Relatox"
                    ),
                    occurred_at=datetime(2026, 8, 30, 12, idx, tzinfo=timezone.utc),
                )
            )
        conversation = build_conversation_layer_from_turns(
            conversation_id=uuid4(),
            event_seq_hwm=len(turns),
            turns=tuple(turns),
        )
        lf = parse_live_facts_response_v1(_live_facts_production_shaped(count=20))
        hint = build_knowledge_selection_hint(
            conversation_text=turns[-1].text,
            live_facts=lf,
            client_turns_newest_first=tuple(t.text for t in reversed(turns)),
        )
        return assemble_runtime_context(
            bot_mode=BotMode.OFF,
            emergency_lock=False,
            settings_publication=parse_settings_publication_v1(_settings_envelope_v2_candidate()),
            settings_readiness=ControlPlaneKindReadiness.READY_FRESH,
            knowledge_publication=parse_knowledge_publication_v1(_knowledge_envelope(keys=None)),
            knowledge_readiness=ControlPlaneKindReadiness.READY_FRESH,
            live_facts=lf,
            conversation=conversation,
            handoff_state="BOT_ACTIVE",
            ownership="BOT",
            conversation_status="OPEN",
            manager_takeover_at_present=False,
            knowledge_hint=hint,
        )

    ctx = _multi_turn_scenario_14_context()
    cp = teya_v2_candidate_content_policy_fields()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.core.shadow_draft_prompt.SHADOW_DRAFT_COMPILED_CHAR_BUDGET",
            1_000_000,
            raising=False,
        )
        baseline_total = measure_shadow_draft_prompt(ctx).total_chars
    high_budget = baseline_total + 500
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.core.shadow_draft_prompt.SHADOW_DRAFT_COMPILED_CHAR_BUDGET",
            high_budget,
            raising=False,
        )
        full_messages = compile_shadow_draft_messages(ctx)
    full_system = full_messages[0].text
    full_dialog = "".join(m.text for m in full_messages if m.role == "user")
    assert cp["safetyRules"] in full_system
    assert "faq.relatox_units" in full_system
    assert full_dialog.count("[UNTRUSTED_CLIENT]") >= 4

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.core.shadow_draft_prompt.SHADOW_DRAFT_COMPILED_CHAR_BUDGET",
            baseline_total - 1,
            raising=False,
        )
        dialog_trimmed_messages = compile_shadow_draft_messages(ctx)
    dialog_trimmed = "".join(
        m.text for m in dialog_trimmed_messages if m.role == "user"
    )
    assert dialog_trimmed.count("[UNTRUSTED_CLIENT]") < full_dialog.count(
        "[UNTRUSTED_CLIENT]"
    )

    unknown_payload = _live_facts_production_shaped(count=15)
    unknown_lf = parse_live_facts_response_v1(unknown_payload)
    unknown_turns = (
        map_history_author(
            author="client",
            conversation_event_seq=1,
            text="Сколько стоит услуга НесуществующаяXYZ?",
            occurred_at=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
        ),
    )
    unknown_conversation = build_conversation_layer_from_turns(
        conversation_id=uuid4(),
        event_seq_hwm=1,
        turns=unknown_turns,
    )
    unknown_ctx = assemble_runtime_context(
        bot_mode=BotMode.OFF,
        emergency_lock=False,
        settings_publication=parse_settings_publication_v1(_settings_envelope_v2_candidate()),
        settings_readiness=ControlPlaneKindReadiness.READY_FRESH,
        knowledge_publication=parse_knowledge_publication_v1(
            _knowledge_envelope(keys=SCENARIO_14_ACTIVE_KB_KEYS)
        ),
        knowledge_readiness=ControlPlaneKindReadiness.READY_FRESH,
        live_facts=unknown_lf,
        conversation=unknown_conversation,
        handoff_state="BOT_ACTIVE",
        ownership="BOT",
        conversation_status="OPEN",
        manager_takeover_at_present=False,
        knowledge_hint=build_knowledge_selection_hint(
            conversation_text=unknown_turns[0].text,
            live_facts=unknown_lf,
            client_turns_newest_first=(unknown_turns[0].text,),
        ),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.core.shadow_draft_prompt.SHADOW_DRAFT_COMPILED_CHAR_BUDGET",
            12_000,
            raising=False,
        )
        catalog_trimmed = compile_shadow_draft_messages(unknown_ctx)[0].text
    assert "service_catalog_names_only" in catalog_trimmed

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.core.shadow_draft_prompt.SHADOW_DRAFT_COMPILED_CHAR_BUDGET",
            baseline_total - 500,
            raising=False,
        )
        kb_trimmed = compile_shadow_draft_messages(ctx)[0].text
    assert "faq.relatox_units" not in kb_trimmed
    assert cp["safetyRules"] in kb_trimmed

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.core.shadow_draft_prompt.SHADOW_DRAFT_COMPILED_CHAR_BUDGET",
            500,
            raising=False,
        )
        with pytest.raises(ValueError, match="PROMPT_BUDGET_EXCEEDED"):
            compile_shadow_draft_messages(ctx)
