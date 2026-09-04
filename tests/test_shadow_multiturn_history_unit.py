"""Regression proofs for shadow-only multi-turn virtual assistant history."""

from __future__ import annotations

import ast
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
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
from app.core.runtime_context_knowledge import KnowledgeSelectionHint
from app.core.runtime_context_types import (
    RuntimeContextBuildResult,
    RuntimeContextReadiness,
)
from app.core.shadow_draft_prompt import (
    _DIALOG_TRUST_NOTE,
    _dialog_messages,
    compile_shadow_draft_messages,
)
from app.core.shadow_draft_types import (
    ShadowAssistantTurn,
    ShadowDraftDisposition,
    ShadowDraftProvenanceSummary,
    ShadowDraftReasonCode,
    ShadowDraftReply,
)
from app.services import shadow_draft_ingress_hook as hook_mod
from app.services.shadow_draft_generation import ShadowDraftGenerationService
from app.services.shadow_draft_ingress_hook import (
    run_shadow_draft_after_client_inbound,
)
from tests.test_shadow_draft_generation_unit import (
    _FakePort,
    _knowledge_envelope,
    _live_facts,
    _settings_envelope,
)
from tests.test_shadow_draft_ingress_wiring_unit import _locked_ready_sources_build

_REPO = Path(__file__).resolve().parents[1]
_SECRET_SHADOW = "RF стоит X и длится Y"
_SECRET_CLIENT = "Кто ты?"
_RF_Q = "сколько стоит RF-лифтинг?"


def _multi_client_context(
    *texts_and_seqs: tuple[str, int],
) -> Any:
    settings = parse_settings_publication_v1(_settings_envelope())
    knowledge = parse_knowledge_publication_v1(_knowledge_envelope())
    live = parse_live_facts_response_v1(_live_facts())
    turns = tuple(
        map_history_author(
            author="client",
            conversation_event_seq=seq,
            text=text,
            occurred_at=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
        )
        for text, seq in texts_and_seqs
    )
    conversation = build_conversation_layer_from_turns(
        conversation_id=uuid4(),
        event_seq_hwm=max(seq for _, seq in texts_and_seqs),
        turns=turns,
    )
    return assemble_runtime_context(
        bot_mode=BotMode.OFF,
        emergency_lock=False,
        settings_publication=settings,
        settings_readiness=ControlPlaneKindReadiness.READY_FRESH,
        knowledge_publication=knowledge,
        knowledge_readiness=ControlPlaneKindReadiness.READY_FRESH,
        live_facts=live,
        conversation=conversation,
        handoff_state="BOT_ACTIVE",
        ownership="BOT",
        conversation_status="OPEN",
        manager_takeover_at_present=False,
        knowledge_hint=KnowledgeSelectionHint(),
    )


def test_shadow_assistant_turn_repr_redacts_text() -> None:
    turn = ShadowAssistantTurn(
        conversation_event_seq=10,
        inbox_message_id=uuid4(),
        text=_SECRET_SHADOW,
    )
    rendered = repr(turn)
    assert _SECRET_SHADOW not in rendered
    assert "text=<redacted>" in rendered
    assert "text_len=" in rendered


def test_prompt_merges_prior_shadow_after_matching_client() -> None:
    """A: RF client + shadow, then Who-are-you → ordered merge."""

    ctx = _multi_client_context((_RF_Q, 10), (_SECRET_CLIENT, 20))
    prior = (
        ShadowAssistantTurn(
            conversation_event_seq=10,
            inbox_message_id=uuid4(),
            text=_SECRET_SHADOW,
        ),
    )
    messages = compile_shadow_draft_messages(ctx, shadow_assistant_turns=prior)
    assert messages[0].role == "system"
    assert "SHADOW_ASSISTANT" in _DIALOG_TRUST_NOTE
    assert "LIVE FACTS" in messages[0].text
    dialog = [(m.role, m.text) for m in messages[1:]]
    assert dialog == [
        ("user", f"[UNTRUSTED_CLIENT] {_RF_Q}"),
        ("assistant", f"[SHADOW_ASSISTANT] {_SECRET_SHADOW}"),
        ("user", f"[UNTRUSTED_CLIENT] {_SECRET_CLIENT}"),
    ]


def test_prompt_without_prior_shadow_stays_user_user() -> None:
    """B: no prior draft → two client turns only."""

    ctx = _multi_client_context((_RF_Q, 10), (_SECRET_CLIENT, 20))
    messages = compile_shadow_draft_messages(ctx)
    dialog = [(m.role, m.text) for m in messages[1:]]
    assert dialog == [
        ("user", f"[UNTRUSTED_CLIENT] {_RF_Q}"),
        ("user", f"[UNTRUSTED_CLIENT] {_SECRET_CLIENT}"),
    ]
    assert not any("[SHADOW_ASSISTANT]" in m.text for m in messages)


def test_prompt_three_turn_deterministic_order() -> None:
    """C: user1/shadow1/user2/shadow2/user3."""

    ctx = _multi_client_context(
        ("q1", 1),
        ("q2", 2),
        ("q3", 3),
    )
    prior = (
        ShadowAssistantTurn(
            conversation_event_seq=1,
            inbox_message_id=uuid4(),
            text="a1",
        ),
        ShadowAssistantTurn(
            conversation_event_seq=2,
            inbox_message_id=uuid4(),
            text="a2",
        ),
    )
    messages = compile_shadow_draft_messages(ctx, shadow_assistant_turns=prior)
    dialog = [(m.role, m.text) for m in messages[1:]]
    assert dialog == [
        ("user", "[UNTRUSTED_CLIENT] q1"),
        ("assistant", "[SHADOW_ASSISTANT] a1"),
        ("user", "[UNTRUSTED_CLIENT] q2"),
        ("assistant", "[SHADOW_ASSISTANT] a2"),
        ("user", "[UNTRUSTED_CLIENT] q3"),
    ]


def test_budget_trim_drops_orphan_shadow_with_client() -> None:
    """D: trimmed old client removes its virtual assistant — no orphan."""

    ctx = _multi_client_context(
        ("old RF?", 10),
        ("middle?", 15),
        ("Кто ты?", 20),
    )
    prior = (
        ShadowAssistantTurn(
            conversation_event_seq=10,
            inbox_message_id=uuid4(),
            text="OLD_SHADOW_MUST_VANISH",
        ),
        ShadowAssistantTurn(
            conversation_event_seq=15,
            inbox_message_id=uuid4(),
            text="MIDDLE_SHADOW",
        ),
    )
    # Force keep only newest client turn via max_turns suffix.
    dialog = _dialog_messages(
        ctx,
        max_turns=1,
        shadow_assistant_turns=prior,
    )
    assert len(dialog) == 1
    assert dialog[0].role == "user"
    assert "Кто ты?" in dialog[0].text
    assert not any(m.role == "assistant" for m in dialog)
    assert "OLD_SHADOW_MUST_VANISH" not in "".join(m.text for m in dialog)

    keep_two = _dialog_messages(
        ctx,
        max_turns=2,
        shadow_assistant_turns=prior,
    )
    roles = [m.role for m in keep_two]
    assert roles == ["user", "assistant", "user"]
    assert "MIDDLE_SHADOW" in keep_two[1].text
    assert "OLD_SHADOW_MUST_VANISH" not in "".join(m.text for m in keep_two)


@pytest.mark.asyncio
async def test_hook_loads_history_before_generate_uses_inbox_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F: load before generation; exact inbox boundary; persist after."""

    conversation_id = uuid4()
    inbox_id = uuid4()
    prior_inbox = uuid4()
    order: list[str] = []
    loaded_kwargs: dict[str, Any] = {}
    prior = (
        ShadowAssistantTurn(
            conversation_event_seq=10,
            inbox_message_id=prior_inbox,
            text=_SECRET_SHADOW,
        ),
    )

    async def _fake_load(**kwargs: Any) -> tuple[ShadowAssistantTurn, ...]:
        order.append("load")
        loaded_kwargs.update(kwargs)
        return prior

    async def _fake_persist(**kwargs: Any) -> None:
        order.append("persist")

    monkeypatch.setattr(
        hook_mod,
        "_load_prior_shadow_assistant_turns_fail_soft",
        _fake_load,
    )
    monkeypatch.setattr(hook_mod, "_persist_shadow_draft_fail_soft", _fake_persist)

    build = _locked_ready_sources_build()
    builder = MagicMock()
    builder.build_for_conversation = AsyncMock(return_value=build)

    captured: dict[str, Any] = {}

    def _generate(build_arg: Any, *, shadow_assistant_turns=()) -> ShadowDraftReply:
        order.append("generate")
        captured["turns"] = shadow_assistant_turns
        return ShadowDraftReply(
            text="ok",
            disposition=ShadowDraftDisposition.REPLY,
            handoff_required=False,
            reason_code=ShadowDraftReasonCode.OK,
            provenance=ShadowDraftProvenanceSummary(
                settings_publication_id=None,
                settings_checksum=None,
                knowledge_publication_id=None,
                knowledge_checksum=None,
                selected_knowledge_keys=(),
                live_facts_service_count=None,
                live_facts_master_count=None,
                history_turn_count=2,
            ),
            generation_metadata={
                "provider": "yandex",
                "shadow": True,
                "model_configured": True,
                "provider_transport_called": True,
            },
        )

    service = MagicMock()
    service.shadow_feature_enabled = True
    service.generate_from_build = MagicMock(side_effect=_generate)

    reply = await run_shadow_draft_after_client_inbound(
        conversation_id=conversation_id,
        inbox_message_id=inbox_id,
        session_factory=MagicMock(),
        builder=builder,
        service=service,
    )
    assert reply is not None
    assert order == ["load", "generate", "persist"]
    assert loaded_kwargs["conversation_id"] == conversation_id
    assert loaded_kwargs["inbox_message_id"] == inbox_id
    assert captured["turns"] is prior
    service.generate_from_build.assert_called_once_with(
        build,
        shadow_assistant_turns=prior,
    )


@pytest.mark.asyncio
async def test_hook_history_load_failure_still_generates(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F: load failure → empty history; generation continues; no raw text logs."""

    async def _boom(**kwargs: Any) -> tuple[ShadowAssistantTurn, ...]:
        raise RuntimeError("history boom")

    # Use real fail-soft loader path via session_scope boom.
    class _BoomScope:
        async def __aenter__(self) -> Any:
            raise RuntimeError("history boom")

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(hook_mod, "session_scope", lambda _f: _BoomScope())

    build = _locked_ready_sources_build()
    builder = MagicMock()
    builder.build_for_conversation = AsyncMock(return_value=build)
    service = MagicMock()
    service.shadow_feature_enabled = True
    expected = ShadowDraftReply(
        text=_SECRET_SHADOW,
        disposition=ShadowDraftDisposition.REPLY,
        handoff_required=False,
        reason_code=ShadowDraftReasonCode.OK,
        provenance=ShadowDraftProvenanceSummary(
            settings_publication_id=None,
            settings_checksum=None,
            knowledge_publication_id=None,
            knowledge_checksum=None,
            selected_knowledge_keys=(),
            live_facts_service_count=None,
            live_facts_master_count=None,
            history_turn_count=1,
        ),
        generation_metadata={
            "provider": "yandex",
            "shadow": True,
            "model_configured": True,
            "provider_transport_called": True,
        },
    )
    service.generate_from_build = MagicMock(return_value=expected)

    persist_calls: list[str] = []

    async def _persist(**kwargs: Any) -> None:
        persist_calls.append("persist")

    monkeypatch.setattr(hook_mod, "_persist_shadow_draft_fail_soft", _persist)

    with caplog.at_level(logging.INFO, logger=hook_mod.__name__):
        reply = await run_shadow_draft_after_client_inbound(
            conversation_id=uuid4(),
            inbox_message_id=uuid4(),
            session_factory=MagicMock(),
            builder=builder,
            service=service,
        )

    assert reply is expected
    assert persist_calls == ["persist"]
    service.generate_from_build.assert_called_once()
    kwargs = service.generate_from_build.call_args.kwargs
    assert kwargs["shadow_assistant_turns"] == ()
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "shadow_history_load_failed" in joined
    assert "RuntimeError" in joined
    assert _SECRET_SHADOW not in joined
    assert _SECRET_CLIENT not in joined


@pytest.mark.asyncio
async def test_hook_feature_off_skips_shadow_history_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G: feature off → no history load/merge."""

    load_calls: list[str] = []

    async def _should_not_load(**kwargs: Any) -> tuple[ShadowAssistantTurn, ...]:
        load_calls.append("load")
        return ()

    monkeypatch.setattr(
        hook_mod,
        "_load_prior_shadow_assistant_turns_fail_soft",
        _should_not_load,
    )
    monkeypatch.setattr(
        hook_mod,
        "_persist_shadow_draft_fail_soft",
        AsyncMock(),
    )

    build = _locked_ready_sources_build()
    builder = MagicMock()
    builder.build_for_conversation = AsyncMock(return_value=build)
    service = MagicMock()
    service.shadow_feature_enabled = False
    service.generate_from_build = MagicMock(
        return_value=ShadowDraftReply(
            text=None,
            disposition=ShadowDraftDisposition.DENIED,
            handoff_required=False,
            reason_code=ShadowDraftReasonCode.SHADOW_FEATURE_DISABLED,
            provenance=ShadowDraftProvenanceSummary(
                settings_publication_id=None,
                settings_checksum=None,
                knowledge_publication_id=None,
                knowledge_checksum=None,
                selected_knowledge_keys=(),
                live_facts_service_count=None,
                live_facts_master_count=None,
                history_turn_count=None,
            ),
            generation_metadata={
                "provider": "yandex",
                "shadow": True,
                "model_configured": False,
                "provider_transport_called": False,
            },
        )
    )

    await run_shadow_draft_after_client_inbound(
        conversation_id=uuid4(),
        inbox_message_id=uuid4(),
        session_factory=MagicMock(),
        builder=builder,
        service=service,
    )
    assert load_calls == []
    service.generate_from_build.assert_called_once_with(
        build,
        shadow_assistant_turns=(),
    )


def test_generation_passes_shadow_turns_into_compile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def _capture(context: Any, *, shadow_assistant_turns=()) -> tuple:
        seen["turns"] = shadow_assistant_turns
        return (
            type("M", (), {"role": "system", "text": "s"})(),
            type("M", (), {"role": "user", "text": "u"})(),
        )

    monkeypatch.setattr(
        "app.services.shadow_draft_generation.compile_shadow_draft_messages",
        _capture,
    )

    ctx = _multi_client_context(("hi", 1))
    build = RuntimeContextBuildResult(
        context=ctx,
        readiness=RuntimeContextReadiness.READY,
        reasons=(),
        generation_allowed=True,
    )
    prior = (
        ShadowAssistantTurn(
            conversation_event_seq=1,
            inbox_message_id=uuid4(),
            text="prior",
        ),
    )
    service = ShadowDraftGenerationService(
        port=_FakePort(text="answer"),
        shadow_feature_enabled=True,
    )
    reply = service.generate_from_build(build, shadow_assistant_turns=prior)
    assert reply.disposition is ShadowDraftDisposition.REPLY
    assert seen["turns"] is prior


def test_static_safety_shadow_history_code_has_no_outbound_writers() -> None:
    """H: new history path must not import ReplyPlan/outbox/CRM/booking/VK send."""

    paths = (
        _REPO / "app" / "repositories" / "yandex_shadow_drafts.py",
        _REPO / "app" / "services" / "shadow_draft_ingress_hook.py",
        _REPO / "app" / "core" / "shadow_draft_prompt.py",
        _REPO / "app" / "services" / "shadow_draft_generation.py",
        _REPO / "app" / "core" / "shadow_draft_types.py",
    )
    forbidden_modules = {
        "app.services.reply_outbound",
        "app.services.outbound_arbiter",
        "app.services.synthetic_outbound",
        "app.repositories.reply_plans",
        "app.models.outbox",
        "app.services.booking_flow",
        "app.services.amocrm_adapter",
        "app.services.amocrm_mirror",
        "app.channels.vk_client",
        "app.services.vk_sender",
    }
    forbidden_tokens = (
        "create_client_reply_plan",
        "insert_synthetic_outbound",
        "create_internal_draft_outbox",
        "ReplyPlan",
        "send_message",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
        assert imported.isdisjoint(forbidden_modules), path.name
        # Docstrings may mention ReplyPlan negatively in the hook only.
        if path.name != "shadow_draft_ingress_hook.py":
            for token in forbidden_tokens:
                if token == "ReplyPlan":
                    continue
                assert token not in source, f"{path.name}:{token}"
