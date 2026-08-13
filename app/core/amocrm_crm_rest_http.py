"""Backend-only amoCRM CRM REST HTTP client (AMO-01B2 foundation).

Bearer auth + OAuth refresh fencing. No contact/deal/note/task writes.
Completely separate from Chat HMAC egress.

OAuth refresh dual-write residual window: amoCRM may accept refresh (HTTP 200
and invalidate the old refresh) before the first durable local rotate commit.
This module never retries that remote POST after a successful 200; it renews
the local lease before HTTP, persists immediately under fencing, retries local
persist bounded times, and may recover only when the DB row still holds the
exact pre-refresh token pair. Crash between remote 200 and first durable write
can still leave auth requiring operator re-seed — that window is not claimed
eliminated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Final, Protocol
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_crm_oauth_keys import (
    AmoCrmOauthKeyProvider,
    EnvAmoCrmOauthKeyProvider,
)
from app.core.amocrm_crm_oauth_types import AmoCrmCrmOauthError
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransportError,
)
from app.db.session import session_scope
from app.repositories import amocrm_crm_oauth_tokens as oauth_repo

__all__ = (
    "AmoCrmCrmRestHttpClient",
    "AmoCrmCrmRestHttpError",
    "AmoCrmCrmRestOutcome",
    "AmoCrmCrmRestTransport",
    "AmoCrmCrmTokenRefreshResult",
)

_TIMEOUT_SECONDS: Final[float] = 10.0
_MAX_RESPONSE_BYTES: Final[int] = 65536
_OAUTH_PATH: Final[str] = "/oauth2/access_token"
_POST_200_PERSIST_ATTEMPTS: Final[int] = 3


class AmoCrmCrmRestOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    PERMANENT_ERROR = "PERMANENT_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    DISABLED = "DISABLED"


class AmoCrmCrmRestHttpError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    def __repr__(self) -> str:
        return f"AmoCrmCrmRestHttpError({self.code!r})"


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmCrmTokenRefreshResult:
    outcome: AmoCrmCrmRestOutcome
    error_code: str | None = None

    def __repr__(self) -> str:
        return (
            "AmoCrmCrmTokenRefreshResult("
            f"outcome={self.outcome!r}, error_code={self.error_code!r})"
        )


class AmoCrmCrmRestTransport(Protocol):
    def request(self, req: S2sHttpRequest) -> S2sHttpResponse: ...


class _CrmHttpStdlibTransport:
    """CRM REST stdlib transport. Query strings allowed for REST paths."""

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        import http.client
        import ssl
        from urllib.parse import urlsplit

        if req.allow_redirects:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        parts = urlsplit(req.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        if parts.username is not None or parts.password is not None:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        path = parts.path if parts.path else "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        conn: http.client.HTTPConnection | None = None
        try:
            if parts.scheme == "https":
                conn = http.client.HTTPSConnection(
                    parts.hostname,
                    port=parts.port,
                    context=ssl.create_default_context(),
                    timeout=float(req.timeout_seconds),
                )
            else:
                conn = http.client.HTTPConnection(
                    parts.hostname,
                    port=parts.port,
                    timeout=float(req.timeout_seconds),
                )
            headers = {str(k): str(v) for k, v in req.headers.items()}
            conn.request(req.method, path, body=req.body, headers=headers)
            response = conn.getresponse()
            body = response.read(req.max_response_bytes + 1)
            if len(body) > req.max_response_bytes:
                raise S2sHttpTransportError("RESPONSE_TOO_LARGE") from None
            return S2sHttpResponse(
                status_code=int(response.status),
                headers={k.lower(): v for k, v in response.getheaders()},
                body=body,
            )
        except S2sHttpTransportError:
            raise
        except TimeoutError:
            raise S2sHttpTransportError("TIMEOUT") from None
        except Exception:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def _classify_status(status_code: int) -> AmoCrmCrmRestOutcome:
    if 200 <= status_code < 300:
        return AmoCrmCrmRestOutcome.SUCCESS
    if status_code in {400, 401, 403}:
        return AmoCrmCrmRestOutcome.PERMANENT_ERROR
    return AmoCrmCrmRestOutcome.TRANSIENT_ERROR


class AmoCrmCrmRestHttpClient:
    """CRM REST client with Bearer tokens. No entity create APIs."""

    def __init__(
        self,
        config: AmoCrmCrmRestConfig,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        key_provider: AmoCrmOauthKeyProvider | None = None,
        transport: AmoCrmCrmRestTransport | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._key_provider = (
            key_provider if key_provider is not None else EnvAmoCrmOauthKeyProvider()
        )
        self._transport = (
            transport if transport is not None else _CrmHttpStdlibTransport()
        )
        self._worker_id = worker_id or f"crm-oauth-{uuid4().hex[:12]}"
        self.http_calls: list[str] = []

    def authorized_get(
        self,
        *,
        path: str,
        access_token: str,
    ) -> tuple[AmoCrmCrmRestOutcome, S2sHttpResponse | None]:
        """Bearer GET helper. Callers supply a decrypted access token.

        Foundation: no create/update entity helpers are exposed.
        """

        if not self._config.enabled:
            return AmoCrmCrmRestOutcome.DISABLED, None
        if type(path) is not str or not path.startswith("/"):
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_PATH_INVALID")
        if type(access_token) is not str or not access_token:
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_TOKEN_INVALID")
        url = f"{self._config.api_base_url}{path}"
        req = S2sHttpRequest(
            method="GET",
            url=url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            body=b"",
            timeout_seconds=_TIMEOUT_SECONDS,
            allow_redirects=False,
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
        self.http_calls.append("GET")
        try:
            response = self._transport.request(req)
        except S2sHttpTransportError:
            return AmoCrmCrmRestOutcome.TRANSIENT_ERROR, None
        return _classify_status(response.status_code), response

    async def refresh_tokens(self) -> AmoCrmCrmTokenRefreshResult:
        """Claim → renew lease → one OAuth HTTP → fenced durable rotate.

        After HTTP 200 with a valid pair, never retries the remote refresh POST.
        Local persist uses bounded retries and guarded recovery when the original
        lease is stale but the DB still holds the exact pre-refresh pair.
        """

        if not self._config.enabled:
            return AmoCrmCrmTokenRefreshResult(outcome=AmoCrmCrmRestOutcome.DISABLED)
        try:
            self._config.require_runtime()
        except Exception:
            return AmoCrmCrmTokenRefreshResult(
                outcome=AmoCrmCrmRestOutcome.DISABLED,
                error_code="AMOCRM_CRM_REST_CONFIG_INVALID",
            )

        lease: oauth_repo.OauthRefreshLease | None = None
        pre_refresh: oauth_repo.OauthPreRefreshSnapshot | None = None
        try:
            async with session_scope(self._session_factory) as session:
                lease = await oauth_repo.claim_refresh_lease(
                    session,
                    worker_id=self._worker_id,
                    connection_scope=self._config.connection_scope,
                )
                row = await oauth_repo.get_by_scope(
                    session,
                    connection_scope=self._config.connection_scope,
                )
                if row is None:
                    await oauth_repo.release_refresh_lease(session, lease=lease)
                    lease = None
                    return AmoCrmCrmTokenRefreshResult(
                        outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
                        error_code="AMOCRM_CRM_OAUTH_NOT_FOUND",
                    )
                tokens = oauth_repo.decrypt_row(row, key_provider=self._key_provider)
                # Renew/validate immediately before remote refresh HTTP.
                lease = await oauth_repo.renew_refresh_lease(session, lease=lease)
                pre_refresh = oauth_repo.OauthPreRefreshSnapshot(
                    access_token=tokens.access_token,
                    refresh_token=tokens.refresh_token,
                    lease_version=lease.lease_version,
                )
        except AmoCrmCrmOauthError as exc:
            if lease is not None and exc.code != "AMOCRM_CRM_OAUTH_STALE_LEASE":
                async with session_scope(self._session_factory) as session:
                    await oauth_repo.release_refresh_lease(session, lease=lease)
            return AmoCrmCrmTokenRefreshResult(
                outcome=AmoCrmCrmRestOutcome.TRANSIENT_ERROR
                if exc.code == "AMOCRM_CRM_OAUTH_STALE_LEASE"
                else AmoCrmCrmRestOutcome.PERMANENT_ERROR,
                error_code=exc.code,
            )
        assert lease is not None and pre_refresh is not None

        body = json.dumps(
            {
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": pre_refresh.refresh_token,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        req = S2sHttpRequest(
            method="POST",
            url=f"{self._config.api_base_url}{_OAUTH_PATH}",
            headers={"Content-Type": "application/json"},
            body=body,
            timeout_seconds=_TIMEOUT_SECONDS,
            allow_redirects=False,
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
        self.http_calls.append("REFRESH")
        try:
            response = self._transport.request(req)
        except S2sHttpTransportError:
            async with session_scope(self._session_factory) as session:
                await oauth_repo.release_refresh_lease(session, lease=lease)
            return AmoCrmCrmTokenRefreshResult(
                outcome=AmoCrmCrmRestOutcome.TRANSIENT_ERROR,
                error_code="AMOCRM_CRM_TRANSPORT",
            )

        outcome = _classify_status(response.status_code)
        if outcome is not AmoCrmCrmRestOutcome.SUCCESS:
            async with session_scope(self._session_factory) as session:
                await oauth_repo.release_refresh_lease(session, lease=lease)
            return AmoCrmCrmTokenRefreshResult(
                outcome=outcome,
                error_code=f"AMOCRM_CRM_HTTP_{response.status_code}",
            )

        parsed = _parse_oauth_token_response(response.body)
        if parsed is None:
            async with session_scope(self._session_factory) as session:
                await oauth_repo.release_refresh_lease(session, lease=lease)
            return AmoCrmCrmTokenRefreshResult(
                outcome=AmoCrmCrmRestOutcome.TRANSIENT_ERROR,
                error_code="AMOCRM_CRM_OAUTH_RESPONSE_INVALID",
            )

        access_expires_at = None
        expires_in = parsed["expires_in"]
        if type(expires_in) is int:
            access_expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=expires_in
            )

        access_token = parsed["access_token"]
        refresh_token = parsed["refresh_token"]
        assert type(access_token) is str and type(refresh_token) is str

        # Remote refresh succeeded once. Persist locally; never re-POST refresh.
        return await self._persist_rotated_tokens_after_200(
            lease=lease,
            pre_refresh=pre_refresh,
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=access_expires_at,
        )

    async def _persist_rotated_tokens_after_200(
        self,
        *,
        lease: oauth_repo.OauthRefreshLease,
        pre_refresh: oauth_repo.OauthPreRefreshSnapshot,
        access_token: str,
        refresh_token: str,
        access_expires_at: datetime | None,
    ) -> AmoCrmCrmTokenRefreshResult:
        last_code = "AMOCRM_CRM_OAUTH_ROTATE_PERSIST_FAILED"
        for _attempt in range(_POST_200_PERSIST_ATTEMPTS):
            try:
                async with session_scope(self._session_factory) as session:
                    try:
                        await oauth_repo.rotate_tokens_with_lease(
                            session,
                            lease=lease,
                            access_token=access_token,
                            refresh_token=refresh_token,
                            key_provider=self._key_provider,
                            access_expires_at=access_expires_at,
                        )
                    except AmoCrmCrmOauthError as exc:
                        if exc.code != "AMOCRM_CRM_OAUTH_STALE_LEASE":
                            raise
                        await oauth_repo.recover_rotate_if_pre_refresh_unchanged(
                            session,
                            connection_scope=self._config.connection_scope,
                            worker_id=self._worker_id,
                            pre_refresh=pre_refresh,
                            access_token=access_token,
                            refresh_token=refresh_token,
                            key_provider=self._key_provider,
                            access_expires_at=access_expires_at,
                        )
                return AmoCrmCrmTokenRefreshResult(
                    outcome=AmoCrmCrmRestOutcome.SUCCESS
                )
            except AmoCrmCrmOauthError as exc:
                last_code = exc.code
                if exc.code == "AMOCRM_CRM_OAUTH_ROTATE_SUPERSEDED":
                    return AmoCrmCrmTokenRefreshResult(
                        outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
                        error_code=exc.code,
                    )
                if exc.code in {
                    "AMOCRM_CRM_OAUTH_STALE_LEASE",
                    "AMOCRM_CRM_OAUTH_STORE_FAILED",
                    "AMOCRM_CRM_OAUTH_ENCRYPT_FAILED",
                    "AMOCRM_CRM_OAUTH_KEY_UNAVAILABLE",
                }:
                    continue
                return AmoCrmCrmTokenRefreshResult(
                    outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
                    error_code=(
                        exc.code
                        if exc.code
                        in {
                            "AMOCRM_CRM_OAUTH_NOT_FOUND",
                            "AMOCRM_CRM_OAUTH_ROTATE_PERSIST_FAILED",
                        }
                        else "AMOCRM_CRM_OAUTH_ROTATE_PERSIST_FAILED"
                    ),
                )
            except Exception:
                last_code = "AMOCRM_CRM_OAUTH_ROTATE_PERSIST_FAILED"
                continue

        return AmoCrmCrmTokenRefreshResult(
            outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
            error_code=(
                "AMOCRM_CRM_OAUTH_ROTATE_PERSIST_FAILED"
                if last_code
                in {
                    "AMOCRM_CRM_OAUTH_STALE_LEASE",
                    "AMOCRM_CRM_OAUTH_STORE_FAILED",
                    "AMOCRM_CRM_OAUTH_ENCRYPT_FAILED",
                    "AMOCRM_CRM_OAUTH_KEY_UNAVAILABLE",
                    "AMOCRM_CRM_OAUTH_ROTATE_PERSIST_FAILED",
                }
                else last_code
                if last_code
                in {
                    "AMOCRM_CRM_OAUTH_ROTATE_SUPERSEDED",
                    "AMOCRM_CRM_OAUTH_NOT_FOUND",
                }
                else "AMOCRM_CRM_OAUTH_ROTATE_PERSIST_FAILED"
            ),
        )


def _parse_oauth_token_response(body: bytes) -> dict[str, object] | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(payload, dict):
        return None
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    if type(access) is not str or not access:
        return None
    if type(refresh) is not str or not refresh:
        return None
    expires_in = payload.get("expires_in")
    if expires_in is not None and (
        type(expires_in) is not int or isinstance(expires_in, bool) or expires_in < 0
    ):
        return None
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires_in,
    }
