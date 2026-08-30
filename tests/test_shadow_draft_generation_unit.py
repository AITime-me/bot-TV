"""AI-DIALOGUE-02 shadow draft generation — gates, prompt, no side effects."""

from __future__ import annotations

import ast
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

import pytest

from app.config import BotMode, Settings
from app.core.control_plane_types import (
    ControlPlaneKindReadiness,
    parse_knowledge_publication_v1,
    parse_settings_publication_v1,
)
from app.core.live_facts_types import parse_live_facts_response_v1
from app.core.outbound_policy import OutboundAction, is_automatic_outbound_allowed
from app.core.runtime_context_assemble import (
    assemble_runtime_context,
    build_conversation_layer_from_turns,
    map_history_author,
)
from app.core.runtime_context_knowledge import KnowledgeSelectionHint
from app.core.runtime_context_types import (
    RuntimeContextBuildResult,
    RuntimeContextReadiness,
    RuntimeContextReason,
)
from app.core.shadow_draft_gate import evaluate_shadow_draft_gate
from app.core.shadow_draft_prompt import (
    compile_shadow_draft_messages,
    compile_shadow_draft_messages_fingerprint,
)
from app.core.shadow_draft_types import (
    ShadowDraftDisposition,
    ShadowDraftReasonCode,
)
from app.core.text_generation_port import (
    TextGenerationMessage,
    TextGenerationResult,
)
from app.core.yandex_gpt_http import YandexGptHttpError
from app.services.shadow_draft_generation import (
    ShadowDraftGenerationService,
    build_shadow_draft_generation_service,
    is_yandex_shadow_draft_enabled,
)
from tests.docker_runtime_allowlist import (
    AI_DIALOGUE_02_DOCKER_RUNTIME_PATHS,
    is_included_in_docker_build_context,
)
from tests.fixtures.online_zapis_live_facts_v1 import (
    ONLINE_ZAPIS_LIVE_FACTS_V1_REPRESENTATIVE,
)
from tests.fixtures.shadow_draft_eval_scenarios import EVAL_SCENARIOS

_REPO = Path(__file__).resolve().parents[1]
_SERVICE_A = "11111111-1111-4111-8111-111111111111"
_CHECKSUM = "a" * 64
_PUB_ID = "11111111-1111-4111-8111-111111111111"
_RELATOX_ID = "33333333-3333-4333-8333-333333333333"


class _FakePort:
    def __init__(self, text: str = "Черновик Теи: отвечаю по фактам.") -> None:
        self.text = text
        self.calls: list[tuple[TextGenerationMessage, ...]] = []
        self.error: BaseException | None = None

    def generate(
        self, messages: Sequence[TextGenerationMessage]
    ) -> TextGenerationResult:
        self.calls.append(tuple(messages))
        if self.error is not None:
            raise self.error
        return TextGenerationResult(text=self.text)


def _settings_envelope() -> dict[str, Any]:
    return {
        "ok": True,
        "schemaVersion": 1,
        "publicationId": _PUB_ID,
        "version": 3,
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
            "key": "procedure.alpha",
            "category": "PROCEDURE_EXPLANATION",
            "title": "Альфа",
            "content": "Объяснение процедуры Альфа без индивидуальной схемы.",
            "tags": ["alpha"],
            "serviceId": _SERVICE_A,
        },
        {
            "key": "prep.alpha",
            "category": "PREPARATION",
            "title": "Подготовка",
            "content": "Перед процедурой не наносить кремы.",
            "tags": ["prep"],
            "serviceId": _SERVICE_A,
        },
        {
            "key": "safety.general",
            "category": "SAFETY_INFORMATION",
            "title": "Безопасность",
            "content": "Не ставим диагноз. Противопоказания уточняет косметолог очно.",
            "tags": ["safety"],
            "serviceId": None,
        },
        {
            "key": "faq.relatox_units",
            "category": "FAQ",
            "title": "Relatox ориентир",
            "content": (
                "Справочно по утверждённым значениям KB: ориентир единиц только "
                "как приблизительная справка. Индивидуальную дозировку по возрасту "
                "или фото не даём. Точная схема — только косметолог очно."
            ),
            "tags": ["relatox"],
            "serviceId": _RELATOX_ID,
        },
    ]


def _knowledge_envelope(entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "schemaVersion": 1,
        "knowledgePublicationId": _PUB_ID,
        "version": 1,
        "checksum": _CHECKSUM,
        "publishedAt": "2026-08-01T12:00:00.000Z",
        "entries": entries if entries is not None else _knowledge_entries(),
    }


def _live_facts(**overrides: Any) -> dict[str, Any]:
    payload = copy.deepcopy(ONLINE_ZAPIS_LIVE_FACTS_V1_REPRESENTATIVE)
    payload.update(overrides)
    # Repurpose the third fixture service as Relatox with a live unit price.
    for service in payload["services"]:
        if service["id"] == _RELATOX_ID:
            service["name"] = "Relatox"
            service["category"] = "injectables"
            service["priceFrom"] = "450.00"
            service["priceTo"] = "450.00"
            service["isActive"] = True
            service["isOnlineBookingEnabled"] = False
            service["bookingMode"] = "MANAGER_ONLY"
            break
    return payload


def _context(
    *,
    client_text: str = "Расскажите про процедуру Альфа",
    emergency_lock: bool = False,
    handoff_state: str | None = "BOT_ACTIVE",
    manager_takeover: bool = False,
    include_settings: bool = True,
    include_knowledge: bool = True,
    include_live: bool = True,
    knowledge_entries: list[dict[str, Any]] | None = None,
) -> Any:
    settings = (
        parse_settings_publication_v1(_settings_envelope())
        if include_settings
        else None
    )
    knowledge = (
        parse_knowledge_publication_v1(
            _knowledge_envelope(knowledge_entries)
        )
        if include_knowledge
        else None
    )
    live = parse_live_facts_response_v1(_live_facts()) if include_live else None
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
    return assemble_runtime_context(
        bot_mode=BotMode.OFF,
        emergency_lock=emergency_lock,
        settings_publication=settings,
        settings_readiness=(
            ControlPlaneKindReadiness.READY_FRESH if settings else None
        ),
        knowledge_publication=knowledge,
        knowledge_readiness=(
            ControlPlaneKindReadiness.READY_FRESH if knowledge else None
        ),
        live_facts=live,
        conversation=conversation,
        handoff_state=handoff_state,
        ownership="MANAGER" if manager_takeover else "BOT",
        conversation_status="HANDOFF" if manager_takeover else "OPEN",
        manager_takeover_at_present=manager_takeover,
        # No service filter — keep general SAFETY + multi-service KB for evals.
        knowledge_hint=KnowledgeSelectionHint(),
    )


def test_shadow_feature_default_off() -> None:
    assert is_yandex_shadow_draft_enabled({}) is False
    assert is_yandex_shadow_draft_enabled({"YANDEX_SHADOW_DRAFT_ENABLED": "false"}) is False
    assert is_yandex_shadow_draft_enabled({"YANDEX_SHADOW_DRAFT_ENABLED": "true"}) is True


def test_prompt_compiler_deterministic_and_live_facts_first() -> None:
    ctx = _context(client_text="Сколько стоит Альфа?")
    m1 = compile_shadow_draft_messages(ctx)
    m2 = compile_shadow_draft_messages(ctx)
    assert compile_shadow_draft_messages_fingerprint(m1) == (
        compile_shadow_draft_messages_fingerprint(m2)
    )
    assert m1[0].role == "system"
    system = m1[0].text
    assert "LIVE FACTS" in system
    assert "ACTIVE MANAGED KB" in system
    assert "помощник менеджера студии" in system.casefold() or "Твоё время" in system
    assert system.index("LIVE FACTS") < system.index("ACTIVE MANAGED KB")
    # Live price from fixture appears; conflicting invented price must not be hardcoded.
    assert "price_from=" in system
    assert m1[-1].role == "user"
    assert "Альфа" in m1[-1].text


def test_generation_gate_requires_all_sources_and_feature() -> None:
    ctx = _context(emergency_lock=False)
    assert ctx.safety.generation_allowed is True
    denied = evaluate_shadow_draft_gate(
        context=ctx,
        generation_allowed=True,
        provider_configured=True,
        shadow_feature_enabled=False,
    )
    assert denied.allowed is False
    assert denied.reason_code is ShadowDraftReasonCode.SHADOW_FEATURE_DISABLED

    ok = evaluate_shadow_draft_gate(
        context=ctx,
        generation_allowed=True,
        provider_configured=True,
        shadow_feature_enabled=True,
    )
    assert ok.allowed is True

    no_kb = evaluate_shadow_draft_gate(
        context=_context(include_knowledge=False, emergency_lock=False),
        generation_allowed=False,
        provider_configured=True,
        shadow_feature_enabled=True,
    )
    assert no_kb.allowed is False
    assert ShadowDraftReasonCode.KNOWLEDGE_NOT_USABLE in no_kb.deny_reasons

    no_live = evaluate_shadow_draft_gate(
        context=_context(include_live=False, emergency_lock=False),
        generation_allowed=False,
        provider_configured=True,
        shadow_feature_enabled=True,
    )
    assert ShadowDraftReasonCode.LIVE_FACTS_NOT_USABLE in no_live.deny_reasons


def test_manager_takeover_and_handoff_deny_without_provider_call() -> None:
    port = _FakePort()
    service = ShadowDraftGenerationService(port=port, shadow_feature_enabled=True)
    for ctx in (
        _context(manager_takeover=True, emergency_lock=False),
        _context(handoff_state="HUMAN_ACTIVE", emergency_lock=False),
    ):
        reply = service.generate_from_context(ctx, generation_allowed=False)
        assert reply.disposition is ShadowDraftDisposition.DENIED
        assert port.calls == []
        assert reply.text is None


def test_shadow_success_and_provider_error_mapping() -> None:
    port = _FakePort("Ориентир по live цене услуги.")
    service = ShadowDraftGenerationService(port=port, shadow_feature_enabled=True)
    ctx = _context(emergency_lock=False)
    reply = service.generate_from_context(ctx, generation_allowed=True)
    assert reply.disposition is ShadowDraftDisposition.REPLY
    assert reply.reason_code is ShadowDraftReasonCode.OK
    assert reply.text is not None
    assert len(port.calls) == 1
    diag = reply.diagnostic_summary()
    assert "Ориентир" not in json.dumps(diag, ensure_ascii=False) or diag["hasText"]
    # diagnostic may include textLen only — ensure no phone-like studio phone leaked via provenance
    blob = json.dumps(diag, ensure_ascii=False)
    assert "8 912" not in blob
    assert "Api-Key" not in blob

    port.error = YandexGptHttpError("TIMEOUT")
    err = service.generate_from_context(ctx, generation_allowed=True)
    assert err.disposition is ShadowDraftDisposition.PROVIDER_ERROR
    assert err.reason_code is ShadowDraftReasonCode.PROVIDER_TIMEOUT
    assert err.text is None
    assert err.handoff_required is True


def test_shadow_no_side_effects_imports_and_outbound_still_denied() -> None:
    source = (
        _REPO / "app" / "services" / "shadow_draft_generation.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    forbidden = {
        "app.services.reply_outbound",
        "app.services.outbound_arbiter",
        "app.services.synthetic_outbound",
        "app.repositories.messages",
        "app.models.outbox",
        "app.services.booking_flow",
        "app.services.amocrm_adapter",
        "app.services.amocrm_mirror",
    }
    assert imported.isdisjoint(forbidden)
    assert (
        is_automatic_outbound_allowed(Settings(), OutboundAction.SEND_MESSAGE) is False
    )


def test_repr_and_logs_redact_draft_text() -> None:
    port = _FakePort("Секретный текст клиента +79001112233")
    service = ShadowDraftGenerationService(port=port, shadow_feature_enabled=True)
    reply = service.generate_from_context(
        _context(client_text="+79001112233 Иван", emergency_lock=False),
        generation_allowed=True,
    )
    rendered = repr(reply) + repr(reply.provenance)
    assert "+79001112233" not in rendered
    assert "Иван" not in rendered
    assert "Секретный" not in rendered


@pytest.mark.parametrize("scenario", EVAL_SCENARIOS, ids=lambda s: s["id"])
def test_eval_scenarios(scenario: dict[str, Any]) -> None:
    port = _FakePort(scenario["model_text"])
    service = ShadowDraftGenerationService(
        port=port, shadow_feature_enabled=scenario.get("shadow_enabled", True)
    )
    ctx = _context(
        client_text=scenario["client_text"],
        emergency_lock=scenario.get("emergency_lock", False),
        handoff_state=scenario.get("handoff_state", "BOT_ACTIVE"),
        manager_takeover=scenario.get("manager_takeover", False),
        include_settings=scenario.get("include_settings", True),
        include_knowledge=scenario.get("include_knowledge", True),
        include_live=scenario.get("include_live", True),
    )
    generation_allowed = scenario.get(
        "generation_allowed", ctx.safety.generation_allowed
    )
    if scenario.get("build_not_ready"):
        build = RuntimeContextBuildResult(
            readiness=RuntimeContextReadiness.NOT_READY,
            reasons=(RuntimeContextReason.KNOWLEDGE_NOT_READY,),
            generation_allowed=False,
            context=ctx,
        )
        reply = service.generate_from_build(build)
    else:
        reply = service.generate_from_context(
            ctx, generation_allowed=generation_allowed
        )

    assert reply.disposition.value == scenario["expect_disposition"]
    assert reply.reason_code.value == scenario["expect_reason"]
    if scenario.get("expect_provider_called"):
        assert len(port.calls) == 1
        system = port.calls[0][0].text
        for needle in scenario.get("system_must_contain", ()):
            assert needle in system
    else:
        assert port.calls == []


def test_docker_allowlist_includes_dialogue_02() -> None:
    lines = (_REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for rel in AI_DIALOGUE_02_DOCKER_RUNTIME_PATHS:
        assert is_included_in_docker_build_context(rel, lines) is True, rel
        assert (_REPO / rel).is_file()


def test_factory_respects_shadow_flag_without_port() -> None:
    service = build_shadow_draft_generation_service(
        port=None,
        environ={"YANDEX_SHADOW_DRAFT_ENABLED": "true"},
    )
    assert service.shadow_feature_enabled is True
    assert service.provider_configured is False
    reply = service.generate_from_context(
        _context(emergency_lock=False), generation_allowed=True
    )
    assert reply.reason_code is ShadowDraftReasonCode.PROVIDER_NOT_CONFIGURED
