"""Read-only amoCRM HTTP for Buyer Card discovery (IR-3).

GET contact with linked leads and GET lead with linked contacts.
Never creates/updates CRM entities. Completely separate from Chat HMAC
and from the write-capable leads client.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

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

__all__ = (
    "AmoCrmContactWithLeadsRecord",
    "AmoCrmContactWithLeadsResult",
    "AmoCrmLeadInspectRecord",
    "AmoCrmLeadInspectResult",
    "AmoCrmBuyerCardHttpClient",
    "parse_contact_with_leads_body",
    "parse_lead_inspect_body",
)

_TIMEOUT_SECONDS: Final[float] = 10.0
_MAX_RESPONSE_BYTES: Final[int] = 65536


def _classify_status(status_code: int) -> AmoCrmCrmRestOutcome:
    """Read-only discovery status policy (same as IR-2 contacts).

    401 is the OAuth refresh path. 400/402/403/404/422 are permanent.
    429/5xx stay transient.
    """

    if 200 <= status_code < 300:
        return AmoCrmCrmRestOutcome.SUCCESS
    if status_code == 401:
        return AmoCrmCrmRestOutcome.UNAUTHORIZED
    if status_code in {400, 402, 403, 404, 422}:
        return AmoCrmCrmRestOutcome.PERMANENT_ERROR
    return AmoCrmCrmRestOutcome.TRANSIENT_ERROR


def _sorted_unique_positive_ids(raw_items: list[object]) -> tuple[str, ...] | None:
    seen: set[str] = set()
    out: list[str] = []
    for item in raw_items:
        if not isinstance(item, dict):
            return None
        entity_id = item.get("id")
        if type(entity_id) is not int or isinstance(entity_id, bool) or entity_id <= 0:
            return None
        token = str(entity_id)
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return tuple(sorted(out, key=lambda value: int(value)))


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmContactWithLeadsRecord:
    contact_id: str
    linked_lead_ids: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "AmoCrmContactWithLeadsRecord("
            f"contact_id={self.contact_id!r}, "
            f"linked_lead_ids={self.linked_lead_ids!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmContactWithLeadsResult:
    outcome: AmoCrmCrmRestOutcome
    contact: AmoCrmContactWithLeadsRecord | None = None
    error_code: str | None = None
    not_found: bool = False
    unauthorized: bool = False
    status_code: int | None = None

    def __repr__(self) -> str:
        return (
            "AmoCrmContactWithLeadsResult("
            f"outcome={self.outcome!r}, "
            f"contact_id={None if self.contact is None else self.contact.contact_id!r}, "
            f"linked_lead_count="
            f"{0 if self.contact is None else len(self.contact.linked_lead_ids)}, "
            f"error_code={self.error_code!r}, not_found={self.not_found!r}, "
            f"unauthorized={self.unauthorized!r}, status_code={self.status_code!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmLeadInspectRecord:
    lead_id: str
    is_deleted: bool = False
    closed_at: int | None = None
    linked_contact_ids: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "AmoCrmLeadInspectRecord("
            f"lead_id={self.lead_id!r}, is_deleted={self.is_deleted!r}, "
            f"closed={self.closed_at is not None}, "
            f"linked_contact_ids={self.linked_contact_ids!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmLeadInspectResult:
    outcome: AmoCrmCrmRestOutcome
    lead: AmoCrmLeadInspectRecord | None = None
    error_code: str | None = None
    not_found: bool = False
    unauthorized: bool = False
    status_code: int | None = None

    def __repr__(self) -> str:
        return (
            "AmoCrmLeadInspectResult("
            f"outcome={self.outcome!r}, "
            f"lead_id={None if self.lead is None else self.lead.lead_id!r}, "
            f"error_code={self.error_code!r}, not_found={self.not_found!r}, "
            f"unauthorized={self.unauthorized!r}, status_code={self.status_code!r})"
        )


def parse_contact_with_leads_body(body: bytes) -> AmoCrmContactWithLeadsRecord | None:
    """Parse GET /contacts/{{id}}?with=leads. Name/email/tags ignored."""

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(payload, dict):
        return None
    contact_id = payload.get("id")
    if type(contact_id) is not int or isinstance(contact_id, bool) or contact_id <= 0:
        return None
    if "_embedded" not in payload:
        return None
    embedded = payload["_embedded"]
    if not isinstance(embedded, dict):
        return None
    if "leads" not in embedded:
        return None
    leads_raw = embedded["leads"]
    if not isinstance(leads_raw, list):
        return None
    linked = _sorted_unique_positive_ids(leads_raw)
    if linked is None:
        return None
    return AmoCrmContactWithLeadsRecord(
        contact_id=str(contact_id),
        linked_lead_ids=linked,
    )


def parse_lead_inspect_body(body: bytes) -> AmoCrmLeadInspectRecord | None:
    """Parse GET /leads/{{id}}?with=contacts. Name/tags ignored."""

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(payload, dict):
        return None
    lead_id = payload.get("id")
    if type(lead_id) is not int or isinstance(lead_id, bool) or lead_id <= 0:
        return None
    if "is_deleted" not in payload:
        return None
    raw_deleted = payload["is_deleted"]
    if type(raw_deleted) is not bool:
        return None
    if "closed_at" not in payload:
        return None
    raw_closed = payload["closed_at"]
    if raw_closed is None:
        closed_at: int | None = None
    elif type(raw_closed) is int and not isinstance(raw_closed, bool):
        closed_at = raw_closed
    else:
        return None
    if "_embedded" not in payload:
        return None
    embedded = payload["_embedded"]
    if not isinstance(embedded, dict):
        return None
    if "contacts" not in embedded:
        return None
    contacts_raw = embedded["contacts"]
    if not isinstance(contacts_raw, list):
        return None
    linked = _sorted_unique_positive_ids(contacts_raw)
    if linked is None:
        return None
    return AmoCrmLeadInspectRecord(
        lead_id=str(lead_id),
        is_deleted=raw_deleted,
        closed_at=closed_at,
        linked_contact_ids=linked,
    )


def _not_found_contact(status_code: int) -> AmoCrmContactWithLeadsResult:
    return AmoCrmContactWithLeadsResult(
        outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
        not_found=True,
        status_code=status_code,
        error_code=f"AMOCRM_CRM_HTTP_{status_code}",
    )


def _not_found_lead(status_code: int) -> AmoCrmLeadInspectResult:
    return AmoCrmLeadInspectResult(
        outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
        not_found=True,
        status_code=status_code,
        error_code=f"AMOCRM_CRM_HTTP_{status_code}",
    )


class AmoCrmBuyerCardHttpClient:
    """v4 GET helpers for Buyer Card discovery. Callers supply access tokens."""

    def __init__(
        self,
        config: AmoCrmCrmRestConfig,
        *,
        transport: AmoCrmCrmRestTransport,
    ) -> None:
        self._config = config
        self._transport = transport
        self.http_calls: list[str] = []

    def get_contact_with_leads(
        self,
        *,
        contact_id: str,
        access_token: str,
    ) -> AmoCrmContactWithLeadsResult:
        if not self._config.enabled:
            return AmoCrmContactWithLeadsResult(outcome=AmoCrmCrmRestOutcome.DISABLED)
        self._require_entity_id(contact_id, code="AMOCRM_CRM_CONTACT_ID_INVALID")
        outcome, response = self._request(
            path=f"/api/v4/contacts/{contact_id}?with=leads",
            access_token=access_token,
            call_label="GET_CONTACT_WITH_LEADS",
        )
        if response is not None and response.status_code in {204, 404}:
            return _not_found_contact(response.status_code)
        mapped = self._map_transport(
            outcome,
            response,
            unauthorized=lambda: AmoCrmContactWithLeadsResult(
                outcome=AmoCrmCrmRestOutcome.UNAUTHORIZED,
                unauthorized=True,
                status_code=401,
                error_code="AMOCRM_CRM_HTTP_401",
            ),
            transport=lambda: AmoCrmContactWithLeadsResult(
                outcome=AmoCrmCrmRestOutcome.TRANSIENT_ERROR,
                error_code="AMOCRM_CRM_TRANSPORT",
            ),
            other=lambda status, code: AmoCrmContactWithLeadsResult(
                outcome=outcome,
                error_code=code,
                status_code=status,
            ),
        )
        if mapped is not None:
            return mapped
        assert response is not None
        parsed = parse_contact_with_leads_body(response.body)
        if parsed is None:
            return AmoCrmContactWithLeadsResult(
                outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
                error_code="AMOCRM_CRM_CONTACT_BODY_INVALID",
                status_code=response.status_code,
            )
        if parsed.contact_id != contact_id:
            return AmoCrmContactWithLeadsResult(
                outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
                error_code="AMOCRM_CRM_CONTACT_ID_MISMATCH",
                status_code=response.status_code,
            )
        return AmoCrmContactWithLeadsResult(
            outcome=AmoCrmCrmRestOutcome.SUCCESS,
            contact=parsed,
            status_code=response.status_code,
        )

    def get_lead_with_contacts(
        self,
        *,
        lead_id: str,
        access_token: str,
    ) -> AmoCrmLeadInspectResult:
        if not self._config.enabled:
            return AmoCrmLeadInspectResult(outcome=AmoCrmCrmRestOutcome.DISABLED)
        self._require_entity_id(lead_id, code="AMOCRM_CRM_LEAD_ID_INVALID")
        outcome, response = self._request(
            path=f"/api/v4/leads/{lead_id}?with=contacts",
            access_token=access_token,
            call_label=f"GET_LEAD_{lead_id}",
        )
        if response is not None and response.status_code in {204, 404}:
            return _not_found_lead(response.status_code)
        mapped = self._map_transport(
            outcome,
            response,
            unauthorized=lambda: AmoCrmLeadInspectResult(
                outcome=AmoCrmCrmRestOutcome.UNAUTHORIZED,
                unauthorized=True,
                status_code=401,
                error_code="AMOCRM_CRM_HTTP_401",
            ),
            transport=lambda: AmoCrmLeadInspectResult(
                outcome=AmoCrmCrmRestOutcome.TRANSIENT_ERROR,
                error_code="AMOCRM_CRM_TRANSPORT",
            ),
            other=lambda status, code: AmoCrmLeadInspectResult(
                outcome=outcome,
                error_code=code,
                status_code=status,
            ),
        )
        if mapped is not None:
            return mapped
        assert response is not None
        parsed = parse_lead_inspect_body(response.body)
        if parsed is None:
            return AmoCrmLeadInspectResult(
                outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
                error_code="AMOCRM_CRM_LEAD_BODY_INVALID",
                status_code=response.status_code,
            )
        if parsed.lead_id != lead_id:
            return AmoCrmLeadInspectResult(
                outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
                error_code="AMOCRM_CRM_LEAD_ID_MISMATCH",
                status_code=response.status_code,
            )
        return AmoCrmLeadInspectResult(
            outcome=AmoCrmCrmRestOutcome.SUCCESS,
            lead=parsed,
            status_code=response.status_code,
        )

    @staticmethod
    def _require_entity_id(entity_id: str, *, code: str) -> None:
        if type(entity_id) is not str or not entity_id.isdigit():
            raise AmoCrmCrmRestHttpError(code)
        if entity_id.startswith("0"):
            raise AmoCrmCrmRestHttpError(code)

    def _map_transport(
        self,
        outcome: AmoCrmCrmRestOutcome,
        response: S2sHttpResponse | None,
        *,
        unauthorized,
        transport,
        other,
    ) -> object | None:
        if outcome is AmoCrmCrmRestOutcome.UNAUTHORIZED:
            return unauthorized()
        if outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR and response is None:
            return transport()
        if outcome is not AmoCrmCrmRestOutcome.SUCCESS or response is None:
            status = None if response is None else response.status_code
            code = (
                "AMOCRM_CRM_TRANSPORT"
                if status is None
                else f"AMOCRM_CRM_HTTP_{status}"
            )
            return other(status, code)
        return None

    def _request(
        self,
        *,
        path: str,
        access_token: str,
        call_label: str,
    ) -> tuple[AmoCrmCrmRestOutcome, S2sHttpResponse | None]:
        if type(access_token) is not str or not access_token:
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_TOKEN_INVALID")
        if type(path) is not str or not path.startswith("/"):
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_PATH_INVALID")
        req = S2sHttpRequest(
            method="GET",
            url=f"{self._config.api_base_url}{path}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            body=b"",
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
