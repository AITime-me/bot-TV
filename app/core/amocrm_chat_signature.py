"""HMAC-SHA1 signature verification for amoCRM Chat webhooks (AMO-01A)."""

from __future__ import annotations

import hashlib
import hmac
from typing import Final

from app.core.amocrm_chat_config import AmoCrmChatConfig

__all__ = ("verify_amocrm_chat_signature",)

_HEX_DIGEST_LEN: Final[int] = 40


def verify_amocrm_chat_signature(
    *,
    raw_body: bytes,
    provided_signature: object,
    config: AmoCrmChatConfig,
) -> bool:
    """Return True iff X-Signature matches HMAC-SHA1(channel secret, raw body).

    Fail-closed on missing/invalid config, missing/non-hex signature, or
    mismatch. Never logs the secret or body.
    """

    if not config.enabled or config.channel_secret is None:
        return False
    if type(provided_signature) is not str or not provided_signature:
        return False
    candidate = provided_signature.strip().lower()
    if len(candidate) != _HEX_DIGEST_LEN:
        return False
    if any(ch not in "0123456789abcdef" for ch in candidate):
        return False
    expected = hmac.new(
        config.channel_secret.encode("utf-8"),
        raw_body,
        hashlib.sha1,
    ).hexdigest()
    try:
        return hmac.compare_digest(candidate, expected)
    except (TypeError, ValueError):
        return False
