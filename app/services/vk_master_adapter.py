"""VK master adapter orchestration (CURSOR-29).

validate → C27 precheck (silent if unbound) → C28 → commit → send.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.vk_master_config import (
    VkMasterAdapterConfig,
    vk_master_business_allowed,
)
from app.channels.vk_master_http import VkMasterSender
from app.channels.vk_master_reply import render_vk_master_reply
from app.channels.vk_master_types import (
    VkMasterNormalizedMessage,
    VkMasterWebhookKind,
)
from app.channels.vk_master_webhook import parse_vk_master_callback
from app.config import Settings
from app.core.master_channel_binding import ResolveMasterBindingOutcome
from app.core.master_command_http import MasterCommandHttpClient
from app.core.master_command_types import (
    MasterCommandFlowResult,
    build_master_command_envelope,
)
from app.db.session import session_scope
from app.repositories import master_command_pendings as pending_repo
from app.services.master_channel_binding import MasterChannelBindingService
from app.services.master_command_flow import MasterCommandFlowService, MasterCommandPiiStore

logger = logging.getLogger(__name__)

__all__ = (
    "VkMasterAdapterService",
    "VkMasterAdapterHttpResult",
)

_ALLOWED_LOG: Final[frozenset[str]] = frozenset(
    {
        "VK_MASTER_CONFIRMATION",
        "VK_MASTER_IGNORED",
        "VK_MASTER_REJECTED",
        "VK_MASTER_SILENT_UNBOUND",
        "VK_MASTER_GATED",
        "VK_MASTER_HANDLED",
        "VK_MASTER_SEND_FAILED",
        "VK_MASTER_DUPLICATE_SILENT",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class VkMasterAdapterHttpResult:
    """HTTP response body for VK Callback (plain text)."""

    body: str
    status_code: int = 200

    def __repr__(self) -> str:
        return (
            "VkMasterAdapterHttpResult("
            f"status_code={self.status_code!r}, "
            "body=<redacted>)"
        )


class VkMasterAdapterService:
    """Application boundary for VK master Callback events."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        config: VkMasterAdapterConfig,
        master_client: MasterCommandHttpClient | None,
        pii_store: MasterCommandPiiStore | None,
        sender: VkMasterSender | None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._config = config
        self._master_client = master_client
        self._pii = pii_store
        self._sender = sender

    async def handle_callback(self, body: object) -> VkMasterAdapterHttpResult:
        parsed = parse_vk_master_callback(body, config=self._config)
        if parsed.kind is VkMasterWebhookKind.CONFIRMATION:
            _log("VK_MASTER_CONFIRMATION")
            assert parsed.confirmation_response is not None
            return VkMasterAdapterHttpResult(body=parsed.confirmation_response)
        if parsed.kind is VkMasterWebhookKind.REJECTED:
            _log("VK_MASTER_REJECTED")
            return VkMasterAdapterHttpResult(body="ok")
        if parsed.kind is VkMasterWebhookKind.IGNORED:
            _log("VK_MASTER_IGNORED")
            return VkMasterAdapterHttpResult(body="ok")
        if parsed.message is None:
            _log("VK_MASTER_IGNORED")
            return VkMasterAdapterHttpResult(body="ok")

        if not vk_master_business_allowed(self._settings, self._config):
            _log("VK_MASTER_GATED")
            return VkMasterAdapterHttpResult(body="ok")

        message = parsed.message
        peer_id = message.peer_id
        reply_text: str | None = None

        # Binding precheck + C28 inside one UoW; commit before any send.
        async with session_scope(self._session_factory) as session:
            if not await self._is_bound_master(session, message):
                _log("VK_MASTER_SILENT_UNBOUND")
                return VkMasterAdapterHttpResult(body="ok")

            if await self._already_seen(session, message):
                _log("VK_MASTER_DUPLICATE_SILENT")
                return VkMasterAdapterHttpResult(body="ok")

            result = await self._run_c28(session, message)
            reply_text = render_vk_master_reply(result)
            _log("VK_MASTER_HANDLED")
        # Durable C28 state is committed before sender runs.

        if reply_text is None or self._sender is None:
            return VkMasterAdapterHttpResult(body="ok")
        try:
            self._sender.send_text(peer_id=peer_id, text=reply_text)
        except Exception:
            # Fail-safe: never roll back committed command state; no auto remutate.
            _log("VK_MASTER_SEND_FAILED")
        return VkMasterAdapterHttpResult(body="ok")

    async def _is_bound_master(
        self,
        session: AsyncSession,
        message: VkMasterNormalizedMessage,
    ) -> bool:
        bindings = MasterChannelBindingService(session)
        try:
            resolved = await bindings.resolve(
                channel="vk",
                external_account_id=message.external_account_id,
                connection_scope=self._config.connection_scope,
            )
        except Exception:
            return False
        return resolved.outcome is ResolveMasterBindingOutcome.RESOLVED

    async def _already_seen(
        self,
        session: AsyncSession,
        message: VkMasterNormalizedMessage,
    ) -> bool:
        existing = await pending_repo.get_by_inbound(
            session,
            channel="vk",
            connection_scope=self._config.connection_scope,
            external_account_id=message.external_account_id,
            inbound_message_id=message.external_message_id,
        )
        return existing is not None

    async def _run_c28(
        self,
        session: AsyncSession,
        message: VkMasterNormalizedMessage,
    ) -> MasterCommandFlowResult:
        envelope = build_master_command_envelope(
            channel="vk",
            external_account_id=message.external_account_id,
            external_message_id=message.external_message_id,
            text=message.text,
            occurred_at=message.occurred_at,
            connection_scope=self._config.connection_scope,
        )
        flow = MasterCommandFlowService(
            session,
            master_client=self._master_client,
            pii_store=self._pii,
        )
        return await flow.handle(envelope)


def _log(event: str) -> None:
    if event not in _ALLOWED_LOG:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return
