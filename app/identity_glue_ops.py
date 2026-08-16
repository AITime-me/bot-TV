"""Offline identity glue ops CLI (IR-1).

Usage:
  # Pipe stdin JSON with resolve signals (never put phone/email/channel ids in argv
  # or shell command examples). Sensitive values must not appear on the command line.
  python -B -m app.identity_glue_ops resolve-from-signals --conversation-id UUID < signals.json

  python -B -m app.identity_glue_ops inspect-reviews [--conversation-id UUID]
  python -B -m app.identity_glue_ops approve-review \\
    --review-case-id UUID --canonical-identity-id UUID

No amoCRM HTTP, DEAL_CREATE, chat binding, or mode changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from collections.abc import Mapping, Sequence
from typing import TextIO

from app.config import Settings
from app.core.identity_glue import IdentityReviewCaseRecord
from app.core.identity_resolution import IdentityEntityKind, IdentityResolveSignals
from app.db.session import create_engine, create_session_factory
from app.services.identity_glue_ops import (
    IdentityGlueOpsOutcome,
    approve_identity_review,
    inspect_open_identity_reviews,
    resolve_conversation_from_signals,
)

logger = logging.getLogger(__name__)

_SUCCESS = frozenset(
    {
        IdentityGlueOpsOutcome.ATTACHED,
        IdentityGlueOpsOutcome.ALREADY_ATTACHED,
        IdentityGlueOpsOutcome.REVIEW_OPENED,
        IdentityGlueOpsOutcome.REVIEW_EXISTS,
        IdentityGlueOpsOutcome.NOT_FOUND,
        IdentityGlueOpsOutcome.APPROVED,
        IdentityGlueOpsOutcome.ALREADY_RESOLVED,
        IdentityGlueOpsOutcome.INSPECTED,
    }
)

_SIGNAL_KEYS = frozenset(
    {
        "phone",
        "email",
        "channel_provider",
        "channel_scope",
        "channel_connection_scope",
        "channel_account",
        "channel_external_account_id",
        "confirmed_links",
    }
)

# Exact flag tokens and ``--flag=`` prefixes rejected before argparse.
_SENSITIVE_ARGV_FLAGS: tuple[str, ...] = (
    "--phone",
    "--email",
    "--channel-account",
    "--channel-provider",
    "--channel-scope",
    "--channel-connection-scope",
    "--channel-external-account-id",
)


def argv_has_sensitive_legacy_flag(argv: Sequence[str]) -> bool:
    """True if argv uses banned sensitive flags (``--flag`` or ``--flag=...``)."""

    for token in argv:
        if type(token) is not str:
            continue
        for flag in _SENSITIVE_ARGV_FLAGS:
            if token == flag or token.startswith(f"{flag}="):
                return True
    return False


def _log_safely(level: int, event: str, **fields: object) -> None:
    try:
        for value in fields.values():
            if type(value) is not int and type(value) is not str:
                return
        if fields:
            keys = tuple(fields.keys())
            message = event + "".join(f" {key}=%s" for key in keys)
            logger.log(level, message, *[fields[k] for k in keys])
        else:
            logger.log(level, event)
    except Exception:
        return


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="identity_glue_ops",
        description=(
            "Offline conversation↔canonical identity glue "
            "(no amoCRM HTTP / chat binding / DEAL_CREATE)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    resolve_cmd = sub.add_parser(
        "resolve-from-signals",
        help=(
            "Resolve stdin JSON signals and attach/review for one conversation. "
            "Sensitive fields (phone/email/channel ids) must come from stdin."
        ),
    )
    resolve_cmd.add_argument("--conversation-id", required=True)

    inspect_cmd = sub.add_parser(
        "inspect-reviews",
        help="List OPEN identity review cases with technical ids (no PII).",
    )
    inspect_cmd.add_argument("--conversation-id", default=None)

    approve_cmd = sub.add_parser(
        "approve-review",
        help="Manually attach ACTIVE canonical and resolve OPEN review.",
    )
    approve_cmd.add_argument("--review-case-id", required=True)
    approve_cmd.add_argument("--canonical-identity-id", required=True)
    return parser


def _parse_uuid(raw: str, *, code: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ValueError(code) from exc


def _optional_str(value: object, *, code: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError(code)
    stripped = value.strip()
    if not stripped:
        return None
    return stripped


def _parse_confirmed_links(raw: object) -> tuple[tuple[str, str, IdentityEntityKind | str, str], ...]:
    if raw is None:
        return ()
    if type(raw) is not list:
        raise ValueError("SIGNALS_CONFIRMED_LINKS_INVALID")
    out: list[tuple[str, str, IdentityEntityKind | str, str]] = []
    for item in raw:
        if type(item) is not list and type(item) is not tuple:
            raise ValueError("SIGNALS_CONFIRMED_LINKS_INVALID")
        if len(item) != 4:
            raise ValueError("SIGNALS_CONFIRMED_LINKS_INVALID")
        provider, scope, kind, external_id = item
        if (
            type(provider) is not str
            or type(scope) is not str
            or type(kind) is not str
            or type(external_id) is not str
        ):
            raise ValueError("SIGNALS_CONFIRMED_LINKS_INVALID")
        out.append((provider, scope, kind, external_id))
    return tuple(out)


def parse_resolve_signals_json(raw: str) -> IdentityResolveSignals:
    """Parse stdin JSON into IdentityResolveSignals. Fail closed on bad shape."""

    if type(raw) is not str:
        raise ValueError("SIGNALS_STDIN_INVALID")
    text = raw.strip()
    if not text:
        raise ValueError("SIGNALS_STDIN_EMPTY")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("SIGNALS_STDIN_JSON_INVALID") from exc
    if type(payload) is not dict:
        raise ValueError("SIGNALS_STDIN_OBJECT_REQUIRED")
    unknown = set(payload.keys()) - _SIGNAL_KEYS
    if unknown:
        raise ValueError("SIGNALS_STDIN_UNKNOWN_KEYS")

    phone = _optional_str(payload.get("phone"), code="SIGNALS_PHONE_INVALID")
    email = _optional_str(payload.get("email"), code="SIGNALS_EMAIL_INVALID")
    channel_provider = _optional_str(
        payload.get("channel_provider"),
        code="SIGNALS_CHANNEL_PROVIDER_INVALID",
    )
    channel_scope = _optional_str(
        payload.get("channel_scope", payload.get("channel_connection_scope")),
        code="SIGNALS_CHANNEL_SCOPE_INVALID",
    )
    channel_account = _optional_str(
        payload.get("channel_account", payload.get("channel_external_account_id")),
        code="SIGNALS_CHANNEL_ACCOUNT_INVALID",
    )
    confirmed_links = _parse_confirmed_links(payload.get("confirmed_links"))
    if (
        phone is None
        and email is None
        and channel_account is None
        and not confirmed_links
    ):
        raise ValueError("SIGNALS_STDIN_EMPTY_SIGNALS")
    return IdentityResolveSignals(
        phone=phone,
        email=email,
        channel_provider=channel_provider,
        channel_connection_scope=channel_scope,
        channel_external_account_id=channel_account,
        confirmed_links=confirmed_links,
    )


def format_inspect_case_line(case: IdentityReviewCaseRecord) -> str:
    proposed = (
        str(case.proposed_canonical_identity_id)
        if case.proposed_canonical_identity_id is not None
        else "-"
    )
    return (
        "identity_glue_ops_case "
        f"review_case_id={case.id} "
        f"conversation_id={case.conversation_id} "
        f"reason_code={case.reason_code} "
        f"proposed_canonical_identity_id={proposed}"
    )


def _read_stdin_text(stdin: TextIO) -> str:
    return stdin.read()


async def _run(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
) -> int:
    # Reject legacy sensitive argv flags before argparse so values never leak
    # into argparse errors, stdout, stderr, or logs.
    if argv_has_sensitive_legacy_flag(argv):
        _log_safely(
            logging.ERROR,
            "identity_glue_ops_failed",
            error_code="SENSITIVE_ARGV_FORBIDDEN",
        )
        print(
            "identity_glue_ops failed error_code=SENSITIVE_ARGV_FORBIDDEN",
            file=sys.stderr,
        )
        return 2

    parser = _build_parser()
    args = parser.parse_args(list(argv))
    settings = Settings.from_env(environ)
    if settings.database_url is None:
        _log_safely(
            logging.ERROR,
            "identity_glue_ops_failed",
            error_code="DATABASE_URL_REQUIRED",
        )
        print(
            "identity_glue_ops failed error_code=DATABASE_URL_REQUIRED",
            file=sys.stderr,
        )
        return 2

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        if args.command == "resolve-from-signals":
            try:
                conversation_id = _parse_uuid(
                    args.conversation_id,
                    code="CONVERSATION_ID_INVALID",
                )
                signals = parse_resolve_signals_json(
                    _read_stdin_text(stdin if stdin is not None else sys.stdin)
                )
            except ValueError as exc:
                code = str(exc.args[0]) if exc.args else "SIGNALS_STDIN_INVALID"
                if type(code) is not str or not code:
                    code = "SIGNALS_STDIN_INVALID"
                _log_safely(
                    logging.ERROR,
                    "identity_glue_ops_failed",
                    error_code=code,
                )
                print(f"identity_glue_ops failed error_code={code}", file=sys.stderr)
                return 2
            result = await resolve_conversation_from_signals(
                session_factory,
                conversation_id=conversation_id,
                signals=signals,
            )
        elif args.command == "inspect-reviews":
            conversation_id = None
            if args.conversation_id is not None:
                try:
                    conversation_id = _parse_uuid(
                        args.conversation_id,
                        code="CONVERSATION_ID_INVALID",
                    )
                except ValueError as exc:
                    code = str(exc.args[0])
                    print(
                        f"identity_glue_ops failed error_code={code}",
                        file=sys.stderr,
                    )
                    return 2
            result = await inspect_open_identity_reviews(
                session_factory,
                conversation_id=conversation_id,
            )
        elif args.command == "approve-review":
            try:
                review_case_id = _parse_uuid(
                    args.review_case_id,
                    code="REVIEW_CASE_ID_INVALID",
                )
                canonical_identity_id = _parse_uuid(
                    args.canonical_identity_id,
                    code="CANONICAL_IDENTITY_ID_INVALID",
                )
            except ValueError as exc:
                code = str(exc.args[0])
                print(f"identity_glue_ops failed error_code={code}", file=sys.stderr)
                return 2
            result = await approve_identity_review(
                session_factory,
                review_case_id=review_case_id,
                canonical_identity_id=canonical_identity_id,
            )
        else:
            return 2
    finally:
        await engine.dispose()

    safe_code = (
        result.error_code
        if type(result.error_code) is str and 0 < len(result.error_code) <= 128
        else "-"
    )
    count = (
        str(result.open_review_count)
        if result.open_review_count is not None
        else "-"
    )
    print(
        "identity_glue_ops "
        f"outcome={result.outcome.value} "
        f"error_code={safe_code} "
        f"open_review_count={count}"
    )
    if result.outcome is IdentityGlueOpsOutcome.INSPECTED:
        for case in result.cases:
            print(format_inspect_case_line(case))
    _log_safely(
        logging.INFO,
        "identity_glue_ops_completed",
        outcome=result.outcome.value,
        error_code=safe_code,
        open_review_count=count,
    )
    if result.outcome in _SUCCESS:
        return 0
    return 2


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(
            _run(
                argv if argv is not None else sys.argv[1:],
                environ=environ,
                stdin=stdin,
            )
        )
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(
            "identity_glue_ops failed "
            f"error_code={type(sys.exc_info()[1]).__name__}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
