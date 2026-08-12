"""AMO-01B1a Chat projection enqueue + durable worker.

Scope: CLIENT_INBOUND only. BOT_OUTBOUND projection is deferred until a durable
rendered user-facing reply body exists (token-only SYNTHETIC_OUTBOUND must never
enqueue or send). Schema may still list BOT_OUTBOUND for a future slice.

HTTP only from this worker. No OAuth/CRM REST. No text in projection rows.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_chat_egress_config import (
    AmoCrmChatEgressConfig,
    AmoCrmChatEgressConfigError,
)
from app.core.amocrm_chat_egress_http import (
    CHAT_HTTP_TIMEOUT_SECONDS,
    AmoCrmChatEgressHttpClient,
    AmoCrmChatEgressOutcome,
    AmoCrmChatHistoryScan,
)
from app.db.session import session_scope
from app.models.amocrm_message_projection import (
    AmocrmMessageProjection,
    AmocrmProjectionSkipReason,
    AmocrmProjectionSourceKind,
    AmocrmProjectionStatus,
)
from app.models.inbox import InboxMessage
from app.models.outbox import OutboxMessage
from app.repositories import amocrm_message_projections as projection_repo
from app.repositories import conversations as conversation_repo
from app.repositories.amocrm_chat_bindings import AmocrmChatBindingAmbiguousError
from app.repositories.amocrm_message_projections import (
    AmocrmProjectionClaim,
    StaleAmocrmProjectionLeaseError,
)

logger = logging.getLogger(__name__)

# Lease budget must cover Chat HTTP timeout with headroom for DB round-trips.
_LEASE_HTTP_HEADROOM_SECONDS = 5


def load_chat_egress_config_fail_closed(
    environ: dict[str, str] | None = None,
) -> AmoCrmChatEgressConfig:
    """Invalid enabled config disables projection only — never raises."""

    try:
        return AmoCrmChatEgressConfig.from_env(environ)
    except AmoCrmChatEgressConfigError as exc:
        code = exc.args[0] if exc.args else "AMOCRM_CHAT_EGRESS_CONFIG_INVALID"
        logger.error("amocrm chat egress disabled error_code=%s", code)
        return AmoCrmChatEgressConfig(enabled=False)


def chat_egress_enabled(environ: dict[str, str] | None = None) -> bool:
    return load_chat_egress_config_fail_closed(environ).enabled


def _lease_seconds_for_http(lease_seconds: int) -> int:
    minimum = int(math.ceil(CHAT_HTTP_TIMEOUT_SECONDS)) + _LEASE_HTTP_HEADROOM_SECONDS
    return max(lease_seconds, minimum)


async def enqueue_client_inbound_projection(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    inbox_id: uuid.UUID,
    correlation_id: uuid.UUID,
    egress_enabled: bool | None = None,
) -> tuple[AmocrmMessageProjection, bool] | None:
    enabled = chat_egress_enabled() if egress_enabled is None else egress_enabled
    if not enabled:
        return None
    return await projection_repo.enqueue_if_absent(
        session,
        conversation_id=conversation_id,
        source_kind=AmocrmProjectionSourceKind.CLIENT_INBOUND,
        source_id=inbox_id,
        correlation_id=correlation_id,
    )


async def enqueue_bot_outbound_projection(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    outbound_id: uuid.UUID,
    correlation_id: uuid.UUID,
    egress_enabled: bool | None = None,
) -> tuple[AmocrmMessageProjection, bool] | None:
    """AMO-01B1a deferred: never enqueue BOT_OUTBOUND projections.

    Kept as a named no-op so call sites cannot silently reintroduce token-only
    Chat projection. Re-enable only when delivered outbound carries durable
    user-facing ``draft_text``/``text`` (not ``synthetic_token``).
    """

    _ = (session, conversation_id, outbound_id, correlation_id, egress_enabled)
    return None


@dataclass(frozen=True, repr=False)
class AmocrmProjectionProcessResult:
    projection_id: uuid.UUID
    status: str
    projected: bool
    skip_reason: str | None = None

    def __repr__(self) -> str:
        return (
            "AmocrmProjectionProcessResult("
            f"projection_id={self.projection_id!r}, "
            f"status={self.status!r}, "
            f"projected={self.projected!r}, "
            f"skip_reason={self.skip_reason!r})"
        )


class AmocrmChatProjectionWorker:
    """Drains amocrm_message_projections through Chat API (silent)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        worker_id: str,
        config: AmoCrmChatEgressConfig | None = None,
        http_client: AmoCrmChatEgressHttpClient | None = None,
        lease_seconds: int = projection_repo.DEFAULT_LEASE_SECONDS,
        retry_delay_seconds: int = projection_repo.DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._worker_id = worker_id
        self._config = (
            config if config is not None else load_chat_egress_config_fail_closed()
        )
        self._http_client = http_client
        self._lease_seconds = _lease_seconds_for_http(lease_seconds)
        self._retry_delay_seconds = retry_delay_seconds
        self.http_calls: list[str] = []

    def _client(self) -> AmoCrmChatEgressHttpClient:
        if self._http_client is not None:
            return self._http_client
        return AmoCrmChatEgressHttpClient(self._config)

    async def claim_one(
        self,
        *,
        now: datetime | None = None,
    ) -> AmocrmProjectionClaim | None:
        if not self._config.enabled:
            return None
        async with session_scope(self._session_factory) as session:
            return await projection_repo.claim_next(
                session,
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
                now=now,
            )

    async def process_claimed(
        self,
        claim: AmocrmProjectionClaim,
        *,
        now: datetime | None = None,
    ) -> AmocrmProjectionProcessResult:
        if not self._config.enabled:
            async with session_scope(self._session_factory) as session:
                await conversation_repo.lock_for_update(
                    session,
                    conversation_id=claim.conversation_id,
                )
                await projection_repo.require_processing_lease(
                    session,
                    projection_id=claim.projection_id,
                    lease_token=claim.lease_token,
                    lease_version=claim.lease_version,
                    lease_owner=claim.lease_owner,
                    now=now,
                )
                row = await projection_repo.skip_with_lease(
                    session,
                    projection_id=claim.projection_id,
                    lease_token=claim.lease_token,
                    lease_version=claim.lease_version,
                    skip_reason=AmocrmProjectionSkipReason.EGRESS_DISABLED,
                    now=now,
                )
                return AmocrmProjectionProcessResult(
                    projection_id=row.id,
                    status=row.status,
                    projected=False,
                    skip_reason=row.skip_reason,
                )

        # Crash-window recovery: amo id already attached under PROCESSING.
        if claim.amocrm_message_id:
            return await self._complete(
                claim,
                amocrm_message_id=claim.amocrm_message_id,
                now=now,
            )

        try:
            prepared = await self._prepare_send(claim, now=now)
        except StaleAmocrmProjectionLeaseError:
            raise
        except _ProjectionSkip as skipped:
            return skipped.result

        # Retries must reconcile before any POST; never blind-resend after ambiguous.
        if claim.attempt_count > 1:
            reconciled = await self._reconcile_before_send(claim, prepared, now=now)
            if reconciled is not None:
                return reconciled

        await self._renew_before_http(claim, now=now)
        client = self._client()
        self.http_calls.append("send")
        send = client.send_silent_text(
            integration_msgid=claim.integration_msgid,
            integration_conversation_id=prepared["integration_conversation_id"],
            conversation_ref_id=prepared["conversation_ref_id"],
            sender_id=prepared["sender_id"],
            sender_name=prepared["sender_name"],
            text=prepared["text"],
            timestamp_unix=prepared["timestamp_unix"],
        )
        if send.outcome is AmoCrmChatEgressOutcome.SUCCESS and send.amocrm_message_id:
            await self._attach_amo_id(claim, send.amocrm_message_id, now=now)
            return await self._complete(
                claim,
                amocrm_message_id=send.amocrm_message_id,
                now=now,
            )
        if send.outcome is AmoCrmChatEgressOutcome.PERMANENT_ERROR:
            await self.fail_claimed(
                claim,
                error_code=send.error_code or "AMOCRM_CHAT_PERMANENT",
                permanent=True,
            )
            raise RuntimeError(send.error_code or "AMOCRM_CHAT_PERMANENT")

        # Ambiguous/transient: reconcile only on later attempts — never POST again here.
        await self.fail_claimed(
            claim,
            error_code=send.error_code or "AMOCRM_CHAT_AMBIGUOUS",
            permanent=False,
        )
        raise RuntimeError(send.error_code or "AMOCRM_CHAT_AMBIGUOUS")

    async def _reconcile_before_send(
        self,
        claim: AmocrmProjectionClaim,
        prepared: dict[str, Any],
        *,
        now: datetime | None,
    ) -> AmocrmProjectionProcessResult | None:
        await self._renew_before_http(claim, now=now)
        client = self._client()
        self.http_calls.append("history")
        scan = client.scan_msgid_in_history(
            integration_conversation_id=prepared["integration_conversation_id"],
            integration_msgid=claim.integration_msgid,
        )
        if scan.scan is AmoCrmChatHistoryScan.FOUND and scan.amocrm_message_id:
            await self._attach_amo_id(claim, scan.amocrm_message_id, now=now)
            return await self._complete(
                claim,
                amocrm_message_id=scan.amocrm_message_id,
                now=now,
            )
        if scan.scan is AmoCrmChatHistoryScan.ABSENT:
            # Positively established absence → caller may POST once.
            return None
        await self.fail_claimed(
            claim,
            error_code=scan.error_code or "AMOCRM_CHAT_HISTORY_UNCERTAIN",
            permanent=False,
        )
        raise RuntimeError(scan.error_code or "AMOCRM_CHAT_HISTORY_UNCERTAIN")

    async def _renew_before_http(
        self,
        claim: AmocrmProjectionClaim,
        *,
        now: datetime | None,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            await conversation_repo.lock_for_update(
                session,
                conversation_id=claim.conversation_id,
            )
            await projection_repo.renew_processing_lease(
                session,
                projection_id=claim.projection_id,
                lease_token=claim.lease_token,
                lease_version=claim.lease_version,
                lease_owner=claim.lease_owner,
                lease_seconds=self._lease_seconds,
                now=now,
            )

    async def _attach_amo_id(
        self,
        claim: AmocrmProjectionClaim,
        amocrm_message_id: str,
        *,
        now: datetime | None,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            await conversation_repo.lock_for_update(
                session,
                conversation_id=claim.conversation_id,
            )
            await projection_repo.attach_amocrm_message_id_with_lease(
                session,
                projection_id=claim.projection_id,
                lease_token=claim.lease_token,
                lease_version=claim.lease_version,
                lease_owner=claim.lease_owner,
                amocrm_message_id=amocrm_message_id,
                now=now,
            )

    async def fail_claimed(
        self,
        claim: AmocrmProjectionClaim,
        *,
        error_code: str,
        permanent: bool = False,
    ) -> AmocrmMessageProjection:
        async with session_scope(self._session_factory) as session:
            return await projection_repo.fail_with_lease(
                session,
                projection_id=claim.projection_id,
                lease_token=claim.lease_token,
                lease_version=claim.lease_version,
                error_code=error_code,
                retry_delay_seconds=self._retry_delay_seconds,
                permanent=permanent,
            )

    async def _complete(
        self,
        claim: AmocrmProjectionClaim,
        *,
        amocrm_message_id: str,
        now: datetime | None,
    ) -> AmocrmProjectionProcessResult:
        async with session_scope(self._session_factory) as session:
            await conversation_repo.lock_for_update(
                session,
                conversation_id=claim.conversation_id,
            )
            await projection_repo.require_processing_lease(
                session,
                projection_id=claim.projection_id,
                lease_token=claim.lease_token,
                lease_version=claim.lease_version,
                lease_owner=claim.lease_owner,
                now=now,
            )
            row = await projection_repo.complete_projected_with_lease(
                session,
                projection_id=claim.projection_id,
                lease_token=claim.lease_token,
                lease_version=claim.lease_version,
                amocrm_message_id=amocrm_message_id,
                now=now,
            )
            return AmocrmProjectionProcessResult(
                projection_id=row.id,
                status=row.status,
                projected=True,
            )

    async def _prepare_send(
        self,
        claim: AmocrmProjectionClaim,
        *,
        now: datetime | None,
    ) -> dict[str, Any]:
        async with session_scope(self._session_factory) as session:
            await conversation_repo.lock_for_update(
                session,
                conversation_id=claim.conversation_id,
            )
            await projection_repo.require_processing_lease(
                session,
                projection_id=claim.projection_id,
                lease_token=claim.lease_token,
                lease_version=claim.lease_version,
                lease_owner=claim.lease_owner,
                now=now,
            )
            try:
                binding = await _active_binding_for_conversation(
                    session,
                    conversation_id=claim.conversation_id,
                )
            except AmocrmChatBindingAmbiguousError:
                row = await projection_repo.skip_with_lease(
                    session,
                    projection_id=claim.projection_id,
                    lease_token=claim.lease_token,
                    lease_version=claim.lease_version,
                    skip_reason=AmocrmProjectionSkipReason.BINDING_UNKNOWN,
                    now=now,
                )
                raise _ProjectionSkip(
                    AmocrmProjectionProcessResult(
                        projection_id=row.id,
                        status=row.status,
                        projected=False,
                        skip_reason=row.skip_reason,
                    )
                ) from None
            except _ProjectionSkipBindingRevoked:
                row = await projection_repo.skip_with_lease(
                    session,
                    projection_id=claim.projection_id,
                    lease_token=claim.lease_token,
                    lease_version=claim.lease_version,
                    skip_reason=AmocrmProjectionSkipReason.BINDING_REVOKED,
                    now=now,
                )
                raise _ProjectionSkip(
                    AmocrmProjectionProcessResult(
                        projection_id=row.id,
                        status=row.status,
                        projected=False,
                        skip_reason=row.skip_reason,
                    )
                ) from None
            if binding is None:
                row = await projection_repo.skip_with_lease(
                    session,
                    projection_id=claim.projection_id,
                    lease_token=claim.lease_token,
                    lease_version=claim.lease_version,
                    skip_reason=AmocrmProjectionSkipReason.BINDING_UNKNOWN,
                    now=now,
                )
                raise _ProjectionSkip(
                    AmocrmProjectionProcessResult(
                        projection_id=row.id,
                        status=row.status,
                        projected=False,
                        skip_reason=row.skip_reason,
                    )
                )
            if (
                type(binding.integration_conversation_id) is not str
                or not binding.integration_conversation_id
            ):
                row = await projection_repo.skip_with_lease(
                    session,
                    projection_id=claim.projection_id,
                    lease_token=claim.lease_token,
                    lease_version=claim.lease_version,
                    skip_reason=(
                        AmocrmProjectionSkipReason.BINDING_INTEGRATION_CONVERSATION_MISSING
                    ),
                    now=now,
                )
                raise _ProjectionSkip(
                    AmocrmProjectionProcessResult(
                        projection_id=row.id,
                        status=row.status,
                        projected=False,
                        skip_reason=row.skip_reason,
                    )
                )

            text, timestamp_unix = await _load_source_text(
                session,
                source_kind=claim.source_kind,
                source_id=claim.source_id,
            )
            if text is None:
                row = await projection_repo.skip_with_lease(
                    session,
                    projection_id=claim.projection_id,
                    lease_token=claim.lease_token,
                    lease_version=claim.lease_version,
                    skip_reason=AmocrmProjectionSkipReason.SOURCE_MISSING,
                    now=now,
                )
                raise _ProjectionSkip(
                    AmocrmProjectionProcessResult(
                        projection_id=row.id,
                        status=row.status,
                        projected=False,
                        skip_reason=row.skip_reason,
                    )
                )
            if not text.strip():
                row = await projection_repo.skip_with_lease(
                    session,
                    projection_id=claim.projection_id,
                    lease_token=claim.lease_token,
                    lease_version=claim.lease_version,
                    skip_reason=AmocrmProjectionSkipReason.SOURCE_EMPTY_TEXT,
                    now=now,
                )
                raise _ProjectionSkip(
                    AmocrmProjectionProcessResult(
                        projection_id=row.id,
                        status=row.status,
                        projected=False,
                        skip_reason=row.skip_reason,
                    )
                )

            kind = AmocrmProjectionSourceKind(claim.source_kind)
            sender_prefix = (
                "cli" if kind is AmocrmProjectionSourceKind.CLIENT_INBOUND else "bot"
            )
            return {
                "text": text,
                "timestamp_unix": timestamp_unix
                if timestamp_unix is not None
                else int(datetime.now(timezone.utc).timestamp()),
                "conversation_ref_id": binding.amocrm_chat_id,
                "integration_conversation_id": binding.integration_conversation_id,
                "sender_id": f"{sender_prefix}{claim.conversation_id.hex[:29]}",
                "sender_name": (
                    "Client"
                    if kind is AmocrmProjectionSourceKind.CLIENT_INBOUND
                    else "Teya"
                ),
            }


class _ProjectionSkip(Exception):
    def __init__(self, result: AmocrmProjectionProcessResult) -> None:
        self.result = result
        super().__init__(result.skip_reason or "SKIPPED")


class _ProjectionSkipBindingRevoked(Exception):
    """Internal signal: binding exists but is REVOKED."""


async def _active_binding_for_conversation(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
):
    from sqlalchemy import select

    from app.models.amocrm_chat_binding import (
        AmocrmChatBinding,
        AmocrmChatBindingStatus,
    )

    rows = list(
        await session.scalars(
            select(AmocrmChatBinding).where(
                AmocrmChatBinding.conversation_id == conversation_id,
                AmocrmChatBinding.status == AmocrmChatBindingStatus.ACTIVE.value,
            )
        )
    )
    if len(rows) > 1:
        raise AmocrmChatBindingAmbiguousError("BINDING_AMBIGUOUS")
    if rows:
        return rows[0]
    revoked = list(
        await session.scalars(
            select(AmocrmChatBinding)
            .where(
                AmocrmChatBinding.conversation_id == conversation_id,
                AmocrmChatBinding.status == AmocrmChatBindingStatus.REVOKED.value,
            )
            .limit(1)
        )
    )
    if revoked:
        raise _ProjectionSkipBindingRevoked()
    return None


async def _load_source_text(
    session: AsyncSession,
    *,
    source_kind: str,
    source_id: uuid.UUID,
) -> tuple[str | None, int | None]:
    if source_kind == AmocrmProjectionSourceKind.CLIENT_INBOUND.value:
        inbox = await session.get(InboxMessage, source_id)
        if inbox is None:
            return None, None
        payload = inbox.payload_json if isinstance(inbox.payload_json, dict) else {}
        text = payload.get("text")
        if type(text) is not str or not text.strip():
            return "", None
        ts = inbox.received_at or inbox.created_at
        unix = (
            int(ts.replace(tzinfo=timezone.utc).timestamp())
            if ts.tzinfo is None
            else int(ts.timestamp())
        )
        return text, unix

    outbound = await session.get(OutboxMessage, source_id)
    if outbound is None:
        return None, None
    payload = outbound.payload_json if isinstance(outbound.payload_json, dict) else {}
    # 01B1a / future BOT_OUTBOUND: never send synthetic_token placeholders.
    # Runtime enqueue is deferred; if a BOT_OUTBOUND row appears, require real text.
    text = payload.get("draft_text")
    if type(text) is not str or not text.strip():
        text = payload.get("text")
    if type(text) is not str or not text.strip():
        return "", None
    ts = outbound.created_at
    unix = (
        int(ts.replace(tzinfo=timezone.utc).timestamp())
        if ts.tzinfo is None
        else int(ts.timestamp())
    )
    return text, unix
