"""Offline amoCRM CRM operator actions (AMO-01B2-OPS).

bootstrap / reseed OAuth token store; resolve TECHNICAL_DEAL RECONCILE_REQUIRED.
Never posts lead create. Never mixes Chat HMAC. Secrets never in argv/logs/repr.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_crm_leads_http import AmoCrmLeadHttpClient
from app.core.amocrm_crm_oauth_keys import (
    AmoCrmOauthKeyProvider,
    EnvAmoCrmOauthKeyProvider,
)
from app.core.amocrm_crm_oauth_types import AmoCrmCrmOauthError
from app.core.amocrm_crm_rest_config import (
    AmoCrmCrmRestConfig,
    load_crm_rest_config_fail_closed,
)
from app.core.amocrm_crm_rest_http import (
    AmoCrmCrmRestHttpClient,
    AmoCrmCrmRestOutcome,
    AmoCrmCrmRestTransport,
    _CrmHttpStdlibTransport,
)
from app.db.session import session_scope
from app.models.amocrm_entity_link import AmocrmEntityKind, AmocrmEntityLinkStatus
from app.repositories import amocrm_crm_oauth_tokens as oauth_repo
from app.repositories import amocrm_entity_links as entity_links
from app.repositories.amocrm_entity_links import AmocrmEntityLinkConflictError
from app.services.amocrm_technical_deal import coerce_conversation_uuid

__all__ = (
    "AmoCrmOpsOutcome",
    "AmoCrmOpsResult",
    "AmoCrmCrmOpsService",
    "read_secret_line",
)


class AmoCrmOpsOutcome(str, Enum):
    SEEDED = "SEEDED"
    ALREADY_PRESENT = "ALREADY_PRESENT"
    RESEEDED = "RESEEDED"
    RECONCILE_ACTIVATED = "RECONCILE_ACTIVATED"
    REFUSED = "REFUSED"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    PERMANENT_ERROR = "PERMANENT_ERROR"


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmOpsResult:
    outcome: AmoCrmOpsOutcome
    error_code: str | None = None
    http_calls: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "AmoCrmOpsResult("
            f"outcome={self.outcome!r}, "
            f"error_code={self.error_code!r}, "
            f"http_calls={self.http_calls!r})"
        )


def read_secret_line(
    prompt: str,
    *,
    stdin_isatty: Callable[[], bool],
    getpass_fn: Callable[[str], str],
    stdin_readline: Callable[[], str],
) -> str:
    """Read one secret from getpass (TTY) or stdin (non-interactive). Never argv."""

    if stdin_isatty():
        raw = getpass_fn(prompt)
    else:
        raw = stdin_readline()
        if raw.endswith("\n"):
            raw = raw[:-1]
        if raw.endswith("\r"):
            raw = raw[:-1]
    if type(raw) is not str or not raw or any(ch.isspace() for ch in raw):
        raise ValueError("AMOCRM_CRM_OAUTH_TOKEN_INVALID")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError("AMOCRM_CRM_OAUTH_TOKEN_INVALID")
    return raw


class AmoCrmCrmOpsService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        key_provider: AmoCrmOauthKeyProvider | None = None,
        rest_config: AmoCrmCrmRestConfig | None = None,
        transport: AmoCrmCrmRestTransport | None = None,
        worker_id: str | None = None,
        connection_scope: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._key_provider = (
            key_provider if key_provider is not None else EnvAmoCrmOauthKeyProvider()
        )
        self._rest = (
            rest_config
            if rest_config is not None
            else load_crm_rest_config_fail_closed(environ)
        )
        self._transport = (
            transport if transport is not None else _CrmHttpStdlibTransport()
        )
        self._worker_id = worker_id or f"crm-ops-{uuid.uuid4().hex[:12]}"
        # Scope comes from config (resolved independently of REST enabled).
        self._connection_scope = (
            connection_scope
            if connection_scope is not None
            else self._rest.connection_scope
        )
        self._oauth = (
            AmoCrmCrmRestHttpClient(
                self._rest,
                session_factory=session_factory,
                key_provider=self._key_provider,
                transport=self._transport,
                worker_id=self._worker_id,
            )
            if self._rest.enabled
            else None
        )

    @property
    def connection_scope(self) -> str:
        """Selected OAuth connection scope (not a secret; safe to report)."""

        return self._connection_scope

    async def bootstrap_oauth(
        self,
        *,
        access_token: str,
        refresh_token: str,
        access_expires_at: datetime | None = None,
    ) -> AmoCrmOpsResult:
        try:
            async with session_scope(self._session_factory) as session:
                _row, inserted = await oauth_repo.insert_token_pair_if_absent(
                    session,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    key_provider=self._key_provider,
                    connection_scope=self._connection_scope,
                    access_expires_at=access_expires_at,
                )
        except AmoCrmCrmOauthError as exc:
            return AmoCrmOpsResult(
                outcome=AmoCrmOpsOutcome.PERMANENT_ERROR,
                error_code=exc.code if hasattr(exc, "code") else str(exc),
            )
        except ValueError as exc:
            return AmoCrmOpsResult(
                outcome=AmoCrmOpsOutcome.PERMANENT_ERROR,
                error_code=str(exc.args[0]) if exc.args else "INVALID",
            )
        if not inserted:
            return AmoCrmOpsResult(
                outcome=AmoCrmOpsOutcome.ALREADY_PRESENT,
                error_code="AMOCRM_CRM_OAUTH_ALREADY_PRESENT",
            )
        return AmoCrmOpsResult(outcome=AmoCrmOpsOutcome.SEEDED)

    async def reseed_oauth(
        self,
        *,
        access_token: str,
        refresh_token: str,
        access_expires_at: datetime | None = None,
    ) -> AmoCrmOpsResult:
        """Replace stored tokens under refresh-lease fencing. No remote OAuth."""

        if type(access_token) is not str or not access_token:
            return AmoCrmOpsResult(
                outcome=AmoCrmOpsOutcome.PERMANENT_ERROR,
                error_code="AMOCRM_CRM_OAUTH_TOKEN_INVALID",
            )
        if type(refresh_token) is not str or not refresh_token:
            return AmoCrmOpsResult(
                outcome=AmoCrmOpsOutcome.PERMANENT_ERROR,
                error_code="AMOCRM_CRM_OAUTH_TOKEN_INVALID",
            )
        try:
            async with session_scope(self._session_factory) as session:
                existing = await oauth_repo.get_by_scope(
                    session, connection_scope=self._connection_scope
                )
                if existing is None:
                    return AmoCrmOpsResult(
                        outcome=AmoCrmOpsOutcome.REFUSED,
                        error_code="AMOCRM_CRM_OAUTH_NOT_FOUND",
                    )
                lease = await oauth_repo.claim_refresh_lease(
                    session,
                    worker_id=self._worker_id,
                    connection_scope=self._connection_scope,
                )
                await oauth_repo.rotate_tokens_with_lease(
                    session,
                    lease=lease,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    key_provider=self._key_provider,
                    access_expires_at=access_expires_at,
                )
        except AmoCrmCrmOauthError as exc:
            code = exc.code if hasattr(exc, "code") else str(exc)
            if code == "AMOCRM_CRM_OAUTH_STALE_LEASE":
                return AmoCrmOpsResult(
                    outcome=AmoCrmOpsOutcome.REFUSED,
                    error_code=code,
                )
            if code == "AMOCRM_CRM_OAUTH_NOT_FOUND":
                return AmoCrmOpsResult(
                    outcome=AmoCrmOpsOutcome.REFUSED,
                    error_code=code,
                )
            return AmoCrmOpsResult(
                outcome=AmoCrmOpsOutcome.PERMANENT_ERROR,
                error_code=code,
            )
        return AmoCrmOpsResult(outcome=AmoCrmOpsOutcome.RESEEDED)

    async def resolve_reconcile(
        self,
        *,
        conversation_id: object,
        confirmed_deal_id: object,
    ) -> AmoCrmOpsResult:
        """Validate confirmed deal via GET, then RECONCILE_REQUIRED → ACTIVE."""

        normalized = coerce_conversation_uuid(conversation_id)
        if normalized is None:
            return AmoCrmOpsResult(
                outcome=AmoCrmOpsOutcome.PERMANENT_ERROR,
                error_code="CONVERSATION_ID_INVALID",
            )
        if type(confirmed_deal_id) is not str or not confirmed_deal_id.isdigit():
            return AmoCrmOpsResult(
                outcome=AmoCrmOpsOutcome.PERMANENT_ERROR,
                error_code="EXTERNAL_ID_INVALID",
            )
        deal_id = confirmed_deal_id

        if not self._rest.enabled or self._oauth is None:
            return AmoCrmOpsResult(
                outcome=AmoCrmOpsOutcome.REFUSED,
                error_code="AMOCRM_CRM_REST_DISABLED",
            )
        try:
            self._rest.require_runtime()
        except Exception:
            return AmoCrmOpsResult(
                outcome=AmoCrmOpsOutcome.REFUSED,
                error_code="AMOCRM_CRM_REST_CONFIG_INVALID",
            )

        async with session_scope(self._session_factory) as session:
            open_row = await entity_links.get_open(
                session,
                conversation_id=normalized,
                entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
            )
            if open_row is None:
                return AmoCrmOpsResult(
                    outcome=AmoCrmOpsOutcome.REFUSED,
                    error_code="ENTITY_LINK_RECONCILE_MISSING",
                )
            if open_row.status != AmocrmEntityLinkStatus.RECONCILE_REQUIRED.value:
                return AmoCrmOpsResult(
                    outcome=AmoCrmOpsOutcome.REFUSED,
                    error_code="ENTITY_LINK_NOT_RECONCILE_REQUIRED",
                )

        access = await self._load_access_token()
        if access is None:
            return AmoCrmOpsResult(
                outcome=AmoCrmOpsOutcome.REFUSED,
                error_code="AMOCRM_CRM_OAUTH_NOT_FOUND",
            )

        leads = AmoCrmLeadHttpClient(self._rest, transport=self._transport)
        got = leads.get_lead(lead_id=deal_id, access_token=access)
        if got.unauthorized:
            refreshed = await self._oauth.refresh_tokens()
            if refreshed.outcome is not AmoCrmCrmRestOutcome.SUCCESS:
                return AmoCrmOpsResult(
                    outcome=AmoCrmOpsOutcome.TRANSIENT_ERROR
                    if refreshed.outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR
                    else AmoCrmOpsOutcome.REFUSED,
                    error_code=refreshed.error_code or "AMOCRM_CRM_HTTP_401",
                    http_calls=tuple(leads.http_calls) + tuple(self._oauth.http_calls),
                )
            access = await self._load_access_token()
            if access is None:
                return AmoCrmOpsResult(
                    outcome=AmoCrmOpsOutcome.REFUSED,
                    error_code="AMOCRM_CRM_OAUTH_NOT_FOUND",
                    http_calls=tuple(leads.http_calls) + tuple(self._oauth.http_calls),
                )
            got = leads.get_lead(lead_id=deal_id, access_token=access)

        http_calls = tuple(leads.http_calls) + (
            tuple(self._oauth.http_calls) if self._oauth is not None else ()
        )

        if got.outcome is not AmoCrmCrmRestOutcome.SUCCESS:
            # Remain RECONCILE_REQUIRED for any non-success GET.
            code = got.error_code or "AMOCRM_CRM_LEAD_GET_FAILED"
            if got.not_found or got.outcome is AmoCrmCrmRestOutcome.PERMANENT_ERROR:
                return AmoCrmOpsResult(
                    outcome=AmoCrmOpsOutcome.REFUSED,
                    error_code=code,
                    http_calls=http_calls,
                )
            return AmoCrmOpsResult(
                outcome=AmoCrmOpsOutcome.TRANSIENT_ERROR,
                error_code=code,
                http_calls=http_calls,
            )

        confirmed = got.lead_id or deal_id
        try:
            async with session_scope(self._session_factory) as session:
                await entity_links.activate_reconcile_required(
                    session,
                    conversation_id=normalized,
                    external_id=confirmed,
                )
        except AmocrmEntityLinkConflictError as exc:
            return AmoCrmOpsResult(
                outcome=AmoCrmOpsOutcome.REFUSED,
                error_code=str(exc),
                http_calls=http_calls,
            )
        except ValueError as exc:
            return AmoCrmOpsResult(
                outcome=AmoCrmOpsOutcome.PERMANENT_ERROR,
                error_code=str(exc.args[0]) if exc.args else "EXTERNAL_ID_INVALID",
                http_calls=http_calls,
            )
        return AmoCrmOpsResult(
            outcome=AmoCrmOpsOutcome.RECONCILE_ACTIVATED,
            http_calls=http_calls,
        )

    async def _load_access_token(self) -> str | None:
        async with session_scope(self._session_factory) as session:
            row = await oauth_repo.get_by_scope(
                session, connection_scope=self._connection_scope
            )
            if row is None:
                return None
            tokens = oauth_repo.decrypt_row(row, key_provider=self._key_provider)
            return tokens.access_token

    async def run_controlled_revision(
        self, *, lead_id: int, complete_till: int, apply: bool
    ) -> "ControlledRevisionReceipt":
        """Offline-only fixed revision with the existing OAuth refresh fencing."""

        # Keep the write implementation unreachable from API/worker/chat imports.
        from app.services.amocrm_controlled_revision import ControlledRevisionExecutor

        if not self._rest.enabled or self._oauth is None:
            return ControlledRevisionExecutor.refused(
                lead_id, "AMOCRM_CRM_REST_DISABLED"
            )
        try:
            self._rest.require_runtime()
        except Exception:
            return ControlledRevisionExecutor.refused(
                lead_id, "AMOCRM_CRM_REST_CONFIG_INVALID"
            )

        async def _refresh_once() -> bool:
            refreshed = await self._oauth.refresh_tokens()
            return refreshed.outcome is AmoCrmCrmRestOutcome.SUCCESS

        return await ControlledRevisionExecutor(
            api_base_url=self._rest.api_base_url,
            transport=self._transport,
            token_loader=self._load_access_token,
            refresh_once=_refresh_once,
        ).execute(lead_id=lead_id, complete_till=complete_till, apply=apply)

    async def run_controlled_move_only(self, *, lead_id: int, apply: bool) -> "ControlledRevisionReceipt":
        from app.services.amocrm_controlled_revision import ControlledRevisionExecutor
        if not self._rest.enabled or self._oauth is None:
            return ControlledRevisionExecutor.refused(lead_id, "AMOCRM_CRM_REST_DISABLED")
        try:
            self._rest.require_runtime()
        except Exception:
            return ControlledRevisionExecutor.refused(lead_id, "AMOCRM_CRM_REST_CONFIG_INVALID")
        async def _refresh_once() -> bool:
            refreshed = await self._oauth.refresh_tokens()
            return refreshed.outcome is AmoCrmCrmRestOutcome.SUCCESS
        return await ControlledRevisionExecutor(api_base_url=self._rest.api_base_url, transport=self._transport, token_loader=self._load_access_token, refresh_once=_refresh_once).execute_move_only(lead_id=lead_id, apply=apply)

    async def run_controlled_rebalance_task(self, *, task_id: int, complete_till: int, apply: bool) -> "ControlledRevisionReceipt":
        from app.services.amocrm_controlled_revision import ControlledRevisionExecutor
        if not self._rest.enabled or self._oauth is None:
            return ControlledRevisionExecutor.refused(task_id, "AMOCRM_CRM_REST_DISABLED")
        async def _refresh_once() -> bool:
            return (await self._oauth.refresh_tokens()).outcome is AmoCrmCrmRestOutcome.SUCCESS
        return await ControlledRevisionExecutor(api_base_url=self._rest.api_base_url, transport=self._transport, token_loader=self._load_access_token, refresh_once=_refresh_once).execute_reschedule_control_task(task_id=task_id, complete_till=complete_till, apply=apply)

    async def run_controlled_terminal_move(self, *, lead_id: int, apply: bool) -> "ControlledRevisionReceipt":
        from app.services.amocrm_controlled_revision import ControlledRevisionExecutor
        if not self._rest.enabled or self._oauth is None:
            return ControlledRevisionExecutor.refused(lead_id, "AMOCRM_CRM_REST_DISABLED")
        async def _refresh_once() -> bool:
            return (await self._oauth.refresh_tokens()).outcome is AmoCrmCrmRestOutcome.SUCCESS
        return await ControlledRevisionExecutor(api_base_url=self._rest.api_base_url, transport=self._transport, token_loader=self._load_access_token, refresh_once=_refresh_once).execute_terminal_move(lead_id=lead_id, apply=apply)
