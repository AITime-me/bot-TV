"""Conversation↔canonical identity glue types (IR-1).

No matching algorithm. No CRM I/O. Reason codes are fixed technical tokens only.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Final

__all__ = (
    "IDENTITY_REVIEW_REASON_CODES",
    "ApproveIdentityReviewOutcome",
    "ApproveIdentityReviewResult",
    "ConversationIdentityGlueOutcome",
    "ConversationIdentityGlueResult",
    "IdentityReviewCaseRecord",
    "IdentityReviewCaseStatus",
    "IdentityReviewReasonCode",
    "InspectIdentityReviewsResult",
    "require_identity_review_reason_code",
)


class IdentityReviewReasonCode(str, enum.Enum):
    AMBIGUOUS_RESOLVE = "AMBIGUOUS_RESOLVE"
    CONFLICTING_CANONICAL = "CONFLICTING_CANONICAL"
    CANONICAL_NOT_ACTIVE = "CANONICAL_NOT_ACTIVE"


IDENTITY_REVIEW_REASON_CODES: Final[frozenset[str]] = frozenset(
    code.value for code in IdentityReviewReasonCode
)


class IdentityReviewCaseStatus(str, enum.Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class ConversationIdentityGlueOutcome(str, enum.Enum):
    ATTACHED = "ATTACHED"
    ALREADY_ATTACHED = "ALREADY_ATTACHED"
    REVIEW_OPENED = "REVIEW_OPENED"
    REVIEW_EXISTS = "REVIEW_EXISTS"
    NOT_FOUND = "NOT_FOUND"
    INVALID_INPUT = "INVALID_INPUT"
    REFUSED = "REFUSED"


class ApproveIdentityReviewOutcome(str, enum.Enum):
    APPROVED = "APPROVED"
    ALREADY_RESOLVED = "ALREADY_RESOLVED"
    REFUSED = "REFUSED"
    INVALID_INPUT = "INVALID_INPUT"


def require_identity_review_reason_code(value: object) -> str:
    if type(value) is IdentityReviewReasonCode:
        return value.value
    if type(value) is not str or value not in IDENTITY_REVIEW_REASON_CODES:
        raise ValueError("IDENTITY_REVIEW_REASON_INVALID")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class IdentityReviewCaseRecord:
    id: uuid.UUID
    conversation_id: uuid.UUID
    reason_code: str
    status: str
    proposed_canonical_identity_id: uuid.UUID | None
    resolved_canonical_identity_id: uuid.UUID | None

    def __repr__(self) -> str:
        return (
            "IdentityReviewCaseRecord("
            "id=<redacted>, "
            "conversation_id=<redacted>, "
            f"reason_code={self.reason_code!r}, "
            f"status={self.status!r}, "
            "proposed_canonical_identity_id=<redacted>, "
            "resolved_canonical_identity_id=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ConversationIdentityGlueResult:
    outcome: ConversationIdentityGlueOutcome
    canonical_identity_id: uuid.UUID | None = None
    review_case_id: uuid.UUID | None = None
    reason_code: str | None = None
    error_code: str | None = None

    def __repr__(self) -> str:
        return (
            "ConversationIdentityGlueResult("
            f"outcome={self.outcome.value!r}, "
            "canonical_identity_id=<redacted>, "
            "review_case_id=<redacted>, "
            f"reason_code={self.reason_code!r}, "
            f"error_code={self.error_code!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ApproveIdentityReviewResult:
    outcome: ApproveIdentityReviewOutcome
    review_case_id: uuid.UUID | None = None
    canonical_identity_id: uuid.UUID | None = None
    error_code: str | None = None

    def __repr__(self) -> str:
        return (
            "ApproveIdentityReviewResult("
            f"outcome={self.outcome.value!r}, "
            "review_case_id=<redacted>, "
            "canonical_identity_id=<redacted>, "
            f"error_code={self.error_code!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class InspectIdentityReviewsResult:
    cases: tuple[IdentityReviewCaseRecord, ...]

    def __repr__(self) -> str:
        return f"InspectIdentityReviewsResult(cases_count={len(self.cases)})"
