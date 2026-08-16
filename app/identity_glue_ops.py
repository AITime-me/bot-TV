"""Offline identity glue ops CLI (IR-1).

Usage:
  python -B -m app.identity_glue_ops resolve-from-signals \\
    --conversation-id UUID --phone E164 [--email EMAIL] \\
    [--channel-provider P --channel-scope S --channel-account A]
  python -B -m app.identity_glue_ops inspect-reviews [--conversation-id UUID]
  python -B -m app.identity_glue_ops approve-review \\
    --review-case-id UUID --canonical-identity-id UUID

No amoCRM HTTP, DEAL_CREATE, chat binding, or mode changes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from collections.abc import Mapping, Sequence

from app.config import Settings
from app.core.identity_resolution import IdentityResolveSignals
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
        help="Resolve signals and attach/review for one conversation.",
    )
    resolve_cmd.add_argument("--conversation-id", required=True)
    resolve_cmd.add_argument("--phone", default=None)
    resolve_cmd.add_argument("--email", default=None)
    resolve_cmd.add_argument("--channel-provider", default=None)
    resolve_cmd.add_argument("--channel-scope", default=None)
    resolve_cmd.add_argument("--channel-account", default=None)

    inspect_cmd = sub.add_parser(
        "inspect-reviews",
        help="List OPEN identity review cases (count only on stdout).",
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


async def _run(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
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
            except ValueError as exc:
                code = str(exc.args[0])
                print(f"identity_glue_ops failed error_code={code}", file=sys.stderr)
                return 2
            signals = IdentityResolveSignals(
                phone=args.phone,
                email=args.email,
                channel_provider=args.channel_provider,
                channel_connection_scope=args.channel_scope,
                channel_external_account_id=args.channel_account,
            )
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


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(_run(argv if argv is not None else sys.argv[1:]))
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
