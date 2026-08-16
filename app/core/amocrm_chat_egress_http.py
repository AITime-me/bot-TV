"""Backend-only amoCRM Chat HTTP client (AMO-01B1).

HMAC-SHA1 request signing. No OAuth. Never logs bodies or secrets.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from enum import Enum
from typing import Any, Final, Mapping, Protocol

from app.core.amocrm_chat_egress_config import AmoCrmChatEgressConfig
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransportError,
)

__all__ = (
    "AmoCrmChatEgressHttpClient",
    "AmoCrmChatEgressHttpError",
    "AmoCrmChatEgressOutcome",
    "AmoCrmChatHistoryHit",
    "AmoCrmChatHistoryScan",
    "AmoCrmChatHistoryScanResult",
    "AmoCrmChatSendResult",
    "AmoCrmChatTransport",
    "CHAT_HTTP_TIMEOUT_SECONDS",
    "build_amocrm_chat_signature",
    "content_md5_hex",
    "find_msgid_in_history_body",
)

_CONTENT_TYPE: Final[str] = "application/json"
_MAX_RESPONSE_BYTES: Final[int] = 65536
CHAT_HTTP_TIMEOUT_SECONDS: Final[float] = 10.0
_TIMEOUT_SECONDS: Final[float] = CHAT_HTTP_TIMEOUT_SECONDS
_HISTORY_PAGE_LIMIT: Final[int] = 50
_HISTORY_MAX_PAGES: Final[int] = 20


class AmoCrmChatEgressOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    PERMANENT_ERROR = "PERMANENT_ERROR"
    NOT_FOUND = "NOT_FOUND"


class AmoCrmChatEgressHttpError(RuntimeError):
    """Fixed error codes only."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    def __repr__(self) -> str:
        return f"AmoCrmChatEgressHttpError({self.code!r})"


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmChatSendResult:
    outcome: AmoCrmChatEgressOutcome
    amocrm_message_id: str | None = None
    error_code: str | None = None

    def __repr__(self) -> str:
        return (
            "AmoCrmChatSendResult("
            f"outcome={self.outcome!r}, "
            f"amocrm_message_id={'set' if self.amocrm_message_id else None}, "
            f"error_code={self.error_code!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmChatHistoryHit:
    found: bool
    amocrm_message_id: str | None = None
    outcome: AmoCrmChatEgressOutcome = AmoCrmChatEgressOutcome.SUCCESS
    error_code: str | None = None
    absence_proven: bool = False
    messages_on_page: int = 0

    def __repr__(self) -> str:
        return (
            "AmoCrmChatHistoryHit("
            f"found={self.found!r}, "
            f"amocrm_message_id={'set' if self.amocrm_message_id else None}, "
            f"outcome={self.outcome!r}, "
            f"absence_proven={self.absence_proven!r}, "
            f"error_code={self.error_code!r})"
        )


class AmoCrmChatHistoryScan(str, Enum):
    FOUND = "FOUND"
    ABSENT = "ABSENT"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmChatHistoryScanResult:
    scan: AmoCrmChatHistoryScan
    amocrm_message_id: str | None = None
    error_code: str | None = None

    def __repr__(self) -> str:
        return (
            "AmoCrmChatHistoryScanResult("
            f"scan={self.scan!r}, "
            f"amocrm_message_id={'set' if self.amocrm_message_id else None}, "
            f"error_code={self.error_code!r})"
        )


class AmoCrmChatTransport(Protocol):
    def request(self, req: S2sHttpRequest) -> S2sHttpResponse: ...


def content_md5_hex(body: bytes) -> str:
    return hashlib.md5(body).hexdigest().lower()


def build_amocrm_chat_signature(
    *,
    method: str,
    content_md5: str,
    content_type: str,
    date_header: str,
    path: str,
    channel_secret: str,
) -> str:
    """HMAC-SHA1 over METHOD\\nMD5\\nContent-Type\\nDate\\npath."""

    payload = "\n".join(
        (
            method.upper(),
            content_md5,
            content_type,
            date_header,
            path,
        )
    )
    return hmac.new(
        channel_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest().lower()


def _rfc2822_now() -> str:
    return format_datetime(datetime.now(timezone.utc), usegmt=True)


def _classify_status(status_code: int) -> AmoCrmChatEgressOutcome:
    if 200 <= status_code < 300:
        return AmoCrmChatEgressOutcome.SUCCESS
    if status_code in {400, 401, 403}:
        return AmoCrmChatEgressOutcome.PERMANENT_ERROR
    if status_code == 204:
        return AmoCrmChatEgressOutcome.NOT_FOUND
    # 429 / 5xx / other → transient
    return AmoCrmChatEgressOutcome.TRANSIENT_ERROR


class _ChatHttpStdlibTransport:
    """Chat-only stdlib transport. Allows query strings required by history API."""

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        # Local import keeps optional dependency surface narrow for unit fakes.
        import http.client
        import ssl
        from urllib.parse import urlsplit

        if req.allow_redirects:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        parts = urlsplit(req.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        if parts.username is not None or parts.password is not None:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        host = parts.hostname
        port = parts.port
        path = parts.path if parts.path else "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        timeout = float(req.timeout_seconds)
        conn: http.client.HTTPConnection | None = None
        try:
            if parts.scheme == "https":
                conn = http.client.HTTPSConnection(
                    host,
                    port=port,
                    context=ssl.create_default_context(),
                    timeout=timeout,
                )
            else:
                conn = http.client.HTTPConnection(host, port=port, timeout=timeout)
            headers = {str(k): str(v) for k, v in req.headers.items()}
            conn.request(req.method, path, body=req.body, headers=headers)
            response = conn.getresponse()
            body = response.read(req.max_response_bytes + 1)
            if len(body) > req.max_response_bytes:
                raise S2sHttpTransportError("RESPONSE_TOO_LARGE") from None
            header_map = {k.lower(): v for k, v in response.getheaders()}
            return S2sHttpResponse(
                status_code=int(response.status),
                headers=header_map,
                body=body,
            )
        except S2sHttpTransportError:
            raise
        except TimeoutError:
            raise S2sHttpTransportError("TIMEOUT") from None
        except Exception:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


class AmoCrmChatEgressHttpClient:
    """Signed Chat API client. Fail-closed; no redirects; no CRM REST."""

    def __init__(
        self,
        config: AmoCrmChatEgressConfig,
        *,
        transport: AmoCrmChatTransport | None = None,
    ) -> None:
        config.require_runtime()
        self._config = config
        self._transport = (
            transport if transport is not None else _ChatHttpStdlibTransport()
        )
    def send_silent_text(
        self,
        *,
        integration_msgid: str,
        integration_conversation_id: str,
        conversation_ref_id: str,
        sender_id: str,
        sender_name: str,
        text: str,
        timestamp_unix: int,
        sender_ref_id: str | None = None,
    ) -> AmoCrmChatSendResult:
        assert self._config.scope_id is not None
        path = f"/v2/origin/custom/{self._config.scope_id}"
        sender: dict[str, Any] = {
            "id": sender_id,
            "name": sender_name,
        }
        if sender_ref_id is not None:
            sender["ref_id"] = sender_ref_id
        body_obj: dict[str, Any] = {
            "event_type": "new_message",
            "payload": {
                "timestamp": timestamp_unix,
                "msec_timestamp": timestamp_unix * 1000,
                "msgid": integration_msgid,
                "conversation_id": integration_conversation_id,
                "conversation_ref_id": conversation_ref_id,
                "sender": sender,
                "message": {
                    "type": "text",
                    "text": text,
                },
                "silent": True,
            },
        }
        raw = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        response = self._signed_request(method="POST", path=path, body=raw)
        if response is None:
            return AmoCrmChatSendResult(
                outcome=AmoCrmChatEgressOutcome.TRANSIENT_ERROR,
                error_code="AMOCRM_CHAT_TRANSPORT",
            )
        outcome = _classify_status(response.status_code)
        if outcome is not AmoCrmChatEgressOutcome.SUCCESS:
            return AmoCrmChatSendResult(
                outcome=outcome,
                error_code=f"AMOCRM_CHAT_HTTP_{response.status_code}",
            )
        amocrm_message_id = _parse_send_msgid(response.body)
        if amocrm_message_id is None:
            # Ambiguous success body — treat as transient so reconcile can run.
            return AmoCrmChatSendResult(
                outcome=AmoCrmChatEgressOutcome.TRANSIENT_ERROR,
                error_code="AMOCRM_CHAT_RESPONSE_INVALID",
            )
        return AmoCrmChatSendResult(
            outcome=AmoCrmChatEgressOutcome.SUCCESS,
            amocrm_message_id=amocrm_message_id,
        )

    def find_msgid_in_history(
        self,
        *,
        amocrm_chat_id: str,
        integration_msgid: str,
        limit: int = _HISTORY_PAGE_LIMIT,
    ) -> AmoCrmChatHistoryHit:
        """Compatibility wrapper around a complete history scan.

        ``amocrm_chat_id`` is the Chat API conversation id (binding /
        conversation_ref_id), not the integration-side conversation_id.
        """

        scan = self.scan_msgid_in_history(
            amocrm_chat_id=amocrm_chat_id,
            integration_msgid=integration_msgid,
            page_limit=limit,
        )
        if scan.scan is AmoCrmChatHistoryScan.FOUND:
            return AmoCrmChatHistoryHit(
                found=True,
                amocrm_message_id=scan.amocrm_message_id,
                outcome=AmoCrmChatEgressOutcome.SUCCESS,
            )
        if scan.scan is AmoCrmChatHistoryScan.ABSENT:
            return AmoCrmChatHistoryHit(
                found=False,
                outcome=AmoCrmChatEgressOutcome.NOT_FOUND,
                absence_proven=True,
            )
        return AmoCrmChatHistoryHit(
            found=False,
            outcome=AmoCrmChatEgressOutcome.TRANSIENT_ERROR,
            error_code=scan.error_code or "AMOCRM_CHAT_HISTORY_UNCERTAIN",
            absence_proven=False,
        )

    def scan_msgid_in_history(
        self,
        *,
        amocrm_chat_id: str,
        integration_msgid: str,
        page_limit: int = _HISTORY_PAGE_LIMIT,
    ) -> AmoCrmChatHistoryScanResult:
        """Paginated history scan. Prefer fail-closed when absence is unproven.

        Path uses ``amocrm_chat_id`` (Chat API id / conversation_ref_id).
        """

        assert self._config.scope_id is not None
        if page_limit < 1 or page_limit > _HISTORY_PAGE_LIMIT:
            raise AmoCrmChatEgressHttpError("AMOCRM_CHAT_HISTORY_LIMIT")

        offset = 0
        for _ in range(_HISTORY_MAX_PAGES):
            page = self._history_page(
                amocrm_chat_id=amocrm_chat_id,
                integration_msgid=integration_msgid,
                limit=page_limit,
                offset=offset,
            )
            if page.outcome is AmoCrmChatEgressOutcome.TRANSIENT_ERROR:
                return AmoCrmChatHistoryScanResult(
                    scan=AmoCrmChatHistoryScan.UNCERTAIN,
                    error_code=page.error_code or "AMOCRM_CHAT_HISTORY_TRANSIENT",
                )
            if page.outcome is AmoCrmChatEgressOutcome.PERMANENT_ERROR:
                return AmoCrmChatHistoryScanResult(
                    scan=AmoCrmChatHistoryScan.UNCERTAIN,
                    error_code=page.error_code or "AMOCRM_CHAT_HISTORY_PERMANENT",
                )
            if page.found and page.amocrm_message_id:
                return AmoCrmChatHistoryScanResult(
                    scan=AmoCrmChatHistoryScan.FOUND,
                    amocrm_message_id=page.amocrm_message_id,
                )
            if page.messages_on_page < page_limit:
                return AmoCrmChatHistoryScanResult(scan=AmoCrmChatHistoryScan.ABSENT)
            offset += page_limit

        return AmoCrmChatHistoryScanResult(
            scan=AmoCrmChatHistoryScan.UNCERTAIN,
            error_code="AMOCRM_CHAT_HISTORY_TRUNCATED",
        )

    def _history_page(
        self,
        *,
        amocrm_chat_id: str,
        integration_msgid: str,
        limit: int,
        offset: int,
    ) -> AmoCrmChatHistoryHit:
        assert self._config.scope_id is not None
        path = (
            f"/v2/origin/custom/{self._config.scope_id}"
            f"/chats/{amocrm_chat_id}/history"
            f"?limit={limit}&offset={offset}"
        )
        sign_path = (
            f"/v2/origin/custom/{self._config.scope_id}"
            f"/chats/{amocrm_chat_id}/history"
        )
        response = self._signed_request(
            method="GET",
            path=path,
            sign_path=sign_path,
            body=b"",
        )
        if response is None:
            return AmoCrmChatHistoryHit(
                found=False,
                outcome=AmoCrmChatEgressOutcome.TRANSIENT_ERROR,
                error_code="AMOCRM_CHAT_TRANSPORT",
            )
        if response.status_code == 204:
            return AmoCrmChatHistoryHit(
                found=False,
                outcome=AmoCrmChatEgressOutcome.NOT_FOUND,
                absence_proven=True,
                messages_on_page=0,
            )
        outcome = _classify_status(response.status_code)
        if outcome is not AmoCrmChatEgressOutcome.SUCCESS:
            return AmoCrmChatHistoryHit(
                found=False,
                outcome=outcome,
                error_code=f"AMOCRM_CHAT_HTTP_{response.status_code}",
            )
        parsed = parse_history_page_for_msgid(
            response.body,
            integration_msgid=integration_msgid,
        )
        if parsed is None:
            return AmoCrmChatHistoryHit(
                found=False,
                outcome=AmoCrmChatEgressOutcome.TRANSIENT_ERROR,
                error_code="AMOCRM_CHAT_HISTORY_PARSE",
            )
        return parsed

    def _signed_request(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        sign_path: str | None = None,
    ) -> S2sHttpResponse | None:
        assert self._config.channel_secret is not None
        date_header = _rfc2822_now()
        md5_hex = content_md5_hex(body)
        signature = build_amocrm_chat_signature(
            method=method,
            content_md5=md5_hex,
            content_type=_CONTENT_TYPE,
            date_header=date_header,
            path=sign_path if sign_path is not None else path.split("?", 1)[0],
            channel_secret=self._config.channel_secret,
        )
        url = f"{self._config.api_base_url}{path}"
        headers: Mapping[str, str] = {
            "Date": date_header,
            "Content-Type": _CONTENT_TYPE,
            "Content-MD5": md5_hex,
            "X-Signature": signature,
        }
        req = S2sHttpRequest(
            method=method,
            url=url,
            headers=headers,
            body=body,
            timeout_seconds=_TIMEOUT_SECONDS,
            allow_redirects=False,
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
        try:
            return self._transport.request(req)
        except S2sHttpTransportError:
            return None


def _parse_send_msgid(body: bytes) -> str | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(payload, dict):
        return None
    new_message = payload.get("new_message")
    if not isinstance(new_message, dict):
        return None
    msgid = new_message.get("msgid")
    if type(msgid) is not str or not msgid or len(msgid) > 128:
        return None
    return msgid


def find_msgid_in_history_body(
    body: bytes,
    *,
    integration_msgid: str,
) -> str | None:
    """Official shape: messages[].message.client_id / messages[].message.id."""

    page = parse_history_page_for_msgid(body, integration_msgid=integration_msgid)
    if page is None or not page.found:
        return None
    return page.amocrm_message_id


def parse_history_page_for_msgid(
    body: bytes,
    *,
    integration_msgid: str,
) -> AmoCrmChatHistoryHit | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(payload, dict):
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    for item in messages:
        if not isinstance(item, dict):
            continue
        message = item.get("message")
        if not isinstance(message, dict):
            continue
        client_id = message.get("client_id")
        amo_id = message.get("id")
        if client_id == integration_msgid and type(amo_id) is str and amo_id:
            return AmoCrmChatHistoryHit(
                found=True,
                amocrm_message_id=amo_id,
                outcome=AmoCrmChatEgressOutcome.SUCCESS,
                messages_on_page=len(messages),
            )
    return AmoCrmChatHistoryHit(
        found=False,
        outcome=AmoCrmChatEgressOutcome.NOT_FOUND,
        messages_on_page=len(messages),
    )
