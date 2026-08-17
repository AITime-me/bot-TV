"""Conversation↔canonical identity glue orchestrator (IR-1).

Wraps IdentityResolutionService. No CRM HTTP, webhook auto-resolve, or chat
binding mutation. Matching logic stays in IdentityResolutionService.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity_glue import (
    ApproveIdentityReviewOutcome,
    ApproveIdentityReviewResult,
    ConversationIdentityGlueOutcome,
    ConversationIdentityGlueResult,
    IdentityReviewReasonCode,
    InspectIdentityReviewsResult,
)
from app.core.identity_resolution import (
    CanonicalIdentityStatus,
    IdentityResolveSignals,
    ResolveIdentityOutcome,
    REASON_EMAIL_ONLY_SECONDARY,
)
from app.repositories import conversations as conversation_repo
from app.repositories import identity_glue as glue_repo
from app.repositories import identity_resolution as identity_repo
from app.services.identity_resolution import IdentityResolutionService

logger = logging.getLogger(__name__)

_ALLOWED_LOG_CODES: frozenset[str] = frozenset(
    {
        "IDENTITY_GLUE_ATTACHED",
        "IDENTITY_GLUE_ALREADY_ATTACHED",
        "IDENTITY_GLUE_REVIEW_OPENED",
        "IDENTITY_GLUE_REVIEW_EXISTS",
        "IDENTITY_GLUE_NOT_FOUND",
        "IDENTITY_GLUE_INVALID_INPUT",
        "IDENTITY_GLUE_REFUSED",
        "IDENTITY_GLUE_APPROVED",
        "IDENTITY_GLUE_INSPECTED",
    }
)


def _log(event: str) -> None:
    if type(event) is not str or event not in _ALLOWED_LOG_CODES:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return


def _as_uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


class ConversationIdentityGlueService:
    """Attach resolved canonical identities to conversations with fail-closed review."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._identity = IdentityResolutionService(session)

    async def resolve_for_conversation(
        self,
        *,
        conversation_id: object,
        signals: IdentityResolveSignals,
    ) -> ConversationIdentityGlueResult:
        cid = _as_uuid(conversation_id)
        if cid is None:
            _log("IDENTITY_GLUE_INVALID_INPUT")
            return ConversationIdentityGlueResult(
                outcome=ConversationIdentityGlueOutcome.INVALID_INPUT,
                error_code="CONVERSATION_ID_INVALID",
            )

        conversation = await conversation_repo.get_by_id_for_update(
            self._session,
            conversation_id=cid,
        )
        if conversation is None:
            _log("IDENTITY_GLUE_REFUSED")
            return ConversationIdentityGlueResult(
                outcome=ConversationIdentityGlueOutcome.REFUSED,
                error_code="CONVERSATION_MISSING",
            )

        resolved = await self._identity.resolve(signals)
        if resolved.outcome is ResolveIdentityOutcome.INVALID_INPUT:
            _log("IDENTITY_GLUE_INVALID_INPUT")
            return ConversationIdentityGlueResult(
                outcome=ConversationIdentityGlueOutcome.INVALID_INPUT,
                error_code="IDENTITY_INVALID_INPUT",
            )
        if resolved.outcome is ResolveIdentityOutcome.NOT_FOUND:
            _log("IDENTITY_GLUE_NOT_FOUND")
            return ConversationIdentityGlueResult(
                outcome=ConversationIdentityGlueOutcome.NOT_FOUND,
                error_code=resolved.reason or "NOT_FOUND",
            )
        if resolved.outcome is ResolveIdentityOutcome.MANUAL_REVIEW_REQUIRED:
            return await self._open_review(
                conversation_id=cid,
                reason_code=IdentityReviewReasonCode.AMBIGUOUS_RESOLVE,
                proposed_canonical_identity_id=None,
            )
        if resolved.outcome is not ResolveIdentityOutcome.RESOLVED:
            _log("IDENTITY_GLUE_REFUSED")
            return ConversationIdentityGlueResult(
                outcome=ConversationIdentityGlueOutcome.REFUSED,
                error_code="IDENTITY_RESOLVE_UNEXPECTED",
            )

        # Email-only must never attach (resolve already returns NOT_FOUND, belt).
        if resolved.reason == REASON_EMAIL_ONLY_SECONDARY:
            _log("IDENTITY_GLUE_NOT_FOUND")
            return ConversationIdentityGlueResult(
                outcome=ConversationIdentityGlueOutcome.NOT_FOUND,
                error_code=REASON_EMAIL_ONLY_SECONDARY,
            )

        assert resolved.canonical_identity_id is not None
        target = resolved.canonical_identity_id
        return await self._attach_or_review(
            conversation=conversation,
            target_canonical_id=target,
        )

    async def approve_review(
        self,
        *,
        review_case_id: object,
        canonical_identity_id: object,
    ) -> ApproveIdentityReviewResult:
        review_id = _as_uuid(review_case_id)
        target = _as_uuid(canonical_identity_id)
        if review_id is None or target is None:
            _log("IDENTITY_GLUE_INVALID_INPUT")
            return ApproveIdentityReviewResult(
                outcome=ApproveIdentityReviewOutcome.INVALID_INPUT,
                error_code="APPROVE_INPUT_INVALID",
            )

        review = await glue_repo.get_review_by_id_for_update(
            self._session,
            review_case_id=review_id,
        )
        if review is None:
            _log("IDENTITY_GLUE_REFUSED")
            return ApproveIdentityReviewResult(
                outcome=ApproveIdentityReviewOutcome.REFUSED,
                error_code="REVIEW_MISSING",
            )
        if review.status != "OPEN":
            if (
                review.status == "RESOLVED"
                and review.resolved_canonical_identity_id is not None
                and uuid.UUID(str(review.resolved_canonical_identity_id)) == target
            ):
                _log("IDENTITY_GLUE_APPROVED")
                return ApproveIdentityReviewResult(
                    outcome=ApproveIdentityReviewOutcome.ALREADY_RESOLVED,
                    review_case_id=review_id,
                    canonical_identity_id=target,
                )
            _log("IDENTITY_GLUE_REFUSED")
            return ApproveIdentityReviewResult(
                outcome=ApproveIdentityReviewOutcome.REFUSED,
                error_code="REVIEW_NOT_OPEN",
            )

        conversation = await conversation_repo.get_by_id_for_update(
            self._session,
            conversation_id=uuid.UUID(str(review.conversation_id)),
        )
        if conversation is None:
            _log("IDENTITY_GLUE_REFUSED")
            return ApproveIdentityReviewResult(
                outcome=ApproveIdentityReviewOutcome.REFUSED,
                error_code="CONVERSATION_MISSING",
            )

        identity = await identity_repo.lock_canonical(
            self._session,
            identity_id=target,
        )
        if identity is None:
            _log("IDENTITY_GLUE_REFUSED")
            return ApproveIdentityReviewResult(
                outcome=ApproveIdentityReviewOutcome.REFUSED,
                error_code="CANONICAL_MISSING",
            )
        if identity.status != CanonicalIdentityStatus.ACTIVE.value:
            await glue_repo.insert_open_review_idempotent(
                self._session,
                conversation_id=uuid.UUID(str(conversation.id)),
                reason_code=IdentityReviewReasonCode.CANONICAL_NOT_ACTIVE,
                proposed_canonical_identity_id=target,
            )
            _log("IDENTITY_GLUE_REFUSED")
            return ApproveIdentityReviewResult(
                outcome=ApproveIdentityReviewOutcome.REFUSED,
                error_code="CANONICAL_NOT_ACTIVE",
                review_case_id=review_id,
            )

        current = conversation.canonical_identity_id
        if current is not None and uuid.UUID(str(current)) != target:
            await glue_repo.insert_open_review_idempotent(
                self._session,
                conversation_id=uuid.UUID(str(conversation.id)),
                reason_code=IdentityReviewReasonCode.CONFLICTING_CANONICAL,
                proposed_canonical_identity_id=target,
            )
            _log("IDENTITY_GLUE_REFUSED")
            return ApproveIdentityReviewResult(
                outcome=ApproveIdentityReviewOutcome.REFUSED,
                error_code="CONFLICTING_CANONICAL",
                review_case_id=review_id,
            )

        if current is None:
            await glue_repo.set_conversation_canonical_identity(
                self._session,
                conversation=conversation,
                canonical_identity_id=target,
            )
        await glue_repo.mark_review_resolved(
            self._session,
            row=review,
            resolved_canonical_identity_id=target,
        )
        _log("IDENTITY_GLUE_APPROVED")
        return ApproveIdentityReviewResult(
            outcome=ApproveIdentityReviewOutcome.APPROVED,
            review_case_id=review_id,
            canonical_identity_id=target,
        )

    async def inspect_open_reviews(
        self,
        *,
        conversation_id: object | None = None,
    ) -> InspectIdentityReviewsResult:
        conv_id: uuid.UUID | None = None
        if conversation_id is not None:
            conv_id = _as_uuid(conversation_id)
            if conv_id is None:
                _log("IDENTITY_GLUE_INVALID_INPUT")
                return InspectIdentityReviewsResult(cases=())
        rows = await glue_repo.list_open_reviews(
            self._session,
            conversation_id=conv_id,
        )
        _log("IDENTITY_GLUE_INSPECTED")
        return InspectIdentityReviewsResult(
            cases=tuple(glue_repo.as_review_record(row) for row in rows)
        )

    async def _attach_or_review(
        self,
        *,
        conversation,
        target_canonical_id: uuid.UUID,
    ) -> ConversationIdentityGlueResult:
        identity = await identity_repo.lock_canonical(
            self._session,
            identity_id=target_canonical_id,
        )
        if identity is None or identity.status != CanonicalIdentityStatus.ACTIVE.value:
            return await self._open_review(
                conversation_id=uuid.UUID(str(conversation.id)),
                reason_code=IdentityReviewReasonCode.CANONICAL_NOT_ACTIVE,
                proposed_canonical_identity_id=target_canonical_id,
            )

        current = conversation.canonical_identity_id
        if current is not None:
            current_id = uuid.UUID(str(current))
            if current_id == target_canonical_id:
                _log("IDENTITY_GLUE_ALREADY_ATTACHED")
                return ConversationIdentityGlueResult(
                    outcome=ConversationIdentityGlueOutcome.ALREADY_ATTACHED,
                    canonical_identity_id=target_canonical_id,
                )
            return await self._open_review(
                conversation_id=uuid.UUID(str(conversation.id)),
                reason_code=IdentityReviewReasonCode.CONFLICTING_CANONICAL,
                proposed_canonical_identity_id=target_canonical_id,
            )

        await glue_repo.set_conversation_canonical_identity(
            self._session,
            conversation=conversation,
            canonical_identity_id=target_canonical_id,
        )
        _log("IDENTITY_GLUE_ATTACHED")
        return ConversationIdentityGlueResult(
            outcome=ConversationIdentityGlueOutcome.ATTACHED,
            canonical_identity_id=target_canonical_id,
        )

    async def _open_review(
        self,
        *,
        conversation_id: uuid.UUID,
        reason_code: IdentityReviewReasonCode,
        proposed_canonical_identity_id: uuid.UUID | None,
    ) -> ConversationIdentityGlueResult:
        row, created = await glue_repo.insert_open_review_idempotent(
            self._session,
            conversation_id=conversation_id,
            reason_code=reason_code,
            proposed_canonical_identity_id=proposed_canonical_identity_id,
        )
        if created:
            _log("IDENTITY_GLUE_REVIEW_OPENED")
            outcome = ConversationIdentityGlueOutcome.REVIEW_OPENED
        else:
            _log("IDENTITY_GLUE_REVIEW_EXISTS")
            outcome = ConversationIdentityGlueOutcome.REVIEW_EXISTS
        return ConversationIdentityGlueResult(
            outcome=outcome,
            review_case_id=uuid.UUID(str(row.id)),
            reason_code=reason_code.value,
            canonical_identity_id=proposed_canonical_identity_id,
        )
