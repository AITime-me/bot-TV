"""Unit tests for CURSOR-30 Identity Resolution (no PostgreSQL)."""

from __future__ import annotations

import inspect
import logging
import uuid
from pathlib import Path

import pytest

from app.core.identity_provider_port import (
    ExternalEntityRef,
    IdentityExternalLookupPort,
)
from app.core.identity_resolution import (
    AttachIdentityLinkOutcome,
    AttachIdentityLinkResult,
    IdentityEntityKind,
    IdentityLinkConfidence,
    IdentityLinkRecord,
    IdentityLinkStatus,
    IdentityResolutionError,
    IdentityResolutionErrorCode,
    IdentityResolveSignals,
    REASON_EMAIL_ONLY_SECONDARY,
    ReconcileBuyerCardOutcome,
    ReconcileBuyerCardResult,
    ResolveIdentityOutcome,
    ResolveIdentityResult,
    normalize_email,
    normalize_phone_e164,
    require_canonical_identity_id,
)

_REPO = Path(__file__).resolve().parents[1]
_PHONE = "+79001234567"
_EMAIL = "client@example.com"


def test_phone_normalization_deterministic() -> None:
    assert normalize_phone_e164("+7 (900) 123-45-67") == _PHONE
    assert normalize_phone_e164("89001234567") == _PHONE
    assert normalize_phone_e164("9001234567") == _PHONE
    assert normalize_phone_e164(_PHONE) == _PHONE


@pytest.mark.parametrize(
    "raw",
    ["", "123", "abc", "+7900", "790012345678901234", "not-a-phone", None, 1],
)
def test_phone_invalid_fail_closed(raw: object) -> None:
    with pytest.raises(IdentityResolutionError) as exc:
        normalize_phone_e164(raw)  # type: ignore[arg-type]
    assert exc.value.code == IdentityResolutionErrorCode.INVALID_INPUT.value
    assert _PHONE not in repr(exc.value)
    assert "900" not in str(exc.value)


def test_email_normalization_conservative() -> None:
    assert normalize_email("  Client@Example.COM ") == _EMAIL


@pytest.mark.parametrize(
    "raw",
    ["", "no-at", "@x.com", "a@b", "a b@c.com", "a@b..com", None, 1],
)
def test_email_invalid_fail_closed(raw: object) -> None:
    with pytest.raises(IdentityResolutionError) as exc:
        normalize_email(raw)  # type: ignore[arg-type]
    assert exc.value.code == IdentityResolutionErrorCode.INVALID_INPUT.value
    assert "example" not in repr(exc.value).lower()


def test_canonical_identity_id_must_be_lowercase_uuid() -> None:
    good = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    assert require_canonical_identity_id(good) == good
    with pytest.raises(IdentityResolutionError):
        require_canonical_identity_id(good.upper())
    with pytest.raises(IdentityResolutionError):
        require_canonical_identity_id("not-a-uuid")


def test_resolve_signals_have_no_name_matching_fields() -> None:
    forbidden = {
        "name",
        "client_name",
        "display_name",
        "full_name",
        "fio",
        "free_text",
        "text",
        "message_text",
    }
    fields = set(IdentityResolveSignals.__dataclass_fields__)
    assert fields.isdisjoint(forbidden)
    sig = inspect.signature(IdentityResolveSignals)
    assert set(sig.parameters).isdisjoint(forbidden)


def test_ast_identity_resolve_forbids_name_matching_paths() -> None:
    """Fail if name/client_name becomes a resolve matching input or attribute path."""

    import ast

    forbidden_attrs = {
        "name",
        "client_name",
        "display_name",
        "full_name",
        "fio",
        "free_text",
    }
    core_path = _REPO / "app/core/identity_resolution.py"
    service_path = _REPO / "app/services/identity_resolution.py"
    core_tree = ast.parse(core_path.read_text(encoding="utf-8"))
    service_tree = ast.parse(service_path.read_text(encoding="utf-8"))

    # IdentityResolveSignals dataclass must not grow forbidden fields.
    for node in core_tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "IdentityResolveSignals":
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(
                    item.target, ast.Name
                ):
                    assert item.target.id not in forbidden_attrs, item.target.id

    # resolve/_resolve_validated must not read forbidden attrs from signals.
    for node in ast.walk(service_tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr not in forbidden_attrs:
            continue
        # Allow only non-signals contexts; ban signals.<forbidden>
        if isinstance(node.value, ast.Name) and node.value.id == "signals":
            raise AssertionError(
                f"identity resolve must not use signals.{node.attr}"
            )


def test_email_only_secondary_reason_constant_is_safe() -> None:
    assert REASON_EMAIL_ONLY_SECONDARY == "EMAIL_ONLY_SECONDARY"
    assert "@" not in REASON_EMAIL_ONLY_SECONDARY
    result = ResolveIdentityResult(
        outcome=ResolveIdentityOutcome.NOT_FOUND,
        reason=REASON_EMAIL_ONLY_SECONDARY,
    )
    assert result.outcome is ResolveIdentityOutcome.NOT_FOUND
    assert result.canonical_identity_id is None
    assert _EMAIL not in repr(result)


def test_resolve_result_repr_redacts_identity() -> None:
    cid = uuid.uuid4()
    result = ResolveIdentityResult(
        outcome=ResolveIdentityOutcome.RESOLVED,
        canonical_identity_id=cid,
        confidence=IdentityLinkConfidence.CONFIRMED,
        reason="exact_channel_link",
    )
    text = repr(result)
    assert str(cid) not in text
    assert "canonical_identity_id=<redacted>" in text
    assert result.outcome is ResolveIdentityOutcome.RESOLVED


def test_error_never_embeds_pii() -> None:
    err = IdentityResolutionError(IdentityResolutionErrorCode.INVALID_INPUT)
    assert repr(err) == "IdentityResolutionError('INVALID_INPUT')"
    assert str(err) == "INVALID_INPUT"
    assert _PHONE not in repr(err)


def test_link_record_repr_redacts_external_id() -> None:
    record = IdentityLinkRecord(
        link_id=uuid.uuid4(),
        canonical_identity_id=uuid.uuid4(),
        provider="vk",
        connection_scope="vk-group-1",
        entity_kind=IdentityEntityKind.CHANNEL_ACCOUNT,
        external_id="12345",
        status=IdentityLinkStatus.ACTIVE,
        confidence=IdentityLinkConfidence.CONFIRMED,
        source="SYSTEM",
        linked_at=__import__("datetime").datetime.now(
            tz=__import__("datetime").timezone.utc
        ),
        revoked_at=None,
    )
    text = repr(record)
    assert "12345" not in text
    assert "external_id=<redacted>" in text
    assert "vk-group-1" not in text


def test_buyer_card_result_manual_review_has_reason() -> None:
    result = ReconcileBuyerCardResult(
        outcome=ReconcileBuyerCardOutcome.MANUAL_REVIEW_REQUIRED,
        reason="ambiguous_buyer_cards",
    )
    assert "ambiguous_buyer_cards" in repr(result)
    assert result.buyer_card_external_id is None


def test_attach_result_success_requires_link() -> None:
    with pytest.raises(TypeError):
        AttachIdentityLinkResult(outcome=AttachIdentityLinkOutcome.LINKED)


def test_provider_port_is_protocol_only() -> None:
    assert hasattr(IdentityExternalLookupPort, "lookup_by_external_id")
    integrations = (_REPO / "app/integrations/__init__.py").read_text(
        encoding="utf-8"
    )
    assert "IdentityExternalLookupPort" not in integrations
    assert "amocrm" not in integrations.lower()
    ref = ExternalEntityRef(
        provider="amocrm",
        connection_scope="default",
        entity_kind=IdentityEntityKind.AMOCRM_BUYER_CARD,
        external_id="card-1",
    )
    assert "card-1" not in repr(ref)


def test_migration_defines_partial_unique_active_key() -> None:
    migration = (
        _REPO / "alembic/versions/20260809_19_identity_resolution.py"
    ).read_text(encoding="utf-8")
    assert "uq_external_identity_links_active_key" in migration
    assert "uq_external_identity_links_active_amocrm_deal_role" in migration
    assert "status = 'ACTIVE'" in migration
    assert "AMOCRM_BUYER_CARD" in migration
    assert "AMOCRM_TECHNICAL_DEAL" in migration
    assert "canonical_identities" in migration


def test_service_has_no_network_or_crm_imports() -> None:
    service = (_REPO / "app/services/identity_resolution.py").read_text(
        encoding="utf-8"
    )
    core = (_REPO / "app/core/identity_resolution.py").read_text(encoding="utf-8")
    for banned in (
        "import httpx",
        "import aiohttp",
        "import requests",
        "import urllib",
        "from app.services.amocrm_adapter",
        "messages.send",
    ):
        assert banned not in service
        assert banned not in core
    assert "IdentityExternalLookupPort" not in service


def test_service_logs_only_allowlisted_codes(caplog: pytest.LogCaptureFixture) -> None:
    from app.services.identity_resolution import _log

    with caplog.at_level(logging.INFO):
        _log("IDENTITY_RESOLVED")
        _log("not-allowed")
        _log(_PHONE)
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "IDENTITY_RESOLVED" in messages
    assert "not-allowed" not in messages
    assert _PHONE not in messages


def test_adr_020_documents_invariants() -> None:
    adr = (_REPO / "docs/adr/020-identity-resolution.md").read_text(encoding="utf-8")
    assert "MANUAL_REVIEW_REQUIRED" in adr
    assert "EMAIL_ONLY_SECONDARY" in adr
    assert "email alone never returns" in adr
    assert "AMOCRM_BUYER_CARD" in adr
    assert "AMOCRM_TECHNICAL_DEAL" in adr
    assert "active_amocrm_deal_role" in adr
    assert "RU-domain" in adr or "RU-oriented" in adr
    assert "ARCHIVED" in adr
    assert "Never" in adr or "never" in adr
    assert "HMAC" in adr or "normalized" in adr


def test_bot_mode_defaults_unchanged() -> None:
    from app.config import BotMode, Settings

    # Defaults remain fail-closed; CURSOR-30 must not flip them.
    assert BotMode.OFF.value == "OFF"
    src = (_REPO / "app/config.py").read_text(encoding="utf-8")
    assert 'BOT_MODE' in src
    assert "EMERGENCY_LOCK" in src
