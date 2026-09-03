"""Post-ingress shadow draft hook (internal only).

Runs AFTER durable client inbound + ingress lease completion so Yandex latency
never holds a DB transaction or ingress lease. Fail-soft: errors never change
ingress outcomes. Generated text is never written to ReplyPlan / outbox / CRM /
booking / client delivery.
"""

from __future__ import annotations

import logging
from uuid import UUID

from app.core.shadow_draft_types import ShadowDraftReply
from app.services.runtime_context_builder import RuntimeContextBuilder
from app.services.shadow_draft_generation import ShadowDraftGenerationService

logger = logging.getLogger(__name__)


def _log_hook(event: str, **fields: object) -> None:
    try:
        extras = " ".join(f"{key}={value!s}" for key, value in fields.items())
        if extras:
            logger.info("shadow_draft event=%s %s", event, extras)
        else:
            logger.info("shadow_draft event=%s", event)
    except Exception:
        return


async def run_shadow_draft_after_client_inbound(
    *,
    conversation_id: UUID,
    builder: RuntimeContextBuilder,
    service: ShadowDraftGenerationService,
) -> ShadowDraftReply | None:
    """Build runtime context and generate an internal shadow draft.

    Observability uses ``diagnostic_summary`` only (no raw client/generated text).
    """

    try:
        build = await builder.build_for_conversation(conversation_id)
    except Exception as exc:
        _log_hook(
            "ingress_hook_build_error",
            error_type=type(exc).__name__,
        )
        return None

    try:
        reply = service.generate_from_build(build)
    except Exception as exc:
        _log_hook(
            "ingress_hook_generate_error",
            error_type=type(exc).__name__,
        )
        return None

    try:
        summary = reply.diagnostic_summary()
        _log_hook(
            "ingress_hook",
            disposition=summary.get("disposition"),
            reason=summary.get("reasonCode"),
            handoff_required=summary.get("handoffRequired"),
            has_text=summary.get("hasText"),
            text_len=summary.get("textLen"),
            provider_transport_called=(
                (summary.get("generationMetadata") or {}).get(
                    "provider_transport_called"
                )
                if isinstance(summary.get("generationMetadata"), dict)
                else None
            ),
        )
    except Exception:
        _log_hook("ingress_hook_log_error")

    return reply
