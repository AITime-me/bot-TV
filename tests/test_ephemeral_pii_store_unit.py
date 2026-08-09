"""Unit tests for encrypted ephemeral PII store (Stage 2B)."""

from __future__ import annotations

import base64
import secrets
import traceback
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.ephemeral_pii_keys import ActiveEphemeralPiiKey, EnvEphemeralPiiKeyProvider
from app.core.ephemeral_pii_types import (
    MAX_REFERENCE_COLLISION_RETRIES,
    REFERENCE_DIGEST_BYTES,
    REFERENCE_RAW_BYTES,
    REFERENCE_TOKEN_LENGTH,
    EphemeralPiiError,
    EphemeralPiiHandle,
    EphemeralPiiKind,
    EphemeralPiiPurpose,
    EphemeralPiiReference,
    EphemeralPiiTtlPolicy,
)
from app.services.ephemeral_pii_store import EphemeralPiiStore

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SYNTHETIC_PLAINTEXT = "SYNTHETIC_PHONE_VALUE_FOR_TEST_ONLY"
_KEY_BYTES = secrets.token_bytes(32)
_KEY_B64 = base64.urlsafe_b64encode(_KEY_BYTES).decode("ascii")


def _key_env() -> dict[str, str]:
    return {
        "EPHEMERAL_PII_ACTIVE_KEY_ID": "TESTK1",
        "EPHEMERAL_PII_KEY_TESTK1": _KEY_B64,
    }


def _assert_safe_error(exc: EphemeralPiiError, code: str, *forbidden: str) -> None:
    assert exc.code == code
    assert str(exc) == code
    assert exc.__cause__ is None
    blob = str(exc) + repr(exc) + "".join(traceback.format_exception(exc))
    for item in forbidden:
        if item:
            assert item not in blob


def test_reference_generate_and_parse_round_trip() -> None:
    reference = EphemeralPiiReference.generate()
    token = reference.to_token()
    assert len(token) == REFERENCE_TOKEN_LENGTH
    assert token.endswith("=")
    assert "+" not in token
    assert "/" not in token
    reparsed = EphemeralPiiReference.parse(token)
    assert reparsed == reference
    digest = reference.digest()
    assert type(digest) is bytes
    assert len(digest) == REFERENCE_DIGEST_BYTES
    assert digest != reference.to_token().encode("ascii")


def test_reference_repr_safe() -> None:
    reference = EphemeralPiiReference.generate()
    token = reference.to_token()
    rendered = f"{reference!r}{reference!s}{reference}"
    assert rendered == "EphemeralPiiReference(<redacted>)" * 3
    assert token not in rendered
    assert reference.digest().hex() not in rendered


def test_reference_rejects_invalid_tokens() -> None:
    reference = EphemeralPiiReference.generate()
    token = reference.to_token()
    invalid_cases = [
        "",
        "A" * 43,
        token + "A",
        token.replace("=", "=="),
        token[:20] + "+" + token[21:],
        token[:20] + "/" + token[21:],
        " " + token,
        token + "\n",
        "тест",
        object(),
        b"bytes",
    ]
    for bad in invalid_cases:
        with pytest.raises(EphemeralPiiError) as raised:
            EphemeralPiiReference.parse(bad)  # type: ignore[arg-type]
        _assert_safe_error(raised.value, "EPHEMERAL_PII_REFERENCE_INVALID")


class _HostileStr(str):
    def __str__(self) -> str:
        raise AssertionError("hostile str")

    def __repr__(self) -> str:
        raise AssertionError("hostile repr")


def test_reference_rejects_str_subclass() -> None:
    reference = EphemeralPiiReference.generate()
    with pytest.raises(EphemeralPiiError) as raised:
        EphemeralPiiReference.parse(_HostileStr(reference.to_token()))
    _assert_safe_error(raised.value, "EPHEMERAL_PII_REFERENCE_INVALID")


def test_ttl_policy_validation() -> None:
    assert EphemeralPiiTtlPolicy(900).ttl_seconds == 900
    for bad in (0, -1, 86401, True, object()):
        with pytest.raises(EphemeralPiiError) as raised:
            EphemeralPiiTtlPolicy(bad)  # type: ignore[arg-type]
        _assert_safe_error(raised.value, "EPHEMERAL_PII_POLICY_INVALID")
    assert "900" not in repr(EphemeralPiiTtlPolicy(900))


def test_handle_repr_safe() -> None:
    handle = EphemeralPiiHandle(
        reference=EphemeralPiiReference.generate(),
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    rendered = f"{handle!r}{handle!s}{handle}"
    assert "reference=<redacted>" in rendered
    assert "PHONE" in rendered
    assert "BOOKING_PHONE_WRITE" in rendered
    assert not hasattr(handle, "conversation_id")


def test_static_import_boundaries() -> None:
    from app.models.ephemeral_pii import EphemeralPiiValue

    pii_gateway = (_REPO_ROOT / "app/core/pii_gateway.py").read_text(encoding="utf-8")
    dialog = (_REPO_ROOT / "app/services/dialog_context.py").read_text(encoding="utf-8")
    repo = (_REPO_ROOT / "app/repositories/ephemeral_pii.py").read_text(encoding="utf-8")
    assert "ephemeral_pii_store" not in pii_gateway
    assert "ephemeral_pii_store" not in dialog
    assert "ephemeral_pii_crypto" not in repo
    assert "ephemeral_pii_keys" not in repo
    assert "plaintext" not in EphemeralPiiValue.__table__.columns
    assert "raw_reference" not in EphemeralPiiValue.__table__.columns
    assert "reference_token" not in EphemeralPiiValue.__table__.columns


def test_locked_row_repr_redacts_sensitive_fields() -> None:
    from app.repositories.ephemeral_pii import EphemeralPiiLockedRow

    row_id = uuid4()
    conversation_id = uuid4()
    ciphertext = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    row = EphemeralPiiLockedRow(
        id=row_id,
        conversation_id=conversation_id,
        pii_kind=EphemeralPiiKind.PHONE.value,
        allowed_purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE.value,
        ciphertext=ciphertext,
        nonce=nonce,
        key_id="TESTK1",
        crypto_version=1,
    )
    rendered = f"{row!r}{row!s}{row}"
    assert ciphertext.hex() not in rendered
    assert nonce.hex() not in rendered
    assert "TESTK1" not in rendered
    assert str(row_id) not in rendered
    assert str(conversation_id) not in rendered
    assert "PHONE" in rendered
    assert "BOOKING_PHONE_WRITE" in rendered


def test_active_key_frozen_rejects_assignment() -> None:
    active = ActiveEphemeralPiiKey("TESTK1", _KEY_BYTES)
    with pytest.raises((AttributeError, TypeError)):
        active.key = secrets.token_bytes(32)  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        active.key_id = "OTHER"  # type: ignore[misc]
    rendered = f"{active!r}{active!s}{active}"
    assert _KEY_BYTES.hex() not in rendered
    assert _KEY_B64 not in rendered
    assert "TESTK1" not in rendered
    assert "key_id=<redacted>" in rendered
    assert "key=<redacted>" in rendered


from app.core.ephemeral_pii_crypto import EphemeralPiiCiphertext
from tests.ephemeral_pii_store_fakes import (
    TxnTracker,
    make_observing_session_scope,
    sample_locked_row,
)


class _CountingKeyProvider(EnvEphemeralPiiKeyProvider):
    def __init__(self, environ: dict[str, str]) -> None:
        super().__init__(environ)
        self.active_key_calls = 0

    def get_active_key(self) -> ActiveEphemeralPiiKey:
        self.active_key_calls += 1
        return super().get_active_key()


def _store_with_tracker(
    tracker: TxnTracker,
    *,
    key_provider: EnvEphemeralPiiKeyProvider | None = None,
    reference_factory: Any | None = None,
) -> EphemeralPiiStore:
    return EphemeralPiiStore(
        session_factory=object(),  # type: ignore[arg-type]
        key_provider=key_provider or EnvEphemeralPiiKeyProvider(_key_env()),
        ttl_policy=EphemeralPiiTtlPolicy(900),
        reference_factory=reference_factory,
    )


@pytest.mark.asyncio
async def test_store_collision_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}
    tracker = TxnTracker()
    references = [
        EphemeralPiiReference.generate(),
        EphemeralPiiReference.generate(),
        EphemeralPiiReference.generate(),
    ]
    digests: list[bytes] = []

    async def _fake_insert(*_args: Any, **kwargs: Any) -> bool:
        attempts["count"] += 1
        digests.append(kwargs["reference_digest"])
        return attempts["count"] >= 2

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.insert_if_reference_available",
        _fake_insert,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(tracker),
    )
    store = _store_with_tracker(
        tracker,
        reference_factory=lambda: references.pop(0),
    )
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=uuid4(),
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    assert isinstance(handle, EphemeralPiiHandle)
    assert attempts["count"] == 2
    assert digests[0] != digests[1]
    assert tracker.events == ["enter", "commit", "exit"]


@pytest.mark.asyncio
async def test_store_collision_exhaustion_raises_store_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = TxnTracker()

    async def _always_false(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.insert_if_reference_available",
        _always_false,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(tracker),
    )
    store = _store_with_tracker(tracker)
    with pytest.raises(EphemeralPiiError) as raised:
        await store.store(
            _SYNTHETIC_PLAINTEXT,
            conversation_id=uuid4(),
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_STORE_FAILED", _SYNTHETIC_PLAINTEXT)
    assert tracker.events == ["enter", "commit", "exit"]


@pytest.mark.asyncio
async def test_store_plaintext_never_reaches_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = TxnTracker()
    captured: list[dict[str, Any]] = []
    encrypt_calls = {"count": 0}

    async def _capture_insert(*_args: Any, **kwargs: Any) -> bool:
        captured.append(kwargs)
        return True

    def _spy_encrypt(value: str, **kwargs: Any) -> EphemeralPiiCiphertext:
        encrypt_calls["count"] += 1
        assert value == _SYNTHETIC_PLAINTEXT
        from app.core.ephemeral_pii_crypto import encrypt_text as real_encrypt

        return real_encrypt(value, **kwargs)

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.encrypt_text",
        _spy_encrypt,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.insert_if_reference_available",
        _capture_insert,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(tracker),
    )
    provider = _CountingKeyProvider(_key_env())
    store = _store_with_tracker(tracker, key_provider=provider)
    handle = await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=uuid4(),
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    assert handle.reference.to_token()
    assert encrypt_calls["count"] == 1
    assert provider.active_key_calls == 1
    assert len(captured) == 1
    assert "plaintext" not in captured[0]
    assert _SYNTHETIC_PLAINTEXT not in captured[0].values()
    token = handle.reference.to_token()
    assert token not in captured[0].values()
    assert tracker.events[-2] == "commit"


@pytest.mark.asyncio
async def test_store_encrypt_not_retried_on_digest_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = TxnTracker()
    encrypt_calls = {"count": 0}
    attempts = {"count": 0}

    async def _fake_insert(*_args: Any, **_kwargs: Any) -> bool:
        attempts["count"] += 1
        return attempts["count"] >= 2

    def _spy_encrypt(value: str, **kwargs: Any) -> EphemeralPiiCiphertext:
        encrypt_calls["count"] += 1
        from app.core.ephemeral_pii_crypto import encrypt_text as real_encrypt

        return real_encrypt(value, **kwargs)

    monkeypatch.setattr("app.services.ephemeral_pii_store.encrypt_text", _spy_encrypt)
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.insert_if_reference_available",
        _fake_insert,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(tracker),
    )
    store = _store_with_tracker(tracker)
    await store.store(
        _SYNTHETIC_PLAINTEXT,
        conversation_id=uuid4(),
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    assert encrypt_calls["count"] == 1
    assert attempts["count"] == 2


@pytest.mark.asyncio
async def test_store_non_collision_insert_failure_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = TxnTracker()
    attempts = {"count": 0}

    async def _fail_insert(*_args: Any, **_kwargs: Any) -> bool:
        attempts["count"] += 1
        raise RuntimeError("synthetic db failure")

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.insert_if_reference_available",
        _fail_insert,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(tracker),
    )
    store = _store_with_tracker(tracker)
    with pytest.raises(EphemeralPiiError) as raised:
        await store.store(
            _SYNTHETIC_PLAINTEXT,
            conversation_id=uuid4(),
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_STORE_FAILED")
    assert attempts["count"] == 1
    assert tracker.events == ["enter", "rollback", "exit"]


@pytest.mark.asyncio
async def test_store_commit_failure_does_not_return_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = TxnTracker(fail_commit=True)

    async def _insert_ok(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.insert_if_reference_available",
        _insert_ok,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(tracker),
    )
    store = _store_with_tracker(tracker)
    with pytest.raises(EphemeralPiiError) as raised:
        await store.store(
            _SYNTHETIC_PLAINTEXT,
            conversation_id=uuid4(),
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_STORE_FAILED")
    assert tracker.events == ["enter", "rollback", "exit"]


@pytest.mark.asyncio
async def test_store_keyboard_interrupt_not_converted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = TxnTracker()

    async def _raise_ki(*_args: Any, **_kwargs: Any) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.insert_if_reference_available",
        _raise_ki,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(tracker),
    )
    store = _store_with_tracker(tracker)
    with pytest.raises(KeyboardInterrupt):
        await store.store(
            _SYNTHETIC_PLAINTEXT,
            conversation_id=uuid4(),
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )


@pytest.mark.asyncio
async def test_consume_commit_before_plaintext_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = TxnTracker()
    conversation_id = uuid4()
    locked = sample_locked_row(conversation_id=conversation_id)
    order: list[str] = []

    async def _select(*_args: Any, **_kwargs: Any) -> Any:
        return locked

    async def _delete(*_args: Any, **_kwargs: Any) -> None:
        order.append("delete")

    def _decrypt(*_args: Any, **_kwargs: Any) -> str:
        order.append("decrypt")
        return _SYNTHETIC_PLAINTEXT

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _select,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.delete_locked_row",
        _delete,
    )
    monkeypatch.setattr("app.services.ephemeral_pii_store.decrypt_text", _decrypt)
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(tracker),
    )
    store = _store_with_tracker(tracker)
    plaintext = await store.consume_once(
        EphemeralPiiReference.generate(),
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    assert plaintext == _SYNTHETIC_PLAINTEXT
    assert order == ["decrypt", "delete"]
    assert tracker.events == ["enter", "commit", "exit"]


@pytest.mark.asyncio
async def test_read_plaintext_decrypts_without_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = TxnTracker()
    conversation_id = uuid4()
    locked = sample_locked_row(conversation_id=conversation_id)
    order: list[str] = []
    delete_calls = {"count": 0}

    async def _select(*_args: Any, **_kwargs: Any) -> Any:
        order.append("select_for_read")
        return locked

    async def _delete(*_args: Any, **_kwargs: Any) -> None:
        delete_calls["count"] += 1

    def _decrypt(*_args: Any, **_kwargs: Any) -> str:
        order.append("decrypt")
        return _SYNTHETIC_PLAINTEXT

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_read",
        _select,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.delete_locked_row",
        _delete,
    )
    monkeypatch.setattr("app.services.ephemeral_pii_store.decrypt_text", _decrypt)
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(tracker),
    )
    store = _store_with_tracker(tracker)
    plaintext = await store.read_plaintext(
        EphemeralPiiReference.generate(),
        conversation_id=conversation_id,
        kind=EphemeralPiiKind.PHONE,
        purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
    )
    assert plaintext == _SYNTHETIC_PLAINTEXT
    assert order == ["select_for_read", "decrypt"]
    assert delete_calls["count"] == 0
    assert tracker.events == ["enter", "commit", "exit"]


@pytest.mark.asyncio
async def test_read_plaintext_wrong_purpose_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = sample_locked_row()
    delete_calls = {"count": 0}

    async def _select(*_args: Any, **_kwargs: Any) -> Any:
        return locked

    async def _delete(*_args: Any, **_kwargs: Any) -> None:
        delete_calls["count"] += 1

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_read",
        _select,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.delete_locked_row",
        _delete,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    store = _store_with_tracker(TxnTracker())
    with pytest.raises(EphemeralPiiError) as raised:
        await store.read_plaintext(
            EphemeralPiiReference.generate(),
            conversation_id=locked.conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.MASTER_BOOKING_CLIENT_WRITE,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED")
    assert delete_calls["count"] == 0


@pytest.mark.asyncio
async def test_consume_missing_row_access_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = TxnTracker()

    async def _none(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _none,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(tracker),
    )
    store = _store_with_tracker(tracker)
    with pytest.raises(EphemeralPiiError) as raised:
        await store.consume_once(
            EphemeralPiiReference.generate(),
            conversation_id=uuid4(),
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED")


@pytest.mark.asyncio
async def test_consume_wrong_binding_access_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    locked = sample_locked_row()
    delete_calls = {"count": 0}
    decrypt_calls = {"count": 0}

    async def _select(*_args: Any, **_kwargs: Any) -> Any:
        return locked

    async def _delete(*_args: Any, **_kwargs: Any) -> None:
        delete_calls["count"] += 1

    def _decrypt(*_args: Any, **_kwargs: Any) -> str:
        decrypt_calls["count"] += 1
        return _SYNTHETIC_PLAINTEXT

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _select,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.delete_locked_row",
        _delete,
    )
    monkeypatch.setattr("app.services.ephemeral_pii_store.decrypt_text", _decrypt)
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    store = _store_with_tracker(TxnTracker())
    with pytest.raises(EphemeralPiiError) as raised:
        await store.consume_once(
            EphemeralPiiReference.generate(),
            conversation_id=uuid4(),
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED")
    assert decrypt_calls["count"] == 0
    assert delete_calls["count"] == 0


@pytest.mark.asyncio
async def test_consume_crypto_version_mismatch_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = sample_locked_row(crypto_version=2)
    decrypt_calls = {"count": 0}

    async def _select(*_args: Any, **_kwargs: Any) -> Any:
        return locked

    def _decrypt(*_args: Any, **_kwargs: Any) -> str:
        decrypt_calls["count"] += 1
        return _SYNTHETIC_PLAINTEXT

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _select,
    )
    monkeypatch.setattr("app.services.ephemeral_pii_store.decrypt_text", _decrypt)
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    store = _store_with_tracker(TxnTracker())
    with pytest.raises(EphemeralPiiError) as raised:
        await store.consume_once(
            EphemeralPiiReference.generate(),
            conversation_id=locked.conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED")
    assert decrypt_calls["count"] == 0


@pytest.mark.asyncio
async def test_consume_decrypt_failure_does_not_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = sample_locked_row()
    delete_calls = {"count": 0}

    async def _select(*_args: Any, **_kwargs: Any) -> Any:
        return locked

    async def _delete(*_args: Any, **_kwargs: Any) -> None:
        delete_calls["count"] += 1

    def _decrypt(*_args: Any, **_kwargs: Any) -> str:
        raise EphemeralPiiError("EPHEMERAL_PII_ACCESS_DENIED")

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _select,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.delete_locked_row",
        _delete,
    )
    monkeypatch.setattr("app.services.ephemeral_pii_store.decrypt_text", _decrypt)
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    store = _store_with_tracker(TxnTracker())
    with pytest.raises(EphemeralPiiError) as raised:
        await store.consume_once(
            EphemeralPiiReference.generate(),
            conversation_id=locked.conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED")
    assert delete_calls["count"] == 0


@pytest.mark.asyncio
async def test_delete_crypto_version_mismatch_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = sample_locked_row(crypto_version=2)
    delete_calls = {"count": 0}

    async def _select(*_args: Any, **_kwargs: Any) -> Any:
        return locked

    async def _delete(*_args: Any, **_kwargs: Any) -> None:
        delete_calls["count"] += 1

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _select,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.delete_locked_row",
        _delete,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    store = _store_with_tracker(TxnTracker())
    with pytest.raises(EphemeralPiiError) as raised:
        await store.delete(
            EphemeralPiiReference.generate(),
            conversation_id=locked.conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED")
    assert delete_calls["count"] == 0


@pytest.mark.asyncio
async def test_purge_limit_validation() -> None:
    store = _store_with_tracker(TxnTracker())
    for bad in (0, -1, 1001, True, object()):
        with pytest.raises(EphemeralPiiError) as raised:
            await store.purge_expired(limit=bad)  # type: ignore[arg-type]
        _assert_safe_error(raised.value, "EPHEMERAL_PII_POLICY_INVALID")


@pytest.mark.asyncio
async def test_purge_returns_count_after_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = TxnTracker()

    async def _purge(*_args: Any, **_kwargs: Any) -> int:
        return 3

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.purge_expired_batch",
        _purge,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(tracker),
    )
    store = _store_with_tracker(tracker)
    count = await store.purge_expired(limit=10)
    assert count == 3
    assert tracker.events == ["enter", "commit", "exit"]


@pytest.mark.asyncio
async def test_purge_failure_maps_to_purge_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = TxnTracker()

    async def _fail(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("db")

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.purge_expired_batch",
        _fail,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(tracker),
    )
    store = _store_with_tracker(tracker)
    with pytest.raises(EphemeralPiiError) as raised:
        await store.purge_expired(limit=1)
    _assert_safe_error(raised.value, "EPHEMERAL_PII_PURGE_FAILED")


def test_reference_collision_retry_limit_constant() -> None:
    assert MAX_REFERENCE_COLLISION_RETRIES == 3


def test_active_key_immutable_pair() -> None:
    active = ActiveEphemeralPiiKey("TESTK1", _KEY_BYTES)
    assert active.key_id == "TESTK1"
    assert active.key == _KEY_BYTES
    assert "TESTK1" not in repr(active)


@pytest.mark.asyncio
async def test_consume_post_lock_expiry_none_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repository None after post-lock expiry must fail closed at service."""
    decrypt_calls = {"count": 0}
    delete_calls = {"count": 0}

    async def _none_after_post_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    def _decrypt(*_args: Any, **_kwargs: Any) -> str:
        decrypt_calls["count"] += 1
        return _SYNTHETIC_PLAINTEXT

    async def _delete(*_args: Any, **_kwargs: Any) -> None:
        delete_calls["count"] += 1

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _none_after_post_lock,
    )
    monkeypatch.setattr("app.services.ephemeral_pii_store.decrypt_text", _decrypt)
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.delete_locked_row",
        _delete,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    store = _store_with_tracker(TxnTracker())
    with pytest.raises(EphemeralPiiError) as raised:
        await store.consume_once(
            EphemeralPiiReference.generate(),
            conversation_id=uuid4(),
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED", _SYNTHETIC_PLAINTEXT)
    assert decrypt_calls["count"] == 0
    assert delete_calls["count"] == 0


@pytest.mark.asyncio
async def test_delete_post_lock_expiry_none_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delete_calls = {"count": 0}

    async def _none_after_post_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _delete(*_args: Any, **_kwargs: Any) -> None:
        delete_calls["count"] += 1

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _none_after_post_lock,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.delete_locked_row",
        _delete,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    store = _store_with_tracker(TxnTracker())
    with pytest.raises(EphemeralPiiError) as raised:
        await store.delete(
            EphemeralPiiReference.generate(),
            conversation_id=uuid4(),
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED")
    assert delete_calls["count"] == 0


@pytest.mark.asyncio
async def test_consume_select_keyboard_interrupt_not_converted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_ki(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _raise_ki,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    store = _store_with_tracker(TxnTracker())
    with pytest.raises(KeyboardInterrupt):
        await store.consume_once(
            EphemeralPiiReference.generate(),
            conversation_id=uuid4(),
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )


@pytest.mark.asyncio
async def test_consume_commit_failure_does_not_return_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = TxnTracker(fail_commit=True)
    locked = sample_locked_row()

    async def _select(*_args: Any, **_kwargs: Any) -> Any:
        return locked

    async def _delete(*_args: Any, **_kwargs: Any) -> None:
        return None

    def _decrypt(*_args: Any, **_kwargs: Any) -> str:
        return _SYNTHETIC_PLAINTEXT

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _select,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.delete_locked_row",
        _delete,
    )
    monkeypatch.setattr("app.services.ephemeral_pii_store.decrypt_text", _decrypt)
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(tracker),
    )
    store = _store_with_tracker(tracker)
    with pytest.raises(EphemeralPiiError) as raised:
        await store.consume_once(
            EphemeralPiiReference.generate(),
            conversation_id=locked.conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_STORE_FAILED", _SYNTHETIC_PLAINTEXT)
    assert tracker.events == ["enter", "rollback", "exit"]


@pytest.mark.asyncio
async def test_consume_infrastructure_failure_maps_to_store_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail_select(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("synthetic db failure")

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _fail_select,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    store = _store_with_tracker(TxnTracker())
    with pytest.raises(EphemeralPiiError) as raised:
        await store.consume_once(
            EphemeralPiiReference.generate(),
            conversation_id=uuid4(),
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_STORE_FAILED", _SYNTHETIC_PLAINTEXT)


@pytest.mark.asyncio
async def test_consume_system_exit_not_converted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_se(*_args: Any, **_kwargs: Any) -> None:
        raise SystemExit

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _raise_se,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    store = _store_with_tracker(TxnTracker())
    with pytest.raises(SystemExit):
        await store.consume_once(
            EphemeralPiiReference.generate(),
            conversation_id=uuid4(),
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )


@pytest.mark.asyncio
async def test_delete_wrong_conversation_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = sample_locked_row()
    delete_calls = {"count": 0}

    async def _select(*_args: Any, **_kwargs: Any) -> Any:
        return locked

    async def _delete(*_args: Any, **_kwargs: Any) -> None:
        delete_calls["count"] += 1

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _select,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.delete_locked_row",
        _delete,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    store = _store_with_tracker(TxnTracker())
    with pytest.raises(EphemeralPiiError) as raised:
        await store.delete(
            EphemeralPiiReference.generate(),
            conversation_id=uuid4(),
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED")
    assert delete_calls["count"] == 0


@pytest.mark.asyncio
async def test_delete_wrong_purpose_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = sample_locked_row()
    delete_calls = {"count": 0}

    async def _select(*_args: Any, **_kwargs: Any) -> Any:
        return locked

    async def _delete(*_args: Any, **_kwargs: Any) -> None:
        delete_calls["count"] += 1

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _select,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.delete_locked_row",
        _delete,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    store = _store_with_tracker(TxnTracker())
    with pytest.raises(EphemeralPiiError) as raised:
        await store.delete(
            EphemeralPiiReference.generate(),
            conversation_id=locked.conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.AMOCRM_CONTACT_SYNC,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED")
    assert delete_calls["count"] == 0


@pytest.mark.asyncio
async def test_delete_wrong_kind_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = sample_locked_row(pii_kind="TAMPERED_KIND")
    delete_calls = {"count": 0}

    async def _select(*_args: Any, **_kwargs: Any) -> Any:
        return locked

    async def _delete(*_args: Any, **_kwargs: Any) -> None:
        delete_calls["count"] += 1

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _select,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.delete_locked_row",
        _delete,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    store = _store_with_tracker(TxnTracker())
    with pytest.raises(EphemeralPiiError) as raised:
        await store.delete(
            EphemeralPiiReference.generate(),
            conversation_id=locked.conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_ACCESS_DENIED")
    assert delete_calls["count"] == 0


@pytest.mark.asyncio
async def test_delete_commit_failure_maps_to_store_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = TxnTracker(fail_commit=True)
    locked = sample_locked_row()

    async def _select(*_args: Any, **_kwargs: Any) -> Any:
        return locked

    async def _delete(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _select,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.delete_locked_row",
        _delete,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(tracker),
    )
    store = _store_with_tracker(tracker)
    with pytest.raises(EphemeralPiiError) as raised:
        await store.delete(
            EphemeralPiiReference.generate(),
            conversation_id=locked.conversation_id,
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )
    _assert_safe_error(raised.value, "EPHEMERAL_PII_STORE_FAILED")
    assert tracker.events == ["enter", "rollback", "exit"]


@pytest.mark.asyncio
async def test_delete_keyboard_interrupt_not_converted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_ki(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.ephemeral_pii_repo.select_for_consume",
        _raise_ki,
    )
    monkeypatch.setattr(
        "app.services.ephemeral_pii_store.session_scope",
        make_observing_session_scope(TxnTracker()),
    )
    store = _store_with_tracker(TxnTracker())
    with pytest.raises(KeyboardInterrupt):
        await store.delete(
            EphemeralPiiReference.generate(),
            conversation_id=uuid4(),
            kind=EphemeralPiiKind.PHONE,
            purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE,
        )


def test_production_never_serializes_locked_row_with_asdict() -> None:
    for py_file in (_REPO_ROOT / "app").rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        if "EphemeralPiiLockedRow" not in content:
            continue
        assert "asdict" not in content
        assert "model_dump" not in content
        assert "to_dict" not in content


def test_locked_row_asdict_would_expose_internal_fields() -> None:
    """Documents residual risk: generic asdict bypasses redacted repr."""
    from dataclasses import asdict

    from app.repositories.ephemeral_pii import EphemeralPiiLockedRow

    secret = secrets.token_bytes(16)
    row = EphemeralPiiLockedRow(
        id=uuid4(),
        conversation_id=uuid4(),
        pii_kind=EphemeralPiiKind.PHONE.value,
        allowed_purpose=EphemeralPiiPurpose.BOOKING_PHONE_WRITE.value,
        ciphertext=secret,
        nonce=secrets.token_bytes(12),
        key_id="TESTK1",
        crypto_version=1,
    )
    exported = asdict(row)
    assert exported["ciphertext"] == secret
    assert secret.hex() not in repr(row)
