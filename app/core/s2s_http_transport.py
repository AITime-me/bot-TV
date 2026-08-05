"""Minimal S2S HTTP transport Protocol for bot-TV adapters.

No concrete live client lives here. Adapters inject a transport; unit tests use
fakes. Redirects must be disabled by callers for credentialed S2S calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping, Protocol

_ALLOWED_TRANSPORT_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "TRANSPORT_ERROR",
        "TIMEOUT",
        "RESPONSE_TOO_LARGE",
    }
)


class S2sHttpTransportError(RuntimeError):
    """Transport-layer failure. Message is a fixed code only."""

    def __init__(self, code: object) -> None:
        if type(code) is not str or code not in _ALLOWED_TRANSPORT_ERROR_CODES:
            super().__init__("TRANSPORT_ERROR")
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "TRANSPORT_ERROR"


@dataclass(frozen=True, slots=True, repr=False)
class S2sHttpRequest:
    """Outbound S2S request. Body and Authorization never appear in repr."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    timeout_seconds: float
    allow_redirects: bool

    def __post_init__(self) -> None:
        if type(self.method) is not str or not self.method:
            raise ValueError("method invalid") from None
        if type(self.url) is not str or not self.url:
            raise ValueError("url invalid") from None
        if not isinstance(self.headers, Mapping):
            raise ValueError("headers invalid") from None
        if type(self.body) is not bytes:
            raise ValueError("body invalid") from None
        if type(self.timeout_seconds) is not float and type(self.timeout_seconds) is not int:
            raise ValueError("timeout invalid") from None
        if type(self.allow_redirects) is not bool:
            raise ValueError("allow_redirects invalid") from None
        object.__setattr__(self, "headers", dict(self.headers))

    def __repr__(self) -> str:
        header_names = sorted(str(name).lower() for name in self.headers)
        return (
            f"S2sHttpRequest(method={self.method!r}, "
            f"header_names={header_names!r}, "
            f"body_len={len(self.body)}, "
            f"timeout_seconds={float(self.timeout_seconds)!r}, "
            f"allow_redirects={self.allow_redirects!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class S2sHttpResponse:
    """Inbound S2S response. Body never appears in repr."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes

    def __post_init__(self) -> None:
        if type(self.status_code) is not int:
            raise ValueError("status_code invalid") from None
        if not isinstance(self.headers, Mapping):
            raise ValueError("headers invalid") from None
        if type(self.body) is not bytes:
            raise ValueError("body invalid") from None
        object.__setattr__(self, "headers", dict(self.headers))

    def __repr__(self) -> str:
        header_names = sorted(str(name).lower() for name in self.headers)
        return (
            f"S2sHttpResponse(status_code={self.status_code!r}, "
            f"header_names={header_names!r}, body_len={len(self.body)})"
        )


class S2sHttpTransport(Protocol):
    """Single-shot HTTP transport. Implementations must honour allow_redirects.

    Credentialed S2S callers set allow_redirects=False and must not follow
    redirects that would retarget Authorization to another origin.

    The response body is fully buffered bytes. Live implementations must enforce
    max response size while reading and raise RESPONSE_TOO_LARGE before retaining
    an oversized payload; this Protocol cannot provide streaming by itself.
    """

    def request(self, request: S2sHttpRequest) -> S2sHttpResponse:
        """Perform exactly one HTTP exchange or raise S2sHttpTransportError."""
...
