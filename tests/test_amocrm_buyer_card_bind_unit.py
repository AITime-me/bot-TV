"""IR-5 manual Buyer Card bind unit coverage."""

from __future__ import annotations

import ast
import inspect
import io
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.amocrm_buyer_card_bind_ops import (
    argv_has_sensitive_legacy_flag,
    parse_bind_approval_json,
)
from app.core.amocrm_buyer_card_bind import (
    AMOCRM_BUYER_CARD_BIND_SOURCE,
    AmoCrmBuyerCardBindOutcome,
    AmoCrmBuyerCardBindResult,
    BuyerCardBindApproval,
)
from app.core.amocrm_buyer_card_discovery import (
    AmoCrmBuyerCardDiscoveryOutcome,
    AmoCrmBuyerCardDiscoveryResult,
)
from app.core.amocrm_identity_lookup import (
    AmoCrmIdentityLookupOutcome,
    AmoCrmIdentityLookupResult,
)
from app.core.identity_resolution import (
    DEFAULT_CONNECTION_SCOPE,
    AttachIdentityLinkOutcome,
    AttachIdentityLinkResult,
    IdentityEntityKind,
    IdentityLinkConfidence,
    IdentityLinkRecord,
    IdentityLinkStatus,
    ReconcileBuyerCardOutcome,
    ReconcileBuyerCardResult,
)
from app.services.amocrm_buyer_card_bind import (
    AmoCrmBuyerCardBindService,
    _GraphSnapshot,
)
from tests.docker_runtime_allowlist import (
    IR5_DOCKER_RUNTIME_PATHS,
    assert_canonical_docker_runtime_allowlist,
    is_included_in_docker_build_context,
)

_REPO = Path(__file__).resolve().parents[1]
_PHONE = "+79001234567"
_NAME = "Secret Person"
_EMAIL = "hidden@example.com"


class _FakeLookup:
    def __init__(self) -> None:
        self.by_id: AmoCrmIdentityLookupResult | None = None
        self.by_id_calls: list[object] = []
        self.by_phone_calls: list[object] = []

    async def lookup_contact_by_id(self, *, contact_id: object) -> AmoCrmIdentityLookupResult:
        self.by_id_calls.append(contact_id)
        assert self.by_id is not None
        return self.by_id

    async def lookup_contact_by_phone(self, *, phone: object) -> AmoCrmIdentityLookupResult:
        self.by_phone_calls.append(phone)
        raise AssertionError("phone lookup is forbidden on the IR-5 write path")


class _FakeDiscovery:
    def __init__(self) -> None:
        self.result: AmoCrmBuyerCardDiscoveryResult | None = None
        self.calls: list[tuple[object, object]] = []

    async def discover_buyer_card_candidates(
        self,
        *,
        contact_id: object,
        known_technical_deal_ids: object = (),
    ) -> AmoCrmBuyerCardDiscoveryResult:
        self.calls.append((contact_id, known_technical_deal_ids))
        assert self.result is not None
        return replace(
            self.result,
            known_technical_deal_ids=tuple(known_technical_deal_ids),  # type: ignore[arg-type]
        )


class _QueuedSnapshots:
    def __init__(self, items: list[_GraphSnapshot | None]) -> None:
        self._items = list(items)
        self.calls = 0

    async def __call__(self, canonical_id: object) -> _GraphSnapshot | None:
        self.calls += 1
        return self._items.pop(0)


class _FakeReconcile:
    def __init__(self, result: ReconcileBuyerCardResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def __call__(self, **kwargs: object) -> ReconcileBuyerCardResult:
        self.calls.append(kwargs)
        return self.result


class _FakeAttach:
    def __init__(self, result: AttachIdentityLinkResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def __call__(self, **kwargs: object) -> AttachIdentityLinkResult:
        self.calls.append(kwargs)
        return self.result


def _snap(
    cid,
    *,
    active: bool = True,
    contacts: tuple[str, ...] = (),
    technical: tuple[str, ...] = (),
    cards: tuple[str, ...] = (),
) -> _GraphSnapshot:
    return _GraphSnapshot(
        canonical_id=cid,
        active=active,
        contact_ids=contacts,
        technical_deal_ids=technical,
        buyer_card_ids=cards,
    )


def _found_id(contact_id: str) -> AmoCrmIdentityLookupResult:
    return AmoCrmIdentityLookupResult(
        outcome=AmoCrmIdentityLookupOutcome.FOUND,
        contact_id=contact_id,
        http_calls=("GET_CONTACT_BY_ID",),
    )


def _disc(
    outcome: AmoCrmBuyerCardDiscoveryOutcome,
    *,
    contact_id: str | None = "42",
    eligible: tuple[str, ...] = (),
    error_code: str | None = None,
) -> AmoCrmBuyerCardDiscoveryResult:
    kwargs: dict = {
        "outcome": outcome,
        "error_code": error_code,
        "http_calls": ("GET_CONTACT_WITH_LEADS",),
        "contact_id": contact_id,
    }
    if outcome is AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE:
        kwargs["eligible_lead_ids"] = eligible
        kwargs["http_calls"] = ("GET_CONTACT_WITH_LEADS", "GET_LEAD_7")
    elif outcome is AmoCrmBuyerCardDiscoveryOutcome.AMBIGUOUS:
        kwargs["eligible_lead_ids"] = eligible
    return AmoCrmBuyerCardDiscoveryResult(**kwargs)


def _link(cid, card: str = "7") -> IdentityLinkRecord:
    return IdentityLinkRecord(
        link_id=uuid4(),
        canonical_identity_id=cid,
        provider="amocrm",
        connection_scope=DEFAULT_CONNECTION_SCOPE,
        entity_kind=IdentityEntityKind.AMOCRM_BUYER_CARD,
        external_id=card,
        status=IdentityLinkStatus.ACTIVE,
        confidence=IdentityLinkConfidence.CONFIRMED,
        source=AMOCRM_BUYER_CARD_BIND_SOURCE,
        linked_at=datetime.now(timezone.utc),
        revoked_at=None,
    )


def _linked(cid, card: str = "7") -> AttachIdentityLinkResult:
    return AttachIdentityLinkResult(
        outcome=AttachIdentityLinkOutcome.LINKED,
        canonical_identity_id=cid,
        link=_link(cid, card),
    )


def _flow(
    *,
    snapshots: list[_GraphSnapshot | None],
    lookup: _FakeLookup,
    discovery: _FakeDiscovery,
    reconcile: _FakeReconcile | None = None,
    attach: _FakeAttach | None = None,
    load_snapshot: _QueuedSnapshots | None = None,
) -> AmoCrmBuyerCardBindService:
    cid = uuid4()
    rec = reconcile or _FakeReconcile(
        ReconcileBuyerCardResult(
            outcome=ReconcileBuyerCardOutcome.NOT_FOUND,
            canonical_identity_id=cid,
            reason="buyer_card_not_linked",
        )
    )
    att = attach or _FakeAttach(
        AttachIdentityLinkResult(outcome=AttachIdentityLinkOutcome.CONFLICT)
    )
    return AmoCrmBuyerCardBindService(
        session_factory=object(),  # type: ignore[arg-type]
        lookup=lookup,  # type: ignore[arg-type]
        discovery=discovery,  # type: ignore[arg-type]
        load_snapshot=load_snapshot or _QueuedSnapshots(snapshots),
        reconcile=rec,
        attach=att,
    )


@pytest.mark.asyncio
async def test_invalid_canonical_id_zero_http() -> None:
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    service = _flow(snapshots=[], lookup=lookup, discovery=discovery)
    result = await service.bind_buyer_card(
        canonical_identity_id="not-a-uuid",
        contact_id="42",
        buyer_card_id="7",
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.INVALID_INPUT
    assert lookup.by_id_calls == []
    assert discovery.calls == []


@pytest.mark.asyncio
async def test_invalid_external_ids_zero_http() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    att = _FakeAttach(AttachIdentityLinkResult(outcome=AttachIdentityLinkOutcome.CONFLICT))
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        attach=att,
    )
    result = await service.bind_buyer_card(
        canonical_identity_id=cid,
        contact_id="bad id",
        buyer_card_id="7",
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.INVALID_INPUT
    assert lookup.by_id_calls == []
    assert rec.calls == []
    assert att.calls == []


@pytest.mark.asyncio
async def test_canonical_missing_zero_http() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    service = _flow(snapshots=[None], lookup=lookup, discovery=discovery)
    result = await service.bind_buyer_card(
        canonical_identity_id=cid, contact_id="42", buyer_card_id="7"
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.NOT_FOUND
    assert result.error_code == "CANONICAL_NOT_FOUND"
    assert lookup.by_id_calls == []


@pytest.mark.asyncio
async def test_durable_contact_mismatch_zero_http() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    att = _FakeAttach(_linked(cid))
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        attach=att,
    )
    result = await service.bind_buyer_card(
        canonical_identity_id=cid, contact_id="99", buyer_card_id="7"
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "DURABLE_CONTACT_MISMATCH"
    assert lookup.by_id_calls == []
    assert discovery.calls == []
    assert rec.calls == []
    assert att.calls == []


@pytest.mark.asyncio
async def test_missing_and_ambiguous_durable_contact_zero_http() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    att = _FakeAttach(_linked(cid))
    missing = _flow(
        snapshots=[_snap(cid)],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        attach=att,
    )
    result = await missing.bind_buyer_card(
        canonical_identity_id=cid, contact_id="42", buyer_card_id="7"
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.NOT_FOUND
    assert result.error_code == "DURABLE_CONTACT_MISSING"

    lookup2, discovery2 = _FakeLookup(), _FakeDiscovery()
    many = _flow(
        snapshots=[_snap(cid, contacts=("42", "43"))],
        lookup=lookup2,
        discovery=discovery2,
        reconcile=rec,
        attach=att,
    )
    result = await many.bind_buyer_card(
        canonical_identity_id=cid, contact_id="42", buyer_card_id="7"
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "AMBIGUOUS_AMOCRM_CONTACTS"
    assert lookup2.by_id_calls == []
    assert discovery2.calls == []


@pytest.mark.asyncio
async def test_happy_bind_calls_attach_once() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE, eligible=("7",)
    )
    rec = _FakeReconcile(
        ReconcileBuyerCardResult(
            outcome=ReconcileBuyerCardOutcome.NOT_FOUND,
            canonical_identity_id=cid,
            reason="buyer_card_not_linked",
        )
    )
    att = _FakeAttach(_linked(cid))
    loader = _QueuedSnapshots(
        [_snap(cid, contacts=("42",), technical=("9",)), _snap(cid, contacts=("42",), technical=("9",))]
    )
    service = _flow(
        snapshots=[],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        attach=att,
        load_snapshot=loader,
    )
    result = await service.bind_buyer_card(
        canonical_identity_id=cid, contact_id="42", buyer_card_id="7"
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.BOUND
    assert result.buyer_card_id == "7"
    assert loader.calls == 2
    assert rec.calls == [
        {
            "canonical_identity_id": cid,
            "candidate_buyer_card_ids": ("7",),
            "candidate_technical_deal_ids": ("9",),
        }
    ]
    assert att.calls[0]["external_id"] == "7"
    assert att.calls[0]["source"] == AMOCRM_BUYER_CARD_BIND_SOURCE
    assert att.calls[0]["create_canonical"] is False
    assert att.calls[0]["entity_kind"] is IdentityEntityKind.AMOCRM_BUYER_CARD
    assert lookup.by_phone_calls == []


@pytest.mark.asyncio
async def test_existing_same_buyer_card_already_bound_skips_attach() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE, eligible=("7",)
    )
    rec = _FakeReconcile(
        ReconcileBuyerCardResult(
            outcome=ReconcileBuyerCardOutcome.REUSED,
            canonical_identity_id=cid,
            buyer_card_external_id="7",
            confidence=IdentityLinkConfidence.CONFIRMED,
            reason="existing_buyer_card",
        )
    )
    att = _FakeAttach(_linked(cid))
    service = _flow(
        snapshots=[
            _snap(cid, contacts=("42",), cards=("7",)),
            _snap(cid, contacts=("42",), cards=("7",)),
        ],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        attach=att,
    )
    result = await service.bind_buyer_card(
        canonical_identity_id=cid, contact_id="42", buyer_card_id="7"
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.ALREADY_BOUND
    assert att.calls == []


@pytest.mark.asyncio
async def test_existing_different_buyer_card_manual() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE, eligible=("7",)
    )
    rec = _FakeReconcile(
        ReconcileBuyerCardResult(
            outcome=ReconcileBuyerCardOutcome.MANUAL_REVIEW_REQUIRED,
            reason="buyer_card_ambiguous",
        )
    )
    att = _FakeAttach(_linked(cid))
    service = _flow(
        snapshots=[
            _snap(cid, contacts=("42",), cards=("8",)),
            _snap(cid, contacts=("42",), cards=("8",)),
        ],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        attach=att,
    )
    result = await service.bind_buyer_card(
        canonical_identity_id=cid, contact_id="42", buyer_card_id="7"
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED
    assert att.calls == []


@pytest.mark.asyncio
async def test_ir2_remote_miss_skips_discovery() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = AmoCrmIdentityLookupResult(
        outcome=AmoCrmIdentityLookupOutcome.NOT_FOUND,
        error_code="CONTACT_NOT_FOUND",
        http_calls=("GET_CONTACT_BY_ID",),
    )
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    att = _FakeAttach(_linked(cid))
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        attach=att,
    )
    result = await service.bind_buyer_card(
        canonical_identity_id=cid, contact_id="42", buyer_card_id="7"
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.NOT_FOUND
    assert discovery.calls == []
    assert rec.calls == []
    assert att.calls == []


@pytest.mark.asyncio
async def test_ir3_not_found_skips_attach() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND)
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    att = _FakeAttach(_linked(cid))
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        attach=att,
    )
    result = await service.bind_buyer_card(
        canonical_identity_id=cid, contact_id="42", buyer_card_id="7"
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.NOT_FOUND
    assert rec.calls == []
    assert att.calls == []


@pytest.mark.asyncio
async def test_ir3_ambiguous_never_picks_first_candidate() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.AMBIGUOUS, eligible=("7", "8")
    )
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    att = _FakeAttach(_linked(cid))
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        attach=att,
    )
    result = await service.bind_buyer_card(
        canonical_identity_id=cid, contact_id="42", buyer_card_id="7"
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "AMBIGUOUS_BUYER_CARD_CANDIDATES"
    assert rec.calls == []
    assert att.calls == []


@pytest.mark.asyncio
async def test_discovered_candidate_not_expected_manual() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE, eligible=("8",)
    )
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    att = _FakeAttach(_linked(cid))
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        attach=att,
    )
    result = await service.bind_buyer_card(
        canonical_identity_id=cid, contact_id="42", buyer_card_id="7"
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "EXPECTED_BUYER_CARD_MISMATCH"
    assert rec.calls == []
    assert att.calls == []


@pytest.mark.asyncio
async def test_technical_role_conflict_from_reconcile() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE, eligible=("7",)
    )
    rec = _FakeReconcile(
        ReconcileBuyerCardResult(
            outcome=ReconcileBuyerCardOutcome.MANUAL_REVIEW_REQUIRED,
            reason="buyer_card_technical_deal_conflict",
        )
    )
    att = _FakeAttach(_linked(cid))
    service = _flow(
        snapshots=[
            _snap(cid, contacts=("42",), technical=("7",)),
            _snap(cid, contacts=("42",), technical=("7",)),
        ],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        attach=att,
    )
    result = await service.bind_buyer_card(
        canonical_identity_id=cid, contact_id="42", buyer_card_id="7"
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "buyer_card_technical_deal_conflict"
    assert att.calls == []


@pytest.mark.asyncio
async def test_canonical_archived_during_http_manual() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE, eligible=("7",)
    )
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    att = _FakeAttach(_linked(cid))
    service = _flow(
        snapshots=[
            _snap(cid, contacts=("42",)),
            _snap(cid, contacts=("42",), active=False),
        ],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        attach=att,
    )
    result = await service.bind_buyer_card(
        canonical_identity_id=cid, contact_id="42", buyer_card_id="7"
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "CANONICAL_CONTEXT_CHANGED"
    assert rec.calls == []
    assert att.calls == []


@pytest.mark.asyncio
async def test_durable_contact_changed_during_http_manual() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE, eligible=("7",)
    )
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    att = _FakeAttach(_linked(cid))
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",)), _snap(cid, contacts=("99",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        attach=att,
    )
    result = await service.bind_buyer_card(
        canonical_identity_id=cid, contact_id="42", buyer_card_id="7"
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "DURABLE_CONTACT_CHANGED"
    assert rec.calls == []
    assert att.calls == []


@pytest.mark.asyncio
async def test_attach_already_linked_same_canonical() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE, eligible=("7",)
    )
    rec = _FakeReconcile(
        ReconcileBuyerCardResult(
            outcome=ReconcileBuyerCardOutcome.NOT_FOUND,
            canonical_identity_id=cid,
            reason="buyer_card_not_linked",
        )
    )
    att = _FakeAttach(
        AttachIdentityLinkResult(
            outcome=AttachIdentityLinkOutcome.ALREADY_LINKED,
            canonical_identity_id=cid,
            link=_link(cid),
        )
    )
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",)), _snap(cid, contacts=("42",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        attach=att,
    )
    result = await service.bind_buyer_card(
        canonical_identity_id=cid, contact_id="42", buyer_card_id="7"
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.ALREADY_BOUND


@pytest.mark.asyncio
async def test_attach_conflict_other_canonical_manual() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE, eligible=("7",)
    )
    rec = _FakeReconcile(
        ReconcileBuyerCardResult(
            outcome=ReconcileBuyerCardOutcome.NOT_FOUND,
            canonical_identity_id=cid,
            reason="buyer_card_not_linked",
        )
    )
    att = _FakeAttach(
        AttachIdentityLinkResult(
            outcome=AttachIdentityLinkOutcome.CONFLICT,
            reason="buyer_card_technical_deal_conflict",
        )
    )
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",)), _snap(cid, contacts=("42",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        attach=att,
    )
    result = await service.bind_buyer_card(
        canonical_identity_id=cid, contact_id="42", buyer_card_id="7"
    )
    assert result.outcome is AmoCrmBuyerCardBindOutcome.MANUAL_REVIEW_REQUIRED


@pytest.mark.parametrize(
    ("disc_outcome", "read_outcome"),
    [
        (AmoCrmBuyerCardDiscoveryOutcome.INCOMPLETE, AmoCrmBuyerCardBindOutcome.INCOMPLETE),
        (AmoCrmBuyerCardDiscoveryOutcome.INVALID_INPUT, AmoCrmBuyerCardBindOutcome.INVALID_INPUT),
        (AmoCrmBuyerCardDiscoveryOutcome.DISABLED, AmoCrmBuyerCardBindOutcome.DISABLED),
        (AmoCrmBuyerCardDiscoveryOutcome.TRANSIENT_ERROR, AmoCrmBuyerCardBindOutcome.TRANSIENT_ERROR),
        (AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR, AmoCrmBuyerCardBindOutcome.PERMANENT_ERROR),
    ],
)
@pytest.mark.asyncio
async def test_discovery_fail_closed_skips_attach(
    disc_outcome: AmoCrmBuyerCardDiscoveryOutcome,
    read_outcome: AmoCrmBuyerCardBindOutcome,
) -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(disc_outcome, error_code="X")
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    att = _FakeAttach(_linked(cid))
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        attach=att,
    )
    result = await service.bind_buyer_card(
        canonical_identity_id=cid, contact_id="42", buyer_card_id="7"
    )
    assert result.outcome is read_outcome
    assert rec.calls == []
    assert att.calls == []


def test_parse_bind_approval_json_shape() -> None:
    approval = parse_bind_approval_json('{"contact_id":"42","buyer_card_id":"7"}')
    assert approval.contact_id == "42"
    assert approval.buyer_card_id == "7"
    assert "42" not in repr(approval)
    assert "7" not in repr(approval)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", "APPROVAL_STDIN_EMPTY"),
        ("{", "APPROVAL_STDIN_JSON_INVALID"),
        ("[]", "APPROVAL_STDIN_OBJECT_REQUIRED"),
        ('{"contact_id":"42","buyer_card_id":"7","extra":1}', "APPROVAL_STDIN_UNKNOWN_KEYS"),
        ('{"contact_id":"42"}', "APPROVAL_STDIN_KEYS_REQUIRED"),
        ('{"contact_id":1,"buyer_card_id":"7"}', "APPROVAL_CONTACT_ID_INVALID"),
        ('{"contact_id":"42","buyer_card_id":" 7"}', "APPROVAL_BUYER_CARD_ID_INVALID"),
    ],
)
def test_parse_bind_approval_json_fail_closed(raw: str, code: str) -> None:
    with pytest.raises(ValueError) as exc:
        parse_bind_approval_json(raw)
    assert str(exc.value.args[0]) == code
    assert "42" not in str(exc.value)
    assert "7" not in str(exc.value)


def test_sensitive_argv_flags_rejected() -> None:
    assert argv_has_sensitive_legacy_flag(["--canonical-identity-id", str(uuid4())]) is False
    assert argv_has_sensitive_legacy_flag(["--contact-id", "42"]) is True
    assert argv_has_sensitive_legacy_flag(["--buyer-card-id=7"]) is True


def test_cli_malformed_stdin_does_not_echo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app import amocrm_buyer_card_bind_ops as cli

    secret = "secret-card-999"
    code = cli.main(
        ["--canonical-identity-id", str(uuid4())],
        environ={},
        stdin=io.StringIO(json.dumps({"contact_id": "42", "buyer_card_id": secret, "x": 1})),
    )
    assert code == 2
    captured = capsys.readouterr()
    assert "APPROVAL_STDIN_UNKNOWN_KEYS" in captured.err
    assert secret not in captured.out
    assert secret not in captured.err


def test_cli_sensitive_argv_does_not_echo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app import amocrm_buyer_card_bind_ops as cli

    secret = "secret-contact-42"
    code = cli.main(
        ["--canonical-identity-id", str(uuid4()), "--contact-id", secret],
        environ={},
        stdin=io.StringIO("{}"),
    )
    assert code == 2
    captured = capsys.readouterr()
    assert "SENSITIVE_ARGV_FORBIDDEN" in captured.err
    assert secret not in captured.out
    assert secret not in captured.err


def test_result_repr_redacts_external_ids() -> None:
    cid = uuid4()
    result = AmoCrmBuyerCardBindResult(
        outcome=AmoCrmBuyerCardBindOutcome.BOUND,
        canonical_identity_id=cid,
        contact_id="42",
        buyer_card_id="7",
    )
    text = repr(result)
    assert "42" not in text
    assert "7" not in text
    assert str(cid) not in text
    assert _PHONE not in text
    assert _NAME not in text
    assert _EMAIL not in text
    assert "BOUND" in text
    approval = BuyerCardBindApproval(contact_id="42", buyer_card_id="7")
    assert "42" not in repr(approval)


def test_cli_module_doc_avoids_external_id_examples() -> None:
    text = (_REPO / "app" / "amocrm_buyer_card_bind_ops.py").read_text(encoding="utf-8")
    doc = text.split('"""', 2)[1]
    assert "echo '{" not in text
    assert "42" not in doc
    assert "contact_id" in doc
    assert "stdin JSON" in doc


def test_no_attach_crm_writes_webhook_or_review_cases() -> None:
    paths = [
        _REPO / "app/services/amocrm_buyer_card_bind.py",
        _REPO / "app/core/amocrm_buyer_card_bind.py",
        _REPO / "app/amocrm_buyer_card_bind_ops.py",
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and type(node.value) is str:
                if node.value in {"POST", "PATCH", "PUT", "DELETE"}:
                    raise AssertionError(f"{path.name} contains {node.value!r}")
        assert "lookup_contact_by_phone" not in source
        assert "identity_review_cases" not in source
        assert "IdentityReviewCase" not in source
        assert "create_lead" not in source


def test_not_wired_into_runtime() -> None:
    for rel in (
        "app/main.py",
        "app/worker.py",
        "app/amocrm_chat_webhook.py",
        "app/services/worker_runtime.py",
        "app/services/inbound.py",
        "app/services/identity_glue.py",
        "app/services/identity_resolution.py",
        "app/services/amocrm_buyer_card_read_flow.py",
    ):
        source = (_REPO / rel).read_text(encoding="utf-8")
        assert "amocrm_buyer_card_bind" not in source
        assert "AmoCrmBuyerCardBindService" not in source
        assert "bind_buyer_card" not in source


def test_identity_resolution_stays_network_free() -> None:
    source = (_REPO / "app/services/identity_resolution.py").read_text(encoding="utf-8")
    assert "No live CRM" in source
    assert "amocrm_buyer_card_bind" not in source


def test_no_db_transaction_held_across_http() -> None:
    source = inspect.getsource(AmoCrmBuyerCardBindService.bind_buyer_card)
    assert "lookup_contact_by_id" in source
    assert "discover_buyer_card_candidates" in source
    assert "session_scope" not in source
    assert "lock_canonical" not in source
    finalize = inspect.getsource(AmoCrmBuyerCardBindService._finalize)
    assert "lookup_contact_by_id" not in finalize
    assert "discover_buyer_card_candidates" not in finalize
    lock_at = finalize.find("lock_canonical")
    read_at = finalize.find("_read_graph")
    assert lock_at != -1
    assert read_at != -1
    assert lock_at < read_at


def test_docker_allowlist_includes_ir5() -> None:
    assert_canonical_docker_runtime_allowlist()
    for rel in IR5_DOCKER_RUNTIME_PATHS:
        assert is_included_in_docker_build_context(rel)
        assert (_REPO / rel).is_file()
