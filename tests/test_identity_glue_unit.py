"""IR-1 identity glue unit/static coverage + ops CLI harden."""

from __future__ import annotations

import ast
import inspect
import io
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.identity_glue import (
    IDENTITY_REVIEW_REASON_CODES,
    ConversationIdentityGlueResult,
    IdentityReviewCaseRecord,
    IdentityReviewReasonCode,
)
from app.identity_glue_ops import (
    format_inspect_case_line,
    parse_resolve_signals_json,
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


def test_identity_glue_ops_cli_in_docker_image() -> None:
    assert_canonical_docker_runtime_allowlist()
    lines = dockerignore_lines(_REPO)
    for rel in (
        "app/identity_glue_ops.py",
        "app/services/identity_glue_ops.py",
    ):
        assert f"!{rel}" in EXPECTED_DOCKER_ALLOW_RULES
        assert is_included_in_docker_build_context(rel, lines, repo_root=_REPO) is True
        assert (_REPO / rel).is_file()
        assert rel in IR1_DOCKER_RUNTIME_PATHS


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


def test_parse_resolve_signals_json_from_stdin_shape() -> None:
    signals = parse_resolve_signals_json(
        '{"phone":"+79001234567","email":"a@example.com",'
        '"channel_provider":"vk","channel_scope":"g1",'
        '"channel_account":"acc-1"}'
    )
    assert signals.phone == "+79001234567"
    assert signals.email == "a@example.com"
    assert signals.channel_provider == "vk"
    assert signals.channel_connection_scope == "g1"
    assert signals.channel_external_account_id == "acc-1"


@pytest.mark.parametrize(
    "raw,code",
    [
        ("", "SIGNALS_STDIN_EMPTY"),
        ("{", "SIGNALS_STDIN_JSON_INVALID"),
        ("[]", "SIGNALS_STDIN_OBJECT_REQUIRED"),
        ('{"phone":"+1","extra":1}', "SIGNALS_STDIN_UNKNOWN_KEYS"),
        ("{}", "SIGNALS_STDIN_EMPTY_SIGNALS"),
        ('{"phone":1}', "SIGNALS_PHONE_INVALID"),
    ],
)
def test_parse_resolve_signals_json_fail_closed(raw: str, code: str) -> None:
    with pytest.raises(ValueError) as exc:
        parse_resolve_signals_json(raw)
    assert str(exc.value.args[0]) == code


def test_module_doc_avoids_sensitive_shell_examples() -> None:
    text = (_REPO / "app" / "identity_glue_ops.py").read_text(encoding="utf-8")
    assert "echo '{" not in text
    assert '"phone"' not in text.split('"""', 2)[1]
    assert "stdin JSON" in text.split('"""', 2)[1] or "stdin" in text.split('"""', 2)[1]


def test_legacy_sensitive_argv_space_form_forbidden(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app import identity_glue_ops as cli

    secret = "SECRET_PHONE_SPACE"
    code = cli.main(
        [
            "resolve-from-signals",
            "--conversation-id",
            str(uuid4()),
            "--phone",
            secret,
        ],
        environ={},
        stdin=io.StringIO("{}"),
    )
    assert code == 2
    captured = capsys.readouterr()
    assert "SENSITIVE_ARGV_FORBIDDEN" in captured.err
    assert secret not in captured.out
    assert secret not in captured.err


@pytest.mark.parametrize(
    "flag",
    [
        "--phone=SECRET",
        "--email=SECRET",
        "--channel-account=SECRET",
        "--channel-provider=SECRET",
        "--channel-scope=SECRET",
        "--channel-connection-scope=SECRET",
        "--channel-external-account-id=SECRET",
    ],
)
def test_legacy_sensitive_argv_equals_form_forbidden(
    flag: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app import identity_glue_ops as cli
    from app.identity_glue_ops import argv_has_sensitive_legacy_flag

    assert argv_has_sensitive_legacy_flag([flag]) is True
    code = cli.main(
        [
            "resolve-from-signals",
            "--conversation-id",
            str(uuid4()),
            flag,
        ],
        environ={},
        stdin=io.StringIO("{}"),
    )
    assert code == 2
    captured = capsys.readouterr()
    assert "SENSITIVE_ARGV_FORBIDDEN" in captured.err
    assert "SECRET" not in captured.out
    assert "SECRET" not in captured.err


def test_inspect_case_line_has_ids_and_no_pii() -> None:
    review_id = uuid4()
    conversation_id = uuid4()
    proposed = uuid4()
    line = format_inspect_case_line(
        IdentityReviewCaseRecord(
            id=review_id,
            conversation_id=conversation_id,
            reason_code="AMBIGUOUS_RESOLVE",
            status="OPEN",
            proposed_canonical_identity_id=proposed,
            resolved_canonical_identity_id=None,
        )
    )
    assert f"review_case_id={review_id}" in line
    assert f"conversation_id={conversation_id}" in line
    assert "reason_code=AMBIGUOUS_RESOLVE" in line
    assert f"proposed_canonical_identity_id={proposed}" in line
    assert "phone" not in line.lower()
    assert "email" not in line.lower()
    assert "name" not in line.lower()
    missing = format_inspect_case_line(
        IdentityReviewCaseRecord(
            id=review_id,
            conversation_id=conversation_id,
            reason_code="CONFLICTING_CANONICAL",
            status="OPEN",
            proposed_canonical_identity_id=None,
            resolved_canonical_identity_id=None,
        )
    )
    assert "proposed_canonical_identity_id=-" in missing


def test_cli_parser_rejects_removed_phone_email_flags() -> None:
    from app.identity_glue_ops import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "resolve-from-signals",
                "--conversation-id",
                str(uuid4()),
                "--phone",
                "+79001234567",
            ]
        )
