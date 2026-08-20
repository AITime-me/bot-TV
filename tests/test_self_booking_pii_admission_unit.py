"""Unit tests for PII admission MAC + service contracts (SELF-BOOKING-COMMAND-03H)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.pii_admission_mac import (
    compute_booking_pii_admission_content_mac,
    verify_booking_pii_admission_content_mac,
)
from app.core.pii_admission_mac_keys import EnvPiiAdmissionMacKeyProvider
from app.core.pii_admission_mac_types import (
    BOOKING_PII_ADMISSION_MAC_DOMAIN,
    CONTENT_MAC_BYTES,
    PiiAdmissionContentMac,
    PiiAdmissionMacError,
)
from app.core.self_booking_pii_admission_types import (
    PiiAdmissionError,
    PiiAdmissionResult,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PHONE = "+79001234567"
_PHONE_ALT = "+79007654321"
_NAME = "Test Client"
_NAME_ALT = "Other Client"
_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
_KEY2_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _mac_env() -> dict[str, str]:
    return {
        "PII_ADMISSION_MAC_ACTIVE_KEY_ID": "MACK1",
        "PII_ADMISSION_MAC_KEY_MACK1": _KEY_B64,
    }


def _mac_env_rotated_keep_historical() -> dict[str, str]:
    return {
        "PII_ADMISSION_MAC_ACTIVE_KEY_ID": "MACK2",
        "PII_ADMISSION_MAC_KEY_MACK1": _KEY_B64,
        "PII_ADMISSION_MAC_KEY_MACK2": _KEY2_B64,
    }


def _mac_env_rotated_drop_historical() -> dict[str, str]:
    return {
        "PII_ADMISSION_MAC_ACTIVE_KEY_ID": "MACK2",
        "PII_ADMISSION_MAC_KEY_MACK2": _KEY2_B64,
    }


def test_content_mac_is_full_hmac_sha256_domain_separated() -> None:
    provider = EnvPiiAdmissionMacKeyProvider(_mac_env())
    mac = compute_booking_pii_admission_content_mac(
        canonical_phone=_PHONE,
        canonical_name=_NAME,
        key_provider=provider,
    )
    assert isinstance(mac, PiiAdmissionContentMac)
    assert len(mac.digest) == CONTENT_MAC_BYTES
    assert mac.key_id == "MACK1"
    expected = hmac.new(
        base64.urlsafe_b64decode(_KEY_B64),
        f"{BOOKING_PII_ADMISSION_MAC_DOMAIN}\0{_PHONE}\0{_NAME}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    assert mac.digest == expected
    assert verify_booking_pii_admission_content_mac(
        canonical_phone=_PHONE,
        canonical_name=_NAME,
        stored_digest=mac.digest,
        mac_key_id=mac.key_id,
        key_provider=provider,
    )
    assert not verify_booking_pii_admission_content_mac(
        canonical_phone=_PHONE,
        canonical_name="Other Name",
        stored_digest=mac.digest,
        mac_key_id=mac.key_id,
        key_provider=provider,
    )


def test_content_mac_rejects_nul_in_components() -> None:
    provider = EnvPiiAdmissionMacKeyProvider(_mac_env())
    with pytest.raises(PiiAdmissionMacError) as exc_info:
        compute_booking_pii_admission_content_mac(
            canonical_phone=_PHONE + "\0x",
            canonical_name=_NAME,
            key_provider=provider,
        )
    assert exc_info.value.code == "PII_ADMISSION_MAC_VALUE_INVALID"


def test_mac_key_repr_hides_material() -> None:
    provider = EnvPiiAdmissionMacKeyProvider(_mac_env())
    active = provider.get_active_key()
    rendered = f"{active!r}{provider!r}{active}"
    assert _KEY_B64 not in rendered
    assert "MACK1" not in rendered
    assert "key=<redacted>" in rendered


def test_admission_result_repr_hides_refs_and_ids() -> None:
    result = PiiAdmissionResult(
        conversation_id=uuid4(),
        request_id="req-1",
        phone_ref_token="token-phone",
        name_ref_token="token-name",
        reused=False,
    )
    rendered = f"{result!r}{result!s}{result}"
    assert "token-phone" not in rendered
    assert "token-name" not in rendered
    assert "req-1" not in rendered
    assert "phone_ref_token=<redacted>" in rendered


def test_pii_admission_service_not_wired_to_confirm_or_create() -> None:
    source = (_REPO_ROOT / "app/services/self_booking_pii_admission.py").read_text(
        encoding="utf-8"
    )
    assert "CONFIRM_SELECTED_SLOT" not in source
    assert "admit_confirmed" not in source
    assert "BookingCreateHttpClient" not in source
    assert "IngressEvent" not in source
    assert "InboxMessage" not in source
    assert "OutboxMessage" not in source
    assert "encrypt_text" not in source
    assert "store_booking_phone_write_pair" in source


def test_admission_errors_are_fixed_codes_only() -> None:
    err = PiiAdmissionError("PII_ADMISSION_CONFLICT")
    assert err.code == "PII_ADMISSION_CONFLICT"
    assert "phone" not in str(err).lower()
    bad = PiiAdmissionError("not-a-code")
    assert bad.code == "PII_ADMISSION_CONFIG_INVALID"


def test_verify_uses_stored_mac_key_id_not_active_after_rotation() -> None:
    """Historical binding stays verifiable under K1 while active is K2."""

    k1_provider = EnvPiiAdmissionMacKeyProvider(_mac_env())
    stored = compute_booking_pii_admission_content_mac(
        canonical_phone=_PHONE,
        canonical_name=_NAME,
        key_provider=k1_provider,
    )
    assert stored.key_id == "MACK1"

    rotated = EnvPiiAdmissionMacKeyProvider(_mac_env_rotated_keep_historical())
    assert rotated.active_key_id() == "MACK2"
    active_mac = compute_booking_pii_admission_content_mac(
        canonical_phone=_PHONE,
        canonical_name=_NAME,
        key_provider=rotated,
    )
    assert active_mac.key_id == "MACK2"
    assert active_mac.digest != stored.digest

    assert verify_booking_pii_admission_content_mac(
        canonical_phone=_PHONE,
        canonical_name=_NAME,
        stored_digest=stored.digest,
        mac_key_id="MACK1",
        key_provider=rotated,
    )
    assert not verify_booking_pii_admission_content_mac(
        canonical_phone=_PHONE_ALT,
        canonical_name=_NAME_ALT,
        stored_digest=stored.digest,
        mac_key_id="MACK1",
        key_provider=rotated,
    )


def test_verify_missing_historical_mac_key_fail_closed() -> None:
    k1_provider = EnvPiiAdmissionMacKeyProvider(_mac_env())
    stored = compute_booking_pii_admission_content_mac(
        canonical_phone=_PHONE,
        canonical_name=_NAME,
        key_provider=k1_provider,
    )
    dropped = EnvPiiAdmissionMacKeyProvider(_mac_env_rotated_drop_historical())
    with pytest.raises(PiiAdmissionMacError) as exc_info:
        verify_booking_pii_admission_content_mac(
            canonical_phone=_PHONE,
            canonical_name=_NAME,
            stored_digest=stored.digest,
            mac_key_id="MACK1",
            key_provider=dropped,
        )
    assert exc_info.value.code == "PII_ADMISSION_MAC_KEY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_replay_after_rotation_uses_stored_key_and_skips_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager
    from dataclasses import dataclass
    from typing import Any
    from uuid import UUID

    from app.core.ephemeral_pii_types import EphemeralPiiReference
    from app.services.self_booking_pii_admission import SelfBookingPiiAdmissionService

    conversation_id = uuid4()
    request_id = "req-rot-replay"
    phone_tok = EphemeralPiiReference.generate().to_token()
    name_tok = EphemeralPiiReference.generate().to_token()
    k1 = EnvPiiAdmissionMacKeyProvider(_mac_env())
    stored = compute_booking_pii_admission_content_mac(
        canonical_phone=_PHONE,
        canonical_name=_NAME,
        key_provider=k1,
    )

    @dataclass
    class _Row:
        conversation_id: UUID
        request_id: str
        phone_ref_token: str
        name_ref_token: str
        content_mac: bytes
        mac_key_id: str

    existing = _Row(
        conversation_id=conversation_id,
        request_id=request_id,
        phone_ref_token=phone_tok,
        name_ref_token=name_tok,
        content_mac=stored.digest,
        mac_key_id="MACK1",
    )
    store_calls = {"count": 0}
    get_key_ids: list[str] = []
    active_calls = {"count": 0}

    class _TrackingProvider(EnvPiiAdmissionMacKeyProvider):
        def get_active_key(self) -> Any:  # type: ignore[override]
            active_calls["count"] += 1
            return super().get_active_key()

        def get_key(self, key_id: object) -> bytes:
            get_key_ids.append(str(key_id))
            return super().get_key(key_id)

    @asynccontextmanager
    async def _scope(_factory: object) -> Any:
        yield object()

    async def _get_by_request(*_a: object, **_k: object) -> _Row:
        return existing

    async def _store_pair(*_a: object, **_k: object) -> tuple[object, object]:
        store_calls["count"] += 1
        raise AssertionError("replay must not re-store")

    async def _alive(*_a: object, **_k: object) -> bool:
        return True

    monkeypatch.setattr(
        "app.services.self_booking_pii_admission.session_scope",
        _scope,
    )
    monkeypatch.setattr(
        "app.services.self_booking_pii_admission.admission_repo.get_by_request",
        _get_by_request,
    )
    provider = _TrackingProvider(_mac_env_rotated_keep_historical())

    class _Pii:
        async def store_booking_phone_write_pair(self, *a: object, **k: object) -> tuple[object, object]:
            return await _store_pair(*a, **k)

        async def booking_phone_write_pair_alive(self, *a: object, **k: object) -> bool:
            return await _alive(*a, **k)

    service = SelfBookingPiiAdmissionService(
        session_factory=object(),  # type: ignore[arg-type]
        pii_store=_Pii(),  # type: ignore[arg-type]
        mac_key_provider=provider,
    )
    result = await service.admit(
        conversation_id=conversation_id,
        request_id=request_id,
        phone=_PHONE,
        client_name=_NAME,
    )
    assert result.reused is True
    assert result.phone_ref_token == phone_tok
    assert result.name_ref_token == name_tok
    assert store_calls["count"] == 0
    assert "MACK1" in get_key_ids
    # Active may be touched to build a candidate MAC, but binding check uses K1.
    assert active_calls["count"] >= 1


@pytest.mark.asyncio
async def test_replay_conflict_and_missing_key_never_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager
    from dataclasses import dataclass
    from typing import Any
    from uuid import UUID

    from app.core.ephemeral_pii_types import EphemeralPiiReference
    from app.services.self_booking_pii_admission import SelfBookingPiiAdmissionService

    conversation_id = uuid4()
    request_id = "req-rot-conflict"
    phone_tok = EphemeralPiiReference.generate().to_token()
    name_tok = EphemeralPiiReference.generate().to_token()
    stored = compute_booking_pii_admission_content_mac(
        canonical_phone=_PHONE,
        canonical_name=_NAME,
        key_provider=EnvPiiAdmissionMacKeyProvider(_mac_env()),
    )

    @dataclass
    class _Row:
        conversation_id: UUID
        request_id: str
        phone_ref_token: str
        name_ref_token: str
        content_mac: bytes
        mac_key_id: str

    existing = _Row(
        conversation_id=conversation_id,
        request_id=request_id,
        phone_ref_token=phone_tok,
        name_ref_token=name_tok,
        content_mac=stored.digest,
        mac_key_id="MACK1",
    )
    store_calls = {"count": 0}

    @asynccontextmanager
    async def _scope(_factory: object) -> Any:
        yield object()

    async def _get_by_request(*_a: object, **_k: object) -> _Row:
        return existing

    async def _store_pair(*_a: object, **_k: object) -> tuple[object, object]:
        store_calls["count"] += 1
        raise AssertionError("conflict/missing-key must not re-store")

    async def _alive(*_a: object, **_k: object) -> bool:
        return True

    monkeypatch.setattr(
        "app.services.self_booking_pii_admission.session_scope",
        _scope,
    )
    monkeypatch.setattr(
        "app.services.self_booking_pii_admission.admission_repo.get_by_request",
        _get_by_request,
    )

    class _Pii:
        async def store_booking_phone_write_pair(self, *a: object, **k: object) -> tuple[object, object]:
            return await _store_pair(*a, **k)

        async def booking_phone_write_pair_alive(self, *a: object, **k: object) -> bool:
            return await _alive(*a, **k)

    # Different body under rotated keys (K1 still present) → CONFLICT.
    service = SelfBookingPiiAdmissionService(
        session_factory=object(),  # type: ignore[arg-type]
        pii_store=_Pii(),  # type: ignore[arg-type]
        mac_key_provider=EnvPiiAdmissionMacKeyProvider(
            _mac_env_rotated_keep_historical()
        ),
    )
    with pytest.raises(PiiAdmissionError) as conflict:
        await service.admit(
            conversation_id=conversation_id,
            request_id=request_id,
            phone=_PHONE_ALT,
            client_name=_NAME_ALT,
        )
    assert conflict.value.code == "PII_ADMISSION_CONFLICT"
    assert store_calls["count"] == 0

    # Missing historical K1 → fail closed, still no re-store / no ref return.
    service_dropped = SelfBookingPiiAdmissionService(
        session_factory=object(),  # type: ignore[arg-type]
        pii_store=_Pii(),  # type: ignore[arg-type]
        mac_key_provider=EnvPiiAdmissionMacKeyProvider(
            _mac_env_rotated_drop_historical()
        ),
    )
    with pytest.raises(PiiAdmissionError) as missing:
        await service_dropped.admit(
            conversation_id=conversation_id,
            request_id=request_id,
            phone=_PHONE,
            client_name=_NAME,
        )
    assert missing.value.code == "PII_ADMISSION_CONFLICT"
    assert store_calls["count"] == 0
    assert phone_tok not in str(missing.value)
    assert name_tok not in str(missing.value)
