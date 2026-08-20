"""Keyed content MAC for self-booking PII admission binding.

HMAC-SHA256 over domain-separated canonical phone+name. No plaintext storage.
"""

from __future__ import annotations

import hashlib
import hmac

from app.core.pii_admission_mac_keys import PiiAdmissionMacKeyProvider
from app.core.pii_admission_mac_types import (
    BOOKING_PII_ADMISSION_MAC_DOMAIN,
    CONTENT_MAC_BYTES,
    ActivePiiAdmissionMacKey,
    PiiAdmissionContentMac,
    PiiAdmissionMacError,
)


def _require_canonical_component(value: object, *, label: str) -> str:
    del label  # never echoed
    if type(value) is not str or value == "":
        raise PiiAdmissionMacError("PII_ADMISSION_MAC_VALUE_INVALID") from None
    if "\0" in value:
        raise PiiAdmissionMacError("PII_ADMISSION_MAC_VALUE_INVALID") from None
    return value


def _mac_message(*, canonical_phone: str, canonical_name: str) -> bytes:
    phone = _require_canonical_component(canonical_phone, label="phone")
    name = _require_canonical_component(canonical_name, label="name")
    return (
        f"{BOOKING_PII_ADMISSION_MAC_DOMAIN}\0{phone}\0{name}".encode("utf-8")
    )


def compute_booking_pii_admission_content_mac(
    *,
    canonical_phone: str,
    canonical_name: str,
    key_provider: PiiAdmissionMacKeyProvider,
    active_key: ActivePiiAdmissionMacKey | None = None,
) -> PiiAdmissionContentMac:
    """Compute full HMAC-SHA256 content MAC with the active (or given) key."""

    message = _mac_message(
        canonical_phone=canonical_phone,
        canonical_name=canonical_name,
    )
    if active_key is not None:
        if type(active_key) is not ActivePiiAdmissionMacKey:
            raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None
        key = active_key.key
        key_id = active_key.key_id
    else:
        try:
            snapshot = key_provider.get_active_key()
        except PiiAdmissionMacError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise PiiAdmissionMacError("PII_ADMISSION_MAC_KEY_UNAVAILABLE") from None
        key = snapshot.key
        key_id = snapshot.key_id

    try:
        digest = hmac.new(key, message, hashlib.sha256).digest()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise PiiAdmissionMacError("PII_ADMISSION_MAC_VALUE_INVALID") from None
    if len(digest) != CONTENT_MAC_BYTES:
        raise PiiAdmissionMacError("PII_ADMISSION_MAC_VALUE_INVALID") from None
    return PiiAdmissionContentMac(digest=digest, key_id=key_id)


def verify_booking_pii_admission_content_mac(
    *,
    canonical_phone: str,
    canonical_name: str,
    stored_digest: bytes,
    mac_key_id: str,
    key_provider: PiiAdmissionMacKeyProvider,
) -> bool:
    """Constant-time verify against a stored full digest + recorded key id."""

    if type(stored_digest) is not bytes or len(stored_digest) != CONTENT_MAC_BYTES:
        raise PiiAdmissionMacError("PII_ADMISSION_MAC_VALUE_INVALID") from None
    if type(mac_key_id) is not str or mac_key_id == "":
        raise PiiAdmissionMacError("PII_ADMISSION_MAC_CONFIG_INVALID") from None
    message = _mac_message(
        canonical_phone=canonical_phone,
        canonical_name=canonical_name,
    )
    try:
        key = key_provider.get_key(mac_key_id)
    except PiiAdmissionMacError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise PiiAdmissionMacError("PII_ADMISSION_MAC_KEY_UNAVAILABLE") from None
    try:
        candidate = hmac.new(key, message, hashlib.sha256).digest()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise PiiAdmissionMacError("PII_ADMISSION_MAC_VALUE_INVALID") from None
    return hmac.compare_digest(candidate, stored_digest)
