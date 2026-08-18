"""Offline amoCRM Buyer Card manual bind ops (IR-5).

Usage:
  python -B -m app.amocrm_buyer_card_bind_ops \\
    --canonical-identity-id UUID < approval.json

stdin JSON must contain only contact_id and buyer_card_id. Unknown keys fail
closed. External ids are never placed in argv, shell examples, logs, or errors.
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
from app.core.amocrm_buyer_card_bind import (
    AmoCrmBuyerCardBindOutcome,
    BuyerCardBindApproval,
)
from app.db.session import create_engine, create_session_factory
from app.services.amocrm_buyer_card_bind import AmoCrmBuyerCardBindService

logger = logging.getLogger(__name__)

_SUCCESS = frozenset(
    {
        AmoCrmBuyerCardBindOutcome.BOUND,
        AmoCrmBuyerCardBindOutcome.ALREADY_BOUND,
    }
)

_APPROVAL_KEYS = frozenset({"contact_id", "buyer_card_id"})

_SENSITIVE_ARGV_FLAGS: tuple[str, ...] = (
    "--contact-id",
    "--buyer-card-id",
    "--buyer_card_id",
    "--contact_id",
)


def argv_has_sensitive_legacy_flag(argv: Sequence[str]) -> bool:
    """True if argv uses banned external-id flags (``--flag`` or ``--flag=...``)."""

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
        prog="amocrm_buyer_card_bind_ops",
        description=(
            "Offline manual Buyer Card bind after live IR-2 + IR-3. "
            "External ids must come from stdin JSON, never argv."
        ),
    )
    parser.add_argument(
        "--canonical-identity-id",
        required=True,
        help="Existing bot-TV canonical identity UUID.",
    )
    return parser


def _required_id(value: object, *, code: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(code)
    stripped = value.strip()
    if not stripped or stripped != value:
        raise ValueError(code)
    return stripped


def parse_bind_approval_json(raw: str) -> BuyerCardBindApproval:
    """Parse stdin JSON. Fail closed on bad shape. Never echoes values."""

    if type(raw) is not str:
        raise ValueError("APPROVAL_STDIN_INVALID")
    text = raw.strip()
    if not text:
        raise ValueError("APPROVAL_STDIN_EMPTY")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("APPROVAL_STDIN_JSON_INVALID") from exc
    if type(payload) is not dict:
        raise ValueError("APPROVAL_STDIN_OBJECT_REQUIRED")
    unknown = set(payload.keys()) - _APPROVAL_KEYS
    if unknown:
        raise ValueError("APPROVAL_STDIN_UNKNOWN_KEYS")
    missing = _APPROVAL_KEYS - set(payload.keys())
    if missing:
        raise ValueError("APPROVAL_STDIN_KEYS_REQUIRED")
    return BuyerCardBindApproval(
        contact_id=_required_id(payload["contact_id"], code="APPROVAL_CONTACT_ID_INVALID"),
        buyer_card_id=_required_id(
            payload["buyer_card_id"], code="APPROVAL_BUYER_CARD_ID_INVALID"
        ),
    )


def _read_stdin_text(stdin: TextIO) -> str:
    return stdin.read()


async def _run(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
) -> int:
    if argv_has_sensitive_legacy_flag(argv):
        _log_safely(
            logging.ERROR,
            "amocrm_buyer_card_bind_ops_failed",
            error_code="SENSITIVE_ARGV_FORBIDDEN",
        )
        print(
            "amocrm_buyer_card_bind_ops failed error_code=SENSITIVE_ARGV_FORBIDDEN",
            file=sys.stderr,
        )
        return 2

    parser = _build_parser()
    args = parser.parse_args(list(argv))
    try:
        canonical_identity_id = uuid.UUID(args.canonical_identity_id)
    except ValueError:
        _log_safely(
            logging.ERROR,
            "amocrm_buyer_card_bind_ops_failed",
            error_code="CANONICAL_IDENTITY_ID_INVALID",
        )
        print(
            "amocrm_buyer_card_bind_ops failed error_code=CANONICAL_IDENTITY_ID_INVALID",
            file=sys.stderr,
        )
        return 2

    try:
        approval = parse_bind_approval_json(
            _read_stdin_text(stdin if stdin is not None else sys.stdin)
        )
    except ValueError as exc:
        code = str(exc.args[0]) if exc.args else "APPROVAL_STDIN_INVALID"
        if type(code) is not str or not code:
            code = "APPROVAL_STDIN_INVALID"
        _log_safely(
            logging.ERROR,
            "amocrm_buyer_card_bind_ops_failed",
            error_code=code,
        )
        print(
            f"amocrm_buyer_card_bind_ops failed error_code={code}",
            file=sys.stderr,
        )
        return 2

    settings = Settings.from_env(environ)
    if settings.database_url is None:
        _log_safely(
            logging.ERROR,
            "amocrm_buyer_card_bind_ops_failed",
            error_code="DATABASE_URL_REQUIRED",
        )
        print(
            "amocrm_buyer_card_bind_ops failed error_code=DATABASE_URL_REQUIRED",
            file=sys.stderr,
        )
        return 2

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        result = await AmoCrmBuyerCardBindService(session_factory=session_factory).bind_buyer_card(
            canonical_identity_id=canonical_identity_id,
            contact_id=approval.contact_id,
            buyer_card_id=approval.buyer_card_id,
        )
    finally:
        await engine.dispose()

    safe_code = (
        result.error_code
        if type(result.error_code) is str and 0 < len(result.error_code) <= 128
        else "-"
    )
    safe_reason = (
        result.reason
        if type(result.reason) is str and 0 < len(result.reason) <= 128
        else "-"
    )
    print(
        "amocrm_buyer_card_bind_ops "
        f"outcome={result.outcome.value} "
        f"reason={safe_reason} "
        f"error_code={safe_code}"
    )
    _log_safely(
        logging.INFO,
        "amocrm_buyer_card_bind_ops_completed",
        outcome=result.outcome.value,
        reason=safe_reason,
        error_code=safe_code,
    )
    if result.outcome in _SUCCESS:
        return 0
    if result.outcome is AmoCrmBuyerCardBindOutcome.TRANSIENT_ERROR:
        return 3
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
            "amocrm_buyer_card_bind_ops failed "
            f"error_code={type(sys.exc_info()[1]).__name__}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
