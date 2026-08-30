"""Shadow draft generation orchestration (AI-DIALOGUE-02).

RuntimeContext → gate → prompt compiler → TextGenerationPort → ShadowDraftReply.

No outbox, outbound, CRM, booking, or durable persistence. Migration-free:
returns an in-process typed result for quality evaluation only.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

from app.core.runtime_context_types import (
    RuntimeContextBuildResult,
    TeyaRuntimeContext,
)
from app.core.shadow_draft_gate import (
    ShadowDraftGateDecision,
    evaluate_shadow_draft_gate,
    evaluate_shadow_draft_gate_from_build,
)
from app.core.shadow_draft_prompt import compile_shadow_draft_messages
from app.core.shadow_draft_types import (
    ShadowDraftDisposition,
    ShadowDraftProvenanceSummary,
    ShadowDraftReasonCode,
    ShadowDraftReply,
)
from app.core.text_generation_port import TextGenerationPort
from app.core.yandex_gpt_http import YandexGptHttpError
from app.core.yandex_llm_config import YandexLlmConfigError

logger = logging.getLogger(__name__)

_SHADOW_FEATURE_ENV = "YANDEX_SHADOW_DRAFT_ENABLED"


def is_yandex_shadow_draft_enabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    source = os.environ if environ is None else environ
    raw = source.get(_SHADOW_FEATURE_ENV, "false")
    if type(raw) is not str:
        return False
    return raw.strip().lower() == "true"


def _provenance_from_context(
    context: TeyaRuntimeContext | None,
) -> ShadowDraftProvenanceSummary:
    if context is None:
        return ShadowDraftProvenanceSummary(
            settings_publication_id=None,
            settings_checksum=None,
            knowledge_publication_id=None,
            knowledge_checksum=None,
            selected_knowledge_keys=(),
            live_facts_service_count=None,
            live_facts_master_count=None,
            history_turn_count=None,
        )
    p = context.provenance
    return ShadowDraftProvenanceSummary(
        settings_publication_id=p.settings_publication_id,
        settings_checksum=p.settings_checksum,
        knowledge_publication_id=p.knowledge_publication_id,
        knowledge_checksum=p.knowledge_checksum,
        selected_knowledge_keys=p.selected_knowledge_keys,
        live_facts_service_count=p.live_facts_service_count,
        live_facts_master_count=p.live_facts_master_count,
        history_turn_count=p.history_turn_count,
    )


def _map_provider_error(exc: BaseException) -> ShadowDraftReasonCode:
    if isinstance(exc, YandexGptHttpError):
        mapping = {
            "TIMEOUT": ShadowDraftReasonCode.PROVIDER_TIMEOUT,
            "TRANSPORT_ERROR": ShadowDraftReasonCode.PROVIDER_TRANSPORT_ERROR,
            "REMOTE_REJECTED": ShadowDraftReasonCode.PROVIDER_REMOTE_REJECTED,
            "RESPONSE_INVALID": ShadowDraftReasonCode.PROVIDER_RESPONSE_INVALID,
            "RESPONSE_TOO_LARGE": ShadowDraftReasonCode.PROVIDER_RESPONSE_TOO_LARGE,
            "EMPTY_COMPLETION": ShadowDraftReasonCode.PROVIDER_EMPTY,
            "CONFIG_INVALID": ShadowDraftReasonCode.PROVIDER_CONFIG_INVALID,
            "DISABLED": ShadowDraftReasonCode.PROVIDER_NOT_CONFIGURED,
        }
        return mapping.get(exc.code, ShadowDraftReasonCode.PROVIDER_ERROR)
    if isinstance(exc, YandexLlmConfigError):
        return ShadowDraftReasonCode.PROVIDER_CONFIG_INVALID
    if isinstance(exc, ValueError):
        return ShadowDraftReasonCode.PROVIDER_RESPONSE_INVALID
    return ShadowDraftReasonCode.PROVIDER_ERROR


def _denied(
    *,
    reason: ShadowDraftReasonCode,
    context: TeyaRuntimeContext | None,
    metadata: Mapping[str, object] | None = None,
) -> ShadowDraftReply:
    handoff = reason in {
        ShadowDraftReasonCode.HANDOFF_ACTIVE,
        ShadowDraftReasonCode.MANAGER_TAKEOVER,
        ShadowDraftReasonCode.LIVE_FACTS_NOT_USABLE,
        ShadowDraftReasonCode.KNOWLEDGE_NOT_USABLE,
        ShadowDraftReasonCode.SETTINGS_NOT_USABLE,
        ShadowDraftReasonCode.CONTEXT_NOT_READY,
    }
    meta = {
        "provider": "yandex",
        "shadow": True,
        "error_code": reason.value,
        "model_configured": False,
    }
    if metadata:
        meta.update(dict(metadata))
    return ShadowDraftReply(
        text=None,
        disposition=ShadowDraftDisposition.DENIED,
        handoff_required=handoff,
        reason_code=reason,
        provenance=_provenance_from_context(context),
        generation_metadata=meta,
    )


def _log_safe(event: str, reason: str) -> None:
    try:
        logger.info("shadow_draft event=%s reason=%s", event, reason)
    except Exception:
        return


@dataclass(frozen=True, slots=True)
class ShadowDraftGenerationService:
    """Pure in-process shadow generation. No DB writes. No outbound."""

    port: TextGenerationPort | None
    shadow_feature_enabled: bool = False

    @property
    def provider_configured(self) -> bool:
        return self.port is not None

    def generate_from_context(
        self,
        context: TeyaRuntimeContext,
        *,
        generation_allowed: bool,
        readiness=None,
    ) -> ShadowDraftReply:
        gate = evaluate_shadow_draft_gate(
            context=context,
            generation_allowed=generation_allowed,
            provider_configured=self.provider_configured,
            shadow_feature_enabled=self.shadow_feature_enabled,
            readiness=readiness,
        )
        return self._run(context=context, gate=gate)

    def generate_from_build(
        self,
        build: RuntimeContextBuildResult,
    ) -> ShadowDraftReply:
        gate = evaluate_shadow_draft_gate_from_build(
            build,
            provider_configured=self.provider_configured,
            shadow_feature_enabled=self.shadow_feature_enabled,
        )
        return self._run(context=build.context, gate=gate)

    def _run(
        self,
        *,
        context: TeyaRuntimeContext | None,
        gate: ShadowDraftGateDecision,
    ) -> ShadowDraftReply:
        if not gate.allowed:
            _log_safe("denied", gate.reason_code.value)
            return _denied(reason=gate.reason_code, context=context)

        assert context is not None
        assert self.port is not None
        try:
            messages = compile_shadow_draft_messages(context)
        except ValueError as exc:
            code = str(exc) if str(exc) in ShadowDraftReasonCode.__members__ else (
                ShadowDraftReasonCode.CONTEXT_NOT_READY.value
            )
            reason = ShadowDraftReasonCode(code)
            _log_safe("compile_denied", reason.value)
            return _denied(reason=reason, context=context)

        try:
            result = self.port.generate(messages)
        except Exception as exc:
            reason = _map_provider_error(exc)
            _log_safe("provider_error", reason.value)
            return ShadowDraftReply(
                text=None,
                disposition=ShadowDraftDisposition.PROVIDER_ERROR,
                handoff_required=True,
                reason_code=reason,
                provenance=_provenance_from_context(context),
                generation_metadata={
                    "provider": "yandex",
                    "shadow": True,
                    "error_code": reason.value,
                    "message_count": len(messages),
                    "model_configured": True,
                },
            )

        text = result.text.strip()
        lowered = text.casefold()
        handoff_required = any(
            marker in lowered
            for marker in (
                "handoff",
                "передам менеджеру",
                "передаю менеджеру",
                "нужен менеджер",
            )
        )
        disposition = (
            ShadowDraftDisposition.HANDOFF
            if handoff_required
            else ShadowDraftDisposition.REPLY
        )
        _log_safe("ok", ShadowDraftReasonCode.OK.value)
        return ShadowDraftReply(
            text=text,
            disposition=disposition,
            handoff_required=handoff_required,
            reason_code=ShadowDraftReasonCode.OK,
            provenance=_provenance_from_context(context),
            generation_metadata={
                "provider": "yandex",
                "shadow": True,
                "text_len": len(text),
                "message_count": len(messages),
                "model_configured": True,
            },
        )


def build_shadow_draft_generation_service(
    *,
    port: TextGenerationPort | None,
    environ: Mapping[str, str] | None = None,
) -> ShadowDraftGenerationService:
    return ShadowDraftGenerationService(
        port=port,
        shadow_feature_enabled=is_yandex_shadow_draft_enabled(environ),
    )
