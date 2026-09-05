"""Thin stdlib VK CLIENT messages.send (closed outbound proof). No SDK, no retries.

Separate from VK master HTTP sender — client token/config never shared.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol

from app.channels.vk_client_outbound_config import VkClientOutboundConfig

logger = logging.getLogger(__name__)

__all__ = (
    "VkClientSender",
    "VkClientHttpSender",
    "NullVkClientSender",
    "VkClientSendOutcome",
    "VkClientSendResult",
    "vk_client_random_id_from_outbound_id",
)

_ALLOWED_LOG: Final[frozenset[str]] = frozenset(
    {
        "VK_CLIENT_SEND_OK",
        "VK_CLIENT_SEND_TRANSIENT",
        "VK_CLIENT_SEND_PERMANENT",
        "VK_CLIENT_SEND_SKIPPED",
    }
)
_MAX_MESSAGE_CHARS: Final[int] = 3500
_MAX_RESPONSE_BYTES: Final[int] = 65536
_TIMEOUT_SECONDS: Final[float] = 5.0

# Conservative permanent VK API error codes (auth / bad request).
_PERMANENT_VK_CODES: Final[frozenset[int]] = frozenset(
    {
        5,  # User authorization failed
        7,  # Permission denied
        15,  # Access denied
        100,  # One of the parameters specified was missing or invalid
        200,  # Access denied (wall/messages)
        203,  # Access to group denied
        900,  # Cannot send message (privacy / blocked)
        902,  # Can't send messages to users without permission
        917,  # You don't have access to this chat
        936,  # Contact not found
    }
)
_TRANSIENT_VK_CODES: Final[frozenset[int]] = frozenset(
    {
        1,  # Unknown error
        6,  # Too many requests per second
        9,  # Flood control
        10,  # Internal server error
        29,  # Rate limit
    }
)


class VkClientSendOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    PERMANENT_ERROR = "PERMANENT_ERROR"


@dataclass(frozen=True, slots=True)
class VkClientSendResult:
    outcome: VkClientSendOutcome
    error_code: str | None = None


def vk_client_random_id_from_outbound_id(outbound_id: uuid.UUID) -> int:
    """Deterministic VK-compatible positive int31 from outbound UUID (not hash())."""

    if type(outbound_id) is not uuid.UUID:
        raise TypeError("outbound_id must be UUID")
    digest = hashlib.sha256(outbound_id.bytes).digest()
    value = int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
    return value if value != 0 else 1


class VkClientSender(Protocol):
    def send_text(
        self,
        *,
        peer_id: int,
        text: str,
        outbound_id: uuid.UUID,
    ) -> VkClientSendResult: ...


@dataclass(frozen=True, slots=True, repr=False)
class NullVkClientSender:
    def send_text(
        self,
        *,
        peer_id: int,
        text: str,
        outbound_id: uuid.UUID,
    ) -> VkClientSendResult:
        _log("VK_CLIENT_SEND_SKIPPED")
        return VkClientSendResult(
            outcome=VkClientSendOutcome.PERMANENT_ERROR,
            error_code="VK_CLIENT_SEND_DISABLED",
        )


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise _TransientSignal() from None


class _TransientSignal(Exception):
    pass


class _PermanentSignal(Exception):
    pass


class VkClientHttpSender:
    """POST api.vk.com/method/messages.send via urllib. One attempt."""

    def __init__(self, config: VkClientOutboundConfig) -> None:
        config.require_send_config()
        self._config = config
        self._opener = urllib.request.build_opener(_RejectRedirects())

    def __repr__(self) -> str:
        return "VkClientHttpSender(config=<redacted>)"

    def send_text(
        self,
        *,
        peer_id: int,
        text: str,
        outbound_id: uuid.UUID,
    ) -> VkClientSendResult:
        if type(peer_id) is not int or isinstance(peer_id, bool) or peer_id <= 0:
            return _permanent("VK_CLIENT_PEER_INVALID")
        if type(text) is not str or not text or len(text) > _MAX_MESSAGE_CHARS:
            return _permanent("VK_CLIENT_TEXT_INVALID")
        if type(outbound_id) is not uuid.UUID:
            return _permanent("VK_CLIENT_OUTBOUND_ID_INVALID")
        assert self._config.access_token is not None
        random_id = vk_client_random_id_from_outbound_id(outbound_id)
        params = urllib.parse.urlencode(
            {
                "access_token": self._config.access_token,
                "v": self._config.api_version,
                "peer_id": str(peer_id),
                "message": text,
                "random_id": str(random_id),
            }
        )
        url = f"{self._config.api_base_url}/method/messages.send"
        request = urllib.request.Request(
            url,
            data=params.encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with self._opener.open(request, timeout=_TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", None)
                if type(status) is int and status >= 500:
                    raise _TransientSignal()
                if type(status) is int and status == 429:
                    raise _TransientSignal()
                if type(status) is int and status >= 400:
                    raise _PermanentSignal()
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except _TransientSignal:
            return _transient("VK_CLIENT_SEND_TRANSIENT")
        except _PermanentSignal:
            return _permanent("VK_CLIENT_SEND_PERMANENT")
        except urllib.error.HTTPError as exc:
            if exc.code in {429, 500, 502, 503, 504}:
                return _transient("VK_CLIENT_HTTP_TRANSIENT")
            return _permanent("VK_CLIENT_HTTP_PERMANENT")
        except (urllib.error.URLError, TimeoutError, OSError):
            return _transient("VK_CLIENT_NETWORK")
        if len(raw) > _MAX_RESPONSE_BYTES:
            return _permanent("VK_CLIENT_RESPONSE_TOO_LARGE")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return _permanent("VK_CLIENT_RESPONSE_INVALID")
        if type(payload) is not dict:
            return _permanent("VK_CLIENT_RESPONSE_INVALID")
        if "error" in payload:
            return _classify_vk_error(payload.get("error"))
        if "response" not in payload:
            return _permanent("VK_CLIENT_RESPONSE_INVALID")
        _log("VK_CLIENT_SEND_OK")
        return VkClientSendResult(outcome=VkClientSendOutcome.SUCCESS)


def _classify_vk_error(error: object) -> VkClientSendResult:
    code: int | None = None
    if type(error) is dict:
        raw_code = error.get("error_code")
        if type(raw_code) is int and not isinstance(raw_code, bool):
            code = raw_code
    if code in _TRANSIENT_VK_CODES:
        return _transient("VK_CLIENT_API_TRANSIENT")
    if code in _PERMANENT_VK_CODES:
        return _permanent("VK_CLIENT_API_PERMANENT")
    # Unknown API errors: fail closed as permanent (no retry storms).
    return _permanent("VK_CLIENT_API_PERMANENT")


def _transient(code: str) -> VkClientSendResult:
    _log("VK_CLIENT_SEND_TRANSIENT")
    return VkClientSendResult(
        outcome=VkClientSendOutcome.TRANSIENT_ERROR,
        error_code=code,
    )


def _permanent(code: str) -> VkClientSendResult:
    _log("VK_CLIENT_SEND_PERMANENT")
    return VkClientSendResult(
        outcome=VkClientSendOutcome.PERMANENT_ERROR,
        error_code=code,
    )


def _log(event: str) -> None:
    if event not in _ALLOWED_LOG:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return
