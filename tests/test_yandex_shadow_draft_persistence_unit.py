"""Unit tests for Yandex shadow draft QA persistence wiring."""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.shadow_draft_types import (
    ShadowDraftDisposition,
    ShadowDraftProvenanceSummary,
    ShadowDraftReasonCode,
    ShadowDraftReply,
)
from app.services.ingress import IngressProcessResult
from app.services.shadow_draft_generation import ShadowDraftGenerationService
from app.services import shadow_draft_ingress_hook as hook_mod
from app.services.shadow_draft_ingress_hook import (
    run_shadow_draft_after_client_inbound,
)
from app.services import worker_runtime as worker_runtime_mod
from tests.test_shadow_draft_generation_unit import _FakePort, _context
from tests.test_shadow_draft_ingress_wiring_unit import (
    _locked_ready_sources_build,
    _patch_worker_constructors,
    _worker_settings,
)

_REPO = Path(__file__).resolve().parents[1]
_SECRET_DRAFT = "SECRET_GENERATED_SHADOW_NEVER_LOG"
_SECRET_CLIENT = "SECRET_CLIENT_INBOUND_NEVER_LOG"


def _reply(
    *,
    disposition: ShadowDraftDisposition,
    text: str | None,
    reason: ShadowDraftReasonCode = ShadowDraftReasonCode.OK,
) -> ShadowDraftReply:
    return ShadowDraftReply(
        text=text,
        disposition=disposition,
        handoff_required=disposition
        in {
            ShadowDraftDisposition.HANDOFF,
            ShadowDraftDisposition.PROVIDER_ERROR,
            ShadowDraftDisposition.DENIED,
        }
        and reason
        in {
            ShadowDraftReasonCode.HANDOFF_ACTIVE,
            ShadowDraftReasonCode.PROVIDER_TIMEOUT,
            ShadowDraftReasonCode.EMERGENCY_LOCK,
            ShadowDraftReasonCode.OK,
        },
        reason_code=reason,
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
            "provider_transport_called": disposition
            is not ShadowDraftDisposition.DENIED,
        },
    )


@pytest.mark.asyncio
async def test_hook_persists_all_dispositions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted: list[dict[str, Any]] = []

    async def _capture_persist(**kwargs: Any) -> None:
        persisted.append(
            {
                "inbox_message_id": kwargs["inbox_message_id"],
                "conversation_id": kwargs["conversation_id"],
                "disposition": kwargs["reply"].disposition,
                "text": kwargs["reply"].text,
            }
        )

    monkeypatch.setattr(hook_mod, "_persist_shadow_draft_fail_soft", _capture_persist)

    cases = (
        (ShadowDraftDisposition.REPLY, _SECRET_DRAFT, ShadowDraftReasonCode.OK),
        (
            ShadowDraftDisposition.DENIED,
            None,
            ShadowDraftReasonCode.EMERGENCY_LOCK,
        ),
        (ShadowDraftDisposition.HANDOFF, "передам менеджеру", ShadowDraftReasonCode.OK),
        (
            ShadowDraftDisposition.PROVIDER_ERROR,
            None,
            ShadowDraftReasonCode.PROVIDER_TIMEOUT,
        ),
    )
    for disposition, text, reason in cases:
        conversation_id = uuid4()
        inbox_id = uuid4()
        build = _locked_ready_sources_build()
        builder = MagicMock()
        builder.build_for_conversation = AsyncMock(return_value=build)
        service = MagicMock()
        service.generate_from_build = MagicMock(
            return_value=_reply(disposition=disposition, text=text, reason=reason)
        )
        reply = await run_shadow_draft_after_client_inbound(
            conversation_id=conversation_id,
            inbox_message_id=inbox_id,
            session_factory=MagicMock(),
            builder=builder,
            service=service,
        )
        assert reply is not None
        assert reply.disposition is disposition

    assert len(persisted) == 4
    assert {item["disposition"] for item in persisted} == {
        ShadowDraftDisposition.REPLY,
        ShadowDraftDisposition.DENIED,
        ShadowDraftDisposition.HANDOFF,
        ShadowDraftDisposition.PROVIDER_ERROR,
    }
    assert persisted[0]["text"] == _SECRET_DRAFT


@pytest.mark.asyncio
async def test_hook_persist_failure_returns_reply(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _boom(**kwargs: Any) -> None:
        raise RuntimeError("db down")

    # Fail-soft wrapper must catch — replace insert path via repo through wrapper.
    async def _failing_persist(**kwargs: Any) -> None:
        try:
            raise RuntimeError("db down")
        except Exception as exc:
            hook_mod._log_hook("persist_failed", error_type=type(exc).__name__)

    monkeypatch.setattr(hook_mod, "_persist_shadow_draft_fail_soft", _failing_persist)

    conversation_id = uuid4()
    inbox_id = uuid4()
    expected = _reply(
        disposition=ShadowDraftDisposition.REPLY,
        text=_SECRET_DRAFT,
    )
    builder = MagicMock()
    builder.build_for_conversation = AsyncMock(
        return_value=_locked_ready_sources_build()
    )
    service = MagicMock()
    service.generate_from_build = MagicMock(return_value=expected)

    with caplog.at_level(logging.INFO, logger=hook_mod.__name__):
        reply = await run_shadow_draft_after_client_inbound(
            conversation_id=conversation_id,
            inbox_message_id=inbox_id,
            session_factory=MagicMock(),
            builder=builder,
            service=service,
        )

    assert reply is expected
    assert reply.text == _SECRET_DRAFT
    assert any("persist_failed" in r.getMessage() for r in caplog.records)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert _SECRET_DRAFT not in joined
    assert _SECRET_CLIENT not in joined


@pytest.mark.asyncio
async def test_hook_real_persist_wrapper_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _BoomScope:
        async def __aenter__(self) -> Any:
            raise RuntimeError("session boom")

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        hook_mod,
        "session_scope",
        lambda _factory: _BoomScope(),
    )

    expected = _reply(disposition=ShadowDraftDisposition.REPLY, text=_SECRET_DRAFT)
    builder = MagicMock()
    builder.build_for_conversation = AsyncMock(
        return_value=_locked_ready_sources_build()
    )
    service = MagicMock()
    service.generate_from_build = MagicMock(return_value=expected)

    with caplog.at_level(logging.INFO, logger=hook_mod.__name__):
        reply = await run_shadow_draft_after_client_inbound(
            conversation_id=uuid4(),
            inbox_message_id=uuid4(),
            session_factory=MagicMock(),
            builder=builder,
            service=service,
        )

    assert reply is expected
    assert any(
        "persist_failed" in r.getMessage() and "RuntimeError" in r.getMessage()
        for r in caplog.records
    )
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert _SECRET_DRAFT not in joined


@pytest.mark.asyncio
async def test_worker_passes_inbox_id_and_skips_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = uuid4()
    inbox_id = uuid4()
    calls: list[dict[str, Any]] = []

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
                outbox_id=None,
                conversation_id=conversation_id,
            )

    async def _capture_hook(**kwargs: Any) -> None:
        calls.append(kwargs)

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
    session_factory = MagicMock(name="session_factory")
    specs = worker_runtime_mod.build_default_loop_specs(
        settings=_worker_settings(),
        session_factory=session_factory,
        worker_id="test-worker",
    )
    await next(s for s in specs if s.name == "ingress").tick()
    assert len(calls) == 1
    assert calls[0]["conversation_id"] == conversation_id
    assert calls[0]["inbox_message_id"] == inbox_id
    assert calls[0]["session_factory"] is session_factory


@pytest.mark.asyncio
async def test_worker_skips_duplicate_and_missing_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook_calls: list[Any] = []

    class _DupIngress:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._n = 0

        async def claim_one(self) -> object | None:
            self._n += 1
            if self._n > 3:
                return None
            return object()

        async def process_claimed(self, claim: object) -> IngressProcessResult:
            if self._n == 1:
                return IngressProcessResult(
                    event_id=uuid4(),
                    status="PROCESSED",
                    duplicate_business=True,
                    inbox_id=uuid4(),
                    outbox_id=None,
                    conversation_id=uuid4(),
                )
            if self._n == 2:
                return IngressProcessResult(
                    event_id=uuid4(),
                    status="PROCESSED",
                    duplicate_business=False,
                    inbox_id=None,
                    outbox_id=None,
                    conversation_id=uuid4(),
                )
            return IngressProcessResult(
                event_id=uuid4(),
                status="PROCESSED",
                duplicate_business=False,
                inbox_id=uuid4(),
                outbox_id=None,
                conversation_id=None,
            )

    async def _hook(**kwargs: Any) -> None:
        hook_calls.append(kwargs)

    _patch_worker_constructors(monkeypatch)
    monkeypatch.setattr(worker_runtime_mod, "IngressWorker", _DupIngress)
    monkeypatch.setattr(
        worker_runtime_mod, "run_shadow_draft_after_client_inbound", _hook
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
    assert hook_calls == []


def test_model_and_reply_repr_hide_generated_text() -> None:
    reply = _reply(
        disposition=ShadowDraftDisposition.REPLY,
        text=_SECRET_DRAFT,
    )
    assert _SECRET_DRAFT not in repr(reply)
    assert "text_len=" in repr(reply)

    from app.models.yandex_shadow_draft import YandexShadowDraft

    row = YandexShadowDraft(
        id=uuid4(),
        inbox_message_id=uuid4(),
        conversation_id=uuid4(),
        disposition=ShadowDraftDisposition.REPLY.value,
        reason_code=ShadowDraftReasonCode.OK.value,
        handoff_required=False,
        generated_text=_SECRET_DRAFT,
        provenance_json={"settingsPublicationId": "x"},
        generation_metadata_json={"provider": "yandex"},
    )
    rendered = repr(row)
    assert _SECRET_DRAFT not in rendered
    assert "generated_text=<redacted>" in rendered
    assert "provenance_json=<redacted>" in rendered


def test_persistence_and_hook_have_no_outbound_crm_booking_imports() -> None:
    forbidden = {
        "app.services.reply_outbound",
        "app.services.outbound_arbiter",
        "app.services.synthetic_outbound",
        "app.repositories.messages",
        "app.repositories.reply_plans",
        "app.repositories.outbound",
        "app.models.outbox",
        "app.models.reply_plan",
        "app.services.booking_flow",
        "app.services.amocrm_adapter",
        "app.services.amocrm_mirror",
        "app.services.amocrm_chat_projection",
    }
    for rel in (
        "app/services/shadow_draft_ingress_hook.py",
        "app/repositories/yandex_shadow_drafts.py",
        "app/models/yandex_shadow_draft.py",
        "app/services/shadow_draft_generation.py",
    ):
        source = (_REPO / rel).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
        assert imported.isdisjoint(forbidden), rel
        for needle in (
            "create_client_reply_plan",
            "create_internal_draft_outbox",
            "insert_synthetic_outbound",
        ):
            assert f"def {needle}" not in source
            assert f".{needle}(" not in source


def test_generation_service_still_db_free() -> None:
    source = (
        _REPO / "app" / "services" / "shadow_draft_generation.py"
    ).read_text(encoding="utf-8")
    assert "yandex_shadow_drafts" not in source
    assert "YandexShadowDraft" not in source
    assert "session_factory" not in source
    assert "insert_if_absent" not in source


def test_shadow_draft_migration_is_in_docker_build_context() -> None:
    from tests.docker_runtime_allowlist import (
        AI_DIALOGUE_02_DOCKER_RUNTIME_PATHS,
        dockerignore_lines,
        is_included_in_docker_build_context,
    )

    rel = "alembic/versions/20260904_39_shadow_drafts.py"
    assert rel in AI_DIALOGUE_02_DOCKER_RUNTIME_PATHS
    assert (_REPO / rel).is_file()
    lines = dockerignore_lines(_REPO)
    assert is_included_in_docker_build_context(rel, lines) is True
    assert f"!{rel}" in lines
