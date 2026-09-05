"""VK CLIENT outbound send + closed-proof configuration. Default-off, fail-closed.

Separate from VK_MASTER_* and from VK_CLIENT callback-only config.
Secrets never appear in repr / logs / errors.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from app.channels.vk_client_config import VkClientCallbackConfig
from app.config import BotMode, Settings

__all__ = (
    "VkClientOutboundConfig",
    "VkClientOutboundConfigError",
    "VkClientPeerResolutionError",
    "parse_vk_client_peer_id",
    "vk_client_outbound_send_allowed",
    "vk_client_outbound_proof_allowed",
)

_TOKEN_MIN: Final[int] = 16
_TOKEN_MAX: Final[int] = 512
_DEFAULT_API_VERSION: Final[str] = "5.199"
_DEFAULT_API_BASE: Final[str] = "https://api.vk.com"
_CONV_RE: Final[re.Pattern[str]] = re.compile(r"^vk-(\d+)-(\d+)$")
_PROOF_TRIGGER_MAX: Final[int] = 500
_PROOF_REPLY_MAX: Final[int] = 3500


class VkClientOutboundConfigError(ValueError):
    def __init__(self, code: str = "VK_CLIENT_OUTBOUND_CONFIG_INVALID") -> None:
        super().__init__(code)

    def __repr__(self) -> str:
        return f"VkClientOutboundConfigError({self.args[0]!r})"


class VkClientPeerResolutionError(ValueError):
    def __init__(self, code: str = "VK_CLIENT_PEER_INVALID") -> None:
        super().__init__(code)

    def __repr__(self) -> str:
        return f"VkClientPeerResolutionError({self.args[0]!r})"


def _optional_str(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return value


def _require_printable_secret(value: str, *, min_len: int, max_len: int) -> str:
    if type(value) is not str or not value:
        raise VkClientOutboundConfigError() from None
    if len(value) < min_len or len(value) > max_len:
        raise VkClientOutboundConfigError() from None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise VkClientOutboundConfigError() from None
    if any(ch.isspace() for ch in value):
        raise VkClientOutboundConfigError() from None
    return value


def parse_vk_client_peer_id(
    *,
    external_conversation_id: str,
    expected_group_id: int,
) -> int:
    """Derive private-dialog peer_id from ``vk-{group}-{user}``."""

    if type(external_conversation_id) is not str or not external_conversation_id:
        raise VkClientPeerResolutionError() from None
    if type(expected_group_id) is not int or isinstance(expected_group_id, bool):
        raise VkClientPeerResolutionError() from None
    if expected_group_id <= 0:
        raise VkClientPeerResolutionError() from None
    match = _CONV_RE.fullmatch(external_conversation_id)
    if match is None:
        raise VkClientPeerResolutionError() from None
    group_id = int(match.group(1))
    user_id = int(match.group(2))
    if group_id != expected_group_id:
        raise VkClientPeerResolutionError("VK_CLIENT_PEER_GROUP_MISMATCH") from None
    if user_id <= 0:
        raise VkClientPeerResolutionError() from None
    return user_id


@dataclass(frozen=True, slots=True, repr=False)
class VkClientOutboundConfig:
    """Gated VK client send settings for closed single-conversation proof."""

    outbound_enabled: bool = False
    access_token: str | None = None
    allow_conversation: str | None = None
    proof_enabled: bool = False
    proof_trigger: str | None = None
    proof_reply: str | None = None
    group_id: int | None = None
    api_version: str = _DEFAULT_API_VERSION
    api_base_url: str = _DEFAULT_API_BASE

    def __repr__(self) -> str:
        return (
            "VkClientOutboundConfig("
            f"outbound_enabled={self.outbound_enabled!r}, "
            f"proof_enabled={self.proof_enabled!r}, "
            f"group_id={self.group_id!r}, "
            f"allow_conversation={self.allow_conversation!r}, "
            "access_token=<redacted>, "
            "proof_trigger=<redacted>, "
            "proof_reply=<redacted>, "
            f"api_version={self.api_version!r}, "
            f"api_base_url={self.api_base_url!r})"
        )

    def send_config_complete(self) -> bool:
        return (
            type(self.group_id) is int
            and self.group_id > 0
            and type(self.access_token) is str
            and bool(self.access_token)
            and type(self.allow_conversation) is str
            and bool(self.allow_conversation)
        )

    def proof_config_complete(self) -> bool:
        return (
            self.send_config_complete()
            and type(self.proof_trigger) is str
            and bool(self.proof_trigger)
            and type(self.proof_reply) is str
            and bool(self.proof_reply)
        )

    def require_send_config(self) -> None:
        if not self.send_config_complete():
            raise VkClientOutboundConfigError() from None
        assert self.group_id is not None
        assert self.allow_conversation is not None
        # Validate allowlist format + group match early.
        try:
            parse_vk_client_peer_id(
                external_conversation_id=self.allow_conversation,
                expected_group_id=self.group_id,
            )
        except VkClientPeerResolutionError as exc:
            raise VkClientOutboundConfigError(str(exc)) from None

    def conversation_allowlisted(self, external_conversation_id: str) -> bool:
        if not self.send_config_complete():
            return False
        return (
            type(external_conversation_id) is str
            and external_conversation_id == self.allow_conversation
        )

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        callback: VkClientCallbackConfig | None = None,
    ) -> VkClientOutboundConfig:
        source = os.environ if environ is None else environ

        def _flag(name: str) -> bool:
            raw = source.get(name, "false")
            if raw == "true":
                return True
            if raw == "false":
                return False
            raise VkClientOutboundConfigError() from None

        outbound_enabled = _flag("VK_CLIENT_OUTBOUND_ENABLED")
        proof_enabled = _flag("VK_CLIENT_OUTBOUND_PROOF_ENABLED")

        token_raw = _optional_str(source.get("VK_CLIENT_ACCESS_TOKEN"))
        allow_raw = _optional_str(source.get("VK_CLIENT_OUTBOUND_ALLOW_CONVERSATION"))
        trigger_raw = _optional_str(source.get("VK_CLIENT_OUTBOUND_PROOF_TRIGGER"))
        reply_raw = _optional_str(source.get("VK_CLIENT_OUTBOUND_PROOF_REPLY"))
        api_version = (
            _optional_str(source.get("VK_CLIENT_API_VERSION")) or _DEFAULT_API_VERSION
        )
        api_base = (
            _optional_str(source.get("VK_CLIENT_API_BASE_URL")) or _DEFAULT_API_BASE
        )

        group_id: int | None = None
        if callback is not None and type(callback.group_id) is int and callback.group_id > 0:
            group_id = callback.group_id
        else:
            group_raw = _optional_str(source.get("VK_CLIENT_GROUP_ID"))
            if group_raw is not None:
                try:
                    group_id = int(group_raw)
                except ValueError:
                    raise VkClientOutboundConfigError() from None
                if group_id <= 0:
                    raise VkClientOutboundConfigError() from None

        token: str | None = None
        if token_raw is not None:
            token = _require_printable_secret(
                token_raw, min_len=_TOKEN_MIN, max_len=_TOKEN_MAX
            )

        allow: str | None = None
        if allow_raw is not None:
            if len(allow_raw) > 128 or any(ord(ch) < 32 for ch in allow_raw):
                raise VkClientOutboundConfigError() from None
            allow = allow_raw

        trigger: str | None = None
        if trigger_raw is not None:
            if len(trigger_raw) > _PROOF_TRIGGER_MAX:
                raise VkClientOutboundConfigError() from None
            trigger = trigger_raw

        reply: str | None = None
        if reply_raw is not None:
            if len(reply_raw) > _PROOF_REPLY_MAX or not reply_raw.strip():
                raise VkClientOutboundConfigError() from None
            reply = reply_raw

        if type(api_version) is not str or not api_version or len(api_version) > 16:
            raise VkClientOutboundConfigError() from None
        if type(api_base) is not str or not api_base.startswith("https://"):
            raise VkClientOutboundConfigError() from None

        cfg = cls(
            outbound_enabled=outbound_enabled,
            access_token=token,
            allow_conversation=allow,
            proof_enabled=proof_enabled,
            proof_trigger=trigger,
            proof_reply=reply,
            group_id=group_id,
            api_version=api_version,
            api_base_url=api_base.rstrip("/"),
        )
        if outbound_enabled:
            cfg.require_send_config()
        if proof_enabled and not cfg.proof_config_complete():
            raise VkClientOutboundConfigError() from None
        if proof_enabled and group_id is not None and allow is not None:
            try:
                parse_vk_client_peer_id(
                    external_conversation_id=allow,
                    expected_group_id=group_id,
                )
            except VkClientPeerResolutionError as exc:
                raise VkClientOutboundConfigError(str(exc)) from None
        return cfg


def vk_client_outbound_send_allowed(
    settings: Settings,
    config: VkClientOutboundConfig,
    *,
    external_conversation_id: str,
) -> bool:
    """Narrow gate for real VK client messages.send. Does not touch global policy."""

    if type(settings) is not Settings:
        return False
    if type(config) is not VkClientOutboundConfig:
        return False
    if not config.outbound_enabled:
        return False
    if not config.send_config_complete():
        return False
    if settings.emergency_lock:
        return False
    if settings.bot_mode is not BotMode.AUTO_WRITE:
        return False
    if not config.conversation_allowlisted(external_conversation_id):
        return False
    try:
        assert config.group_id is not None
        parse_vk_client_peer_id(
            external_conversation_id=external_conversation_id,
            expected_group_id=config.group_id,
        )
    except VkClientPeerResolutionError:
        return False
    return True


def vk_client_outbound_proof_allowed(
    settings: Settings,
    config: VkClientOutboundConfig,
    *,
    external_conversation_id: str,
    inbound_text: str,
) -> bool:
    """Gate for creating a closed-proof CLIENT_REPLY ReplyPlan."""

    if not vk_client_outbound_send_allowed(
        settings,
        config,
        external_conversation_id=external_conversation_id,
    ):
        return False
    if not config.proof_enabled:
        return False
    if not config.proof_config_complete():
        return False
    if type(inbound_text) is not str:
        return False
    return inbound_text == config.proof_trigger
