from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class AmoCrmMirrorOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    PERMANENT_ERROR = "PERMANENT_ERROR"


@dataclass(frozen=True, repr=False)
class AmoCrmMirrorRequest:
    """Technical descriptor of one mirror job handed to the sink.

    Carries internal identifiers only: no client text, contacts, or amoCRM
    entity references. Payload is never included in repr/logs.
    """

    job_id: str
    job_type: str
    subject_kind: str
    subject_id: str
    conversation_id: str
    context_version: int | None
    correlation_id: str
    _payload_schema: str

    def __repr__(self) -> str:
        return (
            f"AmoCrmMirrorRequest(job_id={self.job_id!r}, "
            f"job_type={self.job_type!r}, subject_kind={self.subject_kind!r}, "
            f"subject_id={self.subject_id!r}, "
            f"conversation_id={self.conversation_id!r}, "
            f"context_version={self.context_version!r}, "
            f"correlation_id={self.correlation_id!r}, payload=<redacted>)"
        )


@dataclass(frozen=True)
class AmoCrmMirrorAdapterResult:
    outcome: AmoCrmMirrorOutcome
    error_code: str | None = None


class AmoCrmMirrorAdapter(Protocol):
    """Sink that converges required amoCRM entity state for one mirror job."""

    async def mirror(self, request: AmoCrmMirrorRequest) -> AmoCrmMirrorAdapterResult:
        ...


class NoopAmoCrmMirrorAdapter:
    """In-process no-op amoCRM sink (unit tests).

    Production worker injects ``CrmRestMirrorAdapter``. A successful no-op
    call is not "message content copied to CRM".
    """

    def __init__(
        self,
        *,
        forced_outcome: AmoCrmMirrorOutcome = AmoCrmMirrorOutcome.SUCCESS,
    ) -> None:
        self._forced_outcome = forced_outcome
        self.calls: list[AmoCrmMirrorRequest] = []

    async def mirror(self, request: AmoCrmMirrorRequest) -> AmoCrmMirrorAdapterResult:
        self.calls.append(request)
        if self._forced_outcome is AmoCrmMirrorOutcome.SUCCESS:
            return AmoCrmMirrorAdapterResult(outcome=AmoCrmMirrorOutcome.SUCCESS)
        if self._forced_outcome is AmoCrmMirrorOutcome.TRANSIENT_ERROR:
            return AmoCrmMirrorAdapterResult(
                outcome=AmoCrmMirrorOutcome.TRANSIENT_ERROR,
                error_code="AMOCRM_MIRROR_TRANSIENT",
            )
        return AmoCrmMirrorAdapterResult(
            outcome=AmoCrmMirrorOutcome.PERMANENT_ERROR,
            error_code="AMOCRM_MIRROR_PERMANENT",
        )
