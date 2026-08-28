"""AMO-01B2 foundation unit coverage: CRM REST config, crypto, auth separation."""

from __future__ import annotations

import base64
import json
import secrets
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.amocrm_chat_egress_config import AmoCrmChatEgressConfig
from app.core.amocrm_crm_oauth_crypto import decrypt_token, encrypt_token
from app.core.amocrm_crm_oauth_keys import (
    ActiveAmoCrmOauthKey,
    EnvAmoCrmOauthKeyProvider,
)
from app.core.amocrm_crm_oauth_types import (
    CRYPTO_VERSION_V1,
    KEY_SIZE_BYTES,
    AmoCrmCrmOauthError,
    AmoCrmOauthAad,
    AmoCrmOauthTokenKind,
)
from app.core.amocrm_crm_rest_config import (
    AmoCrmCrmRestConfig,
    AmoCrmCrmRestConfigError,
    load_crm_rest_config_fail_closed,
)
from app.core.amocrm_crm_rest_http import (
    AmoCrmCrmRestHttpClient,
    AmoCrmCrmRestOutcome,
)
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.models.amocrm_crm_oauth_token import AmocrmCrmOauthToken
from app.models.amocrm_entity_link import AmocrmEntityLink
from tests.docker_runtime_allowlist import (
    AMO01B2_DOCKER_RUNTIME_PATHS,
    assert_canonical_docker_runtime_allowlist,
    is_included_in_docker_build_context,
)

_REPO = Path(__file__).resolve().parents[1]
_KEY = secrets.token_bytes(KEY_SIZE_BYTES)
_KEY_B64 = base64.urlsafe_b64encode(_KEY).decode("ascii")
_CLIENT_ID = "crm-client-id-001"
_CLIENT_SECRET = "crm-secret-" + ("x" * 16)
_REDIRECT_URI = "https://example.com/oauth"


def _oauth_env(**extra: str) -> dict[str, str]:
    env = {
        "AMOCRM_CRM_OAUTH_ACTIVE_KEY_ID": "K1",
        "AMOCRM_CRM_OAUTH_KEY_K1": _KEY_B64,
    }
    env.update(extra)
    return env


def _valid_crm_env(**extra: str) -> dict[str, str]:
    env = {
        "AMOCRM_CRM_REST_ENABLED": "true",
        "AMOCRM_CLIENT_ID": _CLIENT_ID,
        "AMOCRM_CLIENT_SECRET": _CLIENT_SECRET,
        "AMOCRM_CRM_API_BASE_URL": "https://example.amocrm.ru",
        "AMOCRM_CRM_REDIRECT_URI": _REDIRECT_URI,
    }
    env.update(extra)
    return env


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[S2sHttpRequest] = []
        self.responses: list[S2sHttpResponse] = []

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        if not self.responses:
            raise AssertionError("no fake response queued")
        return self.responses.pop(0)


def test_crm_rest_default_off() -> None:
    config = AmoCrmCrmRestConfig.from_env({})
    assert config.enabled is False
    assert config.connection_scope == "default"
    assert config.redirect_uri is None


def test_crm_rest_disabled_without_redirect_uri() -> None:
    config = AmoCrmCrmRestConfig.from_env({"AMOCRM_CRM_REST_ENABLED": "false"})
    assert config.enabled is False
    assert config.redirect_uri is None
    closed = load_crm_rest_config_fail_closed({})
    assert closed.enabled is False
    assert closed.redirect_uri is None


def test_crm_rest_disabled_preserves_explicit_connection_scope() -> None:
    config = AmoCrmCrmRestConfig.from_env(
        {
            "AMOCRM_CRM_REST_ENABLED": "false",
            "AMOCRM_CRM_CONNECTION_SCOPE": "prod-scope",
        }
    )
    assert config.enabled is False
    assert config.connection_scope == "prod-scope"
    closed = load_crm_rest_config_fail_closed(
        {
            "AMOCRM_CRM_REST_ENABLED": "false",
            "AMOCRM_CRM_CONNECTION_SCOPE": "prod-scope",
        }
    )
    assert closed.enabled is False
    assert closed.connection_scope == "prod-scope"


def test_crm_rest_invalid_connection_scope_fail_closed() -> None:
    with pytest.raises(AmoCrmCrmRestConfigError, match="CONNECTION_SCOPE_INVALID"):
        AmoCrmCrmRestConfig.from_env(
            {
                "AMOCRM_CRM_REST_ENABLED": "false",
                "AMOCRM_CRM_CONNECTION_SCOPE": "bad scope",
            }
        )
    with pytest.raises(AmoCrmCrmRestConfigError, match="CONNECTION_SCOPE_INVALID"):
        load_crm_rest_config_fail_closed(
            {
                "AMOCRM_CRM_REST_ENABLED": "false",
                "AMOCRM_CRM_CONNECTION_SCOPE": "bad scope",
            }
        )


def test_fail_closed_preserves_scope_when_credentials_missing() -> None:
    config = load_crm_rest_config_fail_closed(
        {
            "AMOCRM_CRM_REST_ENABLED": "true",
            "AMOCRM_CRM_CONNECTION_SCOPE": "ops-scope",
        }
    )
    assert config.enabled is False
    assert config.connection_scope == "ops-scope"


def test_crm_rest_enabled_missing_credentials_raises() -> None:
    with pytest.raises(AmoCrmCrmRestConfigError, match="CLIENT_ID_REQUIRED"):
        AmoCrmCrmRestConfig.from_env({"AMOCRM_CRM_REST_ENABLED": "true"})
    with pytest.raises(AmoCrmCrmRestConfigError, match="CLIENT_SECRET_REQUIRED"):
        AmoCrmCrmRestConfig.from_env(
            {
                "AMOCRM_CRM_REST_ENABLED": "true",
                "AMOCRM_CLIENT_ID": _CLIENT_ID,
            }
        )
    with pytest.raises(AmoCrmCrmRestConfigError, match="REDIRECT_URI_REQUIRED"):
        AmoCrmCrmRestConfig.from_env(
            {
                "AMOCRM_CRM_REST_ENABLED": "true",
                "AMOCRM_CLIENT_ID": _CLIENT_ID,
                "AMOCRM_CLIENT_SECRET": _CLIENT_SECRET,
            }
        )


def test_crm_rest_enabled_invalid_redirect_uri_raises() -> None:
    with pytest.raises(AmoCrmCrmRestConfigError, match="REDIRECT_URI_INVALID"):
        AmoCrmCrmRestConfig.from_env(
            _valid_crm_env(AMOCRM_CRM_REDIRECT_URI="http://insecure.example/oauth")
        )
    with pytest.raises(AmoCrmCrmRestConfigError, match="REDIRECT_URI_INVALID"):
        AmoCrmCrmRestConfig.from_env(
            _valid_crm_env(AMOCRM_CRM_REDIRECT_URI="https://example.com/o auth")
        )


def test_enabled_missing_or_invalid_redirect_fail_closed_zero_oauth_http() -> None:
    transport = _FakeTransport()
    for environ in (
        {
            "AMOCRM_CRM_REST_ENABLED": "true",
            "AMOCRM_CLIENT_ID": _CLIENT_ID,
            "AMOCRM_CLIENT_SECRET": _CLIENT_SECRET,
            "AMOCRM_CRM_API_BASE_URL": "https://example.amocrm.ru",
        },
        _valid_crm_env(AMOCRM_CRM_REDIRECT_URI="http://bad.example/oauth"),
        _valid_crm_env(AMOCRM_CRM_REDIRECT_URI=""),
    ):
        config = load_crm_rest_config_fail_closed(environ)
        assert config.enabled is False
        assert config.redirect_uri is None
        client = AmoCrmCrmRestHttpClient(
            config,
            session_factory=object(),  # type: ignore[arg-type]
            transport=transport,
        )
        outcome, _ = client.authorized_get(path="/api/v4/account", access_token="t")
        assert outcome is AmoCrmCrmRestOutcome.DISABLED
    assert transport.calls == []


@pytest.mark.asyncio
async def test_enabled_invalid_redirect_refresh_zero_oauth_http() -> None:
    transport = _FakeTransport()
    config = load_crm_rest_config_fail_closed(
        _valid_crm_env(AMOCRM_CRM_REDIRECT_URI="http://bad.example/oauth")
    )
    assert config.enabled is False
    client = AmoCrmCrmRestHttpClient(
        config,
        session_factory=object(),  # type: ignore[arg-type]
        transport=transport,
    )
    result = await client.refresh_tokens()
    assert result.outcome is AmoCrmCrmRestOutcome.DISABLED
    assert transport.calls == []
    assert client.http_calls == []


@pytest.mark.asyncio
async def test_refresh_disabled_config_zero_oauth_http_without_redirect() -> None:
    transport = _FakeTransport()
    client = AmoCrmCrmRestHttpClient(
        AmoCrmCrmRestConfig(enabled=False),
        session_factory=object(),  # type: ignore[arg-type]
        transport=transport,
    )
    result = await client.refresh_tokens()
    assert result.outcome is AmoCrmCrmRestOutcome.DISABLED
    assert transport.calls == []
    assert client.http_calls == []


def test_load_crm_rest_config_fail_closed_does_not_raise() -> None:
    config = load_crm_rest_config_fail_closed({"AMOCRM_CRM_REST_ENABLED": "true"})
    assert config.enabled is False


@pytest.mark.parametrize(
    "environ",
    [
        {"AMOCRM_CRM_REST_ENABLED": "yes"},
        {
            "AMOCRM_CRM_REST_ENABLED": "true",
            "AMOCRM_CLIENT_ID": _CLIENT_ID,
            "AMOCRM_CLIENT_SECRET": "short",
        },
        {
            "AMOCRM_CRM_REST_ENABLED": "true",
            "AMOCRM_CLIENT_ID": _CLIENT_ID,
            "AMOCRM_CLIENT_SECRET": _CLIENT_SECRET,
            "AMOCRM_CRM_API_BASE_URL": "http://insecure.example",
            "AMOCRM_CRM_REDIRECT_URI": _REDIRECT_URI,
        },
        {
            "AMOCRM_CRM_REST_ENABLED": "true",
            "AMOCRM_CLIENT_ID": _CLIENT_ID,
            "AMOCRM_CLIENT_SECRET": _CLIENT_SECRET,
            "AMOCRM_CRM_API_BASE_URL": "https://example.amocrm.ru",
            "AMOCRM_CRM_REDIRECT_URI": "http://insecure.example/oauth",
        },
    ],
)
def test_invalid_enabled_config_fail_closed(environ: dict[str, str]) -> None:
    assert load_crm_rest_config_fail_closed(environ).enabled is False


def test_valid_crm_config_preserves_exact_redirect_uri() -> None:
    exact = "https://example.com/oauth/callback/"
    config = AmoCrmCrmRestConfig.from_env(
        _valid_crm_env(AMOCRM_CRM_REDIRECT_URI=exact)
    )
    assert config.enabled is True
    assert config.redirect_uri == exact


def test_valid_crm_config_redacts_secrets() -> None:
    config = AmoCrmCrmRestConfig.from_env(_valid_crm_env())
    assert config.enabled is True
    rendered = repr(config)
    assert _CLIENT_SECRET not in rendered
    assert _CLIENT_ID not in rendered
    assert "client_secret=<redacted>" in rendered


def test_chat_and_crm_auth_are_separate() -> None:
    chat = AmoCrmChatEgressConfig.from_env(
        {
            "AMOCRM_CHAT_EGRESS_ENABLED": "true",
            "AMOCRM_CHAT_CHANNEL_SECRET": "s" * 32,
            "AMOCRM_CHAT_SCOPE_ID": "scope-chat-1",
        }
    )
    crm = AmoCrmCrmRestConfig.from_env(_valid_crm_env())
    assert chat.enabled is True
    assert crm.enabled is True
    # Chat-only env must not enable CRM REST.
    chat_only = load_crm_rest_config_fail_closed(
        {
            "AMOCRM_CHAT_EGRESS_ENABLED": "true",
            "AMOCRM_CHAT_CHANNEL_SECRET": "s" * 32,
            "AMOCRM_CHAT_SCOPE_ID": "scope-chat-1",
        }
    )
    assert chat_only.enabled is False
    # CRM-only env must not enable Chat egress.
    chat_from_crm = AmoCrmChatEgressConfig.from_env(_valid_crm_env())
    assert chat_from_crm.enabled is False
    http_src = (_REPO / "app" / "core" / "amocrm_crm_rest_http.py").read_text(
        encoding="utf-8"
    )
    assert "X-Signature" not in http_src
    assert "channel_secret" not in http_src
    assert "Bearer" in http_src
    assert "build_amocrm_chat_signature" not in http_src


def test_oauth_encrypt_decrypt_roundtrip_and_redaction() -> None:
    provider = EnvAmoCrmOauthKeyProvider(_oauth_env())
    active = provider.get_active_key()
    aad = AmoCrmOauthAad(
        crypto_version=CRYPTO_VERSION_V1,
        key_id=active.key_id,
        connection_scope="default",
        token_kind=AmoCrmOauthTokenKind.ACCESS,
    )
    plaintext = "access-token-value-001"
    encrypted = encrypt_token(
        plaintext, aad=aad, key_provider=provider, active_key=active
    )
    assert plaintext.encode("utf-8") not in encrypted.ciphertext
    assert plaintext not in repr(encrypted)
    assert _KEY_B64 not in repr(active)
    assert plaintext not in repr(ActiveAmoCrmOauthKey(key_id="K1", key=_KEY))
    out = decrypt_token(encrypted, aad=aad, key_provider=provider)
    assert out == plaintext


def test_oauth_aad_kind_mismatch_denies() -> None:
    provider = EnvAmoCrmOauthKeyProvider(_oauth_env())
    active = provider.get_active_key()
    encrypted = encrypt_token(
        "refresh-token-value",
        aad=AmoCrmOauthAad(
            crypto_version=CRYPTO_VERSION_V1,
            key_id=active.key_id,
            connection_scope="default",
            token_kind=AmoCrmOauthTokenKind.REFRESH,
        ),
        key_provider=provider,
        active_key=active,
    )
    with pytest.raises(AmoCrmCrmOauthError, match="ACCESS_DENIED"):
        decrypt_token(
            encrypted,
            aad=AmoCrmOauthAad(
                crypto_version=CRYPTO_VERSION_V1,
                key_id=active.key_id,
                connection_scope="default",
                token_kind=AmoCrmOauthTokenKind.ACCESS,
            ),
            key_provider=provider,
        )


def test_orm_repr_redacts_secrets() -> None:
    token_row = AmocrmCrmOauthToken(
        id=uuid4(),
        connection_scope="default",
        key_id="K1",
        crypto_version=1,
        access_nonce=b"\x00" * 12,
        access_ciphertext=b"\x01" * 16,
        refresh_nonce=b"\x00" * 12,
        refresh_ciphertext=b"\x02" * 16,
        lease_version=0,
    )
    assert "access=<redacted>" in repr(token_row)
    assert b"\x01" * 16 not in repr(token_row).encode("utf-8", errors="ignore")
    link = AmocrmEntityLink(
        id=uuid4(),
        conversation_id=uuid4(),
        entity_kind="CONTACT",
        external_id="ext-secret-99",
        status="ACTIVE",
    )
    assert "ext-secret-99" not in repr(link)


def test_authorized_get_disabled_makes_zero_http() -> None:
    transport = _FakeTransport()
    client = AmoCrmCrmRestHttpClient(
        AmoCrmCrmRestConfig(enabled=False),
        session_factory=object(),  # type: ignore[arg-type]
        transport=transport,
    )
    outcome, response = client.authorized_get(path="/api/v4/account", access_token="t")
    assert outcome is AmoCrmCrmRestOutcome.DISABLED
    assert response is None
    assert transport.calls == []


def test_authorized_get_bearer_header_with_fake_transport() -> None:
    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(status_code=200, headers={}, body=b'{"id":1}')
    )
    client = AmoCrmCrmRestHttpClient(
        AmoCrmCrmRestConfig.from_env(_valid_crm_env()),
        session_factory=object(),  # type: ignore[arg-type]
        transport=transport,
    )
    outcome, response = client.authorized_get(
        path="/api/v4/account",
        access_token="tok-live-value",
    )
    assert outcome is AmoCrmCrmRestOutcome.SUCCESS
    assert response is not None
    assert transport.calls[0].headers["Authorization"] == "Bearer tok-live-value"
    assert "tok-live-value" not in repr(transport.calls[0])


def test_no_entity_write_helpers_on_http_client() -> None:
    names = dir(AmoCrmCrmRestHttpClient)
    for banned in (
        "create_contact",
        "create_deal",
        "create_note",
        "create_task",
        "post_entity",
    ):
        assert banned not in names


@pytest.mark.asyncio
async def test_post_200_stale_lease_recovers_without_second_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote refresh runs once; stale lease triggers guarded local recovery."""

    from contextlib import asynccontextmanager
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock

    from app.repositories import amocrm_crm_oauth_tokens as oauth_repo

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(
            status_code=200,
            headers={},
            body=(
                b'{"access_token":"access-after","refresh_token":"refresh-after",'
                b'"expires_in":3600}'
            ),
        )
    )
    lease = oauth_repo.OauthRefreshLease(
        token_row_id=uuid4(),
        connection_scope="default",
        lease_owner="w1",
        lease_token=uuid4(),
        lease_version=2,
        lease_until=datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    tokens = oauth_repo.DecryptedOauthTokens(
        access_token="access-before",
        refresh_token="refresh-before",
        access_expires_at=None,
    )
    row = object()
    rotate_calls = {"n": 0}
    recover_calls = {"n": 0}

    @asynccontextmanager
    async def _scope(_factory):  # type: ignore[no-untyped-def]
        yield AsyncMock()

    async def _claim(*_a, **_k):  # type: ignore[no-untyped-def]
        return lease

    async def _get(*_a, **_k):  # type: ignore[no-untyped-def]
        return row

    def _decrypt(*_a, **_k):  # type: ignore[no-untyped-def]
        return tokens

    async def _renew(*_a, **_k):  # type: ignore[no-untyped-def]
        return lease

    async def _rotate(*_a, **_k):  # type: ignore[no-untyped-def]
        rotate_calls["n"] += 1
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_STALE_LEASE")

    async def _recover(*_a, **_k):  # type: ignore[no-untyped-def]
        recover_calls["n"] += 1
        return row

    monkeypatch.setattr("app.core.amocrm_crm_rest_http.session_scope", _scope)
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.claim_refresh_lease", _claim
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.get_by_scope", _get
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.decrypt_row", _decrypt
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.renew_refresh_lease", _renew
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.rotate_tokens_with_lease",
        _rotate,
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.recover_rotate_if_pre_refresh_unchanged",
        _recover,
    )

    client = AmoCrmCrmRestHttpClient(
        AmoCrmCrmRestConfig.from_env(_valid_crm_env()),
        session_factory=object(),  # type: ignore[arg-type]
        transport=transport,
        worker_id="w1",
    )
    result = await client.refresh_tokens()
    assert result.outcome is AmoCrmCrmRestOutcome.SUCCESS
    assert len(transport.calls) == 1
    assert client.http_calls == ["REFRESH"]
    refresh_body = json.loads(transport.calls[0].body.decode("utf-8"))
    assert refresh_body["redirect_uri"] == _REDIRECT_URI
    assert refresh_body["grant_type"] == "refresh_token"
    assert rotate_calls["n"] == 1
    assert recover_calls["n"] == 1
    assert "refresh-after" not in repr(result)
    assert "access-after" not in repr(result)


@pytest.mark.asyncio
async def test_post_200_superseded_fails_closed_no_second_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock

    from app.repositories import amocrm_crm_oauth_tokens as oauth_repo

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(
            status_code=200,
            headers={},
            body=(
                b'{"access_token":"access-after","refresh_token":"refresh-after",'
                b'"expires_in":3600}'
            ),
        )
    )
    lease = oauth_repo.OauthRefreshLease(
        token_row_id=uuid4(),
        connection_scope="default",
        lease_owner="w1",
        lease_token=uuid4(),
        lease_version=2,
        lease_until=datetime.now(timezone.utc) + timedelta(seconds=60),
    )

    @asynccontextmanager
    async def _scope(_factory):  # type: ignore[no-untyped-def]
        yield AsyncMock()

    monkeypatch.setattr("app.core.amocrm_crm_rest_http.session_scope", _scope)
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.claim_refresh_lease",
        AsyncMock(return_value=lease),
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.get_by_scope",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.decrypt_row",
        lambda *_a, **_k: oauth_repo.DecryptedOauthTokens(
            access_token="access-before",
            refresh_token="refresh-before",
            access_expires_at=None,
        ),
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.renew_refresh_lease",
        AsyncMock(return_value=lease),
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.rotate_tokens_with_lease",
        AsyncMock(
            side_effect=AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_STALE_LEASE")
        ),
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.recover_rotate_if_pre_refresh_unchanged",
        AsyncMock(
            side_effect=AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_ROTATE_SUPERSEDED")
        ),
    )

    client = AmoCrmCrmRestHttpClient(
        AmoCrmCrmRestConfig.from_env(_valid_crm_env()),
        session_factory=object(),  # type: ignore[arg-type]
        transport=transport,
        worker_id="w1",
    )
    result = await client.refresh_tokens()
    assert result.outcome is AmoCrmCrmRestOutcome.PERMANENT_ERROR
    assert result.error_code == "AMOCRM_CRM_OAUTH_ROTATE_SUPERSEDED"
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_refresh_request_includes_exact_redirect_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock

    from app.repositories import amocrm_crm_oauth_tokens as oauth_repo

    exact = "https://integration.example/amocrm/oauth/callback/"
    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(
            status_code=200,
            headers={},
            body=(
                b'{"access_token":"access-after","refresh_token":"refresh-after",'
                b'"expires_in":3600}'
            ),
        )
    )
    lease = oauth_repo.OauthRefreshLease(
        token_row_id=uuid4(),
        connection_scope="default",
        lease_owner="w1",
        lease_token=uuid4(),
        lease_version=2,
        lease_until=datetime.now(timezone.utc) + timedelta(seconds=60),
    )

    @asynccontextmanager
    async def _scope(_factory):  # type: ignore[no-untyped-def]
        yield AsyncMock()

    monkeypatch.setattr("app.core.amocrm_crm_rest_http.session_scope", _scope)
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.claim_refresh_lease",
        AsyncMock(return_value=lease),
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.get_by_scope",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.decrypt_row",
        lambda *_a, **_k: oauth_repo.DecryptedOauthTokens(
            access_token="access-before",
            refresh_token="refresh-before",
            access_expires_at=None,
        ),
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.renew_refresh_lease",
        AsyncMock(return_value=lease),
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.rotate_tokens_with_lease",
        AsyncMock(return_value=object()),
    )

    client = AmoCrmCrmRestHttpClient(
        AmoCrmCrmRestConfig.from_env(
            _valid_crm_env(AMOCRM_CRM_REDIRECT_URI=exact)
        ),
        session_factory=object(),  # type: ignore[arg-type]
        transport=transport,
        worker_id="w1",
    )
    result = await client.refresh_tokens()
    assert result.outcome is AmoCrmCrmRestOutcome.SUCCESS
    assert len(transport.calls) == 1
    payload = json.loads(transport.calls[0].body.decode("utf-8"))
    assert payload["redirect_uri"] == exact
    assert list(payload.keys()) == [
        "client_id",
        "client_secret",
        "grant_type",
        "refresh_token",
        "redirect_uri",
    ]


@pytest.mark.asyncio
async def test_http_200_missing_expires_in_persists_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock

    from app.repositories import amocrm_crm_oauth_tokens as oauth_repo

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(
            status_code=200,
            headers={},
            body=(
                b'{"access_token":"access-after","refresh_token":"refresh-after"}'
            ),
        )
    )
    lease = oauth_repo.OauthRefreshLease(
        token_row_id=uuid4(),
        connection_scope="default",
        lease_owner="w1",
        lease_token=uuid4(),
        lease_version=2,
        lease_until=datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    persisted: dict[str, object] = {}

    @asynccontextmanager
    async def _scope(_factory):  # type: ignore[no-untyped-def]
        yield AsyncMock()

    async def _rotate(  # type: ignore[no-untyped-def]
        _session,
        *,
        lease,
        access_token,
        refresh_token,
        key_provider,
        access_expires_at=None,
        now=None,
    ):
        persisted["access_token"] = access_token
        persisted["refresh_token"] = refresh_token
        persisted["access_expires_at"] = access_expires_at
        return object()

    monkeypatch.setattr("app.core.amocrm_crm_rest_http.session_scope", _scope)
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.claim_refresh_lease",
        AsyncMock(return_value=lease),
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.get_by_scope",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.decrypt_row",
        lambda *_a, **_k: oauth_repo.DecryptedOauthTokens(
            access_token="access-before",
            refresh_token="refresh-before",
            access_expires_at=None,
        ),
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.renew_refresh_lease",
        AsyncMock(return_value=lease),
    )
    monkeypatch.setattr(
        "app.core.amocrm_crm_rest_http.oauth_repo.rotate_tokens_with_lease",
        _rotate,
    )

    client = AmoCrmCrmRestHttpClient(
        AmoCrmCrmRestConfig.from_env(_valid_crm_env()),
        session_factory=object(),  # type: ignore[arg-type]
        transport=transport,
        worker_id="w1",
    )
    result = await client.refresh_tokens()
    assert result.outcome is AmoCrmCrmRestOutcome.SUCCESS
    assert persisted["access_token"] == "access-after"
    assert persisted["refresh_token"] == "refresh-after"
    expires_at = persisted["access_expires_at"]
    assert isinstance(expires_at, datetime)
    assert expires_at > datetime.now(timezone.utc) + timedelta(hours=20)


def test_oauth_dual_write_residual_documented() -> None:
    src = (_REPO / "app" / "core" / "amocrm_crm_rest_http.py").read_text(
        encoding="utf-8"
    )
    adr = (_REPO / "docs" / "adr" / "004-amocrm-mirror.md").read_text(encoding="utf-8")
    assert "Residual window" in src or "residual" in src.lower()
    assert "ROTATE_PERSIST_FAILED" in adr or "ROTATE_PERSIST_FAILED" in src
    assert "never retries that remote POST" in src or "never retried" in adr.lower()


def test_amo01b2_docker_runtime_paths_allowlisted() -> None:
    assert_canonical_docker_runtime_allowlist()
    for rel in AMO01B2_DOCKER_RUNTIME_PATHS:
        assert is_included_in_docker_build_context(rel, repo_root=_REPO)
        assert (_REPO / rel).is_file()
