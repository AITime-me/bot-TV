"""Regression tests for safe HTTP/1.1 chunked S2S response framing."""

from __future__ import annotations

import io
from http.client import HTTPResponse

import pytest

from app.core.s2s_http_stdlib import (
    _inspect_response_framing,
    _read_body_bounded,
    _require_single_chunked_transfer_encoding,
)
from app.core.s2s_http_transport import S2sHttpTransportError


class _Sock:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def makefile(self, *args: object, **kwargs: object):
        return io.BytesIO(self._data)


def _response(headers: bytes, wire_body: bytes) -> HTTPResponse:
    raw = b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n" + wire_body
    response = HTTPResponse(_Sock(raw))  # type: ignore[arg-type]
    response.begin()
    return response


def _chunked(body: bytes, *, chunk_size: int = 7) -> bytes:
    parts: list[bytes] = []
    for offset in range(0, len(body), chunk_size):
        chunk = body[offset : offset + chunk_size]
        parts.append(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
    parts.append(b"0\r\n\r\n")
    return b"".join(parts)


def test_valid_chunked_json_response_accepted() -> None:
    body = (
        b'{"ok":true,"outcome":"SELF_BOOKING_ALLOWED","reasonCode":null,'
        b'"selectedPairAllowed":true,"serviceOnlineInGeneral":true,'
        b'"otherOnlineMasterCount":0}'
    )
    response = _response(
        b"Content-Type: application/json; charset=utf-8\r\n"
        b"Transfer-Encoding: chunked\r\n",
        _chunked(body, chunk_size=11),
    )
    declared, content_type, content_length = _inspect_response_framing(
        response.headers,
        max_bytes=4096,
    )
    assert declared is None
    assert content_type == "application/json; charset=utf-8"
    assert content_length is None
    assert _read_body_bounded(
        response,
        max_bytes=4096,
        declared_length=declared,
    ) == body


@pytest.mark.parametrize("te_value", ["chunked", "Chunked", "CHUNKED"])
def test_chunked_transfer_encoding_case(te_value: str) -> None:
    body = b'{"ok":true}'
    response = _response(
        f"Content-Type: application/json\r\nTransfer-Encoding: {te_value}\r\n".encode(
            "ascii"
        ),
        _chunked(body),
    )
    declared, _, _ = _inspect_response_framing(response.headers, max_bytes=4096)
    assert _read_body_bounded(
        response,
        max_bytes=4096,
        declared_length=declared,
    ) == body


def test_chunked_transfer_encoding_surrounding_ows_accepted_by_validator() -> None:
    # Header parsers may normalize optional whitespace before exposing the value.
    # Test our framing validator's normalization directly instead of relying on
    # CPython HTTPResponse to classify deliberately irregular raw header syntax.
    _require_single_chunked_transfer_encoding(" chunked ")


def test_duplicate_transfer_encoding_rejected() -> None:
    response = _response(
        b"Content-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Transfer-Encoding: chunked\r\n",
        _chunked(b"{}"),
    )
    with pytest.raises(S2sHttpTransportError) as exc_info:
        _inspect_response_framing(response.headers, max_bytes=4096)
    assert exc_info.value.code == "TRANSPORT_ERROR"


def test_transfer_encoding_plus_content_length_rejected() -> None:
    response = _response(
        b"Content-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Content-Length: 2\r\n",
        _chunked(b"{}"),
    )
    with pytest.raises(S2sHttpTransportError) as exc_info:
        _inspect_response_framing(response.headers, max_bytes=4096)
    assert exc_info.value.code == "TRANSPORT_ERROR"


@pytest.mark.parametrize(
    "te_value",
    [
        "gzip",
        "identity",
        "deflate",
        "chunked, gzip",
        "gzip, chunked",
        "chunked;q=1",
        "chunked chunked",
        "chun ked",
    ],
)
def test_unsupported_transfer_encoding_rejected(te_value: str) -> None:
    response = _response(
        f"Content-Type: application/json\r\nTransfer-Encoding: {te_value}\r\n".encode(
            "ascii"
        ),
        b"",
    )
    with pytest.raises(S2sHttpTransportError) as exc_info:
        _inspect_response_framing(response.headers, max_bytes=4096)
    assert exc_info.value.code == "TRANSPORT_ERROR"


def test_chunked_response_respects_max_response_bytes() -> None:
    response = _response(
        b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n",
        _chunked(b"x" * 40),
    )
    declared, _, _ = _inspect_response_framing(response.headers, max_bytes=16)
    with pytest.raises(S2sHttpTransportError) as exc_info:
        _read_body_bounded(response, max_bytes=16, declared_length=declared)
    assert exc_info.value.code == "RESPONSE_TOO_LARGE"


def test_content_length_response_still_accepted() -> None:
    body = b'{"ok":true}'
    response = _response(
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n".encode(
            "ascii"
        ),
        body,
    )
    declared, content_type, content_length = _inspect_response_framing(
        response.headers,
        max_bytes=4096,
    )
    assert declared == len(body)
    assert content_type == "application/json"
    assert content_length == str(len(body))
    assert _read_body_bounded(
        response,
        max_bytes=4096,
        declared_length=declared,
    ) == body
