"""Proactive amoCRM OAuth lifecycle worker.

The worker only decides when refresh is due. Remote refresh and durable token
rotation remain exclusively in ``AmoCrmCrmRestHttpClient.refresh_tokens()``.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_crm_rest_config import (
    AmoCrmCrmRestConfig,
    AmoCrmCrmRestConfigError,
)
from app.core.amocrm_crm_rest_http import (
    AmoCrmCrmRestHttpClient,
    AmoCrmCrmRestOutcome,
)
from app.db.session import session_scope
from app.repositories import amocrm_crm_oauth_tokens as oauth_repo

PROACTIVE_REFRESH_WINDOW: Final[timedelta] = timedelta(minutes=15)
_SAFE_ERROR_CODE_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z0-9_]{1,64}$")
_STALE_LEASE: Final[str] = "AMOCRM_CRM_OAUTH_STALE_LEASE"
_CONTENTION_UNRESOLVED: Final[str] = "AMOCRM_CRM_OAUTH_REFRESH_CONTENTION_UNRESOLVED"
_CONTENTION_POLL_INTERVAL_SECONDS: Final[float] = 0.1


class AmoCrmCrmOauthLifecycleError(RuntimeError):
    """Safe operational failure persisted by the worker heartbeat."""

    def __init__(self, code: str) -> None:
        safe_code = (
            code
            if _SAFE_ERROR_CODE_RE.fullmatch(code) is not None
            else "AMOCRM_CRM_OAUTH_REFRESH_FAILED"
        )
        super().__init__(safe_code)
        self.code = safe_code

    def __repr__(self) -> str:
        return f"AmoCrmCrmOauthLifecycleError({self.code!r})"


class AmoCrmCrmOauthLifecycleWorker:
    """Refresh a due durable access token through the fenced OAuth client."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        worker_id: str,
        config: AmoCrmCrmRestConfig | None = None,
        oauth: AmoCrmCrmRestHttpClient | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config_error_code: str | None = None
        if config is not None:
            self._config = config
        else:
            try:
                self._config = AmoCrmCrmRestConfig.from_env()
            except AmoCrmCrmRestConfigError as exc:
                self._config = AmoCrmCrmRestConfig(enabled=False)
                self._config_error_code = (
                    str(exc.args[0])
                    if exc.args
                    else "AMOCRM_CRM_REST_CONFIG_INVALID"
                )
        self._oauth = (
            oauth
            if oauth is not None
            else AmoCrmCrmRestHttpClient(
                self._config,
                session_factory=session_factory,
                worker_id=worker_id,
            )
        )
        self._now = now if now is not None else lambda: datetime.now(timezone.utc)

    async def tick(self) -> None:
        if self._config_error_code is not None:
            raise AmoCrmCrmOauthLifecycleError(self._config_error_code)
        if not self._config.enabled:
            return

        moment = _as_utc(
            self._now(),
            error_code="AMOCRM_CRM_OAUTH_CLOCK_INVALID",
        )
        refreshed = await self._oauth.refresh_tokens(
            if_expires_at_lte=moment + PROACTIVE_REFRESH_WINDOW,
        )
        if refreshed.outcome is AmoCrmCrmRestOutcome.SUCCESS:
            return
        if refreshed.error_code == _STALE_LEASE:
            await self._verify_post_contention_success(moment)
            return
        raise AmoCrmCrmOauthLifecycleError(
            refreshed.error_code or _fallback_error_code(refreshed.outcome)
        )

    async def _verify_post_contention_success(self, moment: datetime) -> None:
        deadline = moment + timedelta(
            seconds=oauth_repo.PRE_HTTP_REFRESH_LEASE_SECONDS
        )
        while True:
            now = _as_utc(
                self._now(),
                error_code="AMOCRM_CRM_OAUTH_CLOCK_INVALID",
            )
            cutoff = now + PROACTIVE_REFRESH_WINDOW
            access_expires_at, refresh_in_flight = await self._load_contention_snapshot(
                now
            )
            if access_expires_at is not None:
                expiry = _as_utc(
                    access_expires_at,
                    error_code="AMOCRM_CRM_OAUTH_EXPIRY_INVALID",
                )
                if expiry > cutoff:
                    return
            if not refresh_in_flight:
                raise AmoCrmCrmOauthLifecycleError(_CONTENTION_UNRESOLVED)
            if now >= deadline:
                raise AmoCrmCrmOauthLifecycleError(_CONTENTION_UNRESOLVED)
            sleep_for = min(
                _CONTENTION_POLL_INTERVAL_SECONDS,
                (deadline - now).total_seconds(),
            )
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    async def _load_contention_snapshot(
        self,
        moment: datetime,
    ) -> tuple[datetime | None, bool]:
        async with session_scope(self._session_factory) as session:
            row = await oauth_repo.get_by_scope(
                session,
                connection_scope=self._config.connection_scope,
            )
            if row is None:
                raise AmoCrmCrmOauthLifecycleError(
                    "AMOCRM_CRM_OAUTH_NOT_FOUND"
                )
            lease_until = row.lease_until
            lease_owner = row.lease_owner
            refresh_in_flight = (
                lease_until is not None
                and lease_owner is not None
                and lease_until > moment
            )
            # Do not decrypt or expose either token merely to inspect expiry.
            return row.access_expires_at, refresh_in_flight


def _as_utc(value: datetime, *, error_code: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise AmoCrmCrmOauthLifecycleError(error_code)
    return value.astimezone(timezone.utc)


def _fallback_error_code(outcome: AmoCrmCrmRestOutcome) -> str:
    return f"AMOCRM_CRM_OAUTH_REFRESH_{outcome.value}"
