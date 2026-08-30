"""Types for operator shadow-draft evaluation (AI-EVAL-01).

Synthetic conversations only. Reports never include secrets or raw prompts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Sequence
from uuid import UUID


class ShadowDraftEvalVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    HANDOFF = "HANDOFF"
    DENIED = "DENIED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class ShadowDraftEvalCheck:
    name: str
    passed: bool
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"name": self.name, "passed": self.passed}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True, slots=True)
class ShadowDraftEvalScenario:
    """Synthetic question + deterministic quality/safety criteria (no KB bodies)."""

    id: str
    client_text: str
    # Optional second client turn (e.g. stale price claim after first Q).
    client_followup: str | None = None
    expect_provider_called: bool = True
    expect_disposition_in: tuple[str, ...] = ("REPLY", "HANDOFF")
    forbid_diagnosis: bool = True
    forbid_fabricated_slot: bool = True
    forbid_unsolicited_ai_intro: bool = True
    require_honest_bot_answer: bool = False
    relatox_no_individual_dose: bool = False
    prefer_handoff: bool = False
    require_nonempty_reply: bool = True
    # If answer states a numeric price, it must match Live Facts for hint service.
    live_facts_price_authority: bool = False
    # Client claims this wrong price; answer must not treat it as authoritative.
    stale_price_claim: str | None = None
    # Substring used to bind a Live Facts service for price/duration checks.
    service_name_contains: str | None = None
    # Answer must not invent free slots / exact times when prefer_handoff.
    forbid_exact_slot_claim: bool = False
    notes: str = ""


@dataclass(frozen=True, slots=True)
class ShadowDraftEvalScenarioResult:
    scenario_id: str
    synthetic_conversation_id: str
    question: str
    answer: str | None
    disposition: str
    handoff_required: bool
    reason_code: str
    provider_called: bool
    selected_knowledge_keys: tuple[str, ...]
    checks: tuple[ShadowDraftEvalCheck, ...]
    verdict: ShadowDraftEvalVerdict

    def as_dict(self) -> dict[str, object]:
        return {
            "scenario": self.scenario_id,
            "syntheticConversationId": self.synthetic_conversation_id,
            "question": self.question,
            "answer": self.answer,
            "disposition": self.disposition,
            "handoff": self.handoff_required,
            "reasonCode": self.reason_code,
            "providerCalled": self.provider_called,
            "selectedKnowledgeKeys": list(self.selected_knowledge_keys),
            "checks": [c.as_dict() for c in self.checks],
            "verdict": self.verdict.value,
        }


@dataclass(frozen=True, slots=True)
class ShadowDraftEvalSourceProof:
    settings_publication_id: str | None
    settings_version: int | None
    settings_checksum: str | None
    knowledge_publication_id: str | None
    knowledge_version: int | None
    knowledge_checksum: str | None
    knowledge_entry_count: int | None
    live_facts_schema_version: int | None
    live_facts_service_count: int | None
    live_facts_master_count: int | None
    live_facts_generated_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "settingsPublicationId": self.settings_publication_id,
            "settingsVersion": self.settings_version,
            "settingsChecksum": self.settings_checksum,
            "knowledgePublicationId": self.knowledge_publication_id,
            "knowledgeVersion": self.knowledge_version,
            "knowledgeChecksum": self.knowledge_checksum,
            "knowledgeEntryCount": self.knowledge_entry_count,
            "liveFactsSchemaVersion": self.live_facts_schema_version,
            "liveFactsServiceCount": self.live_facts_service_count,
            "liveFactsMasterCount": self.live_facts_master_count,
            "liveFactsGeneratedAt": self.live_facts_generated_at,
        }


@dataclass(frozen=True, slots=True)
class ShadowDraftEvalAggregate:
    total: int
    passed: int
    failed: int
    handoff: int
    denied: int
    provider_errors: int

    def as_dict(self) -> dict[str, object]:
        return {
            "TOTAL": self.total,
            "PASS": self.passed,
            "FAIL": self.failed,
            "HANDOFF": self.handoff,
            "DENIED": self.denied,
            "PROVIDER_ERRORS": self.provider_errors,
        }


@dataclass(frozen=True, slots=True)
class ShadowDraftEvalReport:
    source_proof: ShadowDraftEvalSourceProof
    scenarios: tuple[ShadowDraftEvalScenarioResult, ...]
    aggregate: ShadowDraftEvalAggregate
    eval_safety_note: str
    raw_prompt_included: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "sourceProof": self.source_proof.as_dict(),
            "evalSafetyNote": self.eval_safety_note,
            "rawPromptIncluded": self.raw_prompt_included,
            "scenarios": [s.as_dict() for s in self.scenarios],
            "aggregate": self.aggregate.as_dict(),
        }


def assert_synthetic_conversation_id(
    conversation_id: UUID,
    *,
    allowed: Sequence[UUID],
) -> None:
    """Reject any conversation id outside the synthetic allowlist for this run."""

    if conversation_id not in set(allowed):
        raise ValueError("REAL_CONVERSATION_ID_FORBIDDEN")


def redact_mapping_secrets(payload: Mapping[str, object]) -> dict[str, object]:
    """Drop secret-like keys from a report mapping (defensive)."""

    banned = {
        "apikey",
        "api_key",
        "yandex_api_key",
        "authorization",
        "bearer",
        "token",
        "password",
        "secret",
        "database_url",
        "raw_prompt",
        "rawprompt",
        "system_prompt",
        "folder_id",
        "yandex_folder_id",
    }
    out: dict[str, object] = {}
    for key, value in payload.items():
        if type(key) is not str:
            continue
        if key.casefold().replace("-", "_") in banned:
            out[key] = "<redacted>"
            continue
        if isinstance(value, Mapping):
            out[key] = redact_mapping_secrets(value)
        elif isinstance(value, list):
            out[key] = [
                redact_mapping_secrets(item) if isinstance(item, Mapping) else item
                for item in value
            ]
        else:
            out[key] = value
    return out
