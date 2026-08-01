"""Unit tests for attachment binary AEAD (Stage 1A1)."""

from __future__ import annotations

import base64
import secrets
import traceback
from uuid import uuid4

import pytest

from app.core.attachment_crypto import decrypt_bytes, encrypt_bytes
from app.core.attachment_keys import ActiveAttachmentKey, EnvAttachmentKeyProvider
from app.core.attachment_types import (
    MAX_PLAINTEXT_BYTES,
    AttachmentAad,
    AttachmentCiphertext,
    AttachmentError,
    AttachmentKind,
    AttachmentMime,
    AttachmentPurpose,
)
from tests.attachment_spool_fakes import synthetic_minimal_jpeg


def _key_env() -> dict[str, str]:
    key = secrets.token_bytes(32)
    return {
        "ATTACHMENT_SPOOL_ACTIVE_KEY_ID": "ATTK1",
        "ATTACHMENT_SPOOL_KEY_ATTK1": base64.urlsafe_b64encode(key).decode("ascii"),
    }


def _aad(*, size: int) -> AttachmentAad:
    return AttachmentAad(
        crypto_version=1,
        record_id=uuid4(),
        object_id=uuid4(),
        key_id="ATTK1",
        kind=AttachmentKind.IMAGE,
        conversation_id=uuid4(),
        purpose=AttachmentPurpose.INBOUND_ATTACHMENT_RELAY,
        mime=AttachmentMime.IMAGE_JPEG,
        plaintext_size=size,
    )


def _assert_safe_error(exc: AttachmentError, code: str, *forbidden: str) -> None:
    assert exc.code == code
    blob = str(exc) + repr(exc) + "".join(traceback.format_exception(exc))
    for item in forbidden:
        if item:
            assert item not in blob


def test_binary_roundtrip() -> None:
    provider = EnvAttachmentKeyProvider(_key_env())
    active = provider.get_active_key()
    plaintext = synthetic_minimal_jpeg()
    aad = _aad(size=len(plaintext))
    encrypted = encrypt_bytes(
        plaintext, aad=aad, key_provider=provider, active_key=active
    )
    assert type(encrypted.ciphertext_sha256) is bytes
    assert len(encrypted.ciphertext_sha256) == 32
    out = decrypt_bytes(encrypted, aad=aad, key_provider=provider)
    assert out == plaintext


def test_empty_and_oversized_rejected() -> None:
    provider = EnvAttachmentKeyProvider(_key_env())
    active = provider.get_active_key()
    with pytest.raises(AttachmentError) as raised:
        encrypt_bytes(b"", aad=_aad(size=1), key_provider=provider, active_key=active)
    _assert_safe_error(raised.value, "ATTACHMENT_VALUE_INVALID")
    huge = b"\x00" * (MAX_PLAINTEXT_BYTES + 1)
    with pytest.raises(AttachmentError) as raised:
        encrypt_bytes(
            huge,
            aad=_aad(size=MAX_PLAINTEXT_BYTES + 1),
            key_provider=provider,
            active_key=active,
        )
    assert raised.value.code == "ATTACHMENT_TOO_LARGE"


def test_tamper_ciphertext_nonce_aad() -> None:
    provider = EnvAttachmentKeyProvider(_key_env())
    active = provider.get_active_key()
    plaintext = synthetic_minimal_jpeg()
    aad = _aad(size=len(plaintext))
    encrypted = encrypt_bytes(
        plaintext, aad=aad, key_provider=provider, active_key=active
    )
    bad_ct = AttachmentCiphertext(
        ciphertext=encrypted.ciphertext[:-1] + bytes([encrypted.ciphertext[-1] ^ 1]),
        nonce=encrypted.nonce,
        key_id=encrypted.key_id,
        crypto_version=1,
        ciphertext_sha256=encrypted.ciphertext_sha256,
    )
    with pytest.raises(AttachmentError) as raised:
        decrypt_bytes(bad_ct, aad=aad, key_provider=provider)
    _assert_safe_error(raised.value, "ATTACHMENT_ACCESS_DENIED", plaintext.hex())

    bad_nonce = AttachmentCiphertext(
        ciphertext=encrypted.ciphertext,
        nonce=secrets.token_bytes(12),
        key_id=encrypted.key_id,
        crypto_version=1,
        ciphertext_sha256=encrypted.ciphertext_sha256,
    )
    with pytest.raises(AttachmentError) as raised:
        decrypt_bytes(bad_nonce, aad=aad, key_provider=provider)
    assert raised.value.code == "ATTACHMENT_ACCESS_DENIED"

    other_aad = AttachmentAad(
        crypto_version=1,
        record_id=uuid4(),
        object_id=aad.object_id,
        key_id="ATTK1",
        kind=AttachmentKind.IMAGE,
        conversation_id=aad.conversation_id,
        purpose=AttachmentPurpose.OUTBOUND_ATTACHMENT_DELIVERY,
        mime=AttachmentMime.IMAGE_JPEG,
        plaintext_size=len(plaintext),
    )
    with pytest.raises(AttachmentError) as raised:
        decrypt_bytes(encrypted, aad=other_aad, key_provider=provider)
    assert raised.value.code == "ATTACHMENT_ACCESS_DENIED"


def test_wrong_key_and_active_snapshot_once() -> None:
    env = _key_env()
    other = secrets.token_bytes(32)
    env["ATTACHMENT_SPOOL_KEY_OTHER"] = base64.urlsafe_b64encode(other).decode("ascii")
    provider = EnvAttachmentKeyProvider(env)

    class Counting(EnvAttachmentKeyProvider):
        def __init__(self) -> None:
            super().__init__(env)
            self.calls = 0

        def get_active_key(self) -> ActiveAttachmentKey:
            self.calls += 1
            return super().get_active_key()

    counting = Counting()
    plaintext = synthetic_minimal_jpeg()
    aad = _aad(size=len(plaintext))
    encrypted = encrypt_bytes(
        plaintext,
        aad=aad,
        key_provider=counting,
        active_key=counting.get_active_key(),
    )
    assert counting.calls == 1
    wrong = AttachmentCiphertext(
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        key_id="OTHER",
        crypto_version=1,
        ciphertext_sha256=encrypted.ciphertext_sha256,
    )
    with pytest.raises(AttachmentError) as raised:
        decrypt_bytes(wrong, aad=aad, key_provider=provider)
    # AAD key_id mismatch or decrypt denial
    assert raised.value.code in {
        "ATTACHMENT_ACCESS_DENIED",
        "ATTACHMENT_CONFIG_INVALID",
    }


def test_repr_redacts_and_no_plaintext_hash_api() -> None:
    provider = EnvAttachmentKeyProvider(_key_env())
    active = provider.get_active_key()
    plaintext = synthetic_minimal_jpeg()
    aad = _aad(size=len(plaintext))
    encrypted = encrypt_bytes(
        plaintext, aad=aad, key_provider=provider, active_key=active
    )
    rendered = f"{encrypted!r}{aad!r}{active!r}"
    assert plaintext.hex() not in rendered
    assert "ATTK1" not in rendered
    assert "ciphertext_sha256=<redacted>" in repr(encrypted)
    assert not hasattr(encrypted, "content_sha256")
    assert not hasattr(encrypted, "plaintext_sha256")


def test_active_key_frozen() -> None:
    key = secrets.token_bytes(32)
    active = ActiveAttachmentKey("ATTK1", key)
    with pytest.raises((AttributeError, TypeError)):
        active.key = secrets.token_bytes(32)  # type: ignore[misc]
