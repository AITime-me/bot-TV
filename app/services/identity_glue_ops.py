"""Offline identity glue ops (IR-1).

Controlled resolve/inspect/approve for conversation↔canonical attachment.
No amoCRM HTTP, DEAL_CREATE, chat binding, or mode changes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.identity_glue import (
    ApproveIdentityReviewOutcome,
    ApproveIdentityReviewResult,
    ConversationIdentityGlueOutcome,
    ConversationIdentityGlueResult,
    IdentityReviewCaseRecord,
    InspectIdentityReviewsResult,
)
from app.core.identity_resolution import IdentityResolveSignals
from app.db.session import session_scope
from app.services.identity_glue import ConversationIdentityGlueService

__all__ = (
    "IdentityGlueOpsOutcome",
    "IdentityGlueOpsResult",
    "approve_identity_review",
    "inspect_open_identity_reviews",
    "resolve_conversation_from_signals",
)


class IdentityGlueOpsOutcome(str, Enum):
    ATTACHED = "ATTACHED"
    ALREADY_ATTACHED = "ALREADY_ATTACHED"
    REVIEW_OPENED = "REVIEW_OPENED"
    REVIEW_EXISTS = "REVIEW_EXISTS"
    NOT_FOUND = "NOT_FOUND"
    APPROVED = "APPROVED"
    ALREADY_RESOLVED = "ALREADY_RESOLVED"
    INSPECTED = "INSPECTED"
    REFUSED = "REFUSED"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True, slots=True, repr=False)
class IdentityGlueOpsResult:
    outcome: IdentityGlueOpsOutcome
    error_code: str | None = None
    review_case_id: uuid.UUID | None = None
    canonical_identity_id: uuid.UUID | None = None
    open_review_count: int | None = None
    cases: tuple[IdentityReviewCaseRecord, ...] = ()

    def __repr__(self) -> str:
        return (
            "IdentityGlueOpsResult("
            f"outcome={self.outcome.value!r}, "
            f"error_code={self.error_code!r}, "
            "review_case_id=<redacted>, "
            "canonical_identity_id=<redacted>, "
            f"open_review_count={self.open_review_count!r}, "
            f"cases_count={len(self.cases)})"
        )


def _map_glue(result: ConversationIdentityGlueResult) -> IdentityGlueOpsResult:
    mapping = {
        ConversationIdentityGlueOutcome.ATTACHED: IdentityGlueOpsOutcome.ATTACHED,
        ConversationIdentityGlueOutcome.ALREADY_ATTACHED: (
            IdentityGlueOpsOutcome.ALREADY_ATTACHED
        ),
        ConversationIdentityGlueOutcome.REVIEW_OPENED: (
            IdentityGlueOpsOutcome.REVIEW_OPENED
        ),
        ConversationIdentityGlueOutcome.REVIEW_EXISTS: (
            IdentityGlueOpsOutcome.REVIEW_EXISTS
        ),
        ConversationIdentityGlueOutcome.NOT_FOUND: IdentityGlueOpsOutcome.NOT_FOUND,
        ConversationIdentityGlueOutcome.INVALID_INPUT: (
            IdentityGlueOpsOutcome.INVALID_INPUT
        ),
        ConversationIdentityGlueOutcome.REFUSED: IdentityGlueOpsOutcome.REFUSED,
    }
    return IdentityGlueOpsResult(
        outcome=mapping[result.outcome],
        error_code=result.error_code or result.reason_code,
        review_case_id=result.review_case_id,
        canonical_identity_id=result.canonical_identity_id,
    )


def _map_approve(result: ApproveIdentityReviewResult) -> IdentityGlueOpsResult:
    mapping = {
        ApproveIdentityReviewOutcome.APPROVED: IdentityGlueOpsOutcome.APPROVED,
        ApproveIdentityReviewOutcome.ALREADY_RESOLVED: (
            IdentityGlueOpsOutcome.ALREADY_RESOLVED
        ),
        ApproveIdentityReviewOutcome.REFUSED: IdentityGlueOpsOutcome.REFUSED,
        ApproveIdentityReviewOutcome.INVALID_INPUT: (
            IdentityGlueOpsOutcome.INVALID_INPUT
        ),
    }
    return IdentityGlueOpsResult(
        outcome=mapping[result.outcome],
        error_code=result.error_code,
        review_case_id=result.review_case_id,
        canonical_identity_id=result.canonical_identity_id,
    )


async def resolve_conversation_from_signals(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    conversation_id: uuid.UUID,
    signals: IdentityResolveSignals,
) -> IdentityGlueOpsResult:
    async with session_scope(session_factory) as session:
        result = await ConversationIdentityGlueService(session).resolve_for_conversation(
            conversation_id=conversation_id,
            signals=signals,
        )
        return _map_glue(result)


async def inspect_open_identity_reviews(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    conversation_id: uuid.UUID | None = None,
) -> IdentityGlueOpsResult:
    async with session_scope(session_factory) as session:
        inspected: InspectIdentityReviewsResult = (
            await ConversationIdentityGlueService(session).inspect_open_reviews(
                conversation_id=conversation_id,
            )
        )
        cases = inspected.cases
        return IdentityGlueOpsResult(
            outcome=IdentityGlueOpsOutcome.INSPECTED,
            open_review_count=len(cases),
            review_case_id=cases[0].id if cases else None,
            cases=cases,
        )


async def approve_identity_review(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    review_case_id: uuid.UUID,
    canonical_identity_id: uuid.UUID,
) -> IdentityGlueOpsResult:
    async with session_scope(session_factory) as session:
        result = await ConversationIdentityGlueService(session).approve_review(
            review_case_id=review_case_id,
            canonical_identity_id=canonical_identity_id,
        )
        return _map_approve(result)
