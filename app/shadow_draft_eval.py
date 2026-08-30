"""Operator CLI: real YandexGPT shadow evaluation harness (AI-EVAL-01).

Usage:
  python -B -m app.shadow_draft_eval source-proof
  python -B -m app.shadow_draft_eval run --allow-live-yandex [--output PATH]

Fetches published Settings/Knowledge/Live Facts via existing S2S clients.
Builds SYNTHETIC conversations only. Never reads real client inbox / PII.
Never mutates production BOT_MODE / EMERGENCY_LOCK. No outbound delivery.
No durable DB writes. Report is local artifact (do not commit live answers).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from app.config import Settings
from app.core.booking_eligibility_factory import build_booking_s2s_config
from app.core.s2s_http_stdlib import S2sHttpStdlibTransport
from app.core.shadow_draft_eval_types import redact_mapping_secrets
from app.core.yandex_llm_factory import build_text_generation_port
from app.services.shadow_draft_eval import (
    ShadowDraftEvalError,
    build_eval_generation_service,
    fetch_published_eval_sources,
    format_eval_report_markdown,
    run_shadow_draft_eval,
)

logger = logging.getLogger(__name__)

_FORBIDDEN_ARGV_FLAGS: tuple[str, ...] = (
    "--conversation-id",
    "--inbox",
    "--client-id",
    "--phone",
    "--amocrm",
)


def argv_has_forbidden_real_dialog_flag(argv: Sequence[str]) -> bool:
    for token in argv:
        if type(token) is not str:
            continue
        for flag in _FORBIDDEN_ARGV_FLAGS:
            if token == flag or token.startswith(f"{flag}="):
                return True
    return False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shadow_draft_eval",
        description=(
            "Synthetic shadow-draft evaluation against published Settings/KB/"
            "Live Facts. No real client dialogs. No outbound."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "source-proof",
        help="Fetch published sources and print redacted provenance only.",
    )

    run_cmd = sub.add_parser(
        "run",
        help=(
            "Run synthetic scenarios. Requires --allow-live-yandex and "
            "YANDEX_SHADOW_DRAFT_ENABLED=true for real YandexGPT calls."
        ),
    )
    run_cmd.add_argument(
        "--allow-live-yandex",
        action="store_true",
        default=False,
        help="Explicit operator consent for live YandexGPT calls.",
    )
    run_cmd.add_argument(
        "--output",
        default=None,
        help="Optional local path for JSON report (do not commit).",
    )
    run_cmd.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="markdown",
        help="Stdout report format (default: markdown).",
    )
    return parser


def _load_settings(environ: Mapping[str, str]) -> Settings:
    try:
        return Settings.from_env(environ)
    except Exception as exc:
        raise ShadowDraftEvalError("SETTINGS_ENV_INVALID") from exc


def _run_source_proof(environ: Mapping[str, str], stdout: TextIO) -> int:
    settings = _load_settings(environ)
    config = build_booking_s2s_config(settings)
    if config is None:
        raise ShadowDraftEvalError("BOOKING_ELIGIBILITY_NOT_CONFIGURED")
    sources = fetch_published_eval_sources(
        config=config,
        transport=S2sHttpStdlibTransport(),
    )
    payload = redact_mapping_secrets(sources.source_proof().as_dict())
    stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0


def _run_eval(
    environ: Mapping[str, str],
    *,
    allow_live_yandex: bool,
    output: str | None,
    fmt: str,
    stdout: TextIO,
) -> int:
    if not allow_live_yandex:
        raise ShadowDraftEvalError("LIVE_YANDEX_FLAG_REQUIRED")

    settings = _load_settings(environ)
    config = build_booking_s2s_config(settings)
    if config is None:
        raise ShadowDraftEvalError("BOOKING_ELIGIBILITY_NOT_CONFIGURED")

    port = build_text_generation_port(environ)
    if port is None:
        raise ShadowDraftEvalError("YANDEX_PROVIDER_NOT_CONFIGURED")

    service = build_eval_generation_service(port=port, environ=environ)
    sources = fetch_published_eval_sources(
        config=config,
        transport=S2sHttpStdlibTransport(),
    )
    report = run_shadow_draft_eval(
        sources=sources,
        service=service,
        allow_live_yandex=True,
        environ=environ,
    )
    payload = redact_mapping_secrets(report.as_dict())
    if "rawPrompt" in payload or payload.get("rawPromptIncluded") is True:
        raise ShadowDraftEvalError("RAW_PROMPT_LEAK_FORBIDDEN")

    if output:
        path = Path(output)
        if path.suffix.lower() == ".md":
            path.write_text(format_eval_report_markdown(report), encoding="utf-8")
        else:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    if fmt == "json":
        stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        stdout.write(format_eval_report_markdown(report) + "\n")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
) -> int:
    args_list = list(argv) if argv is not None else list(sys.argv[1:])
    out = stdout if stdout is not None else sys.stdout
    env = environ if environ is not None else os.environ

    if argv_has_forbidden_real_dialog_flag(args_list):
        print(
            "shadow_draft_eval failed error_code=REAL_CONVERSATION_FLAGS_FORBIDDEN",
            file=sys.stderr,
        )
        return 2

    parser = _build_parser()
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 2

    try:
        if args.command == "source-proof":
            return _run_source_proof(env, out)
        if args.command == "run":
            return _run_eval(
                env,
                allow_live_yandex=bool(args.allow_live_yandex),
                output=args.output,
                fmt=args.format,
                stdout=out,
            )
        raise ShadowDraftEvalError("COMMAND_UNKNOWN")
    except ShadowDraftEvalError as exc:
        print(f"shadow_draft_eval failed error_code={exc.code}", file=sys.stderr)
        return 2
    except Exception:
        print(
            f"shadow_draft_eval failed error_code={type(sys.exc_info()[1]).__name__}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
