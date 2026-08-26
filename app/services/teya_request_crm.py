"""CRM action layer for Teya request orchestrator (identity + business deal).

Uses AmoCrmCrmWritesHttpClient with ACTION → POSTCHECK → VERIFIED.
Never treats technical deals as business deals. Never sends outbound messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.core.teya_request_retry import is_retryable_crm_error
from app.core.amocrm_analytics_fields import AmoCrmAnalyticsApplyDecision
from app.core.amocrm_crm_writes_http import (
    AmoCrmAnalyticsApplyReceipt,
    AmoCrmCrmWriteOutcome,
    AmoCrmCrmWriteReceipt,
    AmoCrmCrmWritesHttpClient,
    TASK_TEXT_DEFAULT,
    format_game_no_booking_task_text,
    format_game_self_booking_task_text,
)
from app.core.amocrm_deal_discovery import (
    AmoCrmDealDiscoveryOutcome,
    AmoCrmDealDiscoveryResult,
)
from app.core.amocrm_identity_lookup import (
    AmoCrmIdentityLookupOutcome,
    AmoCrmIdentityLookupResult,
)


class IdentityLookupPort(Protocol):
    async def lookup_by_phone(
        self, *, phone_e164: str
    ) -> AmoCrmIdentityLookupResult: ...


class DealDiscoveryPort(Protocol):
    async def discover_deal_candidates(
        self,
        *,
        contact_id: str,
        known_technical_deal_ids: tuple[str, ...] = (),
    ) -> AmoCrmDealDiscoveryResult: ...


class TechnicalDealIdsPort(Protocol):
    async def list_active_technical_deal_ids(self) -> tuple[str, ...]: ...


class TokenPort(Protocol):
    async def access_token(self) -> str | None: ...

    async def refresh_access_token(self) -> str | None: ...


class TeyaCrmActionOutcome(StrEnum):
    READY = "READY"
    FAIL_CLOSED = "FAIL_CLOSED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    RETRY = "RETRY"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    NONE = "NONE"


@dataclass(frozen=True, slots=True, repr=False)
class TeyaCrmActionResult:
    outcome: TeyaCrmActionOutcome
    contact_id: str | None = None
    deal_id: str | None = None
    task_id: str | None = None
    note_id: str | None = None
    error_code: str | None = None
    analytics_decision: str | None = None

    def __repr__(self) -> str:
        return (
            "TeyaCrmActionResult("
            f"outcome={self.outcome.value!r}, "
            f"contact_id={self.contact_id!r}, "
            f"deal_id={self.deal_id!r}, "
            f"task_id={self.task_id!r}, "
            f"note_id={self.note_id!r}, "
            f"error_code={self.error_code!r}, "
            f"analytics_decision={self.analytics_decision!r})"
        )


class TeyaRequestCrmService:
    """Resolve/create contact + business lead with postcheck."""

    def __init__(
        self,
        *,
        identity_lookup: IdentityLookupPort,
        deal_discovery: DealDiscoveryPort,
        writes: AmoCrmCrmWritesHttpClient,
        tokens: TokenPort,
        technical_deal_ids: TechnicalDealIdsPort | None = None,
    ) -> None:
        self._identity = identity_lookup
        self._deals = deal_discovery
        self._writes = writes
        self._tokens = tokens
        self._technical_deal_ids = technical_deal_ids

    async def _known_technical_deal_ids(self) -> tuple[str, ...]:
        if self._technical_deal_ids is None:
            return ()
        return await self._technical_deal_ids.list_active_technical_deal_ids()

    async def ensure_contact_and_deal(
        self,
        *,
        phone_e164: str,
        client_name: str | None,
    ) -> TeyaCrmActionResult:
        lookup = await self._identity.lookup_by_phone(phone_e164=phone_e164)
        if lookup.outcome is AmoCrmIdentityLookupOutcome.AMBIGUOUS:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                error_code="IDENTITY_AMBIGUOUS",
            )
        if lookup.outcome is AmoCrmIdentityLookupOutcome.DISABLED:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                error_code=lookup.error_code or "AMOCRM_CRM_REST_DISABLED",
            )
        if lookup.outcome is AmoCrmIdentityLookupOutcome.TRANSIENT_ERROR:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.RETRY,
                error_code=lookup.error_code or "IDENTITY_TRANSIENT",
            )
        if lookup.outcome in {
            AmoCrmIdentityLookupOutcome.PERMANENT_ERROR,
            AmoCrmIdentityLookupOutcome.INVALID_INPUT,
            AmoCrmIdentityLookupOutcome.INCOMPLETE,
        }:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.FAIL_CLOSED,
                error_code=lookup.error_code or "IDENTITY_FAIL_CLOSED",
            )

        token = await self._tokens.access_token()
        if not token:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                error_code="AMOCRM_CRM_OAUTH_NOT_FOUND",
            )

        contact_id: str | None = None
        if lookup.outcome is AmoCrmIdentityLookupOutcome.FOUND:
            contact_id = lookup.contact_id
        elif lookup.outcome is AmoCrmIdentityLookupOutcome.NOT_FOUND:
            created = self._writes.create_contact(
                name=client_name or "Клиент онлайн-записи",
                phone_e164=phone_e164,
                access_token=token,
            )
            mapped = _map_write(created)
            if mapped.outcome is TeyaCrmActionOutcome.READY:
                contact_id = created.contact_id
            elif mapped.outcome is TeyaCrmActionOutcome.RETRY:
                # Lost HTTP after successful create: verify via identity lookup.
                relookup = await self._identity.lookup_by_phone(
                    phone_e164=phone_e164
                )
                if relookup.outcome is AmoCrmIdentityLookupOutcome.FOUND:
                    contact_id = relookup.contact_id
                elif relookup.outcome is AmoCrmIdentityLookupOutcome.AMBIGUOUS:
                    return TeyaCrmActionResult(
                        outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                        error_code="IDENTITY_AMBIGUOUS",
                    )
                else:
                    return mapped
            else:
                return mapped
        else:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.FAIL_CLOSED,
                error_code="IDENTITY_UNEXPECTED",
            )

        if contact_id is None:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.FAIL_CLOSED,
                error_code="CONTACT_MISSING",
            )

        discovery = await self._deals.discover_deal_candidates(
            contact_id=contact_id,
            known_technical_deal_ids=await self._known_technical_deal_ids(),
        )
        if discovery.outcome is AmoCrmDealDiscoveryOutcome.DISABLED:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                contact_id=contact_id,
                error_code=discovery.error_code or "AMOCRM_CRM_REST_DISABLED",
            )
        if discovery.outcome is AmoCrmDealDiscoveryOutcome.TRANSIENT_ERROR:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.RETRY,
                contact_id=contact_id,
                error_code=discovery.error_code or "DEAL_TRANSIENT",
            )
        if discovery.outcome in {
            AmoCrmDealDiscoveryOutcome.PERMANENT_ERROR,
            AmoCrmDealDiscoveryOutcome.INVALID_INPUT,
            AmoCrmDealDiscoveryOutcome.INCOMPLETE,
        }:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.FAIL_CLOSED,
                contact_id=contact_id,
                error_code=discovery.error_code or "DEAL_FAIL_CLOSED",
            )

        if len(discovery.business_active_lead_ids) > 1:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                contact_id=contact_id,
                error_code="ACTIVE_DEAL_AMBIGUOUS",
            )
        if len(discovery.business_active_lead_ids) == 1:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.READY,
                contact_id=contact_id,
                deal_id=discovery.business_active_lead_ids[0],
            )

        if len(discovery.reanimation_candidate_lead_ids) == 1:
            reanimated = self._writes.reanimate_lead(
                lead_id=discovery.reanimation_candidate_lead_ids[0],
                contact_id=contact_id,
                access_token=token,
            )
            return _map_write(reanimated, contact_id=contact_id)
        if len(discovery.reanimation_candidate_lead_ids) > 1:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                contact_id=contact_id,
                error_code="REANIMATION_AMBIGUOUS",
            )

        created_lead = self._writes.create_business_lead(
            name="Заявка онлайн-записи",
            contact_id=contact_id,
            access_token=token,
        )
        mapped = _map_write(created_lead, contact_id=contact_id)
        if mapped.outcome is TeyaCrmActionOutcome.READY:
            return mapped
        if mapped.outcome is TeyaCrmActionOutcome.RETRY:
            rediscovery = await self._deals.discover_deal_candidates(
                contact_id=contact_id,
                known_technical_deal_ids=await self._known_technical_deal_ids(),
            )
            if len(rediscovery.business_active_lead_ids) == 1:
                return TeyaCrmActionResult(
                    outcome=TeyaCrmActionOutcome.READY,
                    contact_id=contact_id,
                    deal_id=rediscovery.business_active_lead_ids[0],
                )
            if len(rediscovery.business_active_lead_ids) > 1:
                return TeyaCrmActionResult(
                    outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                    contact_id=contact_id,
                    error_code="ACTIVE_DEAL_AMBIGUOUS",
                )
        return mapped

    async def reconcile_readonly(
        self,
        *,
        phone_e164: str,
        note_text: str | None = None,
        task_text: str | None = None,
    ) -> TeyaCrmActionResult:
        """Read-only CRM rediscovery. Never creates contacts/deals/notes/tasks."""

        lookup = await self._identity.lookup_by_phone(phone_e164=phone_e164)
        if lookup.outcome is AmoCrmIdentityLookupOutcome.AMBIGUOUS:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                error_code="IDENTITY_AMBIGUOUS",
            )
        if lookup.outcome in {
            AmoCrmIdentityLookupOutcome.TRANSIENT_ERROR,
            AmoCrmIdentityLookupOutcome.DISABLED,
        }:
            if lookup.outcome is AmoCrmIdentityLookupOutcome.DISABLED:
                return TeyaCrmActionResult(
                    outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                    error_code=lookup.error_code or "AMOCRM_CRM_REST_DISABLED",
                )
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.RETRY,
                error_code=lookup.error_code or "IDENTITY_TRANSIENT",
            )
        if lookup.outcome is AmoCrmIdentityLookupOutcome.NOT_FOUND:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.NONE,
                error_code="CONTACT_NONE",
            )
        if lookup.outcome is not AmoCrmIdentityLookupOutcome.FOUND:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.FAIL_CLOSED,
                error_code=lookup.error_code or "IDENTITY_FAIL_CLOSED",
            )
        contact_id = lookup.contact_id
        if not contact_id:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.NONE,
                error_code="CONTACT_NONE",
            )

        discovery = await self._deals.discover_deal_candidates(
            contact_id=contact_id,
            known_technical_deal_ids=await self._known_technical_deal_ids(),
        )
        if discovery.outcome is AmoCrmDealDiscoveryOutcome.DISABLED:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                contact_id=contact_id,
                error_code=discovery.error_code or "AMOCRM_CRM_REST_DISABLED",
            )
        if discovery.outcome is AmoCrmDealDiscoveryOutcome.TRANSIENT_ERROR:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.RETRY,
                contact_id=contact_id,
                error_code=discovery.error_code or "DEAL_TRANSIENT",
            )
        if discovery.outcome in {
            AmoCrmDealDiscoveryOutcome.PERMANENT_ERROR,
            AmoCrmDealDiscoveryOutcome.INVALID_INPUT,
            AmoCrmDealDiscoveryOutcome.INCOMPLETE,
        }:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.FAIL_CLOSED,
                contact_id=contact_id,
                error_code=discovery.error_code or "DEAL_FAIL_CLOSED",
            )
        if len(discovery.business_active_lead_ids) > 1:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                contact_id=contact_id,
                error_code="ACTIVE_DEAL_AMBIGUOUS",
            )
        if len(discovery.business_active_lead_ids) == 0:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.NONE,
                contact_id=contact_id,
                error_code="DEAL_NONE",
            )
        deal_id = discovery.business_active_lead_ids[0]

        note_id: str | None = None
        task_id: str | None = None
        token = await self._tokens.access_token()
        if not token:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                contact_id=contact_id,
                deal_id=deal_id,
                error_code="AMOCRM_CRM_OAUTH_NOT_FOUND",
            )
        if note_text:
            found_note = self._writes.find_lead_note(
                lead_id=deal_id, text=note_text, access_token=token
            )
            if found_note.outcome is AmoCrmCrmWriteOutcome.FAILED:
                if found_note.error_code == "AMOCRM_NOTE_AMBIGUOUS":
                    return TeyaCrmActionResult(
                        outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                        contact_id=contact_id,
                        deal_id=deal_id,
                        error_code="AMOCRM_NOTE_AMBIGUOUS",
                    )
                if found_note.error_code == "AMOCRM_NOTE_LIST_TRANSIENT":
                    return TeyaCrmActionResult(
                        outcome=TeyaCrmActionOutcome.RETRY,
                        contact_id=contact_id,
                        deal_id=deal_id,
                        error_code="AMOCRM_NOTE_LIST_TRANSIENT",
                    )
            elif found_note.outcome is AmoCrmCrmWriteOutcome.VERIFIED:
                note_id = found_note.note_id
        if task_text:
            found_task = self._writes.find_lead_task(
                lead_id=deal_id, text=task_text, access_token=token
            )
            if found_task.outcome is AmoCrmCrmWriteOutcome.FAILED:
                if found_task.error_code == "AMOCRM_TASK_AMBIGUOUS":
                    return TeyaCrmActionResult(
                        outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                        contact_id=contact_id,
                        deal_id=deal_id,
                        note_id=note_id,
                        error_code="AMOCRM_TASK_AMBIGUOUS",
                    )
                if found_task.error_code == "AMOCRM_TASK_LIST_TRANSIENT":
                    return TeyaCrmActionResult(
                        outcome=TeyaCrmActionOutcome.RETRY,
                        contact_id=contact_id,
                        deal_id=deal_id,
                        note_id=note_id,
                        error_code="AMOCRM_TASK_LIST_TRANSIENT",
                    )
            elif found_task.outcome is AmoCrmCrmWriteOutcome.VERIFIED:
                task_id = found_task.task_id

        return TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY,
            contact_id=contact_id,
            deal_id=deal_id,
            note_id=note_id,
            task_id=task_id,
        )

    async def attach_note_and_task(
        self,
        *,
        deal_id: str,
        note_text: str,
        task_text: str = TASK_TEXT_DEFAULT,
    ) -> TeyaCrmActionResult:
        token = await self._tokens.access_token()
        if not token:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                deal_id=deal_id,
                error_code="AMOCRM_CRM_OAUTH_NOT_FOUND",
            )
        note = self._writes.ensure_lead_note(
            lead_id=deal_id, text=note_text, access_token=token
        )
        if note.outcome is AmoCrmCrmWriteOutcome.RECONCILIATION_REQUIRED:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.RECONCILIATION_REQUIRED,
                deal_id=deal_id,
                note_id=note.note_id,
                error_code=note.error_code,
            )
        if note.outcome is not AmoCrmCrmWriteOutcome.VERIFIED:
            mapped_note = _map_write(note, deal_id=deal_id)
            if mapped_note.outcome is not TeyaCrmActionOutcome.READY:
                return mapped_note
        task = self._writes.ensure_lead_task(
            lead_id=deal_id, text=task_text, access_token=token
        )
        mapped = _map_write(task, deal_id=deal_id)
        if mapped.outcome is TeyaCrmActionOutcome.READY:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.READY,
                deal_id=deal_id,
                note_id=note.note_id,
                task_id=task.task_id,
            )
        return mapped

    async def apply_lead_analytics_enum_if_empty(
        self,
        *,
        deal_id: str | None,
        field_id: int,
        enum_id: int | None,
    ) -> TeyaCrmActionResult:
        """Safe write-if-empty on FINAL business deal only.

        ``enum_id is None`` → SKIPPED_NO_EVIDENCE (no write).
        ``deal_id`` missing/invalid → no write (ambiguous ownership).
        Known technical deal IDs are rejected before HTTP.
        Never writes Channel; conflicts never overwrite.
        One bounded OAuth refresh on 401, then one retry.
        """

        if enum_id is None:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.READY,
                deal_id=deal_id,
                analytics_decision=AmoCrmAnalyticsApplyDecision.SKIPPED_NO_EVIDENCE.value,
                error_code="ANALYTICS_SKIPPED_NO_EVIDENCE",
            )
        if type(deal_id) is not str or not deal_id.isdigit():
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.READY,
                deal_id=deal_id,
                analytics_decision=AmoCrmAnalyticsApplyDecision.SKIPPED_NO_EVIDENCE.value,
                error_code="ANALYTICS_SKIPPED_NO_DEAL",
            )
        tech_ids = await self._known_technical_deal_ids()
        if deal_id in tech_ids:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.FAIL_CLOSED,
                deal_id=deal_id,
                analytics_decision=AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW.value,
                error_code="ANALYTICS_TECHNICAL_DEAL_FORBIDDEN",
            )
        token = await self._tokens.access_token()
        if not token:
            return TeyaCrmActionResult(
                outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
                deal_id=deal_id,
                analytics_decision=AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW.value,
                error_code="AMOCRM_CRM_OAUTH_NOT_FOUND",
            )
        receipt = self._writes.ensure_lead_analytics_enum_if_empty(
            lead_id=deal_id,
            field_id=field_id,
            enum_id=enum_id,
            access_token=token,
        )
        if receipt.error_code == "AMOCRM_ANALYTICS_UNAUTHORIZED":
            refreshed = await self._tokens.refresh_access_token()
            if refreshed:
                receipt = self._writes.ensure_lead_analytics_enum_if_empty(
                    lead_id=deal_id,
                    field_id=field_id,
                    enum_id=enum_id,
                    access_token=refreshed,
                )
        return _map_analytics(receipt, deal_id=deal_id)


def build_game_task_text(
    *,
    gift: str | None,
    procedure: str | None,
    appointment_id: str | None,
) -> str:
    safe_gift = gift or "—"
    if appointment_id:
        return format_game_self_booking_task_text(
            gift=safe_gift, appointment_id=appointment_id
        )
    return format_game_no_booking_task_text(
        gift=safe_gift, procedure=procedure or "—"
    )


def build_teya_structured_note(dto: object) -> str:
    """Canonical CRM note text for create and read-only reconcile (no PII)."""

    request_type = getattr(dto, "request_type", None) or "UNKNOWN"
    status = getattr(dto, "status", None) or "UNKNOWN"
    parts = [f"type={request_type}", f"status={status}"]
    if getattr(dto, "service_id", None):
        parts.append("service=set")
    if getattr(dto, "master_id", None):
        parts.append("master=set")
    game = getattr(dto, "game_context", None)
    if game is not None:
        parts.append("game=set")
        gift = getattr(game, "gift", None)
        procedure = getattr(game, "procedure", None)
        if gift:
            parts.append(f"gift={str(gift)[:80]}")
        if procedure:
            parts.append(f"procedure={str(procedure)[:80]}")
    return "; ".join(parts)


def build_teya_crm_task_text(
    dto: object, *, appointment_id: str | None = None
) -> str:
    """Canonical CRM task text matching orchestrator create path."""

    game = getattr(dto, "game_context", None)
    if game is None:
        return TASK_TEXT_DEFAULT
    return build_game_task_text(
        gift=getattr(game, "gift", None),
        procedure=getattr(game, "procedure", None),
        appointment_id=appointment_id,
    )


def _map_write(
    receipt: AmoCrmCrmWriteReceipt,
    *,
    contact_id: str | None = None,
    deal_id: str | None = None,
) -> TeyaCrmActionResult:
    cid = receipt.contact_id or contact_id
    lid = receipt.lead_id or deal_id
    if receipt.outcome is AmoCrmCrmWriteOutcome.VERIFIED:
        return TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY,
            contact_id=cid,
            deal_id=lid,
            task_id=receipt.task_id,
            note_id=receipt.note_id,
        )
    if receipt.outcome is AmoCrmCrmWriteOutcome.RECONCILIATION_REQUIRED:
        return TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.RECONCILIATION_REQUIRED,
            contact_id=cid,
            deal_id=lid,
            task_id=receipt.task_id,
            note_id=receipt.note_id,
            error_code=receipt.error_code,
        )
    if receipt.outcome is AmoCrmCrmWriteOutcome.DISABLED:
        return TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
            contact_id=cid,
            deal_id=lid,
            error_code=receipt.error_code or "AMOCRM_CRM_REST_DISABLED",
        )
    if receipt.error_code in {
        "AMOCRM_NOTE_AMBIGUOUS",
        "AMOCRM_TASK_AMBIGUOUS",
        "AMOCRM_CRM_BUSINESS_WRITE_DISABLED",
        "AMOCRM_CRM_BUSINESS_WRITE_CONFIG_INVALID",
        "AMOCRM_CRM_BUSINESS_WRITE_IDS_INVALID",
    }:
        return TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
            contact_id=cid,
            deal_id=lid,
            task_id=receipt.task_id,
            note_id=receipt.note_id,
            error_code=receipt.error_code,
        )
    if is_retryable_crm_error(receipt.error_code):
        return TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.RETRY,
            contact_id=cid,
            deal_id=lid,
            task_id=receipt.task_id,
            note_id=receipt.note_id,
            error_code=receipt.error_code,
        )
    return TeyaCrmActionResult(
        outcome=TeyaCrmActionOutcome.FAIL_CLOSED,
        contact_id=cid,
        deal_id=lid,
        error_code=receipt.error_code,
    )


def _map_analytics(
    receipt: AmoCrmAnalyticsApplyReceipt,
    *,
    deal_id: str | None = None,
) -> TeyaCrmActionResult:
    lid = receipt.lead_id or deal_id
    decision = (
        None if receipt.decision is None else receipt.decision.value
    )
    if receipt.decision is AmoCrmAnalyticsApplyDecision.APPLIED:
        return TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY,
            deal_id=lid,
            analytics_decision=decision,
            error_code="ANALYTICS_APPLIED",
        )
    if receipt.decision is AmoCrmAnalyticsApplyDecision.ALREADY_SAME:
        return TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY,
            deal_id=lid,
            analytics_decision=decision,
            error_code="ANALYTICS_ALREADY_SAME",
        )
    if receipt.decision is AmoCrmAnalyticsApplyDecision.CONFLICT_NONEMPTY:
        return TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY,
            deal_id=lid,
            analytics_decision=decision,
            error_code="ANALYTICS_CONFLICT_NONEMPTY",
        )
    if receipt.decision is AmoCrmAnalyticsApplyDecision.SKIPPED_NO_EVIDENCE:
        return TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.READY,
            deal_id=lid,
            analytics_decision=decision,
            error_code="ANALYTICS_SKIPPED_NO_EVIDENCE",
        )
    if receipt.error_code in {
        "AMOCRM_ANALYTICS_CHANNEL_WRITE_FORBIDDEN",
        "AMOCRM_ANALYTICS_FIELD_NOT_WRITABLE",
        "AMOCRM_ANALYTICS_FIELD_ID_INVALID",
        "AMOCRM_ANALYTICS_ENUM_ID_INVALID",
        "AMOCRM_ANALYTICS_ENUM_NOT_ALLOWED_FOR_FIELD",
        "ANALYTICS_TECHNICAL_DEAL_FORBIDDEN",
    }:
        return TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.FAIL_CLOSED,
            deal_id=lid,
            analytics_decision=decision
            or AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW.value,
            error_code=receipt.error_code,
        )
    if receipt.error_code in {
        "AMOCRM_ANALYTICS_FIELD_AMBIGUOUS",
        "AMOCRM_CRM_REST_DISABLED",
        "AMOCRM_CRM_OAUTH_NOT_FOUND",
        "AMOCRM_ANALYTICS_UNAUTHORIZED",
        "AMOCRM_ANALYTICS_PATCH_PERMANENT",
        "AMOCRM_ANALYTICS_READ_PERMANENT",
        "AMOCRM_ANALYTICS_POSTCHECK_PERMANENT",
    } or receipt.decision is AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW:
        return TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.MANUAL_REVIEW,
            deal_id=lid,
            analytics_decision=(
                decision or AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW.value
            ),
            error_code=receipt.error_code or "AMOCRM_ANALYTICS_MANUAL",
        )
    if (
        receipt.outcome is AmoCrmCrmWriteOutcome.RECONCILIATION_REQUIRED
        or receipt.decision is AmoCrmAnalyticsApplyDecision.TRANSIENT_RETRY
        or is_retryable_crm_error(receipt.error_code)
    ):
        return TeyaCrmActionResult(
            outcome=TeyaCrmActionOutcome.RETRY,
            deal_id=lid,
            analytics_decision=AmoCrmAnalyticsApplyDecision.TRANSIENT_RETRY.value,
            error_code=receipt.error_code or "AMOCRM_ANALYTICS_PATCH_TRANSIENT",
        )
    return TeyaCrmActionResult(
        outcome=TeyaCrmActionOutcome.FAIL_CLOSED,
        deal_id=lid,
        analytics_decision=decision,
        error_code=receipt.error_code or "AMOCRM_ANALYTICS_FAILED",
    )
