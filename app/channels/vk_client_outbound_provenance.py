"""Authenticated Bot-TV provenance marker for VK CLIENT messages.send payload.

Returned unchanged in Callback ``message_reply.object.payload``. No PII/text.
Raw foreign payloads are never persisted — only a technical classification.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

__all__ = (
    "VK_OUTBOUND_PROVENANCE_NS",
    "VK_OUTBOUND_PROVENANCE_VERSION",
    "VkReplyPayloadKind",
    "VkReplyProvenanceTechnical",
    "build_vk_outbound_provenance_payload",
    "classify_vk_reply_payload",
    "verify_vk_outbound_provenance_payload",
    "verify_vk_outbound_provenance_technical",
)

VK_OUTBOUND_PROVENANCE_NS: Final[str] = "bot_tv.vk_out"
VK_OUTBOUND_PROVENANCE_VERSION: Final[int] = 1
_MAC_HEX_LEN: Final[int] = 32
_ALLOWED_KEYS: Final[frozenset[str]] = frozenset({"v", "ns", "oid", "mac"})
_OID_LEN: Final[int] = 36


class VkReplyPayloadKind(StrEnum):
    ABSENT = "ABSENT"
    FOREIGN = "FOREIGN"
    BOT_TV_CANDIDATE = "BOT_TV_CANDIDATE"


@dataclass(frozen=True, slots=True, repr=False)
class VkReplyProvenanceTechnical:
    """Durable-safe provenance summary. Never holds raw foreign payload."""

    kind: VkReplyPayloadKind
    v: int | None = None
    ns: str | None = None
    oid: str | None = None
    mac: str | None = None

    def __post_init__(self) -> None:
        if self.kind is VkReplyPayloadKind.BOT_TV_CANDIDATE:
            if self.v != VK_OUTBOUND_PROVENANCE_VERSION:
                raise ValueError("INVALID_PROVENANCE_TECHNICAL")
            if self.ns != VK_OUTBOUND_PROVENANCE_NS:
                raise ValueError("INVALID_PROVENANCE_TECHNICAL")
            if type(self.oid) is not str or len(self.oid) != _OID_LEN:
                raise ValueError("INVALID_PROVENANCE_TECHNICAL")
            if type(self.mac) is not str or len(self.mac) != _MAC_HEX_LEN:
                raise ValueError("INVALID_PROVENANCE_TECHNICAL")
        elif self.v is not None or self.ns is not None or self.oid is not None or self.mac is not None:
            raise ValueError("INVALID_PROVENANCE_TECHNICAL")

    def to_envelope_fragment(self) -> dict[str, Any]:
        fragment: dict[str, Any] = {"kind": self.kind.value}
        if self.kind is VkReplyPayloadKind.BOT_TV_CANDIDATE:
            fragment["v"] = self.v
            fragment["ns"] = self.ns
            fragment["oid"] = self.oid
            fragment["mac"] = self.mac
        return fragment

    @classmethod
    def from_envelope_fragment(cls, value: object) -> VkReplyProvenanceTechnical | None:
        if type(value) is not dict:
            return None
        kind_raw = value.get("kind")
        if kind_raw == VkReplyPayloadKind.ABSENT.value:
            if set(value.keys()) != {"kind"}:
                return None
            return cls(kind=VkReplyPayloadKind.ABSENT)
        if kind_raw == VkReplyPayloadKind.FOREIGN.value:
            if set(value.keys()) != {"kind"}:
                return None
            return cls(kind=VkReplyPayloadKind.FOREIGN)
        if kind_raw == VkReplyPayloadKind.BOT_TV_CANDIDATE.value:
            if set(value.keys()) != {"kind", "v", "ns", "oid", "mac"}:
                return None
            try:
                return cls(
                    kind=VkReplyPayloadKind.BOT_TV_CANDIDATE,
                    v=value.get("v") if type(value.get("v")) is int else None,
                    ns=value.get("ns") if type(value.get("ns")) is str else None,
                    oid=value.get("oid") if type(value.get("oid")) is str else None,
                    mac=value.get("mac") if type(value.get("mac")) is str else None,
                )
            except ValueError:
                return None
        return None

    def __repr__(self) -> str:
        return f"VkReplyProvenanceTechnical(kind={self.kind.value!r})"


def build_vk_outbound_provenance_payload(
    *,
    outbound_id: uuid.UUID,
    provenance_key: str,
) -> str:
    """JSON string for VK ``payload`` param (technical marker only)."""

    if type(outbound_id) is not uuid.UUID:
        raise TypeError("outbound_id must be UUID")
    if type(provenance_key) is not str or not provenance_key:
        raise ValueError("PROVENANCE_KEY_INVALID")
    mac = _mac_hex(outbound_id=outbound_id, provenance_key=provenance_key)
    body = {
        "v": VK_OUTBOUND_PROVENANCE_VERSION,
        "ns": VK_OUTBOUND_PROVENANCE_NS,
        "oid": str(outbound_id),
        "mac": mac,
    }
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def classify_vk_reply_payload(payload: object) -> VkReplyProvenanceTechnical:
    """Map raw Callback payload → durable technical classification (no raw keep)."""

    if payload is None:
        return VkReplyProvenanceTechnical(kind=VkReplyPayloadKind.ABSENT)

    obj = _coerce_payload_object(payload)
    if obj is None:
        return VkReplyProvenanceTechnical(kind=VkReplyPayloadKind.FOREIGN)

    if set(obj.keys()) != _ALLOWED_KEYS:
        return VkReplyProvenanceTechnical(kind=VkReplyPayloadKind.FOREIGN)

    version = obj.get("v")
    ns = obj.get("ns")
    oid_raw = obj.get("oid")
    mac_raw = obj.get("mac")
    if version != VK_OUTBOUND_PROVENANCE_VERSION:
        return VkReplyProvenanceTechnical(kind=VkReplyPayloadKind.FOREIGN)
    if ns != VK_OUTBOUND_PROVENANCE_NS:
        return VkReplyProvenanceTechnical(kind=VkReplyPayloadKind.FOREIGN)
    if type(oid_raw) is not str or len(oid_raw) != _OID_LEN:
        return VkReplyProvenanceTechnical(kind=VkReplyPayloadKind.FOREIGN)
    try:
        uuid.UUID(oid_raw)
    except ValueError:
        return VkReplyProvenanceTechnical(kind=VkReplyPayloadKind.FOREIGN)
    if type(mac_raw) is not str or len(mac_raw) != _MAC_HEX_LEN:
        return VkReplyProvenanceTechnical(kind=VkReplyPayloadKind.FOREIGN)
    if any(ch not in "0123456789abcdef" for ch in mac_raw.lower()):
        return VkReplyProvenanceTechnical(kind=VkReplyPayloadKind.FOREIGN)

    return VkReplyProvenanceTechnical(
        kind=VkReplyPayloadKind.BOT_TV_CANDIDATE,
        v=VK_OUTBOUND_PROVENANCE_VERSION,
        ns=VK_OUTBOUND_PROVENANCE_NS,
        oid=oid_raw,
        mac=mac_raw.lower(),
    )


def verify_vk_outbound_provenance_payload(
    payload: object,
    *,
    provenance_key: str,
) -> uuid.UUID | None:
    """Return outbound UUID when raw marker authenticates; else None."""

    technical = classify_vk_reply_payload(payload)
    return verify_vk_outbound_provenance_technical(
        technical,
        provenance_key=provenance_key,
    )


def verify_vk_outbound_provenance_technical(
    technical: VkReplyProvenanceTechnical,
    *,
    provenance_key: str,
) -> uuid.UUID | None:
    """Authenticate durable BOT_TV_CANDIDATE fields only."""

    if type(provenance_key) is not str or not provenance_key:
        return None
    if technical.kind is not VkReplyPayloadKind.BOT_TV_CANDIDATE:
        return None
    assert technical.oid is not None
    assert technical.mac is not None
    try:
        outbound_id = uuid.UUID(technical.oid)
    except ValueError:
        return None
    expected = _mac_hex(outbound_id=outbound_id, provenance_key=provenance_key)
    if not hmac.compare_digest(technical.mac.lower(), expected):
        return None
    return outbound_id


def _mac_hex(*, outbound_id: uuid.UUID, provenance_key: str) -> str:
    material = (
        f"{VK_OUTBOUND_PROVENANCE_VERSION}:{VK_OUTBOUND_PROVENANCE_NS}:{outbound_id}"
    ).encode("utf-8")
    digest = hmac.new(
        provenance_key.encode("utf-8"),
        material,
        hashlib.sha256,
    ).hexdigest()
    return digest[:_MAC_HEX_LEN]


def _coerce_payload_object(payload: object) -> dict[str, Any] | None:
    if type(payload) is dict:
        return payload  # type: ignore[return-value]
    if type(payload) is str:
        if not payload or len(payload) > 1000:
            return None
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if type(parsed) is dict:
            return parsed
    return None
