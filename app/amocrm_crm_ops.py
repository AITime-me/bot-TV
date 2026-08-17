"""Offline amoCRM CRM ops CLI (AMO-01B2-OPS).

Usage:
  python -B -m app.amocrm_crm_ops bootstrap
  python -B -m app.amocrm_crm_ops reseed
  python -B -m app.amocrm_crm_ops resolve-reconcile --conversation-id UUID --confirmed-deal-id N

Access/refresh tokens are read via getpass/stdin only — never CLI args.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from collections.abc import Mapping, Sequence

from app.config import Settings
from app.core.amocrm_crm_rest_config import (
    AmoCrmCrmRestConfigError,
    load_crm_rest_config_fail_closed,
)
from app.db.session import create_engine, create_session_factory
from app.services.amocrm_crm_ops import (
    AmoCrmCrmOpsService,
    AmoCrmOpsOutcome,
    read_secret_line,
)

logger = logging.getLogger(__name__)


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


def _safe_scope_label(scope: object) -> str:
    """Report connection scope without leaking secrets or control chars."""

    if type(scope) is not str or not scope or len(scope) > 64:
        return "-"
    if any(ch.isspace() for ch in scope):
        return "-"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in scope):
        return "-"
    return scope


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amocrm_crm_ops",
        description="Offline amoCRM CRM operator actions (bootstrap/reseed/reconcile).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bootstrap", help="Insert OAuth token pair if scope is empty.")
    sub.add_parser(
        "reseed",
        help="Replace OAuth tokens under refresh-lease fencing (no remote OAuth).",
    )

    reconcile = sub.add_parser(
        "resolve-reconcile",
        help="Activate RECONCILE_REQUIRED TECHNICAL_DEAL after CRM GET validation.",
    )
    reconcile.add_argument(
        "--conversation-id",
        required=True,
        help="Bot-TV conversation UUID.",
    )
    revision = sub.add_parser(
        "controlled-revision", help="Run the fixed PROGREV revision executor."
    )
    revision.add_argument("--lead-id", required=True, type=int)
    revision.add_argument("--complete-till", required=True, type=int)
    revision.add_argument("--apply", action="store_true")
    move_only = sub.add_parser("controlled-move-only", help="Move an active-task PROGREV lead without task changes.")
    move_only.add_argument("--lead-id", required=True, type=int)
    move_only.add_argument("--apply", action="store_true")
    reconcile.add_argument(
        "--confirmed-deal-id",
        required=True,
        help="Operator-confirmed numeric amoCRM lead id.",
    )
    return parser


def _read_token_pair() -> tuple[str, str]:
    access = read_secret_line(
        "AMOCRM CRM access token: ",
        stdin_isatty=sys.stdin.isatty,
        getpass_fn=getpass.getpass,
        stdin_readline=sys.stdin.readline,
    )
    refresh = read_secret_line(
        "AMOCRM CRM refresh token: ",
        stdin_isatty=sys.stdin.isatty,
        getpass_fn=getpass.getpass,
        stdin_readline=sys.stdin.readline,
    )
    return access, refresh


async def _run(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv))
    settings = Settings.from_env(environ)
    if settings.database_url is None:
        _log_safely(logging.ERROR, "amocrm_crm_ops_failed", error_code="DATABASE_URL_REQUIRED")
        print("amocrm_crm_ops failed error_code=DATABASE_URL_REQUIRED", file=sys.stderr)
        return 2

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        rest_config = load_crm_rest_config_fail_closed(environ)
    except AmoCrmCrmRestConfigError as exc:
        code = str(exc.args[0]) if exc.args else "AMOCRM_CRM_REST_CONFIG_INVALID"
        if type(code) is not str or len(code) > 128:
            code = "AMOCRM_CRM_REST_CONFIG_INVALID"
        _log_safely(logging.ERROR, "amocrm_crm_ops_failed", error_code=code)
        print(f"amocrm_crm_ops failed error_code={code}", file=sys.stderr)
        return 2
    service = AmoCrmCrmOpsService(session_factory, rest_config=rest_config)
    scope_label = _safe_scope_label(service.connection_scope)
    try:
        if args.command in {"bootstrap", "reseed"}:
            access, refresh = _read_token_pair()
            if args.command == "bootstrap":
                result = await service.bootstrap_oauth(
                    access_token=access, refresh_token=refresh
                )
            else:
                result = await service.reseed_oauth(
                    access_token=access, refresh_token=refresh
                )
        elif args.command == "resolve-reconcile":
            result = await service.resolve_reconcile(
                conversation_id=args.conversation_id,
                confirmed_deal_id=args.confirmed_deal_id,
            )
        elif args.command == "controlled-revision":
            # Deliberately lazy: api/worker/chat never import this offline-only path.
            receipt = await service.run_controlled_revision(
                lead_id=args.lead_id, complete_till=args.complete_till, apply=args.apply
            )
            print(
                "controlled_revision "
                f"lead_id={receipt.lead_id} outcome={receipt.outcome} "
                f"task_id={receipt.task_id or '-'} error_code={receipt.error_code or '-'}"
            )
            return 0 if receipt.outcome in {"APPLIED", "DRY_RUN"} else 1
        elif args.command == "controlled-move-only":
            receipt = await service.run_controlled_move_only(lead_id=args.lead_id, apply=args.apply)
            print(f"controlled_move_only lead_id={receipt.lead_id} outcome={receipt.outcome} error_code={receipt.error_code or '-'}")
            return 0 if receipt.outcome in {"APPLIED", "DRY_RUN"} else 1
        else:
            return 2
    finally:
        await engine.dispose()

    safe_code = (
        result.error_code
        if type(result.error_code) is str and 0 < len(result.error_code) <= 128
        else "-"
    )
    print(
        "amocrm_crm_ops "
        f"outcome={result.outcome.value} "
        f"error_code={safe_code} "
        f"connection_scope={scope_label}"
    )
    _log_safely(
        logging.INFO,
        "amocrm_crm_ops_completed",
        outcome=result.outcome.value,
        error_code=safe_code,
        connection_scope=scope_label,
    )
    if result.outcome in {
        AmoCrmOpsOutcome.SEEDED,
        AmoCrmOpsOutcome.RESEEDED,
        AmoCrmOpsOutcome.RECONCILE_ACTIVATED,
    }:
        return 0
    if result.outcome is AmoCrmOpsOutcome.ALREADY_PRESENT:
        return 1
    if result.outcome is AmoCrmOpsOutcome.REFUSED:
        return 1
    if result.outcome is AmoCrmOpsOutcome.TRANSIENT_ERROR:
        return 3
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
    except ValueError as exc:
        code = str(exc.args[0]) if exc.args else type(exc).__name__
        if type(code) is not str or len(code) > 128:
            code = type(exc).__name__
        print(f"amocrm_crm_ops failed error_code={code}", file=sys.stderr)
        return 2
    except Exception:
        print(
            f"amocrm_crm_ops failed error_code={type(sys.exc_info()[1]).__name__}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
