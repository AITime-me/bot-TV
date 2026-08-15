"""Offline amoCRM Chat binding ops (AMO-PROD-ENABLEMENT-OPS-01).

Usage:
  python -B -m app.amocrm_chat_binding_ops seed-binding \\
    --conversation-id UUID \\
    --amocrm-chat-id CHAT_ID \\
    --integration-conversation-id INTEG_CID

Seeds one ACTIVE conversation↔chat binding. No Chat HTTP, CRM REST, discovery,
or bulk. Identical existing binding is idempotent (exit 0). NULL integ may be
filled once (UPDATED, exit 0). Conflicts fail closed (exit 2).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from collections.abc import Mapping, Sequence

from app.config import Settings
from app.db.session import create_engine, create_session_factory
from app.services.amocrm_chat_binding_ops import (
    AmoCrmChatBindingOpsOutcome,
    seed_active_chat_binding,
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amocrm_chat_binding_ops",
        description=(
            "Offline Chat binding seed "
            "(explicit ids only; no Chat HTTP / CRM REST)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser(
        "seed-binding",
        help="Insert or confirm one ACTIVE chat binding.",
    )
    seed.add_argument(
        "--conversation-id",
        required=True,
        help="Bot-TV conversation UUID.",
    )
    seed.add_argument(
        "--amocrm-chat-id",
        required=True,
        help="amoCRM chat id (exact).",
    )
    seed.add_argument(
        "--integration-conversation-id",
        required=True,
        help="Chat API integration conversation id (exact).",
    )
    return parser


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
            "amocrm_chat_binding_ops_failed",
            error_code="DATABASE_URL_REQUIRED",
        )
        print(
            "amocrm_chat_binding_ops failed error_code=DATABASE_URL_REQUIRED",
            file=sys.stderr,
        )
        return 2

    try:
        conversation_id = uuid.UUID(args.conversation_id)
    except ValueError:
        _log_safely(
            logging.ERROR,
            "amocrm_chat_binding_ops_failed",
            error_code="CONVERSATION_ID_INVALID",
        )
        print(
            "amocrm_chat_binding_ops failed error_code=CONVERSATION_ID_INVALID",
            file=sys.stderr,
        )
        return 2

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        if args.command == "seed-binding":
            result = await seed_active_chat_binding(
                session_factory,
                conversation_id=conversation_id,
                amocrm_chat_id=args.amocrm_chat_id,
                integration_conversation_id=args.integration_conversation_id,
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
    print(
        "amocrm_chat_binding_ops "
        f"outcome={result.outcome.value} "
        f"created={str(result.created).lower()} "
        f"error_code={safe_code}"
    )
    _log_safely(
        logging.INFO,
        "amocrm_chat_binding_ops_completed",
        outcome=result.outcome.value,
        created=str(result.created).lower(),
        error_code=safe_code,
    )
    if result.outcome in {
        AmoCrmChatBindingOpsOutcome.SEEDED,
        AmoCrmChatBindingOpsOutcome.UPDATED,
        AmoCrmChatBindingOpsOutcome.ALREADY_PRESENT,
    }:
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
            "amocrm_chat_binding_ops failed "
            f"error_code={type(sys.exc_info()[1]).__name__}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
