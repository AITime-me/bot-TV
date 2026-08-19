from __future__ import annotations

from pathlib import Path


def test_client_ref_resolver_service_has_no_http_or_crm_surface() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "app" / "services" / "client_ref_resolution.py"
    text = path.read_text(encoding="utf-8")

    # Resolver boundary must not perform network I/O.
    for banned in (
        "import httpx",
        "import aiohttp",
        "import requests",
        "urllib",
        "http://",
        "https://",
    ):
        assert banned not in text

    # Also ensure we don't couple to amoCRM / online-zapis adapters.
    for banned in (
        "AmoCrm",
        "send_silent_text",
    ):
        assert banned not in text


def test_client_ref_resolver_service_has_no_writes() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "app" / "services" / "client_ref_resolution.py"
    text = path.read_text(encoding="utf-8")

    # Read-only resolver should not insert/update.
    for banned in (
        ".add(",
        "insert(",
        "update(",
        "delete(",
        "mark_",
        "set_conversation_",
    ):
        assert banned not in text

