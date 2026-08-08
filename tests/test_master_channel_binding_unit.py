"""Unit tests for CURSOR-27 master channel binding validators and contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.master_channel_binding import (
    DEFAULT_CONNECTION_SCOPE,
    MasterBindingChannel,
    MasterChannelBindingError,
    MasterChannelBindingErrorCode,
    normalize_connection_scope,
    normalize_external_account_id,
    require_canonical_master_id,
    require_master_binding_channel,
)

_REPO = Path(__file__).resolve().parents[1]
_MASTER_A = "11111111-1111-4111-8111-111111111111"
_MASTER_B = "22222222-2222-4222-8222-222222222222"


def test_default_scope_constant() -> None:
    assert DEFAULT_CONNECTION_SCOPE == "default"
    assert normalize_connection_scope(DEFAULT_CONNECTION_SCOPE) == "default"


def test_channel_closed_set() -> None:
    assert require_master_binding_channel("synthetic") is MasterBindingChannel.SYNTHETIC
    assert require_master_binding_channel("vk") is MasterBindingChannel.VK
    assert require_master_binding_channel("max") is MasterBindingChannel.MAX
    with pytest.raises(MasterChannelBindingError) as exc:
        require_master_binding_channel("telegram")
    assert exc.value.code == MasterChannelBindingErrorCode.INVALID_INPUT.value


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " leading",
        "trailing ",
        "has space",
        "has\ttab",
        "has\nnewline",
        "x" * 129,
        "кириллица",
        "emoji😀",
    ],
)
def test_external_account_id_rejects_unsafe(raw: str) -> None:
    with pytest.raises(MasterChannelBindingError) as exc:
        normalize_external_account_id(raw)
    assert exc.value.code == "INVALID_INPUT"
    rendered = f"{exc.value!s}{exc.value!r}"
    assert "кириллица" not in rendered
    assert "emoji" not in rendered


def test_external_account_id_no_case_folding() -> None:
    upper = normalize_external_account_id("AccountABC")
    lower = normalize_external_account_id("accountabc")
    assert upper == "AccountABC"
    assert lower == "accountabc"
    assert upper != lower


def test_connection_scope_isolates_same_account_string() -> None:
    a = normalize_connection_scope("conn-a")
    b = normalize_connection_scope("conn-b")
    assert a != b
    assert normalize_external_account_id("user-1") == "user-1"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "NOT-A-UUID",
        "11111111-1111-4111-8111-11111111111G",
        "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE",
        "{11111111-1111-4111-8111-111111111111}",
        "urn:uuid:11111111-1111-4111-8111-111111111111",
        12345,
        None,
    ],
)
def test_master_id_must_be_canonical_uuid(raw: object) -> None:
    with pytest.raises(MasterChannelBindingError) as exc:
        require_canonical_master_id(raw)
    assert exc.value.code == "INVALID_INPUT"
    rendered = f"{exc.value!s}{exc.value!r}"
    assert _MASTER_A not in rendered
    assert "11111111" not in rendered


def test_master_id_accepts_canonical() -> None:
    assert require_canonical_master_id(_MASTER_A) == _MASTER_A
    assert require_canonical_master_id(_MASTER_B) == _MASTER_B


def test_error_repr_is_code_only() -> None:
    err = MasterChannelBindingError("CONFLICT")
    assert repr(err) == "MasterChannelBindingError('CONFLICT')"
    assert str(err) == "CONFLICT"


def test_architecture_no_live_channel_or_name_matching() -> None:
    service = (_REPO / "app/services/master_channel_binding.py").read_text(
        encoding="utf-8"
    )
    core = (_REPO / "app/core/master_channel_binding.py").read_text(encoding="utf-8")
    model = (_REPO / "app/models/master_channel_binding.py").read_text(encoding="utf-8")
    for text in (service, core, model):
        assert "vk_api" not in text.lower()
        assert "httpx" not in text
        assert "prisma" not in text.lower()
        assert "BOT_MODE" not in text
        assert "EMERGENCY_LOCK" not in text
        assert "display_name" not in text
        assert "full_name" not in text
        assert "client_name" not in text
    assert "phone" not in model.lower()
    assert "phone" not in service.lower()
    assert "confirm_selected_slot" not in service


def test_migration_defines_partial_unique_active_identity() -> None:
    migration = (
        _REPO / "alembic/versions/20260807_17_master_bindings.py"
    ).read_text(encoding="utf-8")
    assert "uq_master_channel_bindings_active_identity" in migration
    assert "status = 'ACTIVE'" in migration
    assert "20260801_16_spool_leases" in migration
    assert len("20260807_17_master_bindings") <= 32
    assert "^[!-~]+$" in migration
    assert "printable_ascii" in migration
    assert "[[:graph:]]" not in migration
    assert "no_ws" not in migration
    model = (_REPO / "app/models/master_channel_binding.py").read_text(encoding="utf-8")
    assert "^[!-~]+$" in model
    assert "printable_ascii" in model
    assert "[[:graph:]]" not in model


def test_rebind_outcomes_include_conflict_and_ambiguous() -> None:
    from app.core.master_channel_binding import RebindMasterBindingOutcome

    assert RebindMasterBindingOutcome.CONFLICT.value == "CONFLICT"
    assert RebindMasterBindingOutcome.AMBIGUOUS.value == "AMBIGUOUS"
    assert RebindMasterBindingOutcome.INVALID_INPUT.value == "INVALID_INPUT"


def test_revoked_error_code_documented_as_reserved() -> None:
    core = (_REPO / "app/core/master_channel_binding.py").read_text(encoding="utf-8")
    assert "REVOKED = \"REVOKED\"" in core or "REVOKED = 'REVOKED'" in core
    assert "reserved" in core.lower()
    adr = (_REPO / "docs/adr/017-master-channel-binding.md").read_text(encoding="utf-8")
    assert "REVOKED" in adr
    assert "reserved" in adr.lower()


def test_rebind_uses_atomic_savepoint_not_soft_invalid_after_revoke() -> None:
    service = (_REPO / "app/services/master_channel_binding.py").read_text(
        encoding="utf-8"
    )
    assert "begin_nested()" in service
    assert "_classify_rebind_integrity_race" in service
    rebind_section = service.split("async def rebind", 1)[1].split(
        "async def revoke", 1
    )[0]
    assert "IntegrityError" in rebind_section
    assert "ALREADY_BOUND" in rebind_section
    assert "CONFLICT" in rebind_section
    assert "AMBIGUOUS" in rebind_section
    classifier = service.split("async def _classify_rebind_integrity_race", 1)[1].split(
        "async def revoke", 1
    )[0]
    assert "RebindMasterBindingOutcome.INVALID_INPUT" not in classifier
    assert "ALREADY_BOUND" in classifier
    assert "CONFLICT" in classifier
    assert "AMBIGUOUS" in classifier
