"""Concrete stdlib S2S HTTP transport for bot-TV (CURSOR-15).

Uses only http.client + ssl.create_default_context. No redirects, no proxies,
no retries, no insecure TLS, no third-party HTTP clients.

Framing notes (CPython http.client):
- HTTPResponse.read() returns at most Content-Length bytes; trailing bytes after a
  truthful Content-Length are not part of the response body and are not measured
  here. Connections are always closed after one exchange.
- Ambiguous framing (duplicate CL/TE/CE/CT, Transfer-Encoding present, malformed
  CL) is rejected fail closed via get_all inspection before body parse.
"""

from __future__ import annotations

import http.client
import re
import socket
import ssl
from typing import Final
from urllib.parse import urlsplit

from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransportError,
)

# RFC 9110 token (method and header-field names).
_HTTP_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"^[!#$%&'*+\-.0-9A-Z^_`a-z|~]+$"
)
_FORBIDDEN_CALLER_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "proxy-authorization",
        "accept-encoding",
    }
)
_READ_CHUNK_SIZE: Final[int] = 8192
_MAX_TIMEOUT_SECONDS: Final[float] = 120.0
_MAX_RESPONSE_BYTES_CAP: Final[int] = 1_000_000


def _contains_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _validate_request_url(url: object) -> tuple[str, str, int | None, str]:
    """Return (scheme, host, port, path). Fail closed on any ambiguity."""

    if type(url) is not str or not url or any(ch.isspace() for ch in url):
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    if _contains_control_chars(url) or "\\" in url:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    if not parts.hostname:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    if parts.username is not None or parts.password is not None:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    if parts.query or parts.fragment:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    host = parts.hostname
    if any(ch.isspace() for ch in host) or _contains_control_chars(host):
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    try:
        port = parts.port
    except ValueError:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    if port is not None and (port <= 0 or port > 65535):
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    path = parts.path if parts.path else "/"
    # Origin-form only: reject scheme-relative //host/path forms.
    if (
        not path.startswith("/")
        or path.startswith("//")
        or _contains_control_chars(path)
        or any(ch.isspace() for ch in path)
    ):
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    return parts.scheme, host, port, path


def _validate_method(method: object) -> str:
    if type(method) is not str or not method:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    if _HTTP_TOKEN_RE.fullmatch(method) is None:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    return method


def _validate_headers(headers: object) -> dict[str, str]:
    if not isinstance(headers, dict):
        if not hasattr(headers, "items"):
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    try:
        items = list(headers.items())  # type: ignore[union-attr]
    except Exception:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    cleaned: dict[str, str] = {}
    seen_lower: set[str] = set()
    for key, value in items:
        if type(key) is not str or type(value) is not str:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        if _HTTP_TOKEN_RE.fullmatch(key) is None:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        lower = key.lower()
        if lower in _FORBIDDEN_CALLER_HEADERS:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        if lower in seen_lower:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        seen_lower.add(lower)
        cleaned[key] = value
    return cleaned


def _validate_timeout(raw: object) -> float:
    if type(raw) is not float and type(raw) is not int:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    value = float(raw)
    if value != value or value <= 0.0 or value > _MAX_TIMEOUT_SECONDS:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    return value


def _validate_max_response_bytes(raw: object) -> int:
    if type(raw) is not int or isinstance(raw, bool):
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    if raw <= 0 or raw > _MAX_RESPONSE_BYTES_CAP:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    return raw


def _header_values(message: http.client.HTTPMessage, name: str) -> list[str] | None:
    try:
        values = message.get_all(name)
    except Exception:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    if values is None:
        return None
    if type(values) is not list or not values:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    normalized: list[str] = []
    for item in values:
        if type(item) is not str:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        if _contains_control_chars(item):
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        normalized.append(item)
    return normalized


def _require_single_header(message: http.client.HTTPMessage, name: str) -> str | None:
    values = _header_values(message, name)
    if values is None:
        return None
    if len(values) != 1:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    return values[0]


def _parse_content_length(raw: str | None, *, max_bytes: int) -> int | None:
    if raw is None:
        return None
    if type(raw) is not str or not raw or any(ch.isspace() for ch in raw):
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    if not raw.isdigit():
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    length = int(raw)
    if length < 0:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    if length > max_bytes:
        raise S2sHttpTransportError("RESPONSE_TOO_LARGE") from None
    return length


def _inspect_response_framing(
    message: http.client.HTTPMessage,
    *,
    max_bytes: int,
) -> tuple[int | None, str | None, str | None]:
    """Return (declared_length, content_type, content_length_header)."""

    if _header_values(message, "Transfer-Encoding") is not None:
        # Reject chunked / ambiguous TE; S2S JSON uses identity + Content-Length.
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None

    encoding_values = _header_values(message, "Content-Encoding")
    if encoding_values is not None:
        if len(encoding_values) != 1:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        normalized = encoding_values[0].strip().lower()
        if normalized not in ("", "identity"):
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None

    content_type_values = _header_values(message, "Content-Type")
    if content_type_values is None:
        content_type = None
    else:
        if len(content_type_values) != 1:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        content_type = content_type_values[0]

    cl_values = _header_values(message, "Content-Length")
    if cl_values is None:
        return None, content_type, None
    if len(cl_values) != 1:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    declared = _parse_content_length(cl_values[0], max_bytes=max_bytes)
    return declared, content_type, cl_values[0]


def _read_body_bounded(
    response: http.client.HTTPResponse,
    *,
    max_bytes: int,
    declared_length: int | None,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining_allowed = max_bytes - total + 1
        if remaining_allowed <= 0:
            raise S2sHttpTransportError("RESPONSE_TOO_LARGE") from None
        to_read = min(_READ_CHUNK_SIZE, remaining_allowed)
        try:
            chunk = response.read(to_read)
        except (TimeoutError, socket.timeout):
            raise S2sHttpTransportError("TIMEOUT") from None
        except Exception:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        if not chunk:
            break
        if type(chunk) is not bytes:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        total += len(chunk)
        if total > max_bytes:
            raise S2sHttpTransportError("RESPONSE_TOO_LARGE") from None
        chunks.append(chunk)
    body = b"".join(chunks)
    # Under-read vs declared CL (early close) is a framing failure.
    # CPython HTTPResponse.read() does not return bytes beyond Content-Length;
    # trailing socket bytes are not treated as body and are discarded on close.
    if declared_length is not None and len(body) != declared_length:
        raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    return body


def _select_response_headers(
    *,
    content_type: str | None,
    content_length: str | None,
    content_encoding: str | None,
) -> dict[str, str]:
    selected: dict[str, str] = {}
    if content_type is not None:
        selected["Content-Type"] = content_type
    if content_length is not None:
        selected["Content-Length"] = content_length
    if content_encoding is not None:
        selected["Content-Encoding"] = content_encoding
    for key, value in selected.items():
        if _contains_control_chars(key) or _contains_control_chars(value):
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
    return selected


class S2sHttpStdlibTransport:
    """Production-safe stdlib implementation of S2sHttpTransport."""

    def __init__(self) -> None:
        # No insecure TLS knobs and no proxy configuration.
        pass

    def __repr__(self) -> str:
        return "S2sHttpStdlibTransport()"

    def __str__(self) -> str:
        return self.__repr__()

    def request(self, request: S2sHttpRequest) -> S2sHttpResponse:
        if type(request) is not S2sHttpRequest:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        if request.allow_redirects is not False:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
        if type(request.body) is not bytes:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None

        method = _validate_method(request.method)
        scheme, host, port, path = _validate_request_url(request.url)
        timeout = _validate_timeout(request.timeout_seconds)
        max_bytes = _validate_max_response_bytes(request.max_response_bytes)
        caller_headers = _validate_headers(request.headers)

        outbound: dict[str, str] = dict(caller_headers)
        # Transport-owned framing/safety headers (caller cannot set these).
        outbound["Accept-Encoding"] = "identity"
        outbound["Connection"] = "close"
        outbound["Content-Length"] = str(len(request.body))

        connection: http.client.HTTPConnection | None = None
        response: http.client.HTTPResponse | None = None
        try:
            connection = self._open_connection(scheme, host, port, timeout)
            try:
                connection.request(
                    method,
                    path,
                    body=request.body,
                    headers=outbound,
                )
            except (TimeoutError, socket.timeout):
                raise S2sHttpTransportError("TIMEOUT") from None
            except Exception:
                raise S2sHttpTransportError("TRANSPORT_ERROR") from None

            try:
                response = connection.getresponse()
            except (TimeoutError, socket.timeout):
                raise S2sHttpTransportError("TIMEOUT") from None
            except Exception:
                raise S2sHttpTransportError("TRANSPORT_ERROR") from None

            status = response.status
            if type(status) is not int or status < 100 or status > 599:
                raise S2sHttpTransportError("TRANSPORT_ERROR") from None

            declared, content_type, cl_header = _inspect_response_framing(
                response.headers,
                max_bytes=max_bytes,
            )
            encoding = _require_single_header(response.headers, "Content-Encoding")
            body = _read_body_bounded(
                response,
                max_bytes=max_bytes,
                declared_length=declared,
            )
            headers = _select_response_headers(
                content_type=content_type,
                content_length=cl_header,
                content_encoding=encoding,
            )
            return S2sHttpResponse(status_code=status, headers=headers, body=body)
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def _open_connection(
        self,
        scheme: str,
        host: str,
        port: int | None,
        timeout: float,
    ) -> http.client.HTTPConnection:
        try:
            if scheme == "https":
                context = ssl.create_default_context()
                if port is None:
                    return http.client.HTTPSConnection(
                        host,
                        timeout=timeout,
                        context=context,
                    )
                return http.client.HTTPSConnection(
                    host,
                    port=port,
                    timeout=timeout,
                    context=context,
                )
            if port is None:
                return http.client.HTTPConnection(host, timeout=timeout)
            return http.client.HTTPConnection(host, port=port, timeout=timeout)
        except (TimeoutError, socket.timeout):
            raise S2sHttpTransportError("TIMEOUT") from None
        except Exception:
            raise S2sHttpTransportError("TRANSPORT_ERROR") from None
