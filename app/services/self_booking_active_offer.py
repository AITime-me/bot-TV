"""Self-booking active-offer binding (SELF-BOOKING-COMMAND-03C).

Activate/replace only from DELIVERED OFFER_SLOTS outbounds.
Exact slot resolve + explicit invalidate. No PII / admit / CREATE.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.self_booking_active_offer_types import (
    ActiveOfferActivateOutcome,
    ActiveOfferActivateResult,
    ActiveOfferResolveOutcome,
    ActiveOfferResolveResult,
    require_active_offer_slots,
)
from app.db.clock import db_statement_now
from app.models.outbox import DeliveryStatus, OutboxMessage
from app.repositories import self_booking_active_offers as offer_repo

logger = logging.getLogger(__name__)

_ALLOWED_LOG_CODES: frozenset[str] = frozenset(
    {
        "ACTIVE_OFFER_ACTIVATED",
        "ACTIVE_OFFER_REPLACED",
        "ACTIVE_OFFER_REPLAYED",
        "ACTIVE_OFFER_IGNORED_STALE",
        "ACTIVE_OFFER_REJECTED",
        "ACTIVE_OFFER_RESOLVED",
        "ACTIVE_OFFER_NOT_ACTIVE",
        "ACTIVE_OFFER_INVALIDATED",
    }
)


def _log(event: str) -> None:
    if type(event) is not str or event not in _ALLOWED_LOG_CODES:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return


def _as_uuid(value: object) -> uuid.UUID:
    if type(value) is uuid.UUID:
        return value
    if isinstance(value, uuid.UUID):
        return uuid.UUID(str(value))
    return uuid.UUID(str(value))


def _require_nonneg_int(value: object) -> int | None:
    if type(value) is int and not isinstance(value, bool) and value >= 0:
        return value
    return None


class SelfBookingActiveOfferService:
    """Durable active-offer read-model boundary."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._clock = clock

    async def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        return await db_statement_now(self._session)

    async def activate_from_delivered_outbound(
        self,
        *,
        outbound: OutboxMessage,
    ) -> ActiveOfferActivateResult:
        """Activate/replace from a DELIVERED OFFER_SLOTS outbound only."""

        try:
            outbound_id = _as_uuid(outbound.id)
            conversation_id = _as_uuid(outbound.conversation_id)
        except (ValueError, TypeError, AttributeError):
            _log("ACTIVE_OFFER_REJECTED")
            return ActiveOfferActivateResult(
                outcome=ActiveOfferActivateOutcome.REJECTED,
                reason_code="OUTBOUND_IDS_INVALID",
            )

        if outbound.delivery_status != DeliveryStatus.DELIVERED.value:
            _log("ACTIVE_OFFER_REJECTED")
            return ActiveOfferActivateResult(
                outcome=ActiveOfferActivateOutcome.REJECTED,
                conversation_id=conversation_id,
                source_outbound_id=outbound_id,
                reason_code="NOT_DELIVERED",
            )

        context_version = _require_nonneg_int(outbound.context_version)
        manager_epoch = _require_nonneg_int(outbound.manager_epoch)
        event_seq_hwm = _require_nonneg_int(outbound.event_seq_hwm)
        if (
            context_version is None
            or manager_epoch is None
            or event_seq_hwm is None
        ):
            _log("ACTIVE_OFFER_REJECTED")
            return ActiveOfferActivateResult(
                outcome=ActiveOfferActivateOutcome.REJECTED,
                conversation_id=conversation_id,
                source_outbound_id=outbound_id,
                reason_code="FENCE_MISSING",
            )

        payload = outbound.payload_json
        if type(payload) is not dict:
            _log("ACTIVE_OFFER_REJECTED")
            return ActiveOfferActivateResult(
                outcome=ActiveOfferActivateOutcome.REJECTED,
                conversation_id=conversation_id,
                source_outbound_id=outbound_id,
                reason_code="PAYLOAD_INVALID",
            )
        if payload.get("booking_action") != "OFFER_SLOTS":
            _log("ACTIVE_OFFER_REJECTED")
            return ActiveOfferActivateResult(
                outcome=ActiveOfferActivateOutcome.REJECTED,
                conversation_id=conversation_id,
                source_outbound_id=outbound_id,
                reason_code="NOT_OFFER_SLOTS",
            )
        try:
            slots = require_active_offer_slots(payload.get("booking_offered_slots"))
        except (ValueError, TypeError):
            _log("ACTIVE_OFFER_REJECTED")
            return ActiveOfferActivateResult(
                outcome=ActiveOfferActivateOutcome.REJECTED,
                conversation_id=conversation_id,
                source_outbound_id=outbound_id,
                reason_code="OFFER_SLOTS_INVALID",
            )

        now = await self._now()
        action = await offer_repo.upsert_if_newer_or_same_outbound(
            self._session,
            conversation_id=conversation_id,
            source_outbound_id=outbound_id,
            source_context_version=context_version,
            source_manager_epoch=manager_epoch,
            source_event_seq_hwm=event_seq_hwm,
            offered_slots=[slot.to_json_dict() for slot in slots],
            now=now,
        )

        if action == "activated":
            _log("ACTIVE_OFFER_ACTIVATED")
            return ActiveOfferActivateResult(
                outcome=ActiveOfferActivateOutcome.ACTIVATED,
                conversation_id=conversation_id,
                source_outbound_id=outbound_id,
            )
        if action == "replaced":
            _log("ACTIVE_OFFER_REPLACED")
            return ActiveOfferActivateResult(
                outcome=ActiveOfferActivateOutcome.REPLACED,
                conversation_id=conversation_id,
                source_outbound_id=outbound_id,
            )
        if action == "replayed":
            _log("ACTIVE_OFFER_REPLAYED")
            return ActiveOfferActivateResult(
                outcome=ActiveOfferActivateOutcome.REPLAYED,
                conversation_id=conversation_id,
                source_outbound_id=outbound_id,
            )
        _log("ACTIVE_OFFER_IGNORED_STALE")
        return ActiveOfferActivateResult(
            outcome=ActiveOfferActivateOutcome.IGNORED_STALE,
            conversation_id=conversation_id,
            source_outbound_id=outbound_id,
            reason_code="STALE_OR_DELAYED",
        )

    async def resolve_slot(
        self,
        *,
        conversation_id: object,
        slot_id: object,
    ) -> ActiveOfferResolveResult:
        """Exact membership: conversation + slot_id → starts_at | NOT_ACTIVE."""

        try:
            cid = _as_uuid(conversation_id)
        except (ValueError, TypeError, AttributeError):
            _log("ACTIVE_OFFER_NOT_ACTIVE")
            return ActiveOfferResolveResult(
                outcome=ActiveOfferResolveOutcome.NOT_ACTIVE
            )
        if type(slot_id) is not str or not slot_id:
            _log("ACTIVE_OFFER_NOT_ACTIVE")
            return ActiveOfferResolveResult(
                outcome=ActiveOfferResolveOutcome.NOT_ACTIVE
            )

        row = await offer_repo.get_by_conversation(
            self._session, conversation_id=cid
        )
        if row is None:
            _log("ACTIVE_OFFER_NOT_ACTIVE")
            return ActiveOfferResolveResult(
                outcome=ActiveOfferResolveOutcome.NOT_ACTIVE
            )

        try:
            slots = require_active_offer_slots(row.offered_slots)
        except ValueError:
            _log("ACTIVE_OFFER_NOT_ACTIVE")
            return ActiveOfferResolveResult(
                outcome=ActiveOfferResolveOutcome.NOT_ACTIVE
            )

        for slot in slots:
            if slot.slot_id == slot_id:
                _log("ACTIVE_OFFER_RESOLVED")
                return ActiveOfferResolveResult(
                    outcome=ActiveOfferResolveOutcome.FOUND,
                    starts_at=slot.starts_at,
                    source_outbound_id=_as_uuid(row.source_outbound_id),
                )

        _log("ACTIVE_OFFER_NOT_ACTIVE")
        return ActiveOfferResolveResult(
            outcome=ActiveOfferResolveOutcome.NOT_ACTIVE
        )

    async def invalidate(
        self,
        *,
        conversation_id: object,
    ) -> bool:
        """Explicit clear of the active offer snapshot."""

        try:
            cid = _as_uuid(conversation_id)
        except (ValueError, TypeError, AttributeError):
            return False
        deleted = await offer_repo.delete_by_conversation(
            self._session, conversation_id=cid
        )
        if deleted:
            _log("ACTIVE_OFFER_INVALIDATED")
        return deleted
