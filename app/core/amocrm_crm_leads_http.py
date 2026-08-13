"""amoCRM v4 leads HTTP helpers for TECHNICAL_DEAL projection.

Bearer auth only. Completely separate from Chat HMAC.
No notes/tasks. No Filtering API. Contact create never performed here.
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
    "AmoCrmContactGetResult",
    "AmoCrmLeadCreateResult",
    "AmoCrmLeadGetResult",
    "AmoCrmLeadHttpClient",
    "AmoCrmLeadLinkResult",
)

_TIMEOUT_SECONDS: Final[float] = 10.0
_MAX_RESPONSE_BYTES: Final[int] = 65536


def _classify_status(status_code: int) -> AmoCrmCrmRestOutcome:
    if 200 <= status_code < 300:
        return AmoCrmCrmRestOutcome.SUCCESS
    if status_code == 401:
        return AmoCrmCrmRestOutcome.UNAUTHORIZED
    if status_code in {400, 404, 422}:
        return AmoCrmCrmRestOutcome.PERMANENT_ERROR
    # 402 / 403 / 429 / 5xx / transport-equivalent: retry, never revoke.
    return AmoCrmCrmRestOutcome.TRANSIENT_ERROR


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmLeadGetResult:
    outcome: AmoCrmCrmRestOutcome
    lead_id: str | None = None
    error_code: str | None = None
    not_found: bool = False
    unauthorized: bool = False
    status_code: int | None = None
    contact_ids: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "AmoCrmLeadGetResult("
            f"outcome={self.outcome!r}, lead_id={self.lead_id!r}, "
            f"error_code={self.error_code!r}, not_found={self.not_found!r}, "
            f"unauthorized={self.unauthorized!r}, status_code={self.status_code!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmLeadCreateResult:
    outcome: AmoCrmCrmRestOutcome
    lead_id: str | None = None
    error_code: str | None = None
    ambiguous: bool = False
    unauthorized: bool = False
    status_code: int | None = None

    def __repr__(self) -> str:
        return (
            "AmoCrmLeadCreateResult("
            f"outcome={self.outcome!r}, lead_id={self.lead_id!r}, "
            f"error_code={self.error_code!r}, ambiguous={self.ambiguous!r}, "
            f"unauthorized={self.unauthorized!r}, status_code={self.status_code!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmLeadLinkResult:
    outcome: AmoCrmCrmRestOutcome
    error_code: str | None = None
    unauthorized: bool = False
    status_code: int | None = None

    def __repr__(self) -> str:
        return (
            "AmoCrmLeadLinkResult("
            f"outcome={self.outcome!r}, error_code={self.error_code!r}, "
            f"unauthorized={self.unauthorized!r}, status_code={self.status_code!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmContactGetResult:
    outcome: AmoCrmCrmRestOutcome
    exists: bool = False
    unauthorized: bool = False
    status_code: int | None = None

    def __repr__(self) -> str:
        return (
            "AmoCrmContactGetResult("
            f"outcome={self.outcome!r}, exists={self.exists!r}, "
            f"unauthorized={self.unauthorized!r}, status_code={self.status_code!r})"
        )


class AmoCrmLeadHttpClient:
    """v4 leads GET/POST. Callers supply decrypted access tokens."""

    def __init__(
        self,
        config: AmoCrmCrmRestConfig,
        *,
        transport: AmoCrmCrmRestTransport,
    ) -> None:
        self._config = config
        self._transport = transport
        self.http_calls: list[str] = []

    def get_lead(
        self,
        *,
        lead_id: str,
        access_token: str,
        with_contacts: bool = True,
    ) -> AmoCrmLeadGetResult:
        if not self._config.enabled:
            return AmoCrmLeadGetResult(outcome=AmoCrmCrmRestOutcome.DISABLED)
        if type(lead_id) is not str or not lead_id.isdigit():
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_LEAD_ID_INVALID")
        path = f"/api/v4/leads/{lead_id}"
        if with_contacts:
            path = f"{path}?with=contacts"
        outcome, response = self._request(
            method="GET",
            path=path,
            access_token=access_token,
            body=b"",
            call_label="GET_LEAD",
        )
        if outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR and response is None:
            return AmoCrmLeadGetResult(
                outcome=outcome,
                error_code="AMOCRM_CRM_TRANSPORT",
            )
        assert response is not None
        if response.status_code == 404:
            return AmoCrmLeadGetResult(
                outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
                error_code="AMOCRM_CRM_LEAD_NOT_FOUND",
                not_found=True,
                status_code=404,
            )
        if outcome is AmoCrmCrmRestOutcome.UNAUTHORIZED:
            return AmoCrmLeadGetResult(
                outcome=outcome,
                error_code="AMOCRM_CRM_HTTP_401",
                unauthorized=True,
                status_code=401,
            )
        if outcome is not AmoCrmCrmRestOutcome.SUCCESS:
            return AmoCrmLeadGetResult(
                outcome=outcome,
                error_code=f"AMOCRM_CRM_HTTP_{response.status_code}",
                status_code=response.status_code,
            )
        parsed_id, contact_ids = _parse_lead_get(response.body)
        if parsed_id is None:
            return AmoCrmLeadGetResult(
                outcome=AmoCrmCrmRestOutcome.TRANSIENT_ERROR,
                error_code="AMOCRM_CRM_LEAD_RESPONSE_INVALID",
                status_code=response.status_code,
            )
        return AmoCrmLeadGetResult(
            outcome=AmoCrmCrmRestOutcome.SUCCESS,
            lead_id=parsed_id,
            status_code=response.status_code,
            contact_ids=contact_ids,
        )

    def create_lead(
        self,
        *,
        name: str,
        pipeline_id: int,
        status_id: int,
        access_token: str,
        contact_id: str | None = None,
    ) -> AmoCrmLeadCreateResult:
        if not self._config.enabled:
            return AmoCrmLeadCreateResult(outcome=AmoCrmCrmRestOutcome.DISABLED)
        if type(name) is not str or not name or len(name) > 256:
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_LEAD_NAME_INVALID")
        if type(pipeline_id) is not int or isinstance(pipeline_id, bool) or pipeline_id <= 0:
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_PIPELINE_INVALID")
        if type(status_id) is not int or isinstance(status_id, bool) or status_id <= 0:
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_STATUS_INVALID")
        if contact_id is not None and (
            type(contact_id) is not str or not contact_id.isdigit()
        ):
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_CONTACT_ID_INVALID")

        payload: dict[str, object] = {
            "name": name,
            "pipeline_id": pipeline_id,
            "status_id": status_id,
        }
        if contact_id is not None:
            payload["_embedded"] = {
                "contacts": [{"id": int(contact_id), "is_main": True}]
            }
        body = json.dumps([payload], separators=(",", ":")).encode("utf-8")
        outcome, response = self._request(
            method="POST",
            path="/api/v4/leads",
            access_token=access_token,
            body=body,
            call_label="POST_LEAD",
        )
        if outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR and response is None:
            return AmoCrmLeadCreateResult(
                outcome=outcome,
                error_code="AMOCRM_CRM_TRANSPORT",
                ambiguous=True,
            )
        assert response is not None
        if outcome is AmoCrmCrmRestOutcome.SUCCESS:
            lead_id = _parse_lead_id_from_create(response.body)
            if lead_id is None:
                return AmoCrmLeadCreateResult(
                    outcome=AmoCrmCrmRestOutcome.TRANSIENT_ERROR,
                    error_code="AMOCRM_CRM_LEAD_RESPONSE_INVALID",
                    ambiguous=True,
                    status_code=response.status_code,
                )
            return AmoCrmLeadCreateResult(
                outcome=AmoCrmCrmRestOutcome.SUCCESS,
                lead_id=lead_id,
                status_code=response.status_code,
            )
        if outcome is AmoCrmCrmRestOutcome.UNAUTHORIZED:
            return AmoCrmLeadCreateResult(
                outcome=outcome,
                error_code="AMOCRM_CRM_HTTP_401",
                ambiguous=False,
                unauthorized=True,
                status_code=401,
            )
        if outcome is AmoCrmCrmRestOutcome.PERMANENT_ERROR:
            return AmoCrmLeadCreateResult(
                outcome=outcome,
                error_code=f"AMOCRM_CRM_HTTP_{response.status_code}",
                ambiguous=False,
                status_code=response.status_code,
            )
        return AmoCrmLeadCreateResult(
            outcome=outcome,
            error_code=f"AMOCRM_CRM_HTTP_{response.status_code}",
            ambiguous=True,
            status_code=response.status_code,
        )

    def contact_exists(
        self,
        *,
        contact_id: str,
        access_token: str,
    ) -> AmoCrmContactGetResult:
        """GET contact by id. Never creates."""

        if not self._config.enabled:
            return AmoCrmContactGetResult(outcome=AmoCrmCrmRestOutcome.DISABLED)
        if type(contact_id) is not str or not contact_id.isdigit():
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_CONTACT_ID_INVALID")
        outcome, response = self._request(
            method="GET",
            path=f"/api/v4/contacts/{contact_id}",
            access_token=access_token,
            body=b"",
            call_label="GET_CONTACT",
        )
        if outcome is AmoCrmCrmRestOutcome.SUCCESS:
            return AmoCrmContactGetResult(
                outcome=outcome,
                exists=True,
                status_code=200 if response is None else response.status_code,
            )
        if response is not None and response.status_code == 404:
            return AmoCrmContactGetResult(
                outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
                exists=False,
                status_code=404,
            )
        if outcome is AmoCrmCrmRestOutcome.UNAUTHORIZED:
            return AmoCrmContactGetResult(
                outcome=outcome,
                exists=False,
                unauthorized=True,
                status_code=401,
            )
        return AmoCrmContactGetResult(
            outcome=outcome,
            exists=False,
            status_code=None if response is None else response.status_code,
        )

    def link_contact_to_lead(
        self,
        *,
        lead_id: str,
        contact_id: str,
        access_token: str,
    ) -> AmoCrmLeadLinkResult:
        """Optional post-create link. Never creates a contact."""

        if not self._config.enabled:
            return AmoCrmLeadLinkResult(outcome=AmoCrmCrmRestOutcome.DISABLED)
        if type(lead_id) is not str or not lead_id.isdigit():
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_LEAD_ID_INVALID")
        if type(contact_id) is not str or not contact_id.isdigit():
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_CONTACT_ID_INVALID")
        body = json.dumps(
            [
                {
                    "to_entity_id": int(contact_id),
                    "to_entity_type": "contacts",
                    "metadata": {"is_main": True},
                }
            ],
            separators=(",", ":"),
        ).encode("utf-8")
        outcome, response = self._request(
            method="POST",
            path=f"/api/v4/leads/{lead_id}/link",
            access_token=access_token,
            body=body,
            call_label="LINK_CONTACT",
        )
        status = None if response is None else response.status_code
        if outcome is AmoCrmCrmRestOutcome.UNAUTHORIZED:
            return AmoCrmLeadLinkResult(
                outcome=outcome,
                error_code="AMOCRM_CRM_HTTP_401",
                unauthorized=True,
                status_code=401,
            )
        if outcome is not AmoCrmCrmRestOutcome.SUCCESS:
            return AmoCrmLeadLinkResult(
                outcome=outcome,
                error_code=(
                    "AMOCRM_CRM_TRANSPORT"
                    if status is None
                    else f"AMOCRM_CRM_HTTP_{status}"
                ),
                status_code=status,
            )
        return AmoCrmLeadLinkResult(outcome=outcome, status_code=status)

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
        if type(path) is not str or not path.startswith("/"):
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_PATH_INVALID")
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


def _parse_lead_get(body: bytes) -> tuple[str | None, tuple[str, ...]]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None, ()
    if not isinstance(payload, dict):
        return None, ()
    lead_id = payload.get("id")
    if type(lead_id) is not int or isinstance(lead_id, bool) or lead_id <= 0:
        return None, ()
    contact_ids: list[str] = []
    embedded = payload.get("_embedded")
    if isinstance(embedded, dict):
        contacts = embedded.get("contacts")
        if isinstance(contacts, list):
            for item in contacts:
                if not isinstance(item, dict):
                    continue
                cid = item.get("id")
                if type(cid) is int and not isinstance(cid, bool) and cid > 0:
                    contact_ids.append(str(cid))
    return str(lead_id), tuple(contact_ids)


def _parse_lead_id_from_create(body: bytes) -> str | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(payload, dict):
        return None
    embedded = payload.get("_embedded")
    if not isinstance(embedded, dict):
        return None
    leads = embedded.get("leads")
    if not isinstance(leads, list) or not leads:
        return None
    first = leads[0]
    if not isinstance(first, dict):
        return None
    lead_id = first.get("id")
    if type(lead_id) is int and not isinstance(lead_id, bool) and lead_id > 0:
        return str(lead_id)
    return None
