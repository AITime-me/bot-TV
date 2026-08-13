"""CRM REST mirror adapter (AMO-01B2).

Converges required amoCRM entity state for a claimed mirror job:
exactly one TECHNICAL_DEAL per conversation, optional deterministic CONTACT
link. Never copies message text. Never creates/guesses contacts.
MIRRORED means required amoCRM entity state for this mirror job converged successfully
— not "message content copied to CRM". Completely separate from Chat HMAC.
Chat HMAC (`AMOCRM_CHAT_*`) is never used here.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_crm_deal_create_config import (
    AmoCrmDealCreateConfig,
    load_deal_create_config_fail_closed,
)
from app.core.amocrm_crm_oauth_keys import AmoCrmOauthKeyProvider
from app.core.amocrm_crm_rest_http import AmoCrmCrmRestTransport
from app.services.amocrm_adapter import (
    AmoCrmMirrorAdapterResult,
    AmoCrmMirrorOutcome,
    AmoCrmMirrorRequest,
)
from app.services.amocrm_technical_deal import (
    TechnicalDealOutcome,
    TechnicalDealProjectionService,
)

__all__ = ("CrmRestMirrorAdapter",)


class CrmRestMirrorAdapter:
    """Real CRM sink used by AmoCrmMirrorWorker when deal-create is enabled."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        config: AmoCrmDealCreateConfig | None = None,
        key_provider: AmoCrmOauthKeyProvider | None = None,
        transport: AmoCrmCrmRestTransport | None = None,
        worker_id: str | None = None,
        deal_service: TechnicalDealProjectionService | None = None,
    ) -> None:
        self._config = (
            config if config is not None else load_deal_create_config_fail_closed()
        )
        self._deal = deal_service or TechnicalDealProjectionService(
            session_factory=session_factory,
            config=self._config,
            key_provider=key_provider,
            transport=transport,
            worker_id=worker_id,
        )
        self.calls: list[AmoCrmMirrorRequest] = []
        self.last_http_calls: tuple[str, ...] = ()

    async def mirror(self, request: AmoCrmMirrorRequest) -> AmoCrmMirrorAdapterResult:
        self.calls.append(request)
        if not self._config.enabled:
            self.last_http_calls = ()
            return AmoCrmMirrorAdapterResult(outcome=AmoCrmMirrorOutcome.SUCCESS)

        try:
            conversation_id = uuid.UUID(request.conversation_id)
        except (ValueError, TypeError, AttributeError):
            return AmoCrmMirrorAdapterResult(
                outcome=AmoCrmMirrorOutcome.PERMANENT_ERROR,
                error_code="CONVERSATION_ID_INVALID",
            )

        ensured = await self._deal.ensure_technical_deal(conversation_id)
        self.last_http_calls = ensured.http_calls
        if ensured.outcome is TechnicalDealOutcome.ENSURED:
            return AmoCrmMirrorAdapterResult(outcome=AmoCrmMirrorOutcome.SUCCESS)
        if ensured.outcome is TechnicalDealOutcome.DISABLED:
            return AmoCrmMirrorAdapterResult(outcome=AmoCrmMirrorOutcome.SUCCESS)
        if ensured.outcome is TechnicalDealOutcome.PERMANENT_ERROR:
            return AmoCrmMirrorAdapterResult(
                outcome=AmoCrmMirrorOutcome.PERMANENT_ERROR,
                error_code=ensured.error_code or "AMOCRM_CRM_PERMANENT",
            )
        return AmoCrmMirrorAdapterResult(
            outcome=AmoCrmMirrorOutcome.TRANSIENT_ERROR,
            error_code=ensured.error_code or "AMOCRM_CRM_TRANSIENT",
        )
