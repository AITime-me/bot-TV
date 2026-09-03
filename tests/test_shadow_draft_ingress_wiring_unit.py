"""Shadow draft real-ingress wiring + EMERGENCY_LOCK policy split."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.config import BotMode, Settings
from app.core.control_plane_types import ControlPlaneKindReadiness
from app.core.outbound_policy import OutboundAction, is_automatic_outbound_allowed
from app.core.runtime_context_types import (
    RuntimeContextBuildResult,
    RuntimeContextReadiness,
    RuntimeContextReason,
)
from app.core.shadow_draft_gate import (
    evaluate_shadow_draft_gate,
    evaluate_shadow_draft_gate_from_build,
)
from app.core.shadow_draft_types import (
    ShadowDraftDisposition,
    ShadowDraftReasonCode,
)
from app.services.ingress import IngressProcessResult
from app.services.shadow_draft_generation import (
    ShadowDraftGenerationService,
    build_shadow_draft_generation_service,
    is_yandex_shadow_allow_under_emergency_lock,
    is_yandex_shadow_draft_enabled,
)
from app.services.shadow_draft_ingress_hook import (
    run_shadow_draft_after_client_inbound,
)
from app.services import worker_runtime as worker_runtime_mod
from tests.test_shadow_draft_generation_unit import _FakePort, _context

_REPO = Path(__file__).resolve().parents[1]


def _locked_ready_sources_build(
    *,
    handoff: bool = False,
    takeover: bool = False,
    include_settings: bool = True,
    include_knowledge: bool = True,
    include_live: bool = True,
    settings_readiness: ControlPlaneKindReadiness = ControlPlaneKindReadiness.READY_FRESH,
    knowledge_readiness: ControlPlaneKindReadiness = ControlPlaneKindReadiness.READY_FRESH,
    extra_reasons: tuple[RuntimeContextReason, ...] = (),
) -> RuntimeContextBuildResult:
    """Simulate RuntimeContextBuilder under EMERGENCY_LOCK with fresh sources."""

    ctx = _context(
        emergency_lock=True,
        handoff_state="HUMAN_ACTIVE" if handoff else "BOT_ACTIVE",
        manager_takeover=takeover,
        include_settings=include_settings,
        include_knowledge=include_knowledge,
        include_live=include_live,
        settings_readiness=settings_readiness,
        knowledge_readiness=knowledge_readiness,
    )
    reasons: list[RuntimeContextReason] = [
        RuntimeContextReason.EMERGENCY_LOCK_ACTIVE,
        RuntimeContextReason.GENERATION_DISABLED_STAGE,
    ]
    if handoff or takeover:
        reasons.insert(0, RuntimeContextReason.HANDOFF_ACTIVE)
    if not include_settings or settings_readiness is not ControlPlaneKindReadiness.READY_FRESH:
        reasons.append(RuntimeContextReason.SETTINGS_NOT_READY)
    if not include_knowledge or knowledge_readiness is not ControlPlaneKindReadiness.READY_FRESH:
        reasons.append(RuntimeContextReason.KNOWLEDGE_NOT_READY)
    if not include_live:
        reasons.append(RuntimeContextReason.LIVE_FACTS_UNAVAILABLE)
    reasons.extend(extra_reasons)

    data_blocking = {
        RuntimeContextReason.SETTINGS_NOT_READY,
        RuntimeContextReason.KNOWLEDGE_NOT_READY,
        RuntimeContextReason.LIVE_FACTS_UNAVAILABLE,
        RuntimeContextReason.LIVE_FACTS_INVALID,
        RuntimeContextReason.LIVE_FACTS_AUTH_ERROR,
        RuntimeContextReason.LIVE_FACTS_CONTRACT_ERROR,
        RuntimeContextReason.HISTORY_UNAVAILABLE,
        RuntimeContextReason.SAFETY_UNREADABLE,
        RuntimeContextReason.CONVERSATION_UNAVAILABLE,
        RuntimeContextReason.EMERGENCY_LOCK_ACTIVE,
        RuntimeContextReason.HANDOFF_ACTIVE,
    }
    blocking = [r for r in reasons if r in data_blocking]
    readiness = (
        RuntimeContextReadiness.READY
        if not blocking
        else RuntimeContextReadiness.NOT_READY
    )
    return RuntimeContextBuildResult(
        readiness=readiness,
        reasons=tuple(dict.fromkeys(reasons)),
        generation_allowed=False,
        context=ctx,
    )


def test_allow_under_lock_flag_default_off() -> None:
    assert is_yandex_shadow_allow_under_emergency_lock({}) is False
    assert (
        is_yandex_shadow_allow_under_emergency_lock(
            {"YANDEX_SHADOW_ALLOW_UNDER_EMERGENCY_LOCK": "false"}
        )
        is False
    )
    assert (
        is_yandex_shadow_allow_under_emergency_lock(
            {"YANDEX_SHADOW_ALLOW_UNDER_EMERGENCY_LOCK": "true"}
        )
        is True
    )


def test_feature_off_hook_skips_provider() -> None:
    port = _FakePort("should-not-run")
    service = ShadowDraftGenerationService(
        port=port,
        shadow_feature_enabled=False,
        allow_under_emergency_lock=True,
    )
    build = _locked_ready_sources_build()
    reply = service.generate_from_build(build)
    assert reply.disposition is ShadowDraftDisposition.DENIED
    assert reply.reason_code is ShadowDraftReasonCode.SHADOW_FEATURE_DISABLED
    assert port.calls == []


def test_allow_under_lock_off_keeps_denied_under_emergency_lock() -> None:
    port = _FakePort()
    service = ShadowDraftGenerationService(
        port=port,
        shadow_feature_enabled=True,
        allow_under_emergency_lock=False,
    )
    build = _locked_ready_sources_build()
    assert build.readiness is RuntimeContextReadiness.NOT_READY
    assert build.context is not None
    assert build.context.safety.emergency_lock is True
    assert build.context.safety.generation_allowed is False

    gate = evaluate_shadow_draft_gate_from_build(
        build,
        provider_configured=True,
        shadow_feature_enabled=True,
        allow_under_emergency_lock=False,
    )
    assert gate.allowed is False
    assert ShadowDraftReasonCode.EMERGENCY_LOCK in gate.deny_reasons or (
        gate.reason_code
        in {
            ShadowDraftReasonCode.EMERGENCY_LOCK,
            ShadowDraftReasonCode.GENERATION_NOT_ALLOWED,
            ShadowDraftReasonCode.CONTEXT_NOT_READY,
        }
    )

    reply = service.generate_from_build(build)
    assert reply.disposition is ShadowDraftDisposition.DENIED
    assert port.calls == []


def test_allow_under_lock_on_permits_when_only_emergency_lock_blocks() -> None:
    port = _FakePort("Черновик под lock")
    service = ShadowDraftGenerationService(
        port=port,
        shadow_feature_enabled=True,
        allow_under_emergency_lock=True,
    )
    build = _locked_ready_sources_build()
    # Generic builder semantics unchanged:
    assert build.readiness is RuntimeContextReadiness.NOT_READY
    assert build.generation_allowed is False
    assert build.context is not None
    assert build.context.safety.generation_allowed is False

    gate = evaluate_shadow_draft_gate_from_build(
        build,
        provider_configured=True,
        shadow_feature_enabled=True,
        allow_under_emergency_lock=True,
    )
    assert gate.allowed is True
    assert gate.reason_code is ShadowDraftReasonCode.OK

    reply = service.generate_from_build(build)
    assert reply.disposition is ShadowDraftDisposition.REPLY
    assert reply.reason_code is ShadowDraftReasonCode.OK
    assert len(port.calls) == 1
    assert reply.text == "Черновик под lock"


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"handoff": True}, ShadowDraftReasonCode.HANDOFF_ACTIVE),
        ({"takeover": True}, ShadowDraftReasonCode.MANAGER_TAKEOVER),
        (
            {"settings_readiness": ControlPlaneKindReadiness.READY_STALE},
            ShadowDraftReasonCode.SETTINGS_NOT_USABLE,
        ),
        (
            {"include_settings": False},
            ShadowDraftReasonCode.SETTINGS_NOT_USABLE,
        ),
        (
            {"knowledge_readiness": ControlPlaneKindReadiness.READY_STALE},
            ShadowDraftReasonCode.KNOWLEDGE_NOT_USABLE,
        ),
        (
            {"include_knowledge": False},
            ShadowDraftReasonCode.KNOWLEDGE_NOT_USABLE,
        ),
        (
            {"include_live": False},
            ShadowDraftReasonCode.LIVE_FACTS_NOT_USABLE,
        ),
        (
            {"extra_reasons": (RuntimeContextReason.HISTORY_UNAVAILABLE,)},
            ShadowDraftReasonCode.CONTEXT_NOT_READY,
        ),
    ],
)
def test_allow_under_lock_still_denies_other_blockers(
    kwargs: dict[str, Any],
    expected: ShadowDraftReasonCode,
) -> None:
    port = _FakePort()
    service = ShadowDraftGenerationService(
        port=port,
        shadow_feature_enabled=True,
        allow_under_emergency_lock=True,
    )
    build = _locked_ready_sources_build(**kwargs)
    gate = evaluate_shadow_draft_gate_from_build(
        build,
        provider_configured=True,
        shadow_feature_enabled=True,
        allow_under_emergency_lock=True,
    )
    assert gate.allowed is False
    assert expected in gate.deny_reasons or gate.reason_code is expected
    reply = service.generate_from_build(build)
    assert reply.disposition is ShadowDraftDisposition.DENIED
    assert port.calls == []


def test_shadow_allowed_under_lock_outbound_and_delivery_still_impossible() -> None:
    settings = Settings(bot_mode=BotMode.OFF, emergency_lock=True)
    assert (
        is_automatic_outbound_allowed(settings, OutboundAction.SEND_MESSAGE) is False
    )

    port = _FakePort("SECRET_SHADOW_TEXT_NEVER_DELIVER")
    service = ShadowDraftGenerationService(
        port=port,
        shadow_feature_enabled=True,
        allow_under_emergency_lock=True,
    )
    reply = service.generate_from_build(_locked_ready_sources_build())
    assert reply.text == "SECRET_SHADOW_TEXT_NEVER_DELIVER"

    # Service must not import delivery / CRM / booking writers.
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
        "app.repositories.reply_plans",
        "app.models.outbox",
        "app.services.booking_flow",
        "app.services.amocrm_adapter",
        "app.services.amocrm_mirror",
    }
    assert imported.isdisjoint(forbidden)

    hook_source = (
        _REPO / "app" / "services" / "shadow_draft_ingress_hook.py"
    ).read_text(encoding="utf-8")
    # Docstring may mention ReplyPlan as a negative; imports/calls must stay clean.
    tree = ast.parse(hook_source)
    hook_imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            hook_imports.add(node.module)
        if isinstance(node, ast.Import):
            for alias in node.names:
                hook_imports.add(alias.name)
    assert hook_imports.isdisjoint(forbidden)
    assert "insert_synthetic_outbound" not in hook_source
    assert "create_client_reply_plan" not in hook_source
    assert "create_internal_draft_outbox" not in hook_source

    # Ingress result carries conversation_id but never shadow text fields.
    result = IngressProcessResult(
        event_id=uuid4(),
        status="PROCESSED",
        duplicate_business=False,
        inbox_id=uuid4(),
        outbox_id=uuid4(),
        conversation_id=uuid4(),
    )
    assert not hasattr(result, "shadow_text")
    assert "SECRET_SHADOW" not in repr(result)


def test_strict_gate_unchanged_without_override() -> None:
    ctx = _context(emergency_lock=False)
    ok = evaluate_shadow_draft_gate(
        context=ctx,
        generation_allowed=True,
        provider_configured=True,
        shadow_feature_enabled=True,
        allow_under_emergency_lock=False,
    )
    assert ok.allowed is True

    locked = _context(emergency_lock=True)
    denied = evaluate_shadow_draft_gate(
        context=locked,
        generation_allowed=False,
        provider_configured=True,
        shadow_feature_enabled=True,
        readiness=RuntimeContextReadiness.NOT_READY,
        allow_under_emergency_lock=False,
        build_reasons=(
            RuntimeContextReason.EMERGENCY_LOCK_ACTIVE,
            RuntimeContextReason.GENERATION_DISABLED_STAGE,
        ),
    )
    assert denied.allowed is False
    assert ShadowDraftReasonCode.EMERGENCY_LOCK in denied.deny_reasons


@pytest.mark.asyncio
async def test_ingress_hook_calls_builder_and_generate_once() -> None:
    conversation_id = uuid4()
    port = _FakePort("hook-ok")
    service = ShadowDraftGenerationService(
        port=port,
        shadow_feature_enabled=True,
        allow_under_emergency_lock=True,
    )
    build = _locked_ready_sources_build()
    builder = MagicMock()
    builder.build_for_conversation = AsyncMock(return_value=build)

    reply = await run_shadow_draft_after_client_inbound(
        conversation_id=conversation_id,
        builder=builder,
        service=service,
    )
    assert reply is not None
    assert reply.disposition is ShadowDraftDisposition.REPLY
    builder.build_for_conversation.assert_awaited_once_with(conversation_id)
    assert len(port.calls) == 1


@pytest.mark.asyncio
async def test_ingress_hook_provider_exception_is_fail_soft() -> None:
    from app.core.yandex_gpt_http import YandexGptHttpError

    conversation_id = uuid4()
    port = _FakePort()
    port.error = YandexGptHttpError("TIMEOUT")
    service = ShadowDraftGenerationService(
        port=port,
        shadow_feature_enabled=True,
        allow_under_emergency_lock=True,
    )
    builder = MagicMock()
    builder.build_for_conversation = AsyncMock(
        return_value=_locked_ready_sources_build()
    )

    reply = await run_shadow_draft_after_client_inbound(
        conversation_id=conversation_id,
        builder=builder,
        service=service,
    )
    assert reply is not None
    assert reply.disposition is ShadowDraftDisposition.PROVIDER_ERROR


@pytest.mark.asyncio
async def test_ingress_hook_builder_exception_returns_none() -> None:
    service = ShadowDraftGenerationService(
        port=_FakePort(),
        shadow_feature_enabled=True,
        allow_under_emergency_lock=True,
    )
    builder = MagicMock()
    builder.build_for_conversation = AsyncMock(side_effect=RuntimeError("boom"))

    reply = await run_shadow_draft_after_client_inbound(
        conversation_id=uuid4(),
        builder=builder,
        service=service,
    )
    assert reply is None


def test_worker_ingress_tick_wires_shadow_after_lease() -> None:
    source = inspect.getsource(worker_runtime_mod.build_default_loop_specs)
    assert "run_shadow_draft_after_client_inbound" in source
    assert "shadow_feature_enabled" in source
    assert "duplicate_business" in source
    assert "conversation_id" in source
    # Must not hold lease: hook after process_claimed assignment.
    assert "result = await ingress.process_claimed" in source
    assert source.index("process_claimed") < source.index(
        "run_shadow_draft_after_client_inbound"
    )


def _patch_worker_constructors(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HandoffExpiryWorker",
        "ReplyPlanWorker",
        "OutboundArbiter",
        "OutboundWorker",
        "AmoCrmMirrorWorker",
        "AmocrmChatProjectionWorker",
        "SelfBookingCreateExecutionWorker",
        "TeyaRequestOrchestratorWorker",
        "TeyaRequestReconciliationWorker",
        "BookingMethodAnalyticsWorker",
        "AcquisitionSourceAnalyticsWorker",
        "AmoCrmCrmOauthLifecycleWorker",
        "ControlPlaneSnapshotWorker",
        "CrmRestMirrorAdapter",
        "ControlPlaneSnapshotService",
        "RuntimeContextBuilder",
        "LiveFactsHttpClient",
    ):
        monkeypatch.setattr(
            worker_runtime_mod, name, MagicMock(return_value=MagicMock())
        )
    monkeypatch.setattr(
        worker_runtime_mod,
        "build_booking_flow_for_worker",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        worker_runtime_mod,
        "build_ephemeral_pii_store_from_env",
        MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        worker_runtime_mod,
        "build_booking_s2s_config",
        MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        worker_runtime_mod,
        "build_teya_request_crm_service",
        MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        worker_runtime_mod, "build_text_generation_port", lambda *a, **k: None
    )


def _worker_settings() -> Settings:
    return Settings(
        bot_mode=BotMode.OFF,
        emergency_lock=True,
        database_url="postgresql+asyncpg://bot:x@127.0.0.1:5432/bot_tv",
    )


@pytest.mark.asyncio
async def test_worker_ingress_tick_invokes_hook_once_for_new_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = uuid4()
    inbox_id = uuid4()
    calls: list[Any] = []

    class _FakeIngress:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._done = False

        async def claim_one(self) -> object | None:
            if self._done:
                return None
            self._done = True
            return object()

        async def process_claimed(self, claim: object) -> IngressProcessResult:
            return IngressProcessResult(
                event_id=uuid4(),
                status="PROCESSED",
                duplicate_business=False,
                inbox_id=inbox_id,
                outbox_id=uuid4(),
                conversation_id=conversation_id,
            )

    async def _capture_hook(**kwargs: Any) -> None:
        calls.append(kwargs["conversation_id"])

    _patch_worker_constructors(monkeypatch)
    monkeypatch.setattr(worker_runtime_mod, "IngressWorker", _FakeIngress)
    monkeypatch.setattr(
        worker_runtime_mod,
        "run_shadow_draft_after_client_inbound",
        _capture_hook,
    )
    monkeypatch.setattr(
        worker_runtime_mod,
        "build_shadow_draft_generation_service",
        lambda **kwargs: ShadowDraftGenerationService(
            port=_FakePort(),
            shadow_feature_enabled=True,
            allow_under_emergency_lock=False,
        ),
    )

    specs = worker_runtime_mod.build_default_loop_specs(
        settings=_worker_settings(),
        session_factory=MagicMock(),
        worker_id="test-worker",
    )
    await next(s for s in specs if s.name == "ingress").tick()
    assert calls == [conversation_id]


@pytest.mark.asyncio
async def test_worker_ingress_tick_feature_off_skips_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook_calls: list[Any] = []

    class _FakeIngress:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._done = False

        async def claim_one(self) -> object | None:
            if self._done:
                return None
            self._done = True
            return object()

        async def process_claimed(self, claim: object) -> IngressProcessResult:
            return IngressProcessResult(
                event_id=uuid4(),
                status="PROCESSED",
                duplicate_business=False,
                inbox_id=uuid4(),
                outbox_id=uuid4(),
                conversation_id=uuid4(),
            )

    async def _hook(**kwargs: Any) -> None:
        hook_calls.append(kwargs)

    _patch_worker_constructors(monkeypatch)
    monkeypatch.setattr(worker_runtime_mod, "IngressWorker", _FakeIngress)
    monkeypatch.setattr(
        worker_runtime_mod,
        "run_shadow_draft_after_client_inbound",
        _hook,
    )
    monkeypatch.setattr(
        worker_runtime_mod,
        "build_shadow_draft_generation_service",
        lambda **kwargs: ShadowDraftGenerationService(
            port=_FakePort(),
            shadow_feature_enabled=False,
            allow_under_emergency_lock=True,
        ),
    )

    specs = worker_runtime_mod.build_default_loop_specs(
        settings=_worker_settings(),
        session_factory=MagicMock(),
        worker_id="test-worker",
    )
    await next(s for s in specs if s.name == "ingress").tick()
    assert hook_calls == []


@pytest.mark.asyncio
async def test_worker_shadow_exception_keeps_ingress_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed = {"ok": False}

    class _FakeIngress:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._done = False

        async def claim_one(self) -> object | None:
            if self._done:
                return None
            self._done = True
            return object()

        async def process_claimed(self, claim: object) -> IngressProcessResult:
            processed["ok"] = True
            return IngressProcessResult(
                event_id=uuid4(),
                status="PROCESSED",
                duplicate_business=False,
                inbox_id=uuid4(),
                outbox_id=uuid4(),
                conversation_id=uuid4(),
            )

    async def _boom(**kwargs: Any) -> None:
        raise RuntimeError("shadow exploded")

    _patch_worker_constructors(monkeypatch)
    monkeypatch.setattr(worker_runtime_mod, "IngressWorker", _FakeIngress)
    monkeypatch.setattr(
        worker_runtime_mod, "run_shadow_draft_after_client_inbound", _boom
    )
    monkeypatch.setattr(
        worker_runtime_mod,
        "build_shadow_draft_generation_service",
        lambda **kwargs: ShadowDraftGenerationService(
            port=_FakePort(),
            shadow_feature_enabled=True,
            allow_under_emergency_lock=True,
        ),
    )

    specs = worker_runtime_mod.build_default_loop_specs(
        settings=_worker_settings(),
        session_factory=MagicMock(),
        worker_id="test-worker",
    )
    await next(s for s in specs if s.name == "ingress").tick()
    assert processed["ok"] is True


def test_env_example_documents_allow_under_lock_default_false() -> None:
    text = (_REPO / ".env.example").read_text(encoding="utf-8")
    assert "YANDEX_SHADOW_ALLOW_UNDER_EMERGENCY_LOCK=false" in text


def test_build_service_reads_allow_under_lock_flag() -> None:
    service = build_shadow_draft_generation_service(
        port=_FakePort(),
        environ={
            "YANDEX_SHADOW_DRAFT_ENABLED": "true",
            "YANDEX_SHADOW_ALLOW_UNDER_EMERGENCY_LOCK": "true",
        },
    )
    assert service.shadow_feature_enabled is True
    assert service.allow_under_emergency_lock is True
    assert is_yandex_shadow_draft_enabled(
        {"YANDEX_SHADOW_DRAFT_ENABLED": "true"}
    )
