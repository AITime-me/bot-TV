"""IR-1 identity glue unit/static coverage."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from uuid import uuid4

from app.core.identity_glue import (
    IDENTITY_REVIEW_REASON_CODES,
    ConversationIdentityGlueResult,
    IdentityReviewCaseRecord,
    IdentityReviewReasonCode,
)
from app.models.identity_review_case import IdentityReviewCase
from app.services import identity_glue as glue_service_mod
from tests.docker_runtime_allowlist import (
    EXPECTED_DOCKER_ALLOW_RULES,
    IR1_DOCKER_RUNTIME_PATHS,
    assert_canonical_docker_runtime_allowlist,
    dockerignore_lines,
    is_included_in_docker_build_context,
)

_REPO = Path(__file__).resolve().parents[1]


def test_review_reason_codes_are_fixed_technical_tokens() -> None:
    assert IDENTITY_REVIEW_REASON_CODES == {
        "AMBIGUOUS_RESOLVE",
        "CONFLICTING_CANONICAL",
        "CANONICAL_NOT_ACTIVE",
    }
    for code in IdentityReviewReasonCode:
        assert code.value.isupper()
        assert " " not in code.value
        assert "@" not in code.value


def test_review_record_repr_redacts_ids() -> None:
    record = IdentityReviewCaseRecord(
        id=uuid4(),
        conversation_id=uuid4(),
        reason_code="AMBIGUOUS_RESOLVE",
        status="OPEN",
        proposed_canonical_identity_id=uuid4(),
        resolved_canonical_identity_id=None,
    )
    text = repr(record)
    assert "AMBIGUOUS_RESOLVE" in text
    assert "<redacted>" in text
    assert str(record.id) not in text
    assert str(record.conversation_id) not in text


def test_glue_result_repr_redacts_ids() -> None:
    from app.core.identity_glue import ConversationIdentityGlueOutcome

    result = ConversationIdentityGlueResult(
        outcome=ConversationIdentityGlueOutcome.ATTACHED,
        canonical_identity_id=uuid4(),
        review_case_id=uuid4(),
    )
    text = repr(result)
    assert "ATTACHED" in text
    assert "<redacted>" in text


def test_glue_service_delegates_to_identity_resolution_service() -> None:
    source = inspect.getsource(glue_service_mod.ConversationIdentityGlueService)
    assert "IdentityResolutionService" in source
    assert ".resolve(" in source
    assert "normalize_phone_e164" not in source
    assert "send_silent_text" not in source
    assert "seed_active_chat_binding" not in source


def test_glue_ops_zero_external_http_surface() -> None:
    for rel in (
        "app/services/identity_glue.py",
        "app/services/identity_glue_ops.py",
        "app/identity_glue_ops.py",
        "app/repositories/identity_glue.py",
    ):
        tree = ast.parse((_REPO / rel).read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
                imports.add(node.module)
        banned = {
            "httpx",
            "urllib",
            "requests",
            "aiohttp",
        }
        assert imports.isdisjoint(banned), rel
        text = (_REPO / rel).read_text(encoding="utf-8")
        for needle in (
            "send_silent_text",
            "AmoCrmChatEgressHttpClient",
            "AmoCrmLeadHttpClient",
            "AmoCrmCrmRestHttpClient",
            "seed_active_chat_binding",
            "load_deal_create_config",
        ):
            assert needle not in text, f"{rel} contains {needle}"


def test_identity_glue_ops_cli_not_in_docker_image() -> None:
    assert_canonical_docker_runtime_allowlist()
    lines = dockerignore_lines(_REPO)
    for rel in (
        "app/identity_glue_ops.py",
        "app/services/identity_glue_ops.py",
    ):
        assert is_included_in_docker_build_context(rel, lines, repo_root=_REPO) is False
        assert f"!{rel}" not in EXPECTED_DOCKER_ALLOW_RULES


def test_ir1_runtime_paths_allowlisted() -> None:
    assert_canonical_docker_runtime_allowlist()
    lines = dockerignore_lines(_REPO)
    for rel in IR1_DOCKER_RUNTIME_PATHS:
        assert f"!{rel}" in EXPECTED_DOCKER_ALLOW_RULES
        assert is_included_in_docker_build_context(rel, lines, repo_root=_REPO) is True
        assert (_REPO / rel).is_file()


def test_identity_review_case_model_has_no_pii_columns() -> None:
    cols = {c.name for c in IdentityReviewCase.__table__.columns}
    assert "phone" not in cols
    assert "email" not in cols
    assert "name" not in cols
    assert "payload" not in cols
    assert "text" not in cols
    assert "envelope_json" not in cols
