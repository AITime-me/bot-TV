"""Post-ingress shadow draft hook (internal only).

Runs AFTER durable client inbound + ingress lease completion so Yandex latency
never holds a DB transaction or ingress lease. Fail-soft: errors never change
ingress outcomes. Generated text is never written to ReplyPlan / outbox / CRM /
booking / client delivery. QA persistence uses a dedicated table only.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.shadow_draft_types import ShadowDraftReply
from app.db.session import session_scope
from app.repositories import yandex_shadow_drafts as shadow_draft_repo
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


async def _persist_shadow_draft_fail_soft(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    conversation_id: UUID,
    inbox_message_id: UUID,
    reply: ShadowDraftReply,
) -> None:
    """Best-effort QA store. Never raises; never logs draft/client text."""

    try:
        async with session_scope(session_factory) as session:
            await shadow_draft_repo.insert_if_absent(
                session,
                row_id=uuid4(),
                inbox_message_id=inbox_message_id,
                conversation_id=conversation_id,
                disposition=reply.disposition.value,
                reason_code=reply.reason_code.value,
                handoff_required=reply.handoff_required,
                generated_text=reply.text,
                provenance_json=reply.provenance.as_dict(),
                generation_metadata_json=dict(reply.generation_metadata),
            )
    except Exception as exc:
        _log_hook("persist_failed", error_type=type(exc).__name__)


async def run_shadow_draft_after_client_inbound(
    *,
    conversation_id: UUID,
    inbox_message_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    builder: RuntimeContextBuilder,
    service: ShadowDraftGenerationService,
) -> ShadowDraftReply | None:
    """Build runtime context, generate shadow draft, persist QA copy.

    Observability uses ``diagnostic_summary`` only (no raw client/generated text).
    Persistence failures never change ingress outcomes.
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

    await _persist_shadow_draft_fail_soft(
        session_factory=session_factory,
        conversation_id=conversation_id,
        inbox_message_id=inbox_message_id,
        reply=reply,
    )
    return reply
