"""Sanitized Settings v2 candidate ContentPolicy fixtures (AI-DIALOGUE-02-PROMPT-BUDGET).

Source-level dedup design proof for future Settings v2 publication.
Runtime tests load only committed fixtures — never `.tmp-*` analysis artifacts.

Production audit anchors (independent of candidate bundle):
- safety/handoff SHA256: ACTIVE Settings v1 snapshot (`.tmp-pub-proof-settings-active.json`,
  captured 2026-08-30 during design proof; re-verify read-only on production server before publish).
- KB serviceId map: ACTIVE KB publication payload from production DB (87 entries, 27 service-linked).

Semantic preservation map lives in `teya_main_instruction_semantic_dedup_map.py` (test-time contract).
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_FIXTURE_DIR = Path(__file__).resolve().parent
_MAIN_V1_PATH = _FIXTURE_DIR / "teya_main_instruction_v1_golden.txt"
_MAIN_V2_PATH = _FIXTURE_DIR / "teya_main_instruction_v2_candidate.txt"
_BUNDLE_PATH = _FIXTURE_DIR / "teya_settings_v2_candidate_bundle.json"
_SERVICE_ID_MAP_PATH = _FIXTURE_DIR / "teya_knowledge_service_id_map_v1.json"

# Design-proof exact sizes (ACTIVE Settings v1 unchanged fields).
TEYA_MAIN_INSTRUCTION_V1_LEN = 6548
TEYA_MAIN_INSTRUCTION_V2_LEN = 4787
TEYA_MAIN_INSTRUCTION_V2_SAVINGS = TEYA_MAIN_INSTRUCTION_V1_LEN - TEYA_MAIN_INSTRUCTION_V2_LEN
TEYA_SAFETY_RULES_V1_LEN = 1060
TEYA_HANDOFF_RULES_V1_LEN = 930
TEYA_KNOWLEDGE_BASE_NOTE_V1_LEN = 1501
TEYA_TAGGING_RULES_V1_LEN = 616

# Independent ACTIVE v1 production audit anchors (SHA-256 over UTF-8 text).
# Source: production ACTIVE Settings snapshot captured during AI-DIALOGUE-02 design proof.
TEYA_ACTIVE_V1_SAFETY_RULES_SHA256 = (
    "5537c8f23c1666fb4e8edb756bfe8b6e44558fa78b89c1a4ad4d0d4bad6cffcc"
)
TEYA_ACTIVE_V1_HANDOFF_RULES_SHA256 = (
    "601942e683d2feb1bac7505d3715b07d766101b579549ac1616d09e090815ad6"
)

# Canonical serialization of unchanged v1 admin fields (excludes mainInstruction).
TEYA_ACTIVE_V1_UNCHANGED_CONTENT_POLICY_SHA256 = (
    "215e05aead6e1f14b11d3771ca378a004df65c3f5d3bf29ca95bc80dc715af3c"
)

# External production evidence (read-only server measurement; not local harness total).
SCENARIO_14_PRODUCTION_V1_TOTAL_WITH_REQUIRED_KB = 11_748
SCENARIO_14_PRODUCTION_V2_PROJECTED_TOTAL = (
    SCENARIO_14_PRODUCTION_V1_TOTAL_WITH_REQUIRED_KB - TEYA_MAIN_INSTRUCTION_V2_SAVINGS
)
SCENARIO_14_PRODUCTION_PROJECTED_MARGIN = (
    10_500 - SCENARIO_14_PRODUCTION_V2_PROJECTED_TOTAL
)

# Production-shaped scenario 14 acceptance KB keys (4-key ACTIVE proof path).
SCENARIO_14_ACTIVE_KB_KEYS: tuple[str, ...] = (
    "faq.relatox_units",
    "policy.manager_takeover",
    "policy.persona",
    "policy.reactivation",
)

RELATOX_SERVICE_ID = "a3000001-0000-4000-8000-000000000088"
RELATOX_CANONICAL_NAME = "Ботулинотерапия препаратом Релатокс"

_EXPECTED_KB_WITH_SERVICE_ID = 27


@lru_cache(maxsize=1)
def teya_main_instruction_v1_golden() -> str:
    text = _MAIN_V1_PATH.read_text(encoding="utf-8")
    if len(text) != TEYA_MAIN_INSTRUCTION_V1_LEN:
        raise ValueError(
            "TEYA_MAIN_INSTRUCTION_V1_LEN_MISMATCH: "
            f"expected {TEYA_MAIN_INSTRUCTION_V1_LEN}, got {len(text)}"
        )
    return text


@lru_cache(maxsize=1)
def teya_main_instruction_v2_candidate() -> str:
    text = _MAIN_V2_PATH.read_text(encoding="utf-8")
    if len(text) != TEYA_MAIN_INSTRUCTION_V2_LEN:
        raise ValueError(
            "TEYA_MAIN_INSTRUCTION_V2_LEN_MISMATCH: "
            f"expected {TEYA_MAIN_INSTRUCTION_V2_LEN}, got {len(text)}"
        )
    return text


@lru_cache(maxsize=1)
def _bundle() -> dict[str, Any]:
    return json.loads(_BUNDLE_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def teya_knowledge_service_id_map_v1() -> dict[str, str | None]:
    """Production ACTIVE KB publication serviceId anchors (stableKey -> serviceId)."""

    mapping = json.loads(_SERVICE_ID_MAP_PATH.read_text(encoding="utf-8"))
    with_sid = sum(1 for value in mapping.values() if value)
    if len(mapping) != 87 or with_sid != _EXPECTED_KB_WITH_SERVICE_ID:
        raise ValueError(
            "TEYA_KB_SERVICE_ID_MAP_MISMATCH: "
            f"expected 87 entries / {_EXPECTED_KB_WITH_SERVICE_ID} service-linked, "
            f"got {len(mapping)} / {with_sid}"
        )
    return mapping


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def teya_active_v1_unchanged_content_policy_sha256() -> str:
    """Compute canonical SHA256 for unchanged v1 admin fields (bundle cross-check)."""

    fields = teya_v1_unchanged_content_policy_fields()
    canonical = json.dumps(
        {
            "handoffRules": fields["handoffRules"],
            "knowledgeBaseNote": fields["knowledgeBaseNote"],
            "safetyRules": fields["safetyRules"],
            "taggingRules": fields["taggingRules"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_text(canonical)


@lru_cache(maxsize=1)
def _verify_active_v1_unchanged_content_policy_anchor() -> None:
    digest = teya_active_v1_unchanged_content_policy_sha256()
    if digest != TEYA_ACTIVE_V1_UNCHANGED_CONTENT_POLICY_SHA256:
        raise ValueError(
            "TEYA_ACTIVE_V1_UNCHANGED_CONTENT_POLICY_SHA256_MISMATCH: "
            f"expected {TEYA_ACTIVE_V1_UNCHANGED_CONTENT_POLICY_SHA256}, got {digest}"
        )


def teya_v2_candidate_content_policy_fields() -> dict[str, str]:
    """Return full admin ContentPolicy fields for v2 candidate (main v2 + unchanged v1 fields)."""

    _verify_active_v1_unchanged_content_policy_anchor()
    bundle = _bundle()
    cp = bundle["contentPolicy"]
    safety = cp["safetyRules"]
    handoff = cp["handoffRules"]
    if _sha256_text(safety) != TEYA_ACTIVE_V1_SAFETY_RULES_SHA256:
        raise ValueError("TEYA_ACTIVE_V1_SAFETY_RULES_SHA256_MISMATCH")
    if _sha256_text(handoff) != TEYA_ACTIVE_V1_HANDOFF_RULES_SHA256:
        raise ValueError("TEYA_ACTIVE_V1_HANDOFF_RULES_SHA256_MISMATCH")
    return {
        "mainInstruction": teya_main_instruction_v2_candidate(),
        "knowledgeBaseNote": cp["knowledgeBaseNote"],
        "handoffRules": handoff,
        "taggingRules": cp["taggingRules"],
        "safetyRules": safety,
    }


def teya_v1_unchanged_content_policy_fields() -> dict[str, str]:
    """Return safety/handoff/kbnote/tagging unchanged from ACTIVE v1 bundle."""

    bundle = _bundle()
    cp = bundle["contentPolicy"]
    return {
        "knowledgeBaseNote": cp["knowledgeBaseNote"],
        "handoffRules": cp["handoffRules"],
        "taggingRules": cp["taggingRules"],
        "safetyRules": cp["safetyRules"],
    }


def enabled_knowledge_entries() -> list[dict[str, Any]]:
    entries = list(_bundle()["enabledKnowledgeEntries"])
    anchor = teya_knowledge_service_id_map_v1()
    for entry in entries:
        key = entry["stableKey"]
        expected_sid = anchor.get(key)
        if entry.get("serviceId") != expected_sid:
            raise ValueError(
                f"TEYA_KB_ENTRY_SERVICE_ID_MISMATCH: {key} "
                f"bundle={entry.get('serviceId')!r} anchor={expected_sid!r}"
            )
    return entries


def knowledge_entries_for_keys(keys: tuple[str, ...]) -> list[dict[str, Any]]:
    wanted = frozenset(keys)
    return [entry for entry in enabled_knowledge_entries() if entry["stableKey"] in wanted]
