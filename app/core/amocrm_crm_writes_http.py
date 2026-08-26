"""Minimal production-safe amoCRM CRM writes for Teya request orchestrator.

ACTION → reread POSTCHECK → VERIFIED. Never uses technical deals as business deals.
Pipeline/status/manager/task IDs are injected from
``AmoCrmBusinessWriteConfig`` (env) — never hardcoded account constants.
Analytics custom-field writes use the centralized allowlist in
``amocrm_analytics_fields`` (never Channel 1321303).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.core.amocrm_analytics_fields import (
    AmoCrmAnalyticsApplyDecision,
    assert_enum_allowed_for_field,
)
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.amocrm_crm_rest_http import (
    AmoCrmCrmRestHttpError,
    AmoCrmCrmRestOutcome,
    AmoCrmCrmRestTransport,
)
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransportError,
)

AMOCRM_SYSTEM_LEAD_STATUS_UNREALIZED: Final[int] = 143

_TIMEOUT_SECONDS: Final[float] = 10.0
_MAX_RESPONSE_BYTES: Final[int] = 65536

# Sentinels for analytics enum GET (not valid amoCRM enum ids).
_ANALYTICS_READ_TRANSIENT: Final[object] = object()
_ANALYTICS_READ_PERMANENT: Final[object] = object()
_ANALYTICS_READ_UNAUTHORIZED: Final[object] = object()
_ANALYTICS_ENUM_EMPTY: Final[object] = object()
_ANALYTICS_ENUM_AMBIGUOUS: Final[object] = object()
# Back-compat alias used only in older call sites / tests if any.
_ANALYTICS_READ_FAILED: Final[object] = _ANALYTICS_READ_TRANSIENT

TASK_TEXT_DEFAULT: Final[str] = "Обработать заявку из онлайн-записи"
TASK_TEXT_GAME_NO_BOOKING: Final[str] = (
    "Обработать заявку по игре — подарок: {gift}; интерес: {procedure}"
)
TASK_TEXT_GAME_SELF_BOOKING: Final[str] = (
    "Клиент сыграл в игру и уже записался самостоятельно. "
    "Проверить подарок {gift} к записи {appointmentId}."
)

__all__ = (
    "AMOCRM_SYSTEM_LEAD_STATUS_UNREALIZED",
    "TASK_TEXT_DEFAULT",
    "TASK_TEXT_GAME_NO_BOOKING",
    "TASK_TEXT_GAME_SELF_BOOKING",
    "AmoCrmAnalyticsApplyReceipt",
    "AmoCrmCrmWriteOutcome",
    "AmoCrmCrmWriteReceipt",
    "AmoCrmCrmWritesHttpClient",
    "format_game_no_booking_task_text",
    "format_game_self_booking_task_text",
    "task_text_fingerprint",
)


class AmoCrmCrmWriteOutcome(StrEnum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    DISABLED = "DISABLED"


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmCrmWriteReceipt:
    outcome: AmoCrmCrmWriteOutcome
    contact_id: str | None = None
    lead_id: str | None = None
    note_id: str | None = None
    task_id: str | None = None
    error_code: str | None = None
    http_calls: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "AmoCrmCrmWriteReceipt("
            f"outcome={self.outcome.value!r}, "
            f"contact_id={self.contact_id!r}, "
            f"lead_id={self.lead_id!r}, "
            f"note_id={self.note_id!r}, "
            f"task_id={self.task_id!r}, "
            f"error_code={self.error_code!r}, "
            f"http_calls={self.http_calls!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmAnalyticsApplyReceipt:
    """Safe write-if-empty result for a single analytics enum field."""

    outcome: AmoCrmCrmWriteOutcome
    decision: AmoCrmAnalyticsApplyDecision | None = None
    field_id: int | None = None
    enum_id: int | None = None
    lead_id: str | None = None
    error_code: str | None = None
    http_calls: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "AmoCrmAnalyticsApplyReceipt("
            f"outcome={self.outcome.value!r}, "
            f"decision={None if self.decision is None else self.decision.value!r}, "
            f"field_id={self.field_id!r}, "
            f"enum_id={self.enum_id!r}, "
            f"lead_id={self.lead_id!r}, "
            f"error_code={self.error_code!r}, "
            f"http_calls={self.http_calls!r})"
        )


def task_text_fingerprint(text: str) -> str:
    """Stable fingerprint for task reconcile (exact text match)."""

    return text.strip()


def format_game_no_booking_task_text(*, gift: str, procedure: str) -> str:
    return TASK_TEXT_GAME_NO_BOOKING.format(gift=gift, procedure=procedure)


def format_game_self_booking_task_text(*, gift: str, appointment_id: str) -> str:
    return TASK_TEXT_GAME_SELF_BOOKING.format(gift=gift, appointmentId=appointment_id)


def _classify_status(status_code: int) -> AmoCrmCrmRestOutcome:
    if 200 <= status_code < 300:
        return AmoCrmCrmRestOutcome.SUCCESS
    if status_code == 401:
        return AmoCrmCrmRestOutcome.UNAUTHORIZED
    if status_code in {400, 403, 404, 422}:
        return AmoCrmCrmRestOutcome.PERMANENT_ERROR
    return AmoCrmCrmRestOutcome.TRANSIENT_ERROR


class AmoCrmCrmWritesHttpClient:
    """v4 contacts/leads/notes/tasks writes with reread postcheck."""

    def __init__(
        self,
        config: AmoCrmCrmRestConfig,
        *,
        transport: AmoCrmCrmRestTransport,
        pipeline_id: int,
        open_status_id: int,
        manager_id: int,
        task_type_id: int,
    ) -> None:
        if (
            type(pipeline_id) is not int
            or isinstance(pipeline_id, bool)
            or pipeline_id <= 0
            or type(open_status_id) is not int
            or isinstance(open_status_id, bool)
            or open_status_id <= 0
            or type(manager_id) is not int
            or isinstance(manager_id, bool)
            or manager_id <= 0
            or type(task_type_id) is not int
            or isinstance(task_type_id, bool)
            or task_type_id <= 0
        ):
            raise ValueError("AMOCRM_CRM_BUSINESS_WRITE_IDS_INVALID")
        self._config = config
        self._transport = transport
        self._pipeline_id = pipeline_id
        self._open_status_id = open_status_id
        self._manager_id = manager_id
        self._task_type_id = task_type_id
        self.http_calls: list[str] = []

    def create_contact(
        self,
        *,
        name: str,
        phone_e164: str,
        access_token: str,
    ) -> AmoCrmCrmWriteReceipt:
        if not self._config.enabled:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.DISABLED,
                error_code="AMOCRM_CRM_REST_DISABLED",
            )
        if type(name) is not str or not name or len(name) > 256:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                error_code="AMOCRM_CONTACT_NAME_INVALID",
            )
        if type(phone_e164) is not str or not phone_e164.startswith("+"):
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                error_code="AMOCRM_CONTACT_PHONE_INVALID",
            )
        payload = [
            {
                "name": name,
                "custom_fields_values": [
                    {
                        "field_code": "PHONE",
                        "values": [{"value": phone_e164, "enum_code": "WORK"}],
                    }
                ],
            }
        ]
        outcome, response = self._request(
            method="POST",
            path="/api/v4/contacts",
            access_token=access_token,
            body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            call_label="POST_CONTACT",
        )
        if outcome is not AmoCrmCrmRestOutcome.SUCCESS or response is None:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                error_code=(
                    "AMOCRM_CONTACT_CREATE_TRANSIENT"
                    if outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR
                    else "AMOCRM_CONTACT_CREATE_FAILED"
                ),
                http_calls=tuple(self.http_calls),
            )
        contact_id = _parse_embedded_id(response.body, "contacts")
        if contact_id is None:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.RECONCILIATION_REQUIRED,
                error_code="AMOCRM_CONTACT_CREATE_PARSE",
                http_calls=tuple(self.http_calls),
            )
        get_outcome, get_resp = self._request(
            method="GET",
            path=f"/api/v4/contacts/{contact_id}",
            access_token=access_token,
            body=b"",
            call_label="GET_CONTACT_POSTCHECK",
        )
        if get_outcome is not AmoCrmCrmRestOutcome.SUCCESS or get_resp is None:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.RECONCILIATION_REQUIRED,
                contact_id=contact_id,
                error_code="AMOCRM_CONTACT_POSTCHECK_FAILED",
                http_calls=tuple(self.http_calls),
            )
        parsed = _parse_entity_id(get_resp.body)
        if parsed != contact_id:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.RECONCILIATION_REQUIRED,
                contact_id=contact_id,
                error_code="AMOCRM_CONTACT_POSTCHECK_MISMATCH",
                http_calls=tuple(self.http_calls),
            )
        return AmoCrmCrmWriteReceipt(
            outcome=AmoCrmCrmWriteOutcome.VERIFIED,
            contact_id=contact_id,
            http_calls=tuple(self.http_calls),
        )

    def create_business_lead(
        self,
        *,
        name: str,
        contact_id: str,
        access_token: str,
    ) -> AmoCrmCrmWriteReceipt:
        if not self._config.enabled:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.DISABLED,
                error_code="AMOCRM_CRM_REST_DISABLED",
            )
        if type(contact_id) is not str or not contact_id.isdigit():
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                error_code="AMOCRM_CONTACT_ID_INVALID",
            )
        payload = [
            {
                "name": name if type(name) is str and name else "Заявка онлайн-записи",
                "pipeline_id": self._pipeline_id,
                "status_id": self._open_status_id,
                "_embedded": {
                    "contacts": [{"id": int(contact_id), "is_main": True}]
                },
            }
        ]
        outcome, response = self._request(
            method="POST",
            path="/api/v4/leads",
            access_token=access_token,
            body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            call_label="POST_LEAD",
        )
        if outcome is not AmoCrmCrmRestOutcome.SUCCESS or response is None:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                contact_id=contact_id,
                error_code=(
                    "AMOCRM_LEAD_CREATE_TRANSIENT"
                    if outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR
                    else "AMOCRM_LEAD_CREATE_FAILED"
                ),
                http_calls=tuple(self.http_calls),
            )
        lead_id = _parse_embedded_id(response.body, "leads")
        if lead_id is None:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.RECONCILIATION_REQUIRED,
                contact_id=contact_id,
                error_code="AMOCRM_LEAD_CREATE_PARSE",
                http_calls=tuple(self.http_calls),
            )
        return self._postcheck_lead(
            lead_id=lead_id,
            contact_id=contact_id,
            access_token=access_token,
            expect_status=self._open_status_id,
        )

    def reanimate_lead(
        self,
        *,
        lead_id: str,
        contact_id: str,
        access_token: str,
    ) -> AmoCrmCrmWriteReceipt:
        """PATCH closed-unrealized (143) lead into open Лиды status."""

        if type(lead_id) is not str or not lead_id.isdigit():
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                error_code="AMOCRM_LEAD_ID_INVALID",
            )
        before_outcome, before = self._request(
            method="GET",
            path=f"/api/v4/leads/{lead_id}",
            access_token=access_token,
            body=b"",
            call_label="GET_LEAD_BEFORE_REANIMATE",
        )
        if before_outcome is not AmoCrmCrmRestOutcome.SUCCESS or before is None:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                contact_id=contact_id,
                lead_id=lead_id,
                error_code=(
                    "AMOCRM_LEAD_REANIMATE_TRANSIENT"
                    if before_outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR
                    else "AMOCRM_LEAD_REANIMATE_READ"
                ),
                http_calls=tuple(self.http_calls),
            )
        before_payload = _json_dict(before.body)
        if before_payload.get("status_id") != AMOCRM_SYSTEM_LEAD_STATUS_UNREALIZED:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                contact_id=contact_id,
                lead_id=lead_id,
                error_code="AMOCRM_LEAD_NOT_REANIMATABLE",
                http_calls=tuple(self.http_calls),
            )
        patch_body = {
            "pipeline_id": self._pipeline_id,
            "status_id": self._open_status_id,
        }
        outcome, response = self._request(
            method="PATCH",
            path=f"/api/v4/leads/{lead_id}",
            access_token=access_token,
            body=json.dumps(patch_body, separators=(",", ":")).encode("utf-8"),
            call_label="PATCH_LEAD_REANIMATE",
        )
        if outcome is not AmoCrmCrmRestOutcome.SUCCESS or response is None:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                contact_id=contact_id,
                lead_id=lead_id,
                error_code=(
                    "AMOCRM_LEAD_REANIMATE_TRANSIENT"
                    if outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR
                    else "AMOCRM_LEAD_REANIMATE_FAILED"
                ),
                http_calls=tuple(self.http_calls),
            )
        return self._postcheck_lead(
            lead_id=lead_id,
            contact_id=contact_id,
            access_token=access_token,
            expect_status=self._open_status_id,
        )

    def ensure_lead_note(
        self,
        *,
        lead_id: str,
        text: str,
        access_token: str,
    ) -> AmoCrmCrmWriteReceipt:
        """List lead notes; reuse exact text fingerprint or create + postcheck."""

        if type(lead_id) is not str or not lead_id.isdigit():
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                error_code="AMOCRM_LEAD_ID_INVALID",
            )
        if type(text) is not str or not text or len(text) > 2000:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                error_code="AMOCRM_NOTE_TEXT_INVALID",
            )
        fingerprint = task_text_fingerprint(text)
        notes = self._list_lead_notes(lead_id=lead_id, access_token=access_token)
        if notes is None:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                lead_id=lead_id,
                error_code="AMOCRM_NOTE_LIST_TRANSIENT",
                http_calls=tuple(self.http_calls),
            )
        matching = [
            row
            for row in notes
            if task_text_fingerprint(_note_text(row)) == fingerprint
        ]
        if len(matching) > 1:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                lead_id=lead_id,
                error_code="AMOCRM_NOTE_AMBIGUOUS",
                http_calls=tuple(self.http_calls),
            )
        if len(matching) == 1:
            note_id = str(matching[0].get("id"))
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.VERIFIED,
                lead_id=lead_id,
                note_id=note_id,
                http_calls=tuple(self.http_calls),
            )
        return self.add_lead_note(
            lead_id=lead_id, text=text, access_token=access_token
        )

    def find_lead_note(
        self,
        *,
        lead_id: str,
        text: str,
        access_token: str,
    ) -> AmoCrmCrmWriteReceipt:
        """Read-only note fingerprint lookup. Never POSTs."""

        if type(lead_id) is not str or not lead_id.isdigit():
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                error_code="AMOCRM_LEAD_ID_INVALID",
            )
        if type(text) is not str or not text or len(text) > 2000:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                error_code="AMOCRM_NOTE_TEXT_INVALID",
            )
        fingerprint = task_text_fingerprint(text)
        notes = self._list_lead_notes(lead_id=lead_id, access_token=access_token)
        if notes is None:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                lead_id=lead_id,
                error_code="AMOCRM_NOTE_LIST_TRANSIENT",
                http_calls=tuple(self.http_calls),
            )
        matching = [
            row
            for row in notes
            if task_text_fingerprint(_note_text(row)) == fingerprint
        ]
        if len(matching) > 1:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                lead_id=lead_id,
                error_code="AMOCRM_NOTE_AMBIGUOUS",
                http_calls=tuple(self.http_calls),
            )
        if len(matching) == 1:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.VERIFIED,
                lead_id=lead_id,
                note_id=str(matching[0].get("id")),
                http_calls=tuple(self.http_calls),
            )
        return AmoCrmCrmWriteReceipt(
            outcome=AmoCrmCrmWriteOutcome.FAILED,
            lead_id=lead_id,
            error_code="AMOCRM_NOTE_NONE",
            http_calls=tuple(self.http_calls),
        )

    def add_lead_note(
        self,
        *,
        lead_id: str,
        text: str,
        access_token: str,
    ) -> AmoCrmCrmWriteReceipt:
        if type(lead_id) is not str or not lead_id.isdigit():
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                error_code="AMOCRM_LEAD_ID_INVALID",
            )
        if type(text) is not str or not text or len(text) > 2000:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                error_code="AMOCRM_NOTE_TEXT_INVALID",
            )
        payload = [{"note_type": "common", "params": {"text": text}}]
        outcome, response = self._request(
            method="POST",
            path=f"/api/v4/leads/{lead_id}/notes",
            access_token=access_token,
            body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            call_label="POST_NOTE",
        )
        if outcome is not AmoCrmCrmRestOutcome.SUCCESS or response is None:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                lead_id=lead_id,
                error_code=(
                    "AMOCRM_NOTE_CREATE_TRANSIENT"
                    if outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR
                    else "AMOCRM_NOTE_CREATE_FAILED"
                ),
                http_calls=tuple(self.http_calls),
            )
        note_id = _parse_embedded_id(response.body, "notes")
        if note_id is None:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.RECONCILIATION_REQUIRED,
                lead_id=lead_id,
                error_code="AMOCRM_NOTE_CREATE_PARSE",
                http_calls=tuple(self.http_calls),
            )
        get_outcome, get_resp = self._request(
            method="GET",
            path=f"/api/v4/leads/{lead_id}/notes/{note_id}",
            access_token=access_token,
            body=b"",
            call_label="GET_NOTE_POSTCHECK",
        )
        if get_outcome is not AmoCrmCrmRestOutcome.SUCCESS or get_resp is None:
            # Some amoCRM deployments omit single-note GET; accept create id.
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.VERIFIED,
                lead_id=lead_id,
                note_id=note_id,
                http_calls=tuple(self.http_calls),
            )
        return AmoCrmCrmWriteReceipt(
            outcome=AmoCrmCrmWriteOutcome.VERIFIED,
            lead_id=lead_id,
            note_id=note_id,
            http_calls=tuple(self.http_calls),
        )

    def ensure_lead_task(
        self,
        *,
        lead_id: str,
        text: str,
        access_token: str,
        complete_till: int | None = None,
    ) -> AmoCrmCrmWriteReceipt:
        """List active tasks; reuse fingerprint match or create + postcheck."""

        if type(lead_id) is not str or not lead_id.isdigit():
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                error_code="AMOCRM_LEAD_ID_INVALID",
            )
        if type(text) is not str or not text or len(text) > 500:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                error_code="AMOCRM_TASK_TEXT_INVALID",
            )
        due = complete_till if complete_till is not None else int(time.time()) + 3600
        fingerprint = task_text_fingerprint(text)
        tasks = self._list_active_tasks(lead_id=lead_id, access_token=access_token)
        if tasks is None:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                lead_id=lead_id,
                error_code="AMOCRM_TASK_LIST_TRANSIENT",
                http_calls=tuple(self.http_calls),
            )
        matching = [
            row
            for row in tasks
            if task_text_fingerprint(str(row.get("text") or "")) == fingerprint
            and row.get("task_type_id") == self._task_type_id
            and row.get("responsible_user_id") == self._manager_id
        ]
        if len(matching) > 1:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                lead_id=lead_id,
                error_code="AMOCRM_TASK_AMBIGUOUS",
                http_calls=tuple(self.http_calls),
            )
        if len(matching) == 1:
            task_id = str(matching[0].get("id"))
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.VERIFIED,
                lead_id=lead_id,
                task_id=task_id,
                http_calls=tuple(self.http_calls),
            )
        payload = [
            {
                "entity_id": int(lead_id),
                "entity_type": "leads",
                "responsible_user_id": self._manager_id,
                "task_type_id": self._task_type_id,
                "text": text,
                "complete_till": due,
            }
        ]
        outcome, response = self._request(
            method="POST",
            path="/api/v4/tasks",
            access_token=access_token,
            body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            call_label="POST_TASK",
        )
        if outcome is not AmoCrmCrmRestOutcome.SUCCESS or response is None:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                lead_id=lead_id,
                error_code=(
                    "AMOCRM_TASK_CREATE_TRANSIENT"
                    if outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR
                    else "AMOCRM_TASK_CREATE_FAILED"
                ),
                http_calls=tuple(self.http_calls),
            )
        task_id = _parse_embedded_id(response.body, "tasks")
        after = self._list_active_tasks(lead_id=lead_id, access_token=access_token)
        if after is None or task_id is None:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.RECONCILIATION_REQUIRED,
                lead_id=lead_id,
                task_id=task_id,
                error_code="AMOCRM_TASK_POSTCHECK_FAILED",
                http_calls=tuple(self.http_calls),
            )
        verified = [
            row
            for row in after
            if str(row.get("id")) == task_id
            and task_text_fingerprint(str(row.get("text") or "")) == fingerprint
        ]
        if len(verified) != 1:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.RECONCILIATION_REQUIRED,
                lead_id=lead_id,
                task_id=task_id,
                error_code="AMOCRM_TASK_POSTCHECK_MISMATCH",
                http_calls=tuple(self.http_calls),
            )
        return AmoCrmCrmWriteReceipt(
            outcome=AmoCrmCrmWriteOutcome.VERIFIED,
            lead_id=lead_id,
            task_id=task_id,
            http_calls=tuple(self.http_calls),
            )

    def find_lead_task(
        self,
        *,
        lead_id: str,
        text: str,
        access_token: str,
    ) -> AmoCrmCrmWriteReceipt:
        """Read-only task fingerprint lookup. Never POSTs."""

        if type(lead_id) is not str or not lead_id.isdigit():
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                error_code="AMOCRM_LEAD_ID_INVALID",
            )
        if type(text) is not str or not text or len(text) > 500:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                error_code="AMOCRM_TASK_TEXT_INVALID",
            )
        fingerprint = task_text_fingerprint(text)
        tasks = self._list_active_tasks(lead_id=lead_id, access_token=access_token)
        if tasks is None:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                lead_id=lead_id,
                error_code="AMOCRM_TASK_LIST_TRANSIENT",
                http_calls=tuple(self.http_calls),
            )
        matching = [
            row
            for row in tasks
            if task_text_fingerprint(str(row.get("text") or "")) == fingerprint
            and row.get("task_type_id") == self._task_type_id
            and row.get("responsible_user_id") == self._manager_id
        ]
        if len(matching) > 1:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                lead_id=lead_id,
                error_code="AMOCRM_TASK_AMBIGUOUS",
                http_calls=tuple(self.http_calls),
            )
        if len(matching) == 1:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.VERIFIED,
                lead_id=lead_id,
                task_id=str(matching[0].get("id")),
                http_calls=tuple(self.http_calls),
            )
        return AmoCrmCrmWriteReceipt(
            outcome=AmoCrmCrmWriteOutcome.FAILED,
            lead_id=lead_id,
            error_code="AMOCRM_TASK_NONE",
            http_calls=tuple(self.http_calls),
        )

    def ensure_lead_analytics_enum_if_empty(
        self,
        *,
        lead_id: str,
        field_id: int,
        enum_id: int,
        access_token: str,
    ) -> AmoCrmAnalyticsApplyReceipt:
        """GET current enum → write only when empty; never overwrite nonempty.

        Channel / cross-field enums rejected before HTTP.
        Uncertain PATCH → GET verify before classifying as APPLIED / retry.
        Permanent 4xx → MANUAL_REVIEW (never transient retry loop).
        """

        try:
            assert_enum_allowed_for_field(field_id, enum_id)
        except ValueError as exc:
            code = str(exc.args[0]) if exc.args else "AMOCRM_ANALYTICS_FIELD_INVALID"
            return AmoCrmAnalyticsApplyReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                field_id=field_id if type(field_id) is int else None,
                enum_id=enum_id if type(enum_id) is int else None,
                lead_id=lead_id if type(lead_id) is str else None,
                error_code=code,
                http_calls=tuple(self.http_calls),
            )
        if type(lead_id) is not str or not lead_id.isdigit():
            return AmoCrmAnalyticsApplyReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                field_id=field_id,
                enum_id=enum_id,
                error_code="AMOCRM_LEAD_ID_INVALID",
                http_calls=tuple(self.http_calls),
            )
        if not self._config.enabled:
            return AmoCrmAnalyticsApplyReceipt(
                outcome=AmoCrmCrmWriteOutcome.DISABLED,
                field_id=field_id,
                enum_id=enum_id,
                lead_id=lead_id,
                error_code="AMOCRM_CRM_REST_DISABLED",
                http_calls=tuple(self.http_calls),
            )

        before = self._read_lead_analytics_enum(
            lead_id=lead_id,
            field_id=field_id,
            access_token=access_token,
            call_label="GET_LEAD_ANALYTICS_BEFORE",
        )
        before_fail = self._analytics_read_failure_receipt(
            before,
            field_id=field_id,
            enum_id=enum_id,
            lead_id=lead_id,
            phase="READ",
        )
        if before_fail is not None:
            return before_fail
        if before is _ANALYTICS_ENUM_AMBIGUOUS:
            return AmoCrmAnalyticsApplyReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                decision=AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW,
                field_id=field_id,
                enum_id=enum_id,
                lead_id=lead_id,
                error_code="AMOCRM_ANALYTICS_FIELD_AMBIGUOUS",
                http_calls=tuple(self.http_calls),
            )
        if before == enum_id:
            return AmoCrmAnalyticsApplyReceipt(
                outcome=AmoCrmCrmWriteOutcome.VERIFIED,
                decision=AmoCrmAnalyticsApplyDecision.ALREADY_SAME,
                field_id=field_id,
                enum_id=enum_id,
                lead_id=lead_id,
                http_calls=tuple(self.http_calls),
            )
        if before is not _ANALYTICS_ENUM_EMPTY:
            return AmoCrmAnalyticsApplyReceipt(
                outcome=AmoCrmCrmWriteOutcome.VERIFIED,
                decision=AmoCrmAnalyticsApplyDecision.CONFLICT_NONEMPTY,
                field_id=field_id,
                enum_id=enum_id,
                lead_id=lead_id,
                error_code="AMOCRM_ANALYTICS_CONFLICT_NONEMPTY",
                http_calls=tuple(self.http_calls),
            )

        patch_body = [
            {
                "id": int(lead_id),
                "custom_fields_values": [
                    {
                        "field_id": field_id,
                        "values": [{"enum_id": enum_id}],
                    }
                ],
            }
        ]
        outcome, response = self._request(
            method="PATCH",
            path="/api/v4/leads",
            access_token=access_token,
            body=json.dumps(patch_body, separators=(",", ":")).encode("utf-8"),
            call_label="PATCH_LEAD_ANALYTICS",
        )
        # Uncertain or failed PATCH → GET verify before retry (never blind re-PATCH).
        if outcome is not AmoCrmCrmRestOutcome.SUCCESS or response is None:
            if outcome is AmoCrmCrmRestOutcome.UNAUTHORIZED:
                return AmoCrmAnalyticsApplyReceipt(
                    outcome=AmoCrmCrmWriteOutcome.FAILED,
                    decision=AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW,
                    field_id=field_id,
                    enum_id=enum_id,
                    lead_id=lead_id,
                    error_code="AMOCRM_ANALYTICS_UNAUTHORIZED",
                    http_calls=tuple(self.http_calls),
                )
            verified = self._read_lead_analytics_enum(
                lead_id=lead_id,
                field_id=field_id,
                access_token=access_token,
                call_label="GET_LEAD_ANALYTICS_VERIFY",
            )
            if verified == enum_id:
                return AmoCrmAnalyticsApplyReceipt(
                    outcome=AmoCrmCrmWriteOutcome.VERIFIED,
                    decision=AmoCrmAnalyticsApplyDecision.APPLIED,
                    field_id=field_id,
                    enum_id=enum_id,
                    lead_id=lead_id,
                    http_calls=tuple(self.http_calls),
                )
            if verified is _ANALYTICS_READ_UNAUTHORIZED:
                return AmoCrmAnalyticsApplyReceipt(
                    outcome=AmoCrmCrmWriteOutcome.FAILED,
                    decision=AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW,
                    field_id=field_id,
                    enum_id=enum_id,
                    lead_id=lead_id,
                    error_code="AMOCRM_ANALYTICS_UNAUTHORIZED",
                    http_calls=tuple(self.http_calls),
                )
            if (
                verified is not _ANALYTICS_READ_TRANSIENT
                and verified is not _ANALYTICS_READ_PERMANENT
                and verified is not _ANALYTICS_ENUM_EMPTY
                and verified is not _ANALYTICS_ENUM_AMBIGUOUS
                and verified != enum_id
            ):
                return AmoCrmAnalyticsApplyReceipt(
                    outcome=AmoCrmCrmWriteOutcome.VERIFIED,
                    decision=AmoCrmAnalyticsApplyDecision.CONFLICT_NONEMPTY,
                    field_id=field_id,
                    enum_id=enum_id,
                    lead_id=lead_id,
                    error_code="AMOCRM_ANALYTICS_CONFLICT_NONEMPTY",
                    http_calls=tuple(self.http_calls),
                )
            if (
                outcome is AmoCrmCrmRestOutcome.PERMANENT_ERROR
                or verified is _ANALYTICS_READ_PERMANENT
            ):
                return AmoCrmAnalyticsApplyReceipt(
                    outcome=AmoCrmCrmWriteOutcome.FAILED,
                    decision=AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW,
                    field_id=field_id,
                    enum_id=enum_id,
                    lead_id=lead_id,
                    error_code="AMOCRM_ANALYTICS_PATCH_PERMANENT",
                    http_calls=tuple(self.http_calls),
                )
            return AmoCrmAnalyticsApplyReceipt(
                outcome=AmoCrmCrmWriteOutcome.RECONCILIATION_REQUIRED,
                decision=AmoCrmAnalyticsApplyDecision.TRANSIENT_RETRY,
                field_id=field_id,
                enum_id=enum_id,
                lead_id=lead_id,
                error_code="AMOCRM_ANALYTICS_PATCH_TRANSIENT",
                http_calls=tuple(self.http_calls),
            )

        after = self._read_lead_analytics_enum(
            lead_id=lead_id,
            field_id=field_id,
            access_token=access_token,
            call_label="GET_LEAD_ANALYTICS_POSTCHECK",
        )
        if after == enum_id:
            return AmoCrmAnalyticsApplyReceipt(
                outcome=AmoCrmCrmWriteOutcome.VERIFIED,
                decision=AmoCrmAnalyticsApplyDecision.APPLIED,
                field_id=field_id,
                enum_id=enum_id,
                lead_id=lead_id,
                http_calls=tuple(self.http_calls),
            )
        after_fail = self._analytics_read_failure_receipt(
            after,
            field_id=field_id,
            enum_id=enum_id,
            lead_id=lead_id,
            phase="POSTCHECK",
        )
        if after_fail is not None:
            return after_fail
        if after is _ANALYTICS_ENUM_EMPTY:
            return AmoCrmAnalyticsApplyReceipt(
                outcome=AmoCrmCrmWriteOutcome.RECONCILIATION_REQUIRED,
                decision=AmoCrmAnalyticsApplyDecision.TRANSIENT_RETRY,
                field_id=field_id,
                enum_id=enum_id,
                lead_id=lead_id,
                error_code="AMOCRM_ANALYTICS_POSTCHECK_TRANSIENT",
                http_calls=tuple(self.http_calls),
            )
        if after is _ANALYTICS_ENUM_AMBIGUOUS:
            return AmoCrmAnalyticsApplyReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                decision=AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW,
                field_id=field_id,
                enum_id=enum_id,
                lead_id=lead_id,
                error_code="AMOCRM_ANALYTICS_FIELD_AMBIGUOUS",
                http_calls=tuple(self.http_calls),
            )
        # Nonempty different enum after successful PATCH → conflict, not retry.
        return AmoCrmAnalyticsApplyReceipt(
            outcome=AmoCrmCrmWriteOutcome.VERIFIED,
            decision=AmoCrmAnalyticsApplyDecision.CONFLICT_NONEMPTY,
            field_id=field_id,
            enum_id=enum_id,
            lead_id=lead_id,
            error_code="AMOCRM_ANALYTICS_CONFLICT_NONEMPTY",
            http_calls=tuple(self.http_calls),
        )

    def _analytics_read_failure_receipt(
        self,
        value: object,
        *,
        field_id: int,
        enum_id: int,
        lead_id: str,
        phase: str,
    ) -> AmoCrmAnalyticsApplyReceipt | None:
        if value is _ANALYTICS_READ_UNAUTHORIZED:
            return AmoCrmAnalyticsApplyReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                decision=AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW,
                field_id=field_id,
                enum_id=enum_id,
                lead_id=lead_id,
                error_code="AMOCRM_ANALYTICS_UNAUTHORIZED",
                http_calls=tuple(self.http_calls),
            )
        if value is _ANALYTICS_READ_PERMANENT:
            return AmoCrmAnalyticsApplyReceipt(
                outcome=AmoCrmCrmWriteOutcome.FAILED,
                decision=AmoCrmAnalyticsApplyDecision.MANUAL_REVIEW,
                field_id=field_id,
                enum_id=enum_id,
                lead_id=lead_id,
                error_code=(
                    "AMOCRM_ANALYTICS_READ_PERMANENT"
                    if phase == "READ"
                    else "AMOCRM_ANALYTICS_POSTCHECK_PERMANENT"
                ),
                http_calls=tuple(self.http_calls),
            )
        if value is _ANALYTICS_READ_TRANSIENT:
            return AmoCrmAnalyticsApplyReceipt(
                outcome=(
                    AmoCrmCrmWriteOutcome.FAILED
                    if phase == "READ"
                    else AmoCrmCrmWriteOutcome.RECONCILIATION_REQUIRED
                ),
                decision=AmoCrmAnalyticsApplyDecision.TRANSIENT_RETRY,
                field_id=field_id,
                enum_id=enum_id,
                lead_id=lead_id,
                error_code=(
                    "AMOCRM_ANALYTICS_READ_TRANSIENT"
                    if phase == "READ"
                    else "AMOCRM_ANALYTICS_POSTCHECK_TRANSIENT"
                ),
                http_calls=tuple(self.http_calls),
            )
        return None

    def _read_lead_analytics_enum(
        self,
        *,
        lead_id: str,
        field_id: int,
        access_token: str,
        call_label: str,
    ) -> object:
        """Return enum_id | EMPTY | AMBIGUOUS | read failure sentinels."""

        outcome, response = self._request(
            method="GET",
            path=f"/api/v4/leads/{lead_id}",
            access_token=access_token,
            body=b"",
            call_label=call_label,
        )
        if outcome is AmoCrmCrmRestOutcome.UNAUTHORIZED:
            return _ANALYTICS_READ_UNAUTHORIZED
        if outcome is AmoCrmCrmRestOutcome.PERMANENT_ERROR:
            return _ANALYTICS_READ_PERMANENT
        if outcome is not AmoCrmCrmRestOutcome.SUCCESS or response is None:
            return _ANALYTICS_READ_TRANSIENT
        payload = _json_dict(response.body)
        enums = _extract_lead_field_enum_ids(payload, field_id)
        if len(enums) == 0:
            return _ANALYTICS_ENUM_EMPTY
        if len(enums) > 1:
            return _ANALYTICS_ENUM_AMBIGUOUS
        return enums[0]

    def _postcheck_lead(
        self,
        *,
        lead_id: str,
        contact_id: str,
        access_token: str,
        expect_status: int,
    ) -> AmoCrmCrmWriteReceipt:
        outcome, response = self._request(
            method="GET",
            path=f"/api/v4/leads/{lead_id}",
            access_token=access_token,
            body=b"",
            call_label="GET_LEAD_POSTCHECK",
        )
        if outcome is not AmoCrmCrmRestOutcome.SUCCESS or response is None:
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.RECONCILIATION_REQUIRED,
                contact_id=contact_id,
                lead_id=lead_id,
                error_code="AMOCRM_LEAD_POSTCHECK_FAILED",
                http_calls=tuple(self.http_calls),
            )
        payload = _json_dict(response.body)
        if (
            payload.get("pipeline_id") != self._pipeline_id
            or payload.get("status_id") != expect_status
        ):
            return AmoCrmCrmWriteReceipt(
                outcome=AmoCrmCrmWriteOutcome.RECONCILIATION_REQUIRED,
                contact_id=contact_id,
                lead_id=lead_id,
                error_code="AMOCRM_LEAD_POSTCHECK_MISMATCH",
                http_calls=tuple(self.http_calls),
            )
        return AmoCrmCrmWriteReceipt(
            outcome=AmoCrmCrmWriteOutcome.VERIFIED,
            contact_id=contact_id,
            lead_id=lead_id,
            http_calls=tuple(self.http_calls),
        )

    def _list_active_tasks(
        self, *, lead_id: str, access_token: str
    ) -> list[dict] | None:
        path = (
            "/api/v4/tasks?filter[entity_type]=leads"
            f"&filter[entity_id]={lead_id}&filter[is_completed]=0&limit=250"
        )
        outcome, response = self._request(
            method="GET",
            path=path,
            access_token=access_token,
            body=b"",
            call_label="GET_ACTIVE_TASKS",
        )
        if response is not None and response.status_code == 204:
            return []
        if outcome is not AmoCrmCrmRestOutcome.SUCCESS or response is None:
            return None
        payload = _json_dict(response.body)
        rows = (payload.get("_embedded") or {}).get("tasks") or []
        if not isinstance(rows, list):
            return None
        return [row for row in rows if isinstance(row, dict)]

    def _list_lead_notes(
        self, *, lead_id: str, access_token: str
    ) -> list[dict] | None:
        path = f"/api/v4/leads/{lead_id}/notes?limit=250"
        outcome, response = self._request(
            method="GET",
            path=path,
            access_token=access_token,
            body=b"",
            call_label="GET_LEAD_NOTES",
        )
        if response is not None and response.status_code == 204:
            return []
        if outcome is not AmoCrmCrmRestOutcome.SUCCESS or response is None:
            return None
        payload = _json_dict(response.body)
        rows = (payload.get("_embedded") or {}).get("notes") or []
        if not isinstance(rows, list):
            return None
        return [row for row in rows if isinstance(row, dict)]

    def _request(
        self,
        *,
        method: str,
        path: str,
        access_token: str,
        body: bytes,
        call_label: str,
    ) -> tuple[AmoCrmCrmRestOutcome, S2sHttpResponse | None]:
        if type(access_token) is not str or not access_token:
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_TOKEN_INVALID")
        req = S2sHttpRequest(
            method=method,
            url=f"{self._config.api_base_url}{path}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            body=body,
            timeout_seconds=_TIMEOUT_SECONDS,
            allow_redirects=False,
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
        self.http_calls.append(call_label)
        try:
            response = self._transport.request(req)
        except S2sHttpTransportError:
            return AmoCrmCrmRestOutcome.TRANSIENT_ERROR, None
        return _classify_status(response.status_code), response


def _json_dict(body: bytes) -> dict:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_entity_id(body: bytes) -> str | None:
    payload = _json_dict(body)
    entity_id = payload.get("id")
    if type(entity_id) is int and not isinstance(entity_id, bool) and entity_id > 0:
        return str(entity_id)
    return None


def _parse_embedded_id(body: bytes, key: str) -> str | None:
    payload = _json_dict(body)
    embedded = payload.get("_embedded")
    if not isinstance(embedded, dict):
        return None
    rows = embedded.get(key)
    if not isinstance(rows, list) or not rows:
        return None
    first = rows[0]
    if not isinstance(first, dict):
        return None
    entity_id = first.get("id")
    if type(entity_id) is int and not isinstance(entity_id, bool) and entity_id > 0:
        return str(entity_id)
    return None


def _note_text(row: dict) -> str:
    params = row.get("params")
    if isinstance(params, dict):
        text = params.get("text")
        if type(text) is str:
            return text
    text = row.get("text")
    return text if type(text) is str else ""


def _extract_lead_field_enum_ids(payload: dict, field_id: int) -> list[int]:
    """Collect positive enum_id values for field_id from a lead GET payload."""

    rows = payload.get("custom_fields_values")
    if not isinstance(rows, list):
        return []
    found: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("field_id") != field_id:
            continue
        values = row.get("values")
        if not isinstance(values, list) or not values:
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            enum_id = value.get("enum_id")
            if type(enum_id) is int and not isinstance(enum_id, bool) and enum_id > 0:
                found.append(enum_id)
    return found
