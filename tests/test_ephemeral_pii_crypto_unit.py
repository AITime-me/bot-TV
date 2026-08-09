"""Unit tests for ephemeral PII crypto/key-provider foundation (Stage 2A)."""

from __future__ import annotations

import base64
import secrets
import traceback
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.ephemeral_pii_crypto import (
    _decode_utf8_plaintext,
    decrypt_text,
    encrypt_text,
)
from app.core.ephemeral_pii_keys import (
    ActiveEphemeralPiiKey,
    EnvEphemeralPiiKeyProvider,
    validate_key_id,
)
from app.core.ephemeral_pii_types import (
    CRYPTO_VERSION_V1,
    KEY_SIZE_BYTES,
    MAX_PLAINTEXT_BYTES,
    MIN_CIPHERTEXT_BYTES,
    NONCE_SIZE_BYTES,
    EphemeralPiiAad,
    EphemeralPiiCiphertext,
    EphemeralPiiError,
    EphemeralPiiKind,
    EphemeralPiiPurpose,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Explicitly synthetic, non-routable marker — never a real phone number.
_SYNTHETIC_PHONE = "+00055500100"
_SYNTHETIC_PHONE_ALT = "+00055500101"

_KEY_K1 = secrets.token_bytes(KEY_SIZE_BYTES)
_KEY_K2 = secrets.token_bytes(KEY_SIZE_BYTES)
_KEY_K1_B64 = base64.urlsafe_b64encode(_KEY_K1).decode("ascii")
_KEY_K2_B64 = base64.urlsafe_b64encode(_KEY_K2).decode("ascii")


def _env_for(*pairs: tuple[str, str], active: str = "K1") -> dict[str, str]:
    env = {"EPHEMERAL_PII_ACTIVE_KEY_ID": active}
    for key_id, material in pairs:
        env[f"EPHEMERAL_PII_KEY_{key_id}"] = material
    return env


def _provider(active: str = "K1", **keys: str) -> EnvEphemeralPiiKeyProvider:
    pairs = tuple(keys.items()) if keys else (("K1", _KEY_K1_B64),)
    if keys:
        return EnvEphemeralPiiKeyProvider(
            _env_for(*((kid, mat) for kid, mat in keys.items()), active=active)
        )
    return EnvEphemeralPiiKeyProvider(
        _env_for(("K1", _KEY_K1_B64), ("K2", _KEY_K2_B64), active=active)
    )


def _aad(
    *,
    key_id: str = "K1",
    kind: EphemeralPiiKind = EphemeralPiiKind.PHONE,
    purpose: EphemeralPiiPurpose = EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    record_id: UUID | None = None,
    conversation_id: UUID | None = None,
    crypto_version: int = CRYPTO_VERSION_V1,
) -> EphemeralPiiAad:
    return EphemeralPiiAad(
        crypto_version=crypto_version,
        record_id=record_id or uuid4(),
        key_id=key_id,
        kind=kind,
        conversation_id=conversation_id or uuid4(),
        purpose=purpose,
    )


def _assert_safe_error(exc: EphemeralPiiError, code: str, *forbidden: str) -> None:
    assert type(exc) is EphemeralPiiError
    assert exc.code == code
    assert str(exc) == code
    assert repr(exc) == f"EphemeralPiiError({code!r})"
    assert exc.__cause__ is None
    text = f"{exc!s}{exc!r}" + "".join(traceback.format_exception(exc))
    for item in forbidden:
        if item:
            assert item not in text


class _HostileStr(str):
    def __str__(self) -> str:  # pragma: no cover - must not be called
        raise AssertionError("hostile __str__ called")

    def __repr__(self) -> str:  # pragma: no cover - must not be called
        raise AssertionError("hostile __repr__ called")

    def encode(self, *args: Any, **kwargs: Any) -> bytes:  # pragma: no cover
        raise AssertionError("hostile encode called")


class _HostileKeyId:
    def __str__(self) -> str:  # pragma: no cover
        raise AssertionError("hostile key id __str__ called")

    def __repr__(self) -> str:  # pragma: no cover
        raise AssertionError("hostile key id __repr__ called")


class _InterruptProvider:
    def active_key_id(self) -> str:
        raise KeyboardInterrupt

    def get_key(self, key_id: str) -> bytes:
        raise AssertionError("get_key must not run")


class _SystemExitProvider:
    def active_key_id(self) -> str:
        raise SystemExit(7)

    def get_key(self, key_id: str) -> bytes:
        raise AssertionError("get_key must not run")


def test_aes_gcm_round_trip() -> None:
    provider = _provider()
    aad = _aad()
    encrypted = encrypt_text(_SYNTHETIC_PHONE, aad=aad, key_provider=provider)
    assert decrypt_text(encrypted, aad=aad, key_provider=provider) == _SYNTHETIC_PHONE


def test_same_plaintext_yields_distinct_nonce_and_ciphertext() -> None:
    provider = _provider()
    aad = _aad()
    first = encrypt_text(_SYNTHETIC_PHONE, aad=aad, key_provider=provider)
    second = encrypt_text(_SYNTHETIC_PHONE, aad=aad, key_provider=provider)
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext
    assert len(first.nonce) == NONCE_SIZE_BYTES
    assert len(second.nonce) == NONCE_SIZE_BYTES


def test_nonce_and_key_sizes() -> None:
    provider = _provider()
    encrypted = encrypt_text(_SYNTHETIC_PHONE, aad=_aad(), key_provider=provider)
    assert len(encrypted.nonce) == 12
    assert len(provider.get_key("K1")) == 32


def test_missing_active_key_id() -> None:
    provider = EnvEphemeralPiiKeyProvider({"EPHEMERAL_PII_KEY_K1": _KEY_K1_B64})
    with pytest.raises(EphemeralPiiError) as raised:
        provider.active_key_id()
    _assert_safe_error(raised.value, "EPHEMERAL_PII_KEY_UNAVAILABLE", _KEY_K1_B64)


@pytest.mark.parametrize(
    "bad_id",
    [
        "k1",
        "K-1",
        "K.1",
        "K/1",
        "K 1",
        "K\u0410",
        "",
        "A" * 65,
        "../K1",
        "K1;DROP",
    ],
)
def test_malformed_key_id(bad_id: str) -> None:
    with pytest.raises(EphemeralPiiError) as raised:
        validate_key_id(bad_id)
    _assert_safe_error(raised.value, "EPHEMERAL_PII_CONFIG_INVALID", bad_id)


def test_missing_key_material() -> None:
    provider = EnvEphemeralPiiKeyProvider({"EPHEMERAL_PII_ACTIVE_KEY_ID": "K1"})
    with pytest.raises(EphemeralPiiError) as raised:
        provider.get_key("K1")
    # Tracebacks may include the source call site; assert exception payload itself.
    exc = raised.value
    assert exc.code == "EPHEMERAL_PII_KEY_UNAVAILABLE"
    assert str(exc) == "EPHEMERAL_PII_KEY_UNAVAILABLE"
    assert repr(exc) == "EphemeralPiiError('EPHEMERAL_PII_KEY_UNAVAILABLE')"
    assert exc.__cause__ is None
    assert _KEY_K1_B64 not in str(exc)
    assert _KEY_K1_B64 not in repr(exc)


def test_invalid_base64url_key() -> None:
    provider = EnvEphemeralPiiKeyProvider(
        {
            "EPHEMERAL_PII_ACTIVE_KEY_ID": "K1",
            "EPHEMERAL_PII_KEY_K1": "!!!not-base64!!!",
        }
    )
    with pytest.raises(EphemeralPiiError) as raised:
        provider.get_key("K1")
    _assert_safe_error(
        raised.value, "EPHEMERAL_PII_CONFIG_INVALID", "!!!not-base64!!!"
    )


def test_wrong_decoded_key_length() -> None:
    short = base64.urlsafe_b64encode(b"\x00" * 16).decode("ascii")
    provider = EnvEphemeralPiiKeyProvider(
        {"EPHEMERAL_PII_ACTIVE_KEY_ID": "K1", "EPHEMERAL_PII_KEY_K1": short}
    )
    with pytest.raises(EphemeralPiiError) as raised:
        provider.get_key("K1")
    _assert_safe_error(raised.value, "EPHEMERAL_PII_CONFIG_INVALID", short)


def test_whitespace_key_rejected() -> None:
    padded = f" {_KEY_K1_B64} "
    provider = EnvEphemeralPiiKeyProvider(
        {"EPHEMERAL_PII_ACTIVE_KEY_ID": "K1", "EPHEMERAL_PII_KEY_K1": padded}
    )
    with pytest.raises(EphemeralPiiError) as raised:
        provider.get_key("K1")
    _assert_safe_error(raised.value, "EPHEMERAL_PII_CONFIG_INVALID")


def test_non_canonical_base64url_rejected() -> None:
    # Bytes that differ between standard (+/) and urlsafe (-_) alphabets.
    material = bytes([0xFF] * 32)
    std = base64.b64encode(material).decode("ascii")
    url = base64.urlsafe_b64encode(material).decode("ascii")
    assert std != url
    assert "+" in std or "/" in std
    provider = EnvEphemeralPiiKeyProvider(
        {
            "EPHEMERAL_PII_ACTIVE_KEY_ID": "K1",
            "EPHEMERAL_PII_KEY_K1": std,
        }
    )
    with pytest.raises(EphemeralPiiError) as raised:
        provider.get_key("K1")
    _assert_safe_error(raised.value, "EPHEMERAL_PII_CONFIG_INVALID", std)


def test_unpadded_base64url_rejected_as_non_canonical() -> None:
    unpadded = _KEY_K1_B64.rstrip("=")
    assert unpadded != _KEY_K1_B64
    provider = EnvEphemeralPiiKeyProvider(
        {"EPHEMERAL_PII_ACTIVE_KEY_ID": "K1", "EPHEMERAL_PII_KEY_K1": unpadded}
    )
    with pytest.raises(EphemeralPiiError) as raised:
        provider.get_key("K1")
    _assert_safe_error(raised.value, "EPHEMERAL_PII_CONFIG_INVALID", unpadded)


def test_hostile_key_id_object() -> None:
    provider = _provider()
    with pytest.raises(EphemeralPiiError) as raised:
        provider.get_key(_HostileKeyId())  # type: ignore[arg-type]
    _assert_safe_error(raised.value, "EPHEMERAL_PII_CONFIG_INVALID")


def test_hostile_plaintext_object_and_str_subclass() -> None:
    provider = _provider()
    aad = _aad()
    with pytest.raises(EphemeralPiiError) as raised:
        encrypt_text(object(), aad=aad, key_provider=provider)  # type: ignore[arg-type]
    _assert_safe_error(raised.value, "EPHEMERAL_PII_VALUE_INVALID")

    with pytest.raises(EphemeralPiiError) as raised:
        encrypt_text(_HostileStr(_SYNTHETIC_PHONE), aad=aad, key_provider=provider)
    _assert_safe_error(
        raised.value, "EPHEMERAL_PII_VALUE_INVALID", _SYNTHETIC_PHONE
    )


def test_empty_and_oversized_plaintext_rejected() -> None:
    provider = _provider()
    aad = _aad()
    with pytest.raises(EphemeralPiiError) as raised:
        encrypt_text("", aad=aad, key_provider=provider)
    _assert_safe_error(raised.value, "EPHEMERAL_PII_VALUE_INVALID")

    oversized = "X" * (MAX_PLAINTEXT_BYTES + 1)
    with pytest.raises(EphemeralPiiError) as raised:
        encrypt_text(oversized, aad=aad, key_provider=provider)
    _assert_safe_error(raised.value, "EPHEMERAL_PII_VALUE_INVALID", oversized)


def test_tampered_ciphertext_nonce_aad() -> None:
    provider = _provider()
    aad = _aad()
    encrypted = encrypt_text(_SYNTHETIC_PHONE, aad=aad, key_provider=provider)

    flipped_ct = bytearray(encrypted.ciphertext)
    flipped_ct[0] ^= 0x01
    tampered_ct = EphemeralPiiCiphertext(
        ciphertext=bytes(flipped_ct),
        nonce=encrypted.nonce,
        key_id=encrypted.key_id,
        crypto_version=encrypted.crypto_version,
    )
    with pytest.raises(EphemeralPiiError) as raised:
        decrypt_text(tampered_ct, aad=aad, key_provider=provider)
    _assert_safe_error(
        raised.value, "EPHEMERAL_PII_ACCESS_DENIED", _SYNTHETIC_PHONE
    )

    flipped_nonce = bytearray(encrypted.nonce)
    flipped_nonce[0] ^= 0x01
    tampered_nonce = EphemeralPiiCiphertext(
        ciphertext=encrypted.ciphertext,
        nonce=bytes(flipped_nonce),
        key_id=encrypted.key_id,
        crypto_version=encrypted.crypto_version,
    )
    with pytest.raises(EphemeralPiiError) as raised:
        decrypt_text(tampered_nonce, aad=aad, key_provider=provider)
    _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED")

    wrong_aad = _aad(
        key_id=aad.key_id,
        record_id=aad.record_id,
        conversation_id=uuid4(),
        purpose=aad.purpose,
        kind=aad.kind,
    )
    with pytest.raises(EphemeralPiiError) as raised:
        decrypt_text(encrypted, aad=wrong_aad, key_provider=provider)
    _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED")


def test_wrong_nonce_length_rejected_by_dto() -> None:
    provider = _provider()
    aad = _aad()
    encrypted = encrypt_text(_SYNTHETIC_PHONE, aad=aad, key_provider=provider)
    with pytest.raises(EphemeralPiiError) as raised:
        EphemeralPiiCiphertext(
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce + b"\x00",
            key_id=encrypted.key_id,
            crypto_version=encrypted.crypto_version,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_VALUE_INVALID")


def test_wrong_purpose_kind_record_conversation() -> None:
    provider = _provider()
    record_id = uuid4()
    conversation_id = uuid4()
    aad = _aad(record_id=record_id, conversation_id=conversation_id)
    encrypted = encrypt_text(_SYNTHETIC_PHONE, aad=aad, key_provider=provider)

    cases = [
        _aad(
            key_id=aad.key_id,
            record_id=record_id,
            conversation_id=conversation_id,
            purpose=EphemeralPiiPurpose.AMOCRM_CONTACT_SYNC,
        ),
        _aad(
            key_id=aad.key_id,
            record_id=uuid4(),
            conversation_id=conversation_id,
        ),
        _aad(
            key_id=aad.key_id,
            record_id=record_id,
            conversation_id=uuid4(),
        ),
    ]
    for wrong in cases:
        with pytest.raises(EphemeralPiiError) as raised:
            decrypt_text(encrypted, aad=wrong, key_provider=provider)
        _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED")


def test_wrong_key_material() -> None:
    provider = _provider()
    aad = _aad()
    encrypted = encrypt_text(_SYNTHETIC_PHONE, aad=aad, key_provider=provider)
    other = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    evil = EnvEphemeralPiiKeyProvider(
        {"EPHEMERAL_PII_ACTIVE_KEY_ID": "K1", "EPHEMERAL_PII_KEY_K1": other}
    )
    with pytest.raises(EphemeralPiiError) as raised:
        decrypt_text(encrypted, aad=aad, key_provider=evil)
    _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED", other)


def test_old_key_decrypt_after_active_rotation() -> None:
    env = _env_for(("K1", _KEY_K1_B64), ("K2", _KEY_K2_B64), active="K1")
    provider = EnvEphemeralPiiKeyProvider(env)
    aad = _aad(key_id="K1")
    encrypted = encrypt_text(_SYNTHETIC_PHONE, aad=aad, key_provider=provider)

    env["EPHEMERAL_PII_ACTIVE_KEY_ID"] = "K2"
    rotated = EnvEphemeralPiiKeyProvider(env)
    assert rotated.active_key_id() == "K2"
    assert decrypt_text(encrypted, aad=aad, key_provider=rotated) == _SYNTHETIC_PHONE


def test_missing_old_key_after_rotation_fails_closed() -> None:
    env = _env_for(("K1", _KEY_K1_B64), ("K2", _KEY_K2_B64), active="K1")
    provider = EnvEphemeralPiiKeyProvider(env)
    aad = _aad(key_id="K1")
    encrypted = encrypt_text(_SYNTHETIC_PHONE, aad=aad, key_provider=provider)
    del env["EPHEMERAL_PII_KEY_K1"]
    env["EPHEMERAL_PII_ACTIVE_KEY_ID"] = "K2"
    rotated = EnvEphemeralPiiKeyProvider(env)
    with pytest.raises(EphemeralPiiError) as raised:
        decrypt_text(encrypted, aad=aad, key_provider=rotated)
    _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED")


def test_unsupported_crypto_version() -> None:
    provider = _provider()
    aad = _aad()
    encrypted = encrypt_text(_SYNTHETIC_PHONE, aad=aad, key_provider=provider)
    bad = EphemeralPiiCiphertext(
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        key_id=encrypted.key_id,
        crypto_version=2,
    )
    with pytest.raises(EphemeralPiiError) as raised:
        decrypt_text(bad, aad=aad, key_provider=provider)
    _assert_safe_error(raised.value, "EPHEMERAL_PII_CONFIG_INVALID")


def test_invalid_utf8_after_authenticated_decrypt_seam() -> None:
    with pytest.raises(EphemeralPiiError) as raised:
        _decode_utf8_plaintext(b"\xff\xfe")
    _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED")


def test_ciphertext_dto_repr_str_format_safe() -> None:
    provider = _provider()
    encrypted = encrypt_text(_SYNTHETIC_PHONE, aad=_aad(), key_provider=provider)
    rendered = f"{encrypted!r}|{encrypted!s}|{encrypted}"
    assert "EphemeralPiiCiphertext(" in rendered
    assert "key_id=<redacted>" in rendered
    assert _SYNTHETIC_PHONE not in rendered
    assert encrypted.key_id not in rendered
    assert encrypted.nonce.hex() not in rendered
    assert base64.b64encode(encrypted.ciphertext).decode("ascii") not in rendered
    assert not hasattr(encrypted, "plaintext")


def test_provider_repr_and_no_key_enumeration() -> None:
    provider = _provider()
    assert repr(provider) == "EnvEphemeralPiiKeyProvider()"
    assert str(provider) == "EnvEphemeralPiiKeyProvider()"
    assert not hasattr(provider, "list_keys")
    assert not hasattr(provider, "keys")
    assert not hasattr(provider, "all_keys")
    assert callable(provider.active_key_id)
    assert callable(provider.get_key)
    public_names = {
        name
        for name in dir(provider)
        if not name.startswith("_") and name not in {"active_key_id", "get_key"}
    }
    assert "list_keys" not in public_names
    assert "iter_keys" not in public_names
    assert "dump_keys" not in public_names


def test_aad_rejects_arbitrary_purpose_and_kind_strings() -> None:
    with pytest.raises(EphemeralPiiError):
        EphemeralPiiAad(
            crypto_version=1,
            record_id=uuid4(),
            key_id="K1",
            kind="PHONE",  # type: ignore[arg-type]
            conversation_id=uuid4(),
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    with pytest.raises(EphemeralPiiError):
        EphemeralPiiAad(
            crypto_version=1,
            record_id=uuid4(),
            key_id="K1",
            kind=EphemeralPiiKind.PHONE,
            conversation_id=uuid4(),
            purpose="BOOKING_PHONE_WRITE",  # type: ignore[arg-type]
        )


def test_aad_deterministic_bytes() -> None:
    record_id = UUID("00000000-0000-4000-8000-000000000001")
    conversation_id = UUID("00000000-0000-4000-8000-000000000002")
    aad = _aad(record_id=record_id, conversation_id=conversation_id, key_id="K1")
    first = aad.to_bytes()
    second = aad.to_bytes()
    assert first == second
    assert first.startswith(b"epii-aad-v1\n")
    assert b"kind=PHONE" in first
    assert b"purpose=BOOKING_PHONE_WRITE" in first


def test_exception_traceback_has_no_raw_and_cause_cleared() -> None:
    provider = EnvEphemeralPiiKeyProvider(
        {
            "EPHEMERAL_PII_ACTIVE_KEY_ID": "K1",
            "EPHEMERAL_PII_KEY_K1": "bad key material !!",
        }
    )
    with pytest.raises(EphemeralPiiError) as raised:
        provider.get_key("K1")
    exc = raised.value
    assert exc.__cause__ is None
    formatted = "".join(traceback.format_exception(exc))
    assert "bad key material" not in formatted
    assert _KEY_K1_B64 not in formatted


def test_keyboard_interrupt_and_system_exit_not_swallowed() -> None:
    aad = _aad()
    with pytest.raises(KeyboardInterrupt):
        encrypt_text(
            _SYNTHETIC_PHONE, aad=aad, key_provider=_InterruptProvider()  # type: ignore[arg-type]
        )
    with pytest.raises(SystemExit):
        encrypt_text(
            _SYNTHETIC_PHONE, aad=aad, key_provider=_SystemExitProvider()  # type: ignore[arg-type]
        )


def test_import_boundaries_pii_gateway_and_dialog_context() -> None:
    pii_gateway = (_REPO_ROOT / "app/core/pii_gateway.py").read_text(encoding="utf-8")
    dialog = (_REPO_ROOT / "app/services/dialog_context.py").read_text(encoding="utf-8")
    crypto = (_REPO_ROOT / "app/core/ephemeral_pii_crypto.py").read_text(
        encoding="utf-8"
    )
    keys = (_REPO_ROOT / "app/core/ephemeral_pii_keys.py").read_text(encoding="utf-8")
    assert "ephemeral_pii" not in pii_gateway
    assert "decrypt_text" not in pii_gateway
    assert "ephemeral_pii" not in dialog
    assert "decrypt_text" not in dialog
    assert "dialog_context" not in crypto
    assert "pii_gateway" not in crypto
    assert "dialog_context" not in keys
    assert "sanitize_for_ai" not in crypto


def test_env_example_names_without_values() -> None:
    text = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "EPHEMERAL_PII_ACTIVE_KEY_ID=" in text
    assert "EPHEMERAL_PII_KEY_<KEY_ID>" in text
    for line in text.splitlines():
        if line.startswith("EPHEMERAL_PII_ACTIVE_KEY_ID="):
            assert line == "EPHEMERAL_PII_ACTIVE_KEY_ID="
        if line.startswith("EPHEMERAL_PII_KEY_") and "<" not in line:
            _, _, value = line.partition("=")
            assert value == ""


def test_docker_allowlist_contains_exact_ephemeral_files() -> None:
    lines = (_REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    allows = [line for line in lines if line.startswith("!")]
    for required in (
        "!app/core/ephemeral_pii_types.py",
        "!app/core/ephemeral_pii_keys.py",
        "!app/core/ephemeral_pii_crypto.py",
    ):
        assert required in allows
    assert "!app/**" not in allows
    assert "!.env.example" not in allows
    assert "!docs/" not in "".join(allows)
    assert "!tests/" not in "".join(allows)


def test_lockfiles_only_add_cryptography_tree() -> None:
    baseline_prod = {
        "alembic",
        "annotated-doc",
        "annotated-types",
        "anyio",
        "asyncpg",
        "click",
        "colorama",
        "fastapi",
        "greenlet",
        "h11",
        "httptools",
        "idna",
        "Mako",
        "MarkupSafe",
        "pydantic",
        "pydantic_core",
        "python-dotenv",
        "PyYAML",
        "redis",
        "SQLAlchemy",
        "starlette",
        "typing-inspection",
        "typing_extensions",
        "uvicorn",
        "uvloop",
        "watchfiles",
        "websockets",
    }
    baseline_versions = {
        "alembic": "1.16.4",
        "fastapi": "0.140.0",
        "pydantic": "2.13.4",
        "SQLAlchemy": "2.0.41",
        "redis": "8.0.1",
        "asyncpg": "0.30.0",
        "uvicorn": "0.51.0",
        "starlette": "1.3.1",
        "httpx": "0.28.1",
        "pytest": "9.1.1",
    }

    def parse_lock(path: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            req, _, _marker = line.partition(";")
            name, _, version = req.strip().partition("==")
            result[name] = version
        return result

    prod = parse_lock(_REPO_ROOT / "requirements-lock.txt")
    dev = parse_lock(_REPO_ROOT / "requirements-dev-lock.txt")
    assert prod["cryptography"] == "49.0.0"
    assert prod["cffi"] == "2.1.0"
    assert prod["pycparser"] == "3.0"
    assert set(prod) - baseline_prod == {"cffi", "cryptography", "pycparser"}
    for name, version in baseline_versions.items():
        if name in prod:
            assert prod[name] == version
        if name in dev:
            assert dev[name] == version
    assert "uvloop" in (_REPO_ROOT / "requirements-lock.txt").read_text(
        encoding="utf-8"
    )
    assert 'sys_platform != "win32"' in (
        _REPO_ROOT / "requirements-lock.txt"
    ).read_text(encoding="utf-8")


def test_requirements_txt_pins_cryptography() -> None:
    text = (_REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "cryptography==49.0.0" in text


def test_kind_enum_phone_and_client_name() -> None:
    assert list(EphemeralPiiKind) == [
        EphemeralPiiKind.PHONE,
        EphemeralPiiKind.CLIENT_NAME,
    ]
    assert {p.value for p in EphemeralPiiPurpose} == {
        "BOOKING_PHONE_WRITE",
        "APPROVED_STAFF_ALERT_PHONE",
        "AMOCRM_CONTACT_SYNC",
        "MASTER_BOOKING_CLIENT_WRITE",
    }


def test_encrypt_requires_aad_key_id_match_active() -> None:
    provider = _provider(active="K1")
    aad = _aad(key_id="K2")
    with pytest.raises(EphemeralPiiError) as raised:
        encrypt_text(_SYNTHETIC_PHONE, aad=aad, key_provider=provider)
    _assert_safe_error(raised.value, "EPHEMERAL_PII_CONFIG_INVALID")


def test_aad_field_swap_changes_bytes() -> None:
    record_a = uuid4()
    record_b = uuid4()
    conversation = uuid4()
    left = _aad(record_id=record_a, conversation_id=conversation)
    right = _aad(record_id=record_b, conversation_id=conversation)
    assert left.to_bytes() != right.to_bytes()


class _CallTracker:
    def __init__(self) -> None:
        self.str_calls = 0
        self.repr_calls = 0

    def __str__(self) -> str:
        self.str_calls += 1
        return "SYNTHETIC_HOSTILE_NONCE_STR"

    def __repr__(self) -> str:
        self.repr_calls += 1
        return "SYNTHETIC_HOSTILE_NONCE_REPR"


class _TrackingProvider:
    def __init__(self, inner: EnvEphemeralPiiKeyProvider) -> None:
        self._inner = inner
        self.active_calls = 0
        self.get_key_calls = 0

    def active_key_id(self) -> str:
        self.active_calls += 1
        return self._inner.active_key_id()

    def get_key(self, key_id: str) -> bytes:
        self.get_key_calls += 1
        return self._inner.get_key(key_id)


class _RaisingProvider:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.active_calls = 0
        self.get_key_calls = 0

    def active_key_id(self) -> str:
        self.active_calls += 1
        raise AssertionError("active_key_id must not run on decrypt")

    def get_key(self, key_id: str) -> bytes:
        self.get_key_calls += 1
        raise self._exc


def test_encrypt_rejects_invalid_nonce_from_token_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider()
    aad = _aad()
    encrypt_calls: list[object] = []
    real_aesgcm = __import__(
        "cryptography.hazmat.primitives.ciphers.aead", fromlist=["AESGCM"]
    ).AESGCM

    class _TrackingAESGCM:
        def __init__(self, key: bytes) -> None:
            self._inner = real_aesgcm(key)

        def encrypt(self, nonce: bytes, data: bytes, associated_data: bytes | None) -> bytes:
            encrypt_calls.append(nonce)
            return self._inner.encrypt(nonce, data, associated_data)

        def decrypt(self, nonce: bytes, data: bytes, associated_data: bytes | None) -> bytes:
            return self._inner.decrypt(nonce, data, associated_data)

    monkeypatch.setattr("app.core.ephemeral_pii_crypto.AESGCM", _TrackingAESGCM)

    cases: list[object] = [
        b"\x00" * 11,
        b"\x00" * 13,
        bytearray(b"\x00" * 12),
    ]
    hostile = _CallTracker()
    cases.append(hostile)

    for bad_nonce in cases:
        encrypt_calls.clear()

        def _fake_token_bytes(n: int, _value: object = bad_nonce) -> object:
            assert n == NONCE_SIZE_BYTES
            return _value

        monkeypatch.setattr(
            "app.core.ephemeral_pii_crypto.secrets.token_bytes",
            _fake_token_bytes,
        )
        with pytest.raises(EphemeralPiiError) as raised:
            encrypt_text(_SYNTHETIC_PHONE, aad=aad, key_provider=provider)
        _assert_safe_error(
            raised.value,
            "EPHEMERAL_PII_ENCRYPT_FAILED",
            "SYNTHETIC_HOSTILE_NONCE_STR",
            "SYNTHETIC_HOSTILE_NONCE_REPR",
            _SYNTHETIC_PHONE,
        )
        assert encrypt_calls == []

    assert hostile.str_calls == 0
    assert hostile.repr_calls == 0


def test_ciphertext_dto_structural_invariants() -> None:
    valid_ct = b"\x00" * MIN_CIPHERTEXT_BYTES
    valid_nonce = b"\x01" * NONCE_SIZE_BYTES

    ok = EphemeralPiiCiphertext(
        ciphertext=valid_ct,
        nonce=valid_nonce,
        key_id="K1",
        crypto_version=CRYPTO_VERSION_V1,
    )
    assert len(ok.ciphertext) == MIN_CIPHERTEXT_BYTES
    assert "key_id=<redacted>" in repr(ok)
    assert valid_ct.hex() not in repr(ok)
    assert valid_nonce.hex() not in repr(ok)

    invalid_cases: list[tuple[object, object]] = [
        (valid_ct, b"\x00" * 11),
        (valid_ct, b"\x00" * 13),
        (valid_ct, bytearray(valid_nonce)),
        (b"", valid_nonce),
        (b"\x00" * 1, valid_nonce),
        (b"\x00" * 15, valid_nonce),
        (bytearray(valid_ct), valid_nonce),
    ]
    for ciphertext, nonce in invalid_cases:
        with pytest.raises(EphemeralPiiError) as raised:
            EphemeralPiiCiphertext(
                ciphertext=ciphertext,  # type: ignore[arg-type]
                nonce=nonce,  # type: ignore[arg-type]
                key_id="K1",
                crypto_version=CRYPTO_VERSION_V1,
            )
        _assert_safe_error(raised.value, "EPHEMERAL_PII_VALUE_INVALID")


def test_invalid_dto_never_reaches_aesgcm_decrypt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decrypt_calls: list[object] = []
    real_aesgcm = __import__(
        "cryptography.hazmat.primitives.ciphers.aead", fromlist=["AESGCM"]
    ).AESGCM

    class _TrackingAESGCM:
        def __init__(self, key: bytes) -> None:
            self._inner = real_aesgcm(key)

        def encrypt(self, nonce: bytes, data: bytes, associated_data: bytes | None) -> bytes:
            return self._inner.encrypt(nonce, data, associated_data)

        def decrypt(self, nonce: bytes, data: bytes, associated_data: bytes | None) -> bytes:
            decrypt_calls.append(nonce)
            return self._inner.decrypt(nonce, data, associated_data)

    monkeypatch.setattr("app.core.ephemeral_pii_crypto.AESGCM", _TrackingAESGCM)
    provider = _provider()
    aad = _aad()
    with pytest.raises(EphemeralPiiError):
        EphemeralPiiCiphertext(
            ciphertext=b"\x00" * 15,
            nonce=b"\x00" * 12,
            key_id="K1",
            crypto_version=CRYPTO_VERSION_V1,
        )
    assert decrypt_calls == []
    with pytest.raises(EphemeralPiiError) as raised:
        decrypt_text(object(), aad=aad, key_provider=provider)  # type: ignore[arg-type]
    _assert_safe_error(raised.value, "EPHEMERAL_PII_CONFIG_INVALID")
    assert decrypt_calls == []


def test_decrypt_unifies_recorded_key_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encrypt_provider = _provider()
    aad = _aad(key_id="K1")
    encrypted = encrypt_text(
        _SYNTHETIC_PHONE, aad=aad, key_provider=encrypt_provider
    )
    decrypt_calls: list[object] = []
    real_aesgcm = __import__(
        "cryptography.hazmat.primitives.ciphers.aead", fromlist=["AESGCM"]
    ).AESGCM

    class _TrackingAESGCM:
        def __init__(self, key: bytes) -> None:
            self._inner = real_aesgcm(key)

        def encrypt(self, nonce: bytes, data: bytes, associated_data: bytes | None) -> bytes:
            return self._inner.encrypt(nonce, data, associated_data)

        def decrypt(self, nonce: bytes, data: bytes, associated_data: bytes | None) -> bytes:
            decrypt_calls.append(nonce)
            return self._inner.decrypt(nonce, data, associated_data)

    monkeypatch.setattr("app.core.ephemeral_pii_crypto.AESGCM", _TrackingAESGCM)

    short_b64 = base64.urlsafe_b64encode(b"\x00" * 16).decode("ascii")
    other_b64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    secret_marker = "SYNTHETIC_PROVIDER_SECRET_MARKER"

    scenarios: list[tuple[object, bool]] = [
        (EnvEphemeralPiiKeyProvider({"EPHEMERAL_PII_ACTIVE_KEY_ID": "K2"}), True),
        (
            EnvEphemeralPiiKeyProvider(
                {
                    "EPHEMERAL_PII_ACTIVE_KEY_ID": "K1",
                    "EPHEMERAL_PII_KEY_K1": "!!!not-base64!!!",
                }
            ),
            True,
        ),
        (
            EnvEphemeralPiiKeyProvider(
                {
                    "EPHEMERAL_PII_ACTIVE_KEY_ID": "K1",
                    "EPHEMERAL_PII_KEY_K1": short_b64,
                }
            ),
            True,
        ),
        (
            EnvEphemeralPiiKeyProvider(
                {
                    "EPHEMERAL_PII_ACTIVE_KEY_ID": "K1",
                    "EPHEMERAL_PII_KEY_K1": other_b64,
                }
            ),
            False,
        ),
        (_RaisingProvider(RuntimeError(secret_marker)), True),
    ]

    for provider, expect_no_decrypt in scenarios:
        decrypt_calls.clear()
        tracking = provider
        if isinstance(provider, EnvEphemeralPiiKeyProvider):
            tracking = _TrackingProvider(provider)
        with pytest.raises(EphemeralPiiError) as raised:
            decrypt_text(encrypted, aad=aad, key_provider=tracking)  # type: ignore[arg-type]
        _assert_safe_error(
            raised.value,
            "EPHEMERAL_PII_ACCESS_DENIED",
            secret_marker,
            "!!!not-base64!!!",
            short_b64,
            other_b64,
            _KEY_K1_B64,
            _SYNTHETIC_PHONE,
        )
        assert tracking.active_calls == 0  # type: ignore[attr-defined]
        assert tracking.get_key_calls == 1  # type: ignore[attr-defined]
        if expect_no_decrypt:
            assert decrypt_calls == []
        else:
            # Wrong-but-valid key reaches AESGCM and fails authentication.
            assert len(decrypt_calls) == 1

    for fatal in (KeyboardInterrupt(), SystemExit(9)):
        provider = _RaisingProvider(fatal)
        with pytest.raises(type(fatal)):
            decrypt_text(encrypted, aad=aad, key_provider=provider)  # type: ignore[arg-type]
        assert provider.active_calls == 0
        assert provider.get_key_calls == 1


def test_direct_provider_get_key_keeps_config_codes() -> None:
    missing = EnvEphemeralPiiKeyProvider({"EPHEMERAL_PII_ACTIVE_KEY_ID": "K1"})
    with pytest.raises(EphemeralPiiError) as raised:
        missing.get_key("K1")
    assert raised.value.code == "EPHEMERAL_PII_KEY_UNAVAILABLE"

    malformed = EnvEphemeralPiiKeyProvider(
        {
            "EPHEMERAL_PII_ACTIVE_KEY_ID": "K1",
            "EPHEMERAL_PII_KEY_K1": "!!!not-base64!!!",
        }
    )
    with pytest.raises(EphemeralPiiError) as raised:
        malformed.get_key("K1")
    assert raised.value.code == "EPHEMERAL_PII_CONFIG_INVALID"


def test_normal_encrypt_still_builds_valid_dto() -> None:
    encrypted = encrypt_text(_SYNTHETIC_PHONE, aad=_aad(), key_provider=_provider())
    assert type(encrypted.nonce) is bytes
    assert len(encrypted.nonce) == NONCE_SIZE_BYTES
    assert type(encrypted.ciphertext) is bytes
    assert len(encrypted.ciphertext) >= MIN_CIPHERTEXT_BYTES


class _SnapshotTrackingProvider:
    def __init__(self, env: dict[str, str]) -> None:
        self._env = env
        self.active_calls = 0
        self.get_key_calls = 0

    def active_key_id(self) -> str:
        self.active_calls += 1
        return EnvEphemeralPiiKeyProvider(self._env).active_key_id()

    def get_key(self, key_id: str) -> bytes:
        self.get_key_calls += 1
        return EnvEphemeralPiiKeyProvider(self._env).get_key(key_id)

    def get_active_key(self) -> ActiveEphemeralPiiKey:
        return EnvEphemeralPiiKeyProvider(self._env).get_active_key()


def test_active_key_snapshot_encrypt_skips_provider_reads() -> None:
    provider = _provider()
    active = provider.get_active_key()
    aad = _aad(key_id=active.key_id)
    tracker = _SnapshotTrackingProvider(
        _env_for(("K1", _KEY_K1_B64), ("K2", _KEY_K2_B64), active="K2")
    )
    encrypted = encrypt_text(
        _SYNTHETIC_PHONE,
        aad=aad,
        key_provider=tracker,  # type: ignore[arg-type]
        active_key=active,
    )
    assert encrypted.key_id == "K1"
    assert tracker.active_calls == 0
    assert tracker.get_key_calls == 0


def test_active_key_snapshot_survives_active_rotation() -> None:
    env = _env_for(("K1", _KEY_K1_B64), ("K2", _KEY_K2_B64), active="K1")
    provider = EnvEphemeralPiiKeyProvider(env)
    active = provider.get_active_key()
    aad = _aad(key_id="K1")
    env["EPHEMERAL_PII_ACTIVE_KEY_ID"] = "K2"
    encrypted = encrypt_text(
        _SYNTHETIC_PHONE,
        aad=aad,
        key_provider=EnvEphemeralPiiKeyProvider(env),
        active_key=active,
    )
    assert encrypted.key_id == "K1"
    assert decrypt_text(encrypted, aad=aad, key_provider=provider) == _SYNTHETIC_PHONE


def test_active_key_mismatch_rejected() -> None:
    provider = _provider()
    active = provider.get_active_key()
    aad = _aad(key_id="K2")
    with pytest.raises(EphemeralPiiError) as raised:
        encrypt_text(
            _SYNTHETIC_PHONE,
            aad=aad,
            key_provider=provider,
            active_key=active,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_CONFIG_INVALID")


def test_active_key_repr_safe() -> None:
    active = ActiveEphemeralPiiKey("K1", _KEY_K1)
    rendered = f"{active!r}{active!s}{active}"
    assert "ActiveEphemeralPiiKey(key_id=<redacted>, key=<redacted>)" in rendered
    assert _KEY_K1_B64 not in rendered
    assert "K1" not in rendered


def test_get_active_key_reads_id_once() -> None:
    calls: list[str] = []

    class _OnceProvider(EnvEphemeralPiiKeyProvider):
        def active_key_id(self) -> str:
            calls.append("active")
            return super().active_key_id()

        def get_key(self, key_id: str) -> bytes:
            calls.append("get")
            return super().get_key(key_id)

    provider = _OnceProvider(_env_for(("K1", _KEY_K1_B64), active="K1"))
    active = provider.get_active_key()
    assert active.key_id == "K1"
    assert calls == ["active", "get"]
