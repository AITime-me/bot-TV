"""Thin stdlib VK messages.send client (CURSOR-29). No SDK, no retries."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Final, Protocol

from app.channels.vk_master_config import VkMasterAdapterConfig

logger = logging.getLogger(__name__)

__all__ = (
    "VkMasterSender",
    "VkMasterHttpSender",
    "VkMasterSendError",
    "NullVkMasterSender",
)

_ALLOWED_LOG: Final[frozenset[str]] = frozenset(
    {
        "VK_MASTER_SEND_OK",
        "VK_MASTER_SEND_FAILED",
        "VK_MASTER_SEND_SKIPPED",
    }
)
_MAX_MESSAGE_CHARS: Final[int] = 3500


class VkMasterSendError(RuntimeError):
    def __init__(self, code: str = "VK_MASTER_SEND_FAILED") -> None:
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "VK_MASTER_SEND_FAILED"

    def __repr__(self) -> str:
        return f"VkMasterSendError({self.code!r})"


class VkMasterSender(Protocol):
    def send_text(self, *, peer_id: int, text: str) -> None: ...


@dataclass(frozen=True, slots=True, repr=False)
class NullVkMasterSender:
    def send_text(self, *, peer_id: int, text: str) -> None:
        return None


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Fail closed: never follow 3xx to another host/path."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise VkMasterSendError("VK_MASTER_SEND_FAILED") from None


class VkMasterHttpSender:
    """POST api.vk.com/method/messages.send via urllib. One attempt."""

    def __init__(self, config: VkMasterAdapterConfig) -> None:
        config.require_runtime_config()
        self._config = config
        self._opener = urllib.request.build_opener(_RejectRedirects())

    def __repr__(self) -> str:
        return "VkMasterHttpSender(config=<redacted>)"

    def send_text(self, *, peer_id: int, text: str) -> None:
        if type(peer_id) is not int or isinstance(peer_id, bool) or peer_id <= 0:
            raise VkMasterSendError("VK_MASTER_SEND_FAILED") from None
        if type(text) is not str or not text or len(text) > _MAX_MESSAGE_CHARS:
            raise VkMasterSendError("VK_MASTER_SEND_FAILED") from None
        assert self._config.access_token is not None
        params = urllib.parse.urlencode(
            {
                "access_token": self._config.access_token,
                "v": self._config.api_version,
                "peer_id": str(peer_id),
                "message": text,
                "random_id": "0",
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
            with self._opener.open(request, timeout=5.0) as response:
                raw = response.read(65536)
        except VkMasterSendError:
            _log("VK_MASTER_SEND_FAILED")
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            _log("VK_MASTER_SEND_FAILED")
            raise VkMasterSendError("VK_MASTER_SEND_FAILED") from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _log("VK_MASTER_SEND_FAILED")
            raise VkMasterSendError("VK_MASTER_SEND_FAILED") from None
        if type(payload) is not dict or "error" in payload:
            _log("VK_MASTER_SEND_FAILED")
            raise VkMasterSendError("VK_MASTER_SEND_FAILED") from None
        _log("VK_MASTER_SEND_OK")


def _log(event: str) -> None:
    if event not in _ALLOWED_LOG:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return
