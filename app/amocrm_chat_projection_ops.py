"""Offline amoCRM Chat projection ops (AMO-01B1b catch-up).

Usage:
  python -B -m app.amocrm_chat_projection_ops repair-bot-outbound --outbound-id UUID

Restores a durable BOT_OUTBOUND projection row for one DELIVERED outbound.
Does not POST to Chat. No bulk backfill.
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
from app.services.amocrm_chat_projection import repair_bot_outbound_projection

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
        prog="amocrm_chat_projection_ops",
        description="Offline Chat projection catch-up (id-scoped, no Chat HTTP).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    repair = sub.add_parser(
        "repair-bot-outbound",
        help="Enqueue BOT_OUTBOUND projection row for one DELIVERED outbound_id.",
    )
    repair.add_argument(
        "--outbound-id",
        required=True,
        help="Outbox message UUID (must be DELIVERED with persisted text).",
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
            "amocrm_chat_projection_ops_failed",
            error_code="DATABASE_URL_REQUIRED",
        )
        print(
            "amocrm_chat_projection_ops failed error_code=DATABASE_URL_REQUIRED",
            file=sys.stderr,
        )
        return 2

    try:
        outbound_id = uuid.UUID(args.outbound_id)
    except ValueError:
        _log_safely(
            logging.ERROR,
            "amocrm_chat_projection_ops_failed",
            error_code="OUTBOUND_ID_INVALID",
        )
        print(
            "amocrm_chat_projection_ops failed error_code=OUTBOUND_ID_INVALID",
            file=sys.stderr,
        )
        return 2

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        if args.command == "repair-bot-outbound":
            result = await repair_bot_outbound_projection(
                session_factory,
                outbound_id=outbound_id,
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
        "amocrm_chat_projection_ops "
        f"enqueued={str(result.enqueued).lower()} "
        f"created={str(result.created).lower()} "
        f"error_code={safe_code}"
    )
    _log_safely(
        logging.INFO,
        "amocrm_chat_projection_ops_completed",
        enqueued=str(result.enqueued).lower(),
        created=str(result.created).lower(),
        error_code=safe_code,
    )
    if result.enqueued:
        return 0
    if result.error_code == "EGRESS_DISABLED":
        return 1
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
            "amocrm_chat_projection_ops failed "
            f"error_code={type(sys.exc_info()[1]).__name__}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
