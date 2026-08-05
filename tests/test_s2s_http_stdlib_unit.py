"""Unit tests for stdlib S2S HTTP transport.

Uses fake connection/response objects only. No real sockets or network I/O.
"""

from __future__ import annotations

import http.client
import socket
import ssl
from typing import Any

import pytest

from app.core.s2s_http_stdlib import S2sHttpStdlibTransport
from app.core.s2s_http_transport import (
    S2sHttpRequest,
    S2sHttpResponse,
    S2sHttpTransportError,
)

_TOKEN = "t" * 32
_URL = "https://eligibility.example/api/internal/bot/v1/eligibility"
_PATH = "/api/internal/bot/v1/eligibility"


class FakeHeaders:
    """Minimal header map supporting get/get_all/items like HTTPMessage."""

    def __init__(self, headers: dict[str, str] | list[tuple[str, str]] | None = None) -> None:
        if headers is None:
            self._items: list[tuple[str, str]] = []
        elif isinstance(headers, dict):
            self._items = list(headers.items())
        else:
            self._items = list(headers)

    def get(self, name: str, default: Any = None) -> Any:  # noqa: ANN401
        values = self.get_all(name)
        if not values:
            return default
        return values[0]

    def get_all(self, name: str, failobj: Any = None) -> Any:  # noqa: ANN401
        target = name.lower()
        values = [value for key, value in self._items if str(key).lower() == target]
        if not values:
            return failobj
        return values

    def items(self):
        return list(self._items)


class FakeHTTPResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | list[tuple[str, str]] | None = None,
        body: bytes = b"",
        chunk_size: int = 8,
        read_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self.headers = FakeHeaders(headers)
        self._body = body
        self._offset = 0
        self._chunk_size = chunk_size
        self._read_error = read_error
        self.closed = False

    def read(self, amt: int = -1) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        if self._offset >= len(self._body):
            return b""
        if amt is None or amt < 0:
            chunk = self._body[self._offset :]
            self._offset = len(self._body)
            return chunk
        size = min(amt, self._chunk_size, len(self._body) - self._offset)
        chunk = self._body[self._offset : self._offset + size]
        self._offset += size
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeHTTPConnection:
    instances: list[FakeHTTPConnection] = []

    def __init__(self, host: str, port: int | None = None, timeout: float | None = None, **kwargs: Any) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.kwargs = kwargs
        self.closed = False
        self.request_calls: list[tuple[Any, ...]] = []
        self._response: FakeHTTPResponse | None = None
        self._request_error: BaseException | None = None
        self._getresponse_error: BaseException | None = None
        FakeHTTPConnection.instances.append(self)

    def configure(
        self,
        *,
        response: FakeHTTPResponse | None = None,
        request_error: BaseException | None = None,
        getresponse_error: BaseException | None = None,
    ) -> None:
        self._response = response
        self._request_error = request_error
        self._getresponse_error = getresponse_error

    def request(self, method: str, url: str, body: Any = None, headers: dict[str, str] | None = None) -> None:
        self.request_calls.append((method, url, body, dict(headers or {})))
        if self._request_error is not None:
            raise self._request_error

    def getresponse(self) -> FakeHTTPResponse:
        if self._getresponse_error is not None:
            raise self._getresponse_error
        assert self._response is not None
        return self._response

    def close(self) -> None:
        self.closed = True


class FakeHTTPSConnection(FakeHTTPConnection):
    pass


def _request(**overrides: Any) -> S2sHttpRequest:
    values: dict[str, Any] = {
        "method": "POST",
        "url": _URL,
        "headers": {
            "Authorization": f"Bearer {_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        "body": b'{"ok":true}',
        "timeout_seconds": 3.0,
        "allow_redirects": False,
        "max_response_bytes": 4096,
    }
    values.update(overrides)
    return S2sHttpRequest(**values)


@pytest.fixture(autouse=True)
def _reset_fake_connections(monkeypatch: pytest.MonkeyPatch):
    FakeHTTPConnection.instances.clear()

    def http_factory(host: str, port: int | None = None, timeout: float | None = None, **kwargs: Any) -> FakeHTTPConnection:
        return FakeHTTPConnection(host, port=port, timeout=timeout, **kwargs)

    def https_factory(
        host: str,
        port: int | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> FakeHTTPSConnection:
        return FakeHTTPSConnection(host, port=port, timeout=timeout, **kwargs)

    monkeypatch.setattr(http.client, "HTTPConnection", http_factory)
    monkeypatch.setattr(http.client, "HTTPSConnection", https_factory)
    yield
    FakeHTTPConnection.instances.clear()


def _wire_success(
    *,
    status: int = 200,
    body: bytes = b'{"ok":true}',
    headers: dict[str, str] | None = None,
    chunk_size: int = 8,
) -> FakeHTTPResponse:
    hdrs = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    if headers:
        hdrs.update(headers)
    response = FakeHTTPResponse(status=status, headers=hdrs, body=body, chunk_size=chunk_size)
    # Pre-create connection via a probe open so configure works after first request.
    return response


def _transport_with_response(response: FakeHTTPResponse, *, https: bool = True) -> S2sHttpStdlibTransport:
    transport = S2sHttpStdlibTransport()
    original_open = transport._open_connection

    def open_and_configure(scheme: str, host: str, port: int | None, timeout: float):
        conn = original_open(scheme, host, port, timeout)
        assert isinstance(conn, FakeHTTPConnection)
        conn.configure(response=response)
        return conn

    transport._open_connection = open_and_configure  # type: ignore[method-assign]
    return transport


# ---------------------------------------------------------------------------
# Connection selection / lifecycle
# ---------------------------------------------------------------------------


def test_https_uses_ssl_default_context(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[ssl.SSLContext] = []
    real = ssl.create_default_context

    def tracking_context(*args: Any, **kwargs: Any) -> ssl.SSLContext:
        ctx = real(*args, **kwargs)
        calls.append(ctx)
        return ctx

    monkeypatch.setattr(ssl, "create_default_context", tracking_context)
    body = b'{"ok":true}'
    response = FakeHTTPResponse(
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        body=body,
    )
    transport = _transport_with_response(response)
    result = transport.request(_request())
    assert isinstance(result, S2sHttpResponse)
    assert len(calls) == 1
    assert calls[0].verify_mode == ssl.CERT_REQUIRED
    conn = FakeHTTPConnection.instances[-1]
    assert isinstance(conn, FakeHTTPSConnection)
    assert "context" in conn.kwargs
    assert conn.closed is True
    assert response.closed is True


def test_http_uses_http_connection() -> None:
    body = b'{"ok":true}'
    response = FakeHTTPResponse(
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        body=body,
    )
    transport = _transport_with_response(response)
    result = transport.request(_request(url="http://eligibility.example" + _PATH))
    assert result.status_code == 200
    conn = FakeHTTPConnection.instances[-1]
    assert type(conn) is FakeHTTPConnection
    assert not isinstance(conn, FakeHTTPSConnection)
    assert conn.closed is True


def test_single_request_no_retry_on_error() -> None:
    transport = S2sHttpStdlibTransport()
    original_open = transport._open_connection

    def open_fail(scheme: str, host: str, port: int | None, timeout: float):
        conn = original_open(scheme, host, port, timeout)
        assert isinstance(conn, FakeHTTPConnection)
        conn.configure(request_error=TimeoutError())
        return conn

    transport._open_connection = open_fail  # type: ignore[method-assign]
    with pytest.raises(S2sHttpTransportError) as exc_info:
        transport.request(_request())
    assert exc_info.value.code == "TIMEOUT"
    assert len(FakeHTTPConnection.instances) == 1
    assert len(FakeHTTPConnection.instances[0].request_calls) == 1
    assert FakeHTTPConnection.instances[0].closed is True


@pytest.mark.parametrize(
    "error",
    [TimeoutError(), socket.timeout()],
)
def test_timeout_closes_connection(error: BaseException) -> None:
    transport = S2sHttpStdlibTransport()
    original_open = transport._open_connection

    def open_fail(scheme: str, host: str, port: int | None, timeout: float):
        conn = original_open(scheme, host, port, timeout)
        assert isinstance(conn, FakeHTTPConnection)
        conn.configure(getresponse_error=error)
        return conn

    transport._open_connection = open_fail  # type: ignore[method-assign]
    with pytest.raises(S2sHttpTransportError) as exc_info:
        transport.request(_request())
    assert exc_info.value.code == "TIMEOUT"
    assert FakeHTTPConnection.instances[0].closed is True
    assert _TOKEN not in str(exc_info.value)
    assert _URL not in str(exc_info.value)


def test_read_error_closes_connection() -> None:
    response = FakeHTTPResponse(
        headers={"Content-Type": "application/json", "Content-Length": "10"},
        body=b"abcdefghij",
        read_error=OSError("boom"),
    )
    transport = _transport_with_response(response)
    with pytest.raises(S2sHttpTransportError) as exc_info:
        transport.request(_request())
    assert exc_info.value.code == "TRANSPORT_ERROR"
    assert FakeHTTPConnection.instances[0].closed is True
    assert response.closed is True


def test_oversized_body_closes_connection() -> None:
    body = b"x" * 50
    response = FakeHTTPResponse(
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        body=body,
        chunk_size=8,
    )
    transport = _transport_with_response(response)
    with pytest.raises(S2sHttpTransportError) as exc_info:
        transport.request(_request(max_response_bytes=16))
    # Content-Length already exceeds limit before read.
    assert exc_info.value.code == "RESPONSE_TOO_LARGE"
    assert FakeHTTPConnection.instances[0].closed is True
    assert response.closed is True


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def test_request_uses_path_not_absolute_url_and_sets_headers() -> None:
    body = b'{"serviceId":"11111111-1111-1111-1111-111111111111"}'
    response = FakeHTTPResponse(
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        body=body,
    )
    transport = _transport_with_response(response)
    result = transport.request(_request(body=body))
    assert result.status_code == 200
    method, path, sent_body, headers = FakeHTTPConnection.instances[0].request_calls[0]
    assert method == "POST"
    assert path == _PATH
    assert not path.startswith("http")
    assert sent_body == body
    assert headers["Authorization"] == f"Bearer {_TOKEN}"
    assert headers["Content-Length"] == str(len(body))
    assert headers["Accept-Encoding"] == "identity"
    assert headers["Connection"] == "close"
    assert _TOKEN not in repr(result)


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example"},
        {"Content-Length": "1"},
        {"Transfer-Encoding": "chunked"},
        {"Connection": "keep-alive"},
        {"Proxy-Authorization": "Basic xxx"},
        {"Accept-Encoding": "gzip"},
        {"X-Test": "a\r\nb"},
        {"X-Test": "a\x00b"},
        {"Bad\nName": "value"},
        {"X(Test)": "value"},
        {"X:Test": "value"},
    ],
)
def test_forbidden_or_unsafe_headers_rejected_before_network(headers: dict[str, str]) -> None:
    transport = S2sHttpStdlibTransport()
    merged = {
        "Authorization": f"Bearer {_TOKEN}",
        "Content-Type": "application/json",
        **headers,
    }
    with pytest.raises(S2sHttpTransportError) as exc_info:
        transport.request(_request(headers=merged))
    assert exc_info.value.code == "TRANSPORT_ERROR"
    assert FakeHTTPConnection.instances == []


@pytest.mark.parametrize("method", ["POST:", "POST/1", "POST\t", "PÖST", "POST,"])
def test_invalid_methods_rejected_before_network(method: str) -> None:
    transport = S2sHttpStdlibTransport()
    with pytest.raises(S2sHttpTransportError):
        transport.request(_request(method=method))
    assert FakeHTTPConnection.instances == []


@pytest.mark.parametrize(
    "url",
    [
        "https://eligibility.example//evil.example/api",
        "https://eligibility.example/api?x=1",
        "https://user:pass@eligibility.example/api",
    ],
)
def test_unsafe_urls_rejected_before_network(url: str) -> None:
    transport = S2sHttpStdlibTransport()
    with pytest.raises(S2sHttpTransportError):
        transport.request(_request(url=url))
    assert FakeHTTPConnection.instances == []


def test_allow_redirects_true_rejected_before_network() -> None:
    transport = S2sHttpStdlibTransport()
    with pytest.raises(S2sHttpTransportError):
        transport.request(_request(allow_redirects=True))
    assert FakeHTTPConnection.instances == []


def test_invalid_timeout_rejected_before_network() -> None:
    transport = S2sHttpStdlibTransport()
    with pytest.raises(S2sHttpTransportError):
        transport.request(_request(timeout_seconds=0))
    assert FakeHTTPConnection.instances == []


# ---------------------------------------------------------------------------
# Response bounds / encoding / redirects
# ---------------------------------------------------------------------------


def test_missing_content_length_still_bounds_body() -> None:
    body = b"x" * 40
    response = FakeHTTPResponse(
        headers={"Content-Type": "application/json"},
        body=body,
        chunk_size=7,
    )
    transport = _transport_with_response(response)
    with pytest.raises(S2sHttpTransportError) as exc_info:
        transport.request(_request(max_response_bytes=16))
    assert exc_info.value.code == "RESPONSE_TOO_LARGE"


def test_content_length_equal_limit_ok() -> None:
    body = b"a" * 16
    response = FakeHTTPResponse(
        headers={"Content-Type": "application/json", "Content-Length": "16"},
        body=body,
        chunk_size=5,
    )
    result = _transport_with_response(response).request(_request(max_response_bytes=16))
    assert result.body == body


def test_content_length_one_over_limit_rejected() -> None:
    response = FakeHTTPResponse(
        headers={"Content-Type": "application/json", "Content-Length": "17"},
        body=b"a" * 17,
    )
    with pytest.raises(S2sHttpTransportError) as exc_info:
        _transport_with_response(response).request(_request(max_response_bytes=16))
    assert exc_info.value.code == "RESPONSE_TOO_LARGE"


@pytest.mark.parametrize("raw_cl", ["nope", "-1", "1.5", ""])
def test_malformed_content_length_rejected(raw_cl: str) -> None:
    response = FakeHTTPResponse(
        headers={"Content-Type": "application/json", "Content-Length": raw_cl},
        body=b"abc",
    )
    with pytest.raises(S2sHttpTransportError) as exc_info:
        _transport_with_response(response).request(_request())
    assert exc_info.value.code == "TRANSPORT_ERROR"


def test_short_body_vs_declared_content_length_fails() -> None:
    """Early close / under-read vs Content-Length is a framing failure."""

    response = FakeHTTPResponse(
        headers={"Content-Type": "application/json", "Content-Length": "10"},
        body=b"short",
        chunk_size=8,
    )
    with pytest.raises(S2sHttpTransportError) as exc_info:
        _transport_with_response(response).request(_request(max_response_bytes=64))
    assert exc_info.value.code == "TRANSPORT_ERROR"


def test_duplicate_content_length_rejected() -> None:
    response = FakeHTTPResponse(
        headers=[
            ("Content-Type", "application/json"),
            ("Content-Length", "2"),
            ("Content-Length", "3"),
        ],
        body=b"{}",
    )
    with pytest.raises(S2sHttpTransportError) as exc_info:
        _transport_with_response(response).request(_request())
    assert exc_info.value.code == "TRANSPORT_ERROR"


def test_transfer_encoding_rejected() -> None:
    response = FakeHTTPResponse(
        headers={
            "Content-Type": "application/json",
            "Transfer-Encoding": "chunked",
            "Content-Length": "2",
        },
        body=b"{}",
    )
    with pytest.raises(S2sHttpTransportError) as exc_info:
        _transport_with_response(response).request(_request())
    assert exc_info.value.code == "TRANSPORT_ERROR"


def test_duplicate_content_encoding_rejected() -> None:
    response = FakeHTTPResponse(
        headers=[
            ("Content-Type", "application/json"),
            ("Content-Length", "2"),
            ("Content-Encoding", "identity"),
            ("Content-Encoding", "gzip"),
        ],
        body=b"{}",
    )
    with pytest.raises(S2sHttpTransportError) as exc_info:
        _transport_with_response(response).request(_request())
    assert exc_info.value.code == "TRANSPORT_ERROR"


def test_real_http_response_truncates_to_content_length() -> None:
    """Document CPython behavior: trailing bytes after CL are not body."""

    import io
    from http.client import HTTPResponse

    class _Sock:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def makefile(self, *args: object, **kwargs: object):
            return io.BytesIO(self._data)

    raw = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nHELLOWORLD"
    resp = HTTPResponse(_Sock(raw))  # type: ignore[arg-type]
    resp.begin()
    assert resp.read() == b"HELLO"
    # Transport closes the connection after one exchange; trailing bytes are not parsed as JSON.
    resp.close()


def test_unicode_body_limit_is_bytes() -> None:
    body = ("ы" * 20).encode("utf-8")
    assert len(body) > 16
    response = FakeHTTPResponse(
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        body=body,
        chunk_size=4,
    )
    with pytest.raises(S2sHttpTransportError) as exc_info:
        _transport_with_response(response).request(_request(max_response_bytes=16))
    assert exc_info.value.code == "RESPONSE_TOO_LARGE"


def test_empty_body_ok() -> None:
    response = FakeHTTPResponse(
        status=204,
        headers={"Content-Length": "0"},
        body=b"",
    )
    result = _transport_with_response(response).request(_request())
    assert result.status_code == 204
    assert result.body == b""


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "gzip, identity"])
def test_compressed_content_encoding_rejected(encoding: str) -> None:
    response = FakeHTTPResponse(
        headers={
            "Content-Type": "application/json",
            "Content-Length": "2",
            "Content-Encoding": encoding,
        },
        body=b"{}",
    )
    with pytest.raises(S2sHttpTransportError) as exc_info:
        _transport_with_response(response).request(_request())
    assert exc_info.value.code == "TRANSPORT_ERROR"


def test_identity_content_encoding_accepted() -> None:
    response = FakeHTTPResponse(
        headers={
            "Content-Type": "application/json",
            "Content-Length": "2",
            "Content-Encoding": "identity",
        },
        body=b"{}",
    )
    result = _transport_with_response(response).request(_request())
    assert result.body == b"{}"


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_not_followed(status: int) -> None:
    response = FakeHTTPResponse(
        status=status,
        headers={
            "Content-Length": "0",
            "Location": "https://evil.example/steal",
        },
        body=b"",
    )
    transport = _transport_with_response(response)
    result = transport.request(_request())
    assert result.status_code == status
    assert len(FakeHTTPConnection.instances) == 1
    assert len(FakeHTTPConnection.instances[0].request_calls) == 1
    assert "Location" not in result.headers


@pytest.mark.parametrize("status", [400, 401, 413, 429, 500])
def test_error_statuses_returned_without_retry(status: int) -> None:
    body = b'{"error":"x"}'
    response = FakeHTTPResponse(
        status=status,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        body=body,
    )
    result = _transport_with_response(response).request(_request())
    assert result.status_code == status
    assert len(FakeHTTPConnection.instances[0].request_calls) == 1


def test_exception_with_secrets_does_not_leak() -> None:
    secret = "SECRETTOKEN" + ("Z" * 32)
    transport = S2sHttpStdlibTransport()
    original_open = transport._open_connection

    def open_fail(scheme: str, host: str, port: int | None, timeout: float):
        conn = original_open(scheme, host, port, timeout)
        assert isinstance(conn, FakeHTTPConnection)
        conn.configure(
            request_error=RuntimeError(
                f"Authorization Bearer {secret} url={_URL} body={{\"a\":1}}"
            )
        )
        return conn

    transport._open_connection = open_fail  # type: ignore[method-assign]
    with pytest.raises(S2sHttpTransportError) as exc_info:
        transport.request(_request())
    assert exc_info.value.code == "TRANSPORT_ERROR"
    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)
    assert _URL not in str(exc_info.value)
    assert "Authorization" not in str(exc_info.value)
    assert "S2sHttpStdlibTransport()" == repr(S2sHttpStdlibTransport())
