"""AMO-01B2-OPS unit coverage: bootstrap/reseed/reconcile fail-closed."""

from __future__ import annotations

import base64
import json
import secrets
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.amocrm_crm_oauth_keys import EnvAmoCrmOauthKeyProvider
from app.core.amocrm_crm_oauth_types import KEY_SIZE_BYTES
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.services.amocrm_crm_ops import (
    AmoCrmCrmOpsService,
    AmoCrmOpsOutcome,
    read_secret_line,
)

_REPO = Path(__file__).resolve().parents[1]
_KEY = secrets.token_bytes(KEY_SIZE_BYTES)
_KEY_B64 = base64.urlsafe_b64encode(_KEY).decode("ascii")


def _provider() -> EnvAmoCrmOauthKeyProvider:
    return EnvAmoCrmOauthKeyProvider(
        {
            "AMOCRM_CRM_OAUTH_ACTIVE_KEY_ID": "K1",
            "AMOCRM_CRM_OAUTH_KEY_K1": _KEY_B64,
        }
    )


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[S2sHttpRequest] = []
        self.responses: list[S2sHttpResponse] = []

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        if not self.responses:
            raise AssertionError(f"no response for {req.method} {req.url}")
        return self.responses.pop(0)


def _enabled_rest() -> AmoCrmCrmRestConfig:
    return AmoCrmCrmRestConfig(
        enabled=True,
        client_id="cid",
        client_secret="csecret12",
        api_base_url="https://example.amocrm.ru",
        connection_scope="default",
    )


def test_read_secret_line_rejects_whitespace_and_empty() -> None:
    with pytest.raises(ValueError, match="TOKEN_INVALID"):
        read_secret_line(
            "p: ",
            stdin_isatty=lambda: False,
            getpass_fn=lambda _p: "x",
            stdin_readline=lambda: "\n",
        )
    with pytest.raises(ValueError, match="TOKEN_INVALID"):
        read_secret_line(
            "p: ",
            stdin_isatty=lambda: True,
            getpass_fn=lambda _p: "has space",
            stdin_readline=lambda: "x",
        )


def test_read_secret_line_uses_getpass_on_tty() -> None:
    seen: list[str] = []

    def _gp(prompt: str) -> str:
        seen.append(prompt)
        return "tok-value-1"

    out = read_secret_line(
        "Access: ",
        stdin_isatty=lambda: True,
        getpass_fn=_gp,
        stdin_readline=lambda: "should-not-run",
    )
    assert out == "tok-value-1"
    assert seen == ["Access: "]


def test_cli_parser_has_no_token_arguments() -> None:
    from app.amocrm_crm_ops import _build_parser

    source = (_REPO / "app" / "amocrm_crm_ops.py").read_text(encoding="utf-8")
    assert "access_token" not in source or "getpass" in source
    assert "--access" not in source
    assert "--refresh" not in source
    parser = _build_parser()
    help_text = parser.format_help()
    assert "access" not in help_text.lower() or "getpass" in source
    assert "--access-token" not in help_text
    assert "--refresh-token" not in help_text


def test_ops_modules_never_mix_chat_hmac() -> None:
    for rel in (
        "app/services/amocrm_crm_ops.py",
        "app/amocrm_crm_ops.py",
    ):
        text = (_REPO / rel).read_text(encoding="utf-8")
        assert "AMOCRM_CHAT_" not in text or "separate" in text.lower()
        assert "channel_secret" not in text
        assert "X-Signature" not in text
        assert "/api/v4/leads" not in text or "GET" in text
        assert "create_lead" not in text


def test_ops_service_repr_and_result_redact() -> None:
    result = __import__(
        "app.services.amocrm_crm_ops", fromlist=["AmoCrmOpsResult"]
    ).AmoCrmOpsResult(
        outcome=AmoCrmOpsOutcome.SEEDED,
        error_code=None,
    )
    assert "token" not in repr(result).lower() or "<redacted>" in repr(result)


@pytest.mark.asyncio
async def test_resolve_disabled_crm_refuses_zero_http() -> None:
    transport = _FakeTransport()
    service = AmoCrmCrmOpsService(
        AsyncMock(),
        key_provider=_provider(),
        rest_config=AmoCrmCrmRestConfig(enabled=False),
        transport=transport,
    )
    result = await service.resolve_reconcile(
        conversation_id=str(uuid4()),
        confirmed_deal_id="42",
    )
    assert result.outcome is AmoCrmOpsOutcome.REFUSED
    assert result.error_code == "AMOCRM_CRM_REST_DISABLED"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_resolve_invalid_ids_fail_closed_zero_http() -> None:
    transport = _FakeTransport()
    service = AmoCrmCrmOpsService(
        AsyncMock(),
        key_provider=_provider(),
        rest_config=_enabled_rest(),
        transport=transport,
    )
    bad_conv = await service.resolve_reconcile(
        conversation_id="not-a-uuid",
        confirmed_deal_id="42",
    )
    assert bad_conv.outcome is AmoCrmOpsOutcome.PERMANENT_ERROR
    assert bad_conv.error_code == "CONVERSATION_ID_INVALID"
    bad_deal = await service.resolve_reconcile(
        conversation_id=str(uuid4()),
        confirmed_deal_id="abc",
    )
    assert bad_deal.outcome is AmoCrmOpsOutcome.PERMANENT_ERROR
    assert bad_deal.error_code == "EXTERNAL_ID_INVALID"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_reseed_invalid_token_zero_store() -> None:
    service = AmoCrmCrmOpsService(
        AsyncMock(),
        key_provider=_provider(),
        rest_config=AmoCrmCrmRestConfig(enabled=False),
    )
    result = await service.reseed_oauth(access_token="", refresh_token="r")
    assert result.outcome is AmoCrmOpsOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_OAUTH_TOKEN_INVALID"


def test_ops_uses_scope_when_crm_rest_disabled() -> None:
    service = AmoCrmCrmOpsService(
        AsyncMock(),
        key_provider=_provider(),
        rest_config=AmoCrmCrmRestConfig(
            enabled=False, connection_scope="custom-ops-scope"
        ),
    )
    assert service.connection_scope == "custom-ops-scope"


def test_cli_reports_connection_scope_safely() -> None:
    from app.amocrm_crm_ops import _safe_scope_label

    assert _safe_scope_label("prod-scope") == "prod-scope"
    assert _safe_scope_label("has space") == "-"
    assert _safe_scope_label("") == "-"
    assert _safe_scope_label("x" * 65) == "-"


def test_ops_not_required_in_docker_worker_allowlist() -> None:
    """Offline CLI stays out of default Docker runtime allowlist."""

    from tests.docker_runtime_allowlist import (
        AMO01B2_DOCKER_RUNTIME_PATHS,
        is_included_in_docker_build_context,
    )

    assert "app/amocrm_crm_ops.py" not in AMO01B2_DOCKER_RUNTIME_PATHS
    assert "app/services/amocrm_crm_ops.py" not in AMO01B2_DOCKER_RUNTIME_PATHS
    assert (
        is_included_in_docker_build_context(
            "app/amocrm_crm_ops.py", repo_root=_REPO
        )
        is False
    )


def test_lead_create_not_imported_by_ops_cli() -> None:
    source = (_REPO / "app" / "services" / "amocrm_crm_ops.py").read_text(
        encoding="utf-8"
    )
    assert "create_lead" not in source
    assert "POST_LEAD" not in source
    assert "get_lead" in source
