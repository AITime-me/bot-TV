"""amoCRM v4 contacts HTTP helpers for identity lookup (IR-2).

Read-only GET by id and GET list by query. Bearer auth only.
Never creates/updates contacts. Completely separate from Chat HMAC.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

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
    "AmoCrmContactRecord",
    "AmoCrmContactByIdResult",
    "AmoCrmContactQueryPageResult",
    "AmoCrmContactsHttpClient",
    "contact_has_exact_phone",
    "extract_contact_phone_raw_values",
    "parse_contact_body",
    "parse_contacts_query_page",
)

_TIMEOUT_SECONDS: Final[float] = 10.0
_MAX_RESPONSE_BYTES: Final[int] = 65536
_DEFAULT_PAGE_LIMIT: Final[int] = 50


def _classify_status(status_code: int) -> AmoCrmCrmRestOutcome:
    """Read-only contacts status policy.

    401 is the special OAuth refresh path (UNAUTHORIZED). 400/402/403/404/422
    are permanent. 429/5xx stay transient, matching CRM REST retryability
    except 402/403 which must not be treated as retryable here.
    """

    if 200 <= status_code < 300:
        return AmoCrmCrmRestOutcome.SUCCESS
    if status_code == 401:
        return AmoCrmCrmRestOutcome.UNAUTHORIZED
    if status_code in {400, 402, 403, 404, 422}:
        return AmoCrmCrmRestOutcome.PERMANENT_ERROR
    return AmoCrmCrmRestOutcome.TRANSIENT_ERROR


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmContactRecord:
    """Parsed contact. Raw phone strings stay out of repr."""

    contact_id: str
    phone_raw_values: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "AmoCrmContactRecord("
            f"contact_id={self.contact_id!r}, "
            f"phone_raw_count={len(self.phone_raw_values)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmContactByIdResult:
    outcome: AmoCrmCrmRestOutcome
    contact: AmoCrmContactRecord | None = None
    error_code: str | None = None
    not_found: bool = False
    unauthorized: bool = False
    status_code: int | None = None

    def __repr__(self) -> str:
        return (
            "AmoCrmContactByIdResult("
            f"outcome={self.outcome!r}, "
            f"contact_id={None if self.contact is None else self.contact.contact_id!r}, "
            f"error_code={self.error_code!r}, not_found={self.not_found!r}, "
            f"unauthorized={self.unauthorized!r}, status_code={self.status_code!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmContactQueryPageResult:
    outcome: AmoCrmCrmRestOutcome
    contacts: tuple[AmoCrmContactRecord, ...] = ()
    has_next_page: bool = False
    error_code: str | None = None
    unauthorized: bool = False
    status_code: int | None = None
    empty: bool = False

    def __repr__(self) -> str:
        return (
            "AmoCrmContactQueryPageResult("
            f"outcome={self.outcome!r}, contacts_count={len(self.contacts)}, "
            f"has_next_page={self.has_next_page!r}, error_code={self.error_code!r}, "
            f"unauthorized={self.unauthorized!r}, status_code={self.status_code!r}, "
            f"empty={self.empty!r})"
        )


def extract_contact_phone_raw_values(contact: object) -> tuple[str, ...]:
    """Collect PHONE custom-field values only. Name/email ignored."""

    if not isinstance(contact, dict):
        return ()
    fields = contact.get("custom_fields_values")
    if not isinstance(fields, list):
        return ()
    out: list[str] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        code = field.get("field_code")
        if type(code) is not str or code != "PHONE":
            continue
        values = field.get("values")
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            raw = item.get("value")
            if type(raw) is str and raw.strip():
                out.append(raw.strip())
    return tuple(out)


def parse_contact_body(body: bytes) -> AmoCrmContactRecord | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(payload, dict):
        return None
    contact_id = payload.get("id")
    if type(contact_id) is not int or isinstance(contact_id, bool) or contact_id <= 0:
        return None
    return AmoCrmContactRecord(
        contact_id=str(contact_id),
        phone_raw_values=extract_contact_phone_raw_values(payload),
    )


def parse_contacts_query_page(
    body: bytes,
    *,
    status_code: int,
) -> tuple[tuple[AmoCrmContactRecord, ...], bool] | None:
    """Return (contacts, has_next_page) or None on malformed payload.

    HTTP 200 requires a complete envelope: ``_embedded`` object,
    ``_embedded.contacts`` list, and ``_links`` object. Missing or malformed
    pagination must not be treated as the last page. ``204`` is empty.
    """

    if status_code == 204:
        return (), False
    if not body:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(payload, dict):
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

    parsed: list[AmoCrmContactRecord] = []
    for item in contacts_raw:
        if not isinstance(item, dict):
            return None
        cid = item.get("id")
        if type(cid) is not int or isinstance(cid, bool) or cid <= 0:
            return None
        parsed.append(
            AmoCrmContactRecord(
                contact_id=str(cid),
                phone_raw_values=extract_contact_phone_raw_values(item),
            )
        )

    if "_links" not in payload:
        return None
    links = payload["_links"]
    if not isinstance(links, dict):
        return None
    if "next" not in links:
        return tuple(parsed), False
    nxt = links["next"]
    if not isinstance(nxt, dict):
        return None
    href = nxt.get("href")
    if type(href) is not str or not href:
        return None
    return tuple(parsed), True


def contact_has_exact_phone(
    contact: AmoCrmContactRecord,
    *,
    normalized_phone: str,
    normalize_fn,
) -> bool:
    """True iff any PHONE value normalizes to ``normalized_phone``."""

    for raw in contact.phone_raw_values:
        try:
            if normalize_fn(raw) == normalized_phone:
                return True
        except Exception:
            continue
    return False


class AmoCrmContactsHttpClient:
    """v4 contacts GET helpers. Callers supply decrypted access tokens."""

    def __init__(
        self,
        config: AmoCrmCrmRestConfig,
        *,
        transport: AmoCrmCrmRestTransport,
    ) -> None:
        self._config = config
        self._transport = transport
        self.http_calls: list[str] = []

    def get_contact_by_id(
        self,
        *,
        contact_id: str,
        access_token: str,
    ) -> AmoCrmContactByIdResult:
        if not self._config.enabled:
            return AmoCrmContactByIdResult(outcome=AmoCrmCrmRestOutcome.DISABLED)
        if type(contact_id) is not str or not contact_id.isdigit():
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_CONTACT_ID_INVALID")
        if contact_id.startswith("0") and contact_id != "0":
            # Leading zeros are not valid amoCRM numeric ids.
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_CONTACT_ID_INVALID")
        outcome, response = self._request(
            method="GET",
            path=f"/api/v4/contacts/{contact_id}",
            access_token=access_token,
            call_label="GET_CONTACT_BY_ID",
        )
        if response is not None and response.status_code == 204:
            return AmoCrmContactByIdResult(
                outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
                not_found=True,
                status_code=204,
                error_code="AMOCRM_CRM_HTTP_204",
            )
        if response is not None and response.status_code == 404:
            return AmoCrmContactByIdResult(
                outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
                not_found=True,
                status_code=404,
                error_code="AMOCRM_CRM_HTTP_404",
            )
        if outcome is AmoCrmCrmRestOutcome.UNAUTHORIZED:
            return AmoCrmContactByIdResult(
                outcome=outcome,
                unauthorized=True,
                status_code=401,
                error_code="AMOCRM_CRM_HTTP_401",
            )
        if outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR and response is None:
            return AmoCrmContactByIdResult(
                outcome=outcome,
                error_code="AMOCRM_CRM_TRANSPORT",
            )
        if outcome is not AmoCrmCrmRestOutcome.SUCCESS or response is None:
            status = None if response is None else response.status_code
            return AmoCrmContactByIdResult(
                outcome=outcome,
                error_code=(
                    "AMOCRM_CRM_TRANSPORT"
                    if status is None
                    else f"AMOCRM_CRM_HTTP_{status}"
                ),
                status_code=status,
            )
        parsed = parse_contact_body(response.body)
        if parsed is None:
            return AmoCrmContactByIdResult(
                outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
                error_code="AMOCRM_CRM_CONTACT_BODY_INVALID",
                status_code=response.status_code,
            )
        if parsed.contact_id != contact_id:
            return AmoCrmContactByIdResult(
                outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
                error_code="AMOCRM_CRM_CONTACT_ID_MISMATCH",
                status_code=response.status_code,
            )
        return AmoCrmContactByIdResult(
            outcome=AmoCrmCrmRestOutcome.SUCCESS,
            contact=parsed,
            status_code=response.status_code,
        )

    def query_contacts_page(
        self,
        *,
        query: str,
        access_token: str,
        page: int = 1,
        limit: int = _DEFAULT_PAGE_LIMIT,
    ) -> AmoCrmContactQueryPageResult:
        if not self._config.enabled:
            return AmoCrmContactQueryPageResult(outcome=AmoCrmCrmRestOutcome.DISABLED)
        if type(query) is not str or not query.strip():
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_CONTACT_QUERY_INVALID")
        if type(page) is not int or isinstance(page, bool) or page < 1:
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_CONTACT_PAGE_INVALID")
        if type(limit) is not int or isinstance(limit, bool) or limit < 1 or limit > 250:
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_CONTACT_LIMIT_INVALID")
        encoded = quote(query.strip(), safe="")
        path = f"/api/v4/contacts?query={encoded}&limit={limit}&page={page}"
        outcome, response = self._request(
            method="GET",
            path=path,
            access_token=access_token,
            call_label=f"GET_CONTACTS_QUERY_P{page}",
        )
        if outcome is AmoCrmCrmRestOutcome.UNAUTHORIZED:
            return AmoCrmContactQueryPageResult(
                outcome=outcome,
                unauthorized=True,
                status_code=401,
                error_code="AMOCRM_CRM_HTTP_401",
            )
        if outcome is AmoCrmCrmRestOutcome.TRANSIENT_ERROR and response is None:
            return AmoCrmContactQueryPageResult(
                outcome=outcome,
                error_code="AMOCRM_CRM_TRANSPORT",
            )
        if response is not None and response.status_code == 204:
            return AmoCrmContactQueryPageResult(
                outcome=AmoCrmCrmRestOutcome.SUCCESS,
                contacts=(),
                has_next_page=False,
                status_code=204,
                empty=True,
            )
        if outcome is not AmoCrmCrmRestOutcome.SUCCESS or response is None:
            status = None if response is None else response.status_code
            return AmoCrmContactQueryPageResult(
                outcome=outcome,
                error_code=(
                    "AMOCRM_CRM_TRANSPORT"
                    if status is None
                    else f"AMOCRM_CRM_HTTP_{status}"
                ),
                status_code=status,
            )
        parsed = parse_contacts_query_page(
            response.body,
            status_code=response.status_code,
        )
        if parsed is None:
            return AmoCrmContactQueryPageResult(
                outcome=AmoCrmCrmRestOutcome.PERMANENT_ERROR,
                error_code="AMOCRM_CRM_CONTACTS_BODY_INVALID",
                status_code=response.status_code,
            )
        contacts, has_next = parsed
        return AmoCrmContactQueryPageResult(
            outcome=AmoCrmCrmRestOutcome.SUCCESS,
            contacts=contacts,
            has_next_page=has_next,
            status_code=response.status_code,
            empty=len(contacts) == 0,
        )

    def _request(
        self,
        *,
        method: str,
        path: str,
        access_token: str,
        call_label: str,
    ) -> tuple[AmoCrmCrmRestOutcome, S2sHttpResponse | None]:
        if type(access_token) is not str or not access_token:
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_TOKEN_INVALID")
        if type(path) is not str or not path.startswith("/"):
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_PATH_INVALID")
        if method != "GET":
            raise AmoCrmCrmRestHttpError("AMOCRM_CRM_METHOD_FORBIDDEN")
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
