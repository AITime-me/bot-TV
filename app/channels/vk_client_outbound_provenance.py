"""Authenticated Bot-TV provenance marker for VK CLIENT messages.send payload.

Returned unchanged in Callback ``message_reply.object.payload``. No PII/text.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any, Final

__all__ = (
    "VK_OUTBOUND_PROVENANCE_NS",
    "VK_OUTBOUND_PROVENANCE_VERSION",
    "build_vk_outbound_provenance_payload",
    "verify_vk_outbound_provenance_payload",
)

VK_OUTBOUND_PROVENANCE_NS: Final[str] = "bot_tv.vk_out"
VK_OUTBOUND_PROVENANCE_VERSION: Final[int] = 1
_MAC_HEX_LEN: Final[int] = 32


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


def verify_vk_outbound_provenance_payload(
    payload: object,
    *,
    provenance_key: str,
) -> uuid.UUID | None:
    """Return outbound UUID when marker authenticates; else None (fail-closed)."""

    if type(provenance_key) is not str or not provenance_key:
        return None
    obj = _coerce_payload_object(payload)
    if obj is None:
        return None
    version = obj.get("v")
    ns = obj.get("ns")
    oid_raw = obj.get("oid")
    mac_raw = obj.get("mac")
    if version != VK_OUTBOUND_PROVENANCE_VERSION:
        return None
    if ns != VK_OUTBOUND_PROVENANCE_NS:
        return None
    if type(oid_raw) is not str or not oid_raw:
        return None
    if type(mac_raw) is not str or len(mac_raw) != _MAC_HEX_LEN:
        return None
    try:
        outbound_id = uuid.UUID(oid_raw)
    except ValueError:
        return None
    expected = _mac_hex(outbound_id=outbound_id, provenance_key=provenance_key)
    if not hmac.compare_digest(mac_raw, expected):
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
