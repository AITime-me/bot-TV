"""IR-4 read-only Buyer Card orchestration unit coverage."""

from __future__ import annotations

import ast
import inspect
import uuid
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.amocrm_buyer_card_discovery import (
    AmoCrmBuyerCardDiscoveryOutcome,
    AmoCrmBuyerCardDiscoveryResult,
)
from app.core.amocrm_buyer_card_read_flow import (
    AmoCrmBuyerCardReadOutcome,
    AmoCrmBuyerCardReadResult,
    BuyerCardContactSource,
)
from app.core.amocrm_identity_lookup import (
    AmoCrmIdentityLookupOutcome,
    AmoCrmIdentityLookupResult,
)
from app.core.identity_resolution import (
    IdentityEntityKind,
    IdentityLinkConfidence,
    ReconcileBuyerCardOutcome,
    ReconcileBuyerCardResult,
)
from app.services.amocrm_buyer_card_read_flow import (
    AmoCrmBuyerCardReadFlowService,
    _GraphSnapshot,
    _unique_ids_for_kind,
)
from tests.docker_runtime_allowlist import (
    IR4_DOCKER_RUNTIME_PATHS,
    assert_canonical_docker_runtime_allowlist,
    is_included_in_docker_build_context,
)

_REPO = Path(__file__).resolve().parents[1]
_PHONE = "+79001234567"
_NAME = "Secret Person"


class _FakeLookup:
    def __init__(self) -> None:
        self.by_id: AmoCrmIdentityLookupResult | None = None
        self.by_phone: AmoCrmIdentityLookupResult | None = None
        self.by_id_calls: list[object] = []
        self.by_phone_calls: list[object] = []

    async def lookup_contact_by_id(self, *, contact_id: object) -> AmoCrmIdentityLookupResult:
        self.by_id_calls.append(contact_id)
        assert self.by_id is not None
        return self.by_id

    async def lookup_contact_by_phone(self, *, phone: object) -> AmoCrmIdentityLookupResult:
        self.by_phone_calls.append(phone)
        assert self.by_phone is not None
        return self.by_phone


class _FakeDiscovery:
    def __init__(self) -> None:
        self.result: AmoCrmBuyerCardDiscoveryResult | None = None
        self.calls: list[object] = []

    async def discover_buyer_card_candidates(
        self,
        *,
        contact_id: object,
    ) -> AmoCrmBuyerCardDiscoveryResult:
        self.calls.append(contact_id)
        assert self.result is not None
        return self.result


class _QueuedSnapshots:
    def __init__(self, items: list[_GraphSnapshot | None]) -> None:
        self._items = list(items)
        self.calls = 0

    async def __call__(self, canonical_id: uuid.UUID) -> _GraphSnapshot | None:
        self.calls += 1
        return self._items.pop(0)


class _FakeReconcile:
    def __init__(self, result: ReconcileBuyerCardResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def __call__(self, **kwargs: object) -> ReconcileBuyerCardResult:
        self.calls.append(kwargs)
        return self.result


def _snap(
    cid: uuid.UUID,
    *,
    active: bool = True,
    contacts: tuple[str, ...] = (),
    technical: tuple[str, ...] = (),
) -> _GraphSnapshot:
    return _GraphSnapshot(
        canonical_id=cid,
        active=active,
        contact_ids=contacts,
        technical_deal_ids=technical,
    )


def _found_id(contact_id: str, calls: tuple[str, ...] = ("GET_CONTACT_BY_ID",)) -> AmoCrmIdentityLookupResult:
    return AmoCrmIdentityLookupResult(
        outcome=AmoCrmIdentityLookupOutcome.FOUND,
        contact_id=contact_id,
        http_calls=calls,
    )


def _found_phone(contact_id: str) -> AmoCrmIdentityLookupResult:
    return AmoCrmIdentityLookupResult(
        outcome=AmoCrmIdentityLookupOutcome.FOUND,
        contact_id=contact_id,
        http_calls=("GET_CONTACTS_QUERY_P1",),
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
        "http_calls": ("GET_CONTACT_WITH_CUSTOMERS",),
    }
    if outcome is AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE:
        kwargs["contact_id"] = contact_id
        kwargs["eligible_customer_ids"] = eligible
        kwargs["http_calls"] = ("GET_CONTACT_WITH_CUSTOMERS", "GET_CUSTOMER_7")
    elif outcome is AmoCrmBuyerCardDiscoveryOutcome.AMBIGUOUS:
        kwargs["contact_id"] = contact_id
        kwargs["eligible_customer_ids"] = eligible
    elif outcome is AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND:
        kwargs["contact_id"] = contact_id
    else:
        kwargs["contact_id"] = contact_id
    return AmoCrmBuyerCardDiscoveryResult(**kwargs)


class _Link:
    def __init__(self, entity_kind: str, external_id: str) -> None:
        self.entity_kind = entity_kind
        self.external_id = external_id


def _flow(
    *,
    snapshots: list[_GraphSnapshot | None],
    lookup: _FakeLookup,
    discovery: _FakeDiscovery,
    reconcile: _FakeReconcile | None = None,
    load_snapshot: _QueuedSnapshots | None = None,
) -> AmoCrmBuyerCardReadFlowService:
    rec = reconcile or _FakeReconcile(
        ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND)
    )
    return AmoCrmBuyerCardReadFlowService(
        session_factory=object(),  # type: ignore[arg-type]
        lookup=lookup,  # type: ignore[arg-type]
        discovery=discovery,  # type: ignore[arg-type]
        load_snapshot=load_snapshot or _QueuedSnapshots(snapshots),
        reconcile=rec,
    )


def _reuse_reconcile(cid: uuid.UUID) -> _FakeReconcile:
    return _FakeReconcile(
        ReconcileBuyerCardResult(
            outcome=ReconcileBuyerCardOutcome.REUSED,
            canonical_identity_id=cid,
            buyer_card_external_id="99",
            confidence=IdentityLinkConfidence.CONFIRMED,
            reason="existing_buyer_card",
        )
    )


@pytest.mark.asyncio
async def test_invalid_canonical_id() -> None:
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    service = _flow(snapshots=[], lookup=lookup, discovery=discovery)
    result = await service.read_buyer_card(canonical_identity_id="not-a-uuid")
    assert result.outcome is AmoCrmBuyerCardReadOutcome.INVALID_INPUT
    assert lookup.by_id_calls == []
    assert discovery.calls == []


@pytest.mark.asyncio
async def test_canonical_missing() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    service = _flow(snapshots=[None], lookup=lookup, discovery=discovery)
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.NOT_FOUND
    assert result.error_code == "CANONICAL_NOT_FOUND"
    assert lookup.by_id_calls == []


@pytest.mark.asyncio
async def test_canonical_archived() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    service = _flow(
        snapshots=[_snap(cid, active=False)],
        lookup=lookup,
        discovery=discovery,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.NOT_FOUND
    assert result.error_code == "CANONICAL_NOT_ACTIVE"
    assert lookup.by_id_calls == []
    assert discovery.calls == []


@pytest.mark.asyncio
async def test_multiple_active_contacts_manual_zero_http() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    service = _flow(
        snapshots=[_snap(cid, contacts=("1", "2"))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid, phone=_PHONE)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "AMBIGUOUS_AMOCRM_CONTACTS"
    assert lookup.by_id_calls == []
    assert lookup.by_phone_calls == []
    assert discovery.calls == []
    assert rec.calls == []


@pytest.mark.asyncio
async def test_one_durable_contact_uses_ir2_by_id() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE,
        eligible=("7",),
    )
    rec = _FakeReconcile(
        ReconcileBuyerCardResult(
            outcome=ReconcileBuyerCardOutcome.NOT_FOUND,
            canonical_identity_id=cid,
            reason="buyer_card_not_linked",
        )
    )
    service = _flow(
        snapshots=[
            _snap(cid, contacts=("42",), technical=("9",)),
            _snap(cid, contacts=("42",), technical=("9",)),
        ],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid, phone=_PHONE)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.NOT_FOUND
    assert result.contact_source is BuyerCardContactSource.DURABLE_LINK
    assert lookup.by_id_calls == ["42"]
    assert lookup.by_phone_calls == []
    assert discovery.calls == ["42"]
    assert rec.calls
    assert rec.calls[0]["candidate_buyer_card_ids"] == ("7",)
    assert rec.calls[0]["candidate_technical_deal_ids"] == ()


@pytest.mark.asyncio
async def test_durable_contact_remote_not_found_no_phone_fallback() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = AmoCrmIdentityLookupResult(
        outcome=AmoCrmIdentityLookupOutcome.NOT_FOUND,
        error_code="AMOCRM_CRM_HTTP_204",
        http_calls=("GET_CONTACT_BY_ID",),
    )
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid, phone=_PHONE)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.NOT_FOUND
    assert lookup.by_phone_calls == []
    assert discovery.calls == []
    assert rec.calls == []


@pytest.mark.asyncio
async def test_no_contact_no_phone_not_found() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    service = _flow(
        snapshots=[_snap(cid)],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.NOT_FOUND
    assert result.error_code == "CONTACT_NOT_RESOLVED"
    assert lookup.by_id_calls == []
    assert lookup.by_phone_calls == []
    assert rec.calls == []


@pytest.mark.asyncio
async def test_no_contact_phone_found_ephemeral_source() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_phone = _found_phone("77")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE,
        contact_id="77",
        eligible=("7",),
    )
    rec = _FakeReconcile(
        ReconcileBuyerCardResult(
            outcome=ReconcileBuyerCardOutcome.NOT_FOUND,
            canonical_identity_id=cid,
            reason="buyer_card_not_linked",
        )
    )
    service = _flow(
        snapshots=[_snap(cid), _snap(cid)],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid, phone=_PHONE)
    assert result.contact_source is BuyerCardContactSource.PHONE_LOOKUP
    assert result.contact_id == "77"
    assert lookup.by_id_calls == []
    assert lookup.by_phone_calls == [_PHONE]
    assert rec.calls


@pytest.mark.asyncio
async def test_phone_ambiguous_manual() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_phone = AmoCrmIdentityLookupResult(
        outcome=AmoCrmIdentityLookupOutcome.AMBIGUOUS,
        contact_ids=("10", "20"),
        http_calls=("GET_CONTACTS_QUERY_P1",),
    )
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    service = _flow(
        snapshots=[_snap(cid)],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid, phone=_PHONE)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "AMBIGUOUS_PHONE_CONTACTS"
    assert discovery.calls == []
    assert rec.calls == []


@pytest.mark.asyncio
async def test_phone_invalid_input() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_phone = AmoCrmIdentityLookupResult(
        outcome=AmoCrmIdentityLookupOutcome.INVALID_INPUT,
        error_code="PHONE_INVALID",
    )
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    service = _flow(
        snapshots=[_snap(cid)],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid, phone="not-a-phone")
    assert result.outcome is AmoCrmBuyerCardReadOutcome.INVALID_INPUT
    assert result.error_code == "PHONE_INVALID"
    assert rec.calls == []


@pytest.mark.asyncio
async def test_ir3_not_found_does_not_call_reconcile() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND, contact_id="42")
    rec = _reuse_reconcile(cid)
    loader = _QueuedSnapshots(
        [_snap(cid, contacts=("42",)), _snap(cid, contacts=("42",))]
    )
    service = _flow(
        snapshots=[],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        load_snapshot=loader,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.NOT_FOUND
    assert result.outcome is not AmoCrmBuyerCardReadOutcome.REUSED
    assert rec.calls == []
    assert loader.calls == 2
    assert result.buyer_card_external_id is None


@pytest.mark.asyncio
async def test_ir3_not_found_must_not_reuse_existing_linked_buyer_card() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND, contact_id="42")
    rec = _reuse_reconcile(cid)
    loader = _QueuedSnapshots(
        [_snap(cid, contacts=("42",)), _snap(cid, contacts=("42",))]
    )
    service = _flow(
        snapshots=[],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        load_snapshot=loader,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert rec.calls == []
    assert loader.calls == 2
    assert result.outcome is AmoCrmBuyerCardReadOutcome.NOT_FOUND
    assert result.buyer_card_external_id is None


@pytest.mark.asyncio
async def test_ir3_not_found_durable_contact_unchanged() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND, contact_id="42")
    rec = _reuse_reconcile(cid)
    loader = _QueuedSnapshots(
        [_snap(cid, contacts=("42",)), _snap(cid, contacts=("42",))]
    )
    service = _flow(
        snapshots=[],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        load_snapshot=loader,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.NOT_FOUND
    assert result.reason is None
    assert rec.calls == []
    assert loader.calls == 2


@pytest.mark.asyncio
async def test_ir3_not_found_durable_contact_changed_manual() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND, contact_id="42")
    rec = _reuse_reconcile(cid)
    service = _flow(
        snapshots=[
            _snap(cid, contacts=("42",)),
            _snap(cid, contacts=("99",)),
        ],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "DURABLE_CONTACT_CHANGED"
    assert rec.calls == []


@pytest.mark.asyncio
async def test_ir3_not_found_phone_same_durable_ok() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_phone = _found_phone("77")
    discovery.result = _disc(AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND, contact_id="77")
    rec = _reuse_reconcile(cid)
    loader = _QueuedSnapshots([_snap(cid), _snap(cid, contacts=("77",))])
    service = _flow(
        snapshots=[],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
        load_snapshot=loader,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid, phone=_PHONE)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.NOT_FOUND
    assert result.contact_source is BuyerCardContactSource.PHONE_LOOKUP
    assert rec.calls == []
    assert loader.calls == 2


@pytest.mark.asyncio
async def test_ir3_not_found_phone_different_durable_manual() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_phone = _found_phone("77")
    discovery.result = _disc(AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND, contact_id="77")
    rec = _reuse_reconcile(cid)
    service = _flow(
        snapshots=[_snap(cid), _snap(cid, contacts=("88",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid, phone=_PHONE)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "PHONE_CONTACT_CONFLICTS_DURABLE"
    assert rec.calls == []


@pytest.mark.asyncio
async def test_ir3_not_found_canonical_archived_during_http_manual() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND, contact_id="42")
    rec = _reuse_reconcile(cid)
    service = _flow(
        snapshots=[
            _snap(cid, contacts=("42",)),
            _snap(cid, contacts=("42",), active=False),
        ],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "CANONICAL_CONTEXT_CHANGED"
    assert rec.calls == []


@pytest.mark.asyncio
async def test_ir3_not_found_canonical_missing_during_http_manual() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(AmoCrmBuyerCardDiscoveryOutcome.NOT_FOUND, contact_id="42")
    rec = _reuse_reconcile(cid)
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",)), None],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "CANONICAL_CONTEXT_CHANGED"
    assert rec.calls == []


@pytest.mark.asyncio
async def test_one_eligible_and_same_linked_card_reused() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE,
        eligible=("7",),
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
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",)), _snap(cid, contacts=("42",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.REUSED
    assert result.buyer_card_external_id == "7"
    assert rec.calls[0]["candidate_buyer_card_ids"] == ("7",)


@pytest.mark.asyncio
async def test_one_eligible_no_linked_card_not_found() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE,
        eligible=("7",),
    )
    rec = _FakeReconcile(
        ReconcileBuyerCardResult(
            outcome=ReconcileBuyerCardOutcome.NOT_FOUND,
            canonical_identity_id=cid,
            reason="buyer_card_not_linked",
        )
    )
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",)), _snap(cid, contacts=("42",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.NOT_FOUND
    assert result.buyer_card_external_id is None
    assert rec.calls


@pytest.mark.asyncio
async def test_customer_id_overlapping_technical_lead_still_reused() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE,
        eligible=("7",),
    )
    rec = _reuse_reconcile(cid)
    rec.result = ReconcileBuyerCardResult(
        outcome=ReconcileBuyerCardOutcome.REUSED,
        canonical_identity_id=cid,
        buyer_card_external_id="7",
        confidence=IdentityLinkConfidence.CONFIRMED,
        reason="existing_buyer_card",
    )
    service = _flow(
        snapshots=[
            _snap(cid, contacts=("42",), technical=("7",)),
            _snap(cid, contacts=("42",), technical=("7",)),
        ],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.REUSED
    assert result.buyer_card_external_id == "7"
    assert rec.calls[0]["candidate_buyer_card_ids"] == ("7",)
    assert rec.calls[0]["candidate_technical_deal_ids"] == ()


@pytest.mark.asyncio
async def test_ambiguous_eligible_no_linked_card_manual() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.AMBIGUOUS,
        eligible=("10", "20"),
    )
    rec = _FakeReconcile(
        ReconcileBuyerCardResult(
            outcome=ReconcileBuyerCardOutcome.MANUAL_REVIEW_REQUIRED,
            reason="ambiguous_buyer_cards",
        )
    )
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",)), _snap(cid, contacts=("42",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.MANUAL_REVIEW_REQUIRED
    assert rec.calls[0]["candidate_buyer_card_ids"] == ("10", "20")


@pytest.mark.asyncio
async def test_ambiguous_eligible_with_existing_linked_card_preserves_reconcile() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.AMBIGUOUS,
        eligible=("7", "8"),
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
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",)), _snap(cid, contacts=("42",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert rec.calls[0]["candidate_buyer_card_ids"] == ("7", "8")
    assert result.outcome is AmoCrmBuyerCardReadOutcome.REUSED
    assert result.buyer_card_external_id == "7"


@pytest.mark.parametrize(
    "disc_outcome,read_outcome",
    [
        (AmoCrmBuyerCardDiscoveryOutcome.INCOMPLETE, AmoCrmBuyerCardReadOutcome.INCOMPLETE),
        (AmoCrmBuyerCardDiscoveryOutcome.TRANSIENT_ERROR, AmoCrmBuyerCardReadOutcome.TRANSIENT_ERROR),
        (AmoCrmBuyerCardDiscoveryOutcome.PERMANENT_ERROR, AmoCrmBuyerCardReadOutcome.PERMANENT_ERROR),
        (AmoCrmBuyerCardDiscoveryOutcome.DISABLED, AmoCrmBuyerCardReadOutcome.DISABLED),
    ],
)
@pytest.mark.asyncio
async def test_discovery_fail_closed_skips_reconcile(
    disc_outcome: AmoCrmBuyerCardDiscoveryOutcome,
    read_outcome: AmoCrmBuyerCardReadOutcome,
) -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(disc_outcome, contact_id="42", error_code="X")
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    service = _flow(
        snapshots=[_snap(cid, contacts=("42",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is read_outcome
    assert rec.calls == []


@pytest.mark.asyncio
async def test_durable_contact_changed_during_http_manual() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE,
        eligible=("7",),
    )
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    service = _flow(
        snapshots=[
            _snap(cid, contacts=("42",)),
            _snap(cid, contacts=("99",)),
        ],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "DURABLE_CONTACT_CHANGED"
    assert rec.calls == []


@pytest.mark.asyncio
async def test_phone_found_then_same_durable_link_ok() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_phone = _found_phone("77")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE,
        contact_id="77",
        eligible=("7",),
    )
    rec = _FakeReconcile(
        ReconcileBuyerCardResult(
            outcome=ReconcileBuyerCardOutcome.NOT_FOUND,
            canonical_identity_id=cid,
            reason="buyer_card_not_linked",
        )
    )
    service = _flow(
        snapshots=[_snap(cid), _snap(cid, contacts=("77",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid, phone=_PHONE)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.NOT_FOUND
    assert rec.calls
    assert result.contact_source is BuyerCardContactSource.PHONE_LOOKUP


@pytest.mark.asyncio
async def test_phone_found_then_different_durable_link_manual() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_phone = _found_phone("77")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE,
        contact_id="77",
        eligible=("7",),
    )
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    service = _flow(
        snapshots=[_snap(cid), _snap(cid, contacts=("88",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid, phone=_PHONE)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "PHONE_CONTACT_CONFLICTS_DURABLE"
    assert rec.calls == []


@pytest.mark.asyncio
async def test_mixed_contact_links_manual_zero_http() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    service = _flow(
        snapshots=[_snap(cid, contacts=("42", "legacy-bad-id"))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.MANUAL_REVIEW_REQUIRED
    assert result.reason == "AMBIGUOUS_AMOCRM_CONTACTS"
    assert lookup.by_id_calls == []
    assert lookup.by_phone_calls == []
    assert discovery.calls == []
    assert rec.calls == []


@pytest.mark.asyncio
async def test_mixed_technical_ids_no_crash_fail_closed() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = _found_id("42")
    discovery.result = _disc(
        AmoCrmBuyerCardDiscoveryOutcome.FOUND_CANDIDATE,
        eligible=("7",),
    )
    rec = _reuse_reconcile(cid)
    service = _flow(
        snapshots=[
            _snap(cid, contacts=("42",), technical=("9", "legacy-bad-id")),
            _snap(cid, contacts=("42",), technical=("9", "legacy-bad-id")),
        ],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.REUSED
    assert discovery.calls == ["42"]
    assert rec.calls


@pytest.mark.asyncio
async def test_single_malformed_contact_fail_closed_ir2() -> None:
    cid = uuid4()
    lookup, discovery = _FakeLookup(), _FakeDiscovery()
    lookup.by_id = AmoCrmIdentityLookupResult(
        outcome=AmoCrmIdentityLookupOutcome.INVALID_INPUT,
        error_code="AMOCRM_CONTACT_ID_INVALID",
    )
    rec = _FakeReconcile(ReconcileBuyerCardResult(outcome=ReconcileBuyerCardOutcome.NOT_FOUND))
    service = _flow(
        snapshots=[_snap(cid, contacts=("legacy-bad-id",))],
        lookup=lookup,
        discovery=discovery,
        reconcile=rec,
    )
    result = await service.read_buyer_card(canonical_identity_id=cid)
    assert result.outcome is AmoCrmBuyerCardReadOutcome.INVALID_INPUT
    assert result.error_code == "AMOCRM_CONTACT_ID_INVALID"
    assert lookup.by_id_calls == ["legacy-bad-id"]
    assert discovery.calls == []
    assert rec.calls == []


def test_unique_ids_for_kind_mixed_numeric_non_numeric_no_crash() -> None:
    contact_ids = _unique_ids_for_kind(
        [
            _Link(IdentityEntityKind.AMOCRM_CONTACT.value, "legacy-bad-id"),
            _Link(IdentityEntityKind.AMOCRM_CONTACT.value, "42"),
        ],
        IdentityEntityKind.AMOCRM_CONTACT,
    )
    assert contact_ids == ("42", "legacy-bad-id")
    technical_ids = _unique_ids_for_kind(
        [
            _Link(IdentityEntityKind.AMOCRM_TECHNICAL_DEAL.value, "legacy-bad-id"),
            _Link(IdentityEntityKind.AMOCRM_TECHNICAL_DEAL.value, "9"),
        ],
        IdentityEntityKind.AMOCRM_TECHNICAL_DEAL,
    )
    assert technical_ids == ("9", "legacy-bad-id")


def test_unique_ids_for_kind_numeric_tuple_deterministic() -> None:
    ids = _unique_ids_for_kind(
        [
            _Link(IdentityEntityKind.AMOCRM_CONTACT.value, "10"),
            _Link(IdentityEntityKind.AMOCRM_CONTACT.value, "2"),
            _Link(IdentityEntityKind.AMOCRM_CONTACT.value, "10"),
        ],
        IdentityEntityKind.AMOCRM_CONTACT,
    )
    assert ids == ("10", "2")


def test_result_repr_no_pii() -> None:
    cid = uuid4()
    result = AmoCrmBuyerCardReadResult(
        outcome=AmoCrmBuyerCardReadOutcome.REUSED,
        canonical_identity_id=cid,
        contact_id="42",
        buyer_card_external_id="7",
        contact_source=BuyerCardContactSource.PHONE_LOOKUP,
        reason="existing_buyer_card",
    )
    text = repr(result)
    assert _PHONE not in text
    assert _NAME not in text
    assert "@" not in text
    assert "PHONE_LOOKUP" in text


def test_no_attach_revoke_create_or_crm_writes() -> None:
    paths = [
        _REPO / "app/services/amocrm_buyer_card_read_flow.py",
        _REPO / "app/core/amocrm_buyer_card_read_flow.py",
    ]
    forbidden_names = {
        "attach",
        "insert_canonical",
        "insert_active_link",
        "mark_link_revoked",
        "create_lead",
        "POST",
        "PATCH",
        "PUT",
        "DELETE",
    }
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and type(node.value) is str:
                if node.value in {"POST", "PATCH", "PUT", "DELETE"}:
                    raise AssertionError(f"{path.name} contains {node.value!r}")
            if isinstance(node, ast.Attribute) and node.attr in {
                "attach",
                "insert_canonical",
                "insert_active_link",
                "mark_link_revoked",
                "create_lead",
            }:
                raise AssertionError(f"{path.name} calls {node.attr}")
        assert ".attach(" not in source
        assert "insert_active_link" not in source
        assert "mark_link_revoked" not in source
        for name in forbidden_names:
            if name in {"POST", "PATCH", "PUT", "DELETE", "attach"}:
                continue


def test_no_webhook_worker_glue_wiring() -> None:
    for rel in (
        "app/amocrm_chat_webhook.py",
        "app/worker.py",
        "app/services/worker_runtime.py",
        "app/services/inbound.py",
        "app/services/identity_glue.py",
        "app/services/identity_resolution.py",
        "app/services/amocrm_technical_deal.py",
    ):
        source = (_REPO / rel).read_text(encoding="utf-8")
        assert "amocrm_buyer_card_read_flow" not in source
        assert "AmoCrmBuyerCardReadFlowService" not in source
        assert "read_buyer_card" not in source


def test_identity_resolution_stays_network_free() -> None:
    source = (_REPO / "app/services/identity_resolution.py").read_text(encoding="utf-8")
    assert "No live CRM" in source
    assert "amocrm_identity_lookup" not in source
    assert "amocrm_buyer_card_discovery" not in source
    assert "amocrm_buyer_card_read_flow" not in source


def test_no_db_transaction_held_across_http() -> None:
    source = inspect.getsource(AmoCrmBuyerCardReadFlowService.read_buyer_card)
    assert "lookup_contact_by_id" in source or "_resolve_contact" in source
    assert "session_scope" not in source
    finalize = inspect.getsource(AmoCrmBuyerCardReadFlowService._finalize)
    assert "discover_buyer_card_candidates" not in finalize
    load = inspect.getsource(AmoCrmBuyerCardReadFlowService._load_snapshot)
    assert "lookup_contact" not in load
    assert "discover_buyer_card" not in load


def test_docker_allowlist_includes_ir4() -> None:
    assert_canonical_docker_runtime_allowlist()
    for rel in IR4_DOCKER_RUNTIME_PATHS:
        assert is_included_in_docker_build_context(rel)
