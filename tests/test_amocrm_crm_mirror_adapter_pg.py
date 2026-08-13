"""AMO-01B2 PostgreSQL: mirror job → CRM TECHNICAL_DEAL convergence."""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_crm_deal_create_config import AmoCrmDealCreateConfig
from app.core.amocrm_crm_oauth_keys import EnvAmoCrmOauthKeyProvider
from app.core.amocrm_crm_oauth_types import KEY_SIZE_BYTES
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.db.clock import db_now
from app.db.session import session_scope
from app.models.amocrm_entity_link import (
    AmocrmEntityKind,
    AmocrmEntityLink,
    AmocrmEntityLinkStatus,
)
from app.models.amocrm_mirror import AmoCrmMirrorJob, AmoCrmMirrorStatus
from app.repositories import amocrm_crm_oauth_tokens as oauth_repo
from app.repositories import amocrm_entity_links as entity_links
from app.repositories import amocrm_mirror as mirror_repo
from app.repositories.amocrm_mirror import StaleAmoCrmMirrorLeaseError
from app.schemas.inbound import SyntheticInboundEvent
from app.services.amocrm_crm_mirror_adapter import CrmRestMirrorAdapter
from app.services.amocrm_mirror import AmoCrmMirrorRejected, AmoCrmMirrorWorker
from app.services.inbound import InboundService
from app.services.takeover import ManagerTakeoverService
from tests.pg_harness import truncate_foundation_tables

_KEY = secrets.token_bytes(KEY_SIZE_BYTES)
_KEY_B64 = base64.urlsafe_b64encode(_KEY).decode("ascii")
_SCOPE = "default"
_PIPELINE = 1001
_STATUS = 2002


def _provider() -> EnvAmoCrmOauthKeyProvider:
    return EnvAmoCrmOauthKeyProvider(
        {
            "AMOCRM_CRM_OAUTH_ACTIVE_KEY_ID": "K1",
            "AMOCRM_CRM_OAUTH_KEY_K1": _KEY_B64,
        }
    )


def _deal_config() -> AmoCrmDealCreateConfig:
    return AmoCrmDealCreateConfig(
        enabled=True,
        pipeline_id=_PIPELINE,
        status_id=_STATUS,
        rest=AmoCrmCrmRestConfig(
            enabled=True,
            client_id="cid",
            client_secret="csecret12",
            api_base_url="https://example.amocrm.ru",
            connection_scope=_SCOPE,
        ),
    )


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[S2sHttpRequest] = []
        self.responses: list[S2sHttpResponse] = []

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        if not self.responses:
            raise AssertionError(f"no response for {req.method} {req.url}")
        return self.responses.pop(0)


def _lead_created(lead_id: int = 9001) -> S2sHttpResponse:
    return S2sHttpResponse(
        status_code=200,
        headers={},
        body=json.dumps({"_embedded": {"leads": [{"id": lead_id}]}}).encode(),
    )


def _lead_get(lead_id: int = 9001) -> S2sHttpResponse:
    return S2sHttpResponse(
        status_code=200,
        headers={},
        body=json.dumps({"id": lead_id}).encode(),
    )


def _inbound(event_id: str, conv: str = "crm-mirror-conv") -> SyntheticInboundEvent:
    return SyntheticInboundEvent(
        external_conversation_id=conv,
        external_message_id=event_id,
        text="synth-text",
    )


@pytest_asyncio.fixture(autouse=True)
async def cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


async def _seed_oauth(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(session_factory) as session:
        await oauth_repo.upsert_token_pair(
            session,
            access_token="access-live",
            refresh_token="refresh-live",
            key_provider=_provider(),
            connection_scope=_SCOPE,
        )


def _worker(
    session_factory: async_sessionmaker[AsyncSession],
    transport: _FakeTransport,
    *,
    worker_id: str = "crm-mirror",
    config: AmoCrmDealCreateConfig | None = None,
) -> AmoCrmMirrorWorker:
    adapter = CrmRestMirrorAdapter(
        session_factory,
        config=config if config is not None else _deal_config(),
        key_provider=_provider(),
        transport=transport,
        worker_id=worker_id,
    )
    return AmoCrmMirrorWorker(
        session_factory,
        worker_id=worker_id,
        adapter=adapter,
        retry_delay_seconds=1,
    )


def _lead_posts(transport: _FakeTransport) -> list[S2sHttpRequest]:
    return [
        c
        for c in transport.calls
        if c.method == "POST" and c.url.endswith("/api/v4/leads")
    ]


@pytest.mark.asyncio
async def test_mirror_job_ensures_deal_and_marks_mirrored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_oauth(session_factory)
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(_inbound("crm-msg-1"))
        conversation_id = accepted.conversation.id

    transport = _FakeTransport()
    transport.responses.append(_lead_created(9001))
    worker = _worker(session_factory, transport)
    claim = await worker.claim_one()
    assert claim is not None
    result = await worker.process_claimed(claim)
    assert result.mirrored is True
    assert result.status == AmoCrmMirrorStatus.MIRRORED.value
    assert len(_lead_posts(transport)) == 1

    async with session_scope(session_factory) as session:
        job = await mirror_repo.get_by_id(session, job_id=claim.job_id)
        assert job is not None
        assert job.status == AmoCrmMirrorStatus.MIRRORED.value
        active = await entity_links.get_active(
            session,
            conversation_id=conversation_id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
        )
        assert active is not None
        assert active.external_id == "9001"


@pytest.mark.asyncio
async def test_multiple_jobs_same_conversation_one_deal_create(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_oauth(session_factory)
    conv = "crm-mirror-multi"
    async with session_scope(session_factory) as session:
        first = await InboundService(session).accept(_inbound("crm-msg-a", conv))
        conversation_id = first.conversation.id
    async with session_scope(session_factory) as session:
        await InboundService(session).accept(_inbound("crm-msg-b", conv))

    transport = _FakeTransport()
    transport.responses.append(_lead_created(777))
    transport.responses.append(_lead_get(777))
    worker = _worker(session_factory, transport)

    first_claim = await worker.claim_one()
    assert first_claim is not None
    first_result = await worker.process_claimed(first_claim)
    assert first_result.status == AmoCrmMirrorStatus.MIRRORED.value

    second_claim = await worker.claim_one()
    assert second_claim is not None
    second_result = await worker.process_claimed(second_claim)
    assert second_result.status == AmoCrmMirrorStatus.MIRRORED.value
    assert len(_lead_posts(transport)) == 1

    async with session_scope(session_factory) as session:
        active = await entity_links.get_active(
            session,
            conversation_id=conversation_id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
        )
        assert active is not None
        assert active.external_id == "777"
        jobs = (await session.scalars(select(AmoCrmMirrorJob))).all()
        assert {job.status for job in jobs} == {AmoCrmMirrorStatus.MIRRORED.value}


@pytest.mark.asyncio
async def test_takeover_and_later_job_reuse_same_deal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_oauth(session_factory)
    conv = "crm-mirror-handoff"
    async with session_scope(session_factory) as session:
        first = await InboundService(session).accept(_inbound("crm-msg-h1", conv))
        conversation_id = first.conversation.id

    transport = _FakeTransport()
    transport.responses.append(_lead_created(700))
    worker = _worker(session_factory, transport)
    claim = await worker.claim_one()
    assert claim is not None
    await worker.process_claimed(claim)
    assert len(_lead_posts(transport)) == 1

    await ManagerTakeoverService(session_factory).apply(conversation_id)
    transport.responses.append(_lead_get(700))
    takeover_claim = await worker.claim_one()
    assert takeover_claim is not None
    takeover_result = await worker.process_claimed(takeover_claim)
    assert takeover_result.status == AmoCrmMirrorStatus.MIRRORED.value
    assert len(_lead_posts(transport)) == 1

    async with session_scope(session_factory) as session:
        await session.execute(
            text(
                "UPDATE conversations SET ownership = 'BOT', status = 'OPEN', "
                "handoff_state = 'BOT_ACTIVE', handoff_deadline_at = NULL, "
                "human_pause_anchor_at = NULL, manager_takeover_at = NULL "
                "WHERE id = :id"
            ),
            {"id": conversation_id},
        )

    async with session_scope(session_factory) as session:
        await InboundService(session).accept(_inbound("crm-msg-h2", conv))
    transport.responses.append(_lead_get(700))
    later = await worker.claim_one()
    assert later is not None
    later_result = await worker.process_claimed(later)
    assert later_result.status == AmoCrmMirrorStatus.MIRRORED.value
    assert len(_lead_posts(transport)) == 1

    async with session_scope(session_factory) as session:
        active = await entity_links.get_active(
            session,
            conversation_id=conversation_id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
        )
        assert active is not None
        assert active.external_id == "700"


@pytest.mark.asyncio
async def test_existing_contact_attaches_on_mirror_job(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_oauth(session_factory)
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(_inbound("crm-msg-ct"))
        conversation_id = accepted.conversation.id
        await entity_links.insert_active_if_absent(
            session,
            conversation_id=conversation_id,
            entity_kind=AmocrmEntityKind.CONTACT,
            external_id="55",
        )

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(status_code=200, headers={}, body=json.dumps({"id": 55}).encode())
    )
    transport.responses.append(_lead_created(880))
    worker = _worker(session_factory, transport)
    claim = await worker.claim_one()
    assert claim is not None
    result = await worker.process_claimed(claim)
    assert result.status == AmoCrmMirrorStatus.MIRRORED.value
    create = _lead_posts(transport)[0]
    payload = json.loads(create.body.decode())
    assert payload[0]["_embedded"]["contacts"][0]["id"] == 55
    assert not any(
        "/api/v4/contacts" in c.url and c.method == "POST" for c in transport.calls
    )


@pytest.mark.asyncio
async def test_reconcile_required_never_posts_deal_again(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_oauth(session_factory)
    async with session_scope(session_factory) as session:
        await InboundService(session).accept(_inbound("crm-msg-amb"))

    transport = _FakeTransport()
    transport.responses.append(
        S2sHttpResponse(status_code=503, headers={}, body=b"boom")
    )
    worker = _worker(session_factory, transport)
    first = await worker.claim_one()
    assert first is not None
    with pytest.raises(AmoCrmMirrorRejected):
        await worker.process_claimed(first)
    assert len(_lead_posts(transport)) == 1

    async with session_scope(session_factory) as session:
        job = await mirror_repo.get_by_id(session, job_id=first.job_id)
        assert job is not None
        assert job.status == AmoCrmMirrorStatus.FAILED.value
        retry_at = job.next_attempt_at
        open_row = await entity_links.get_open(
            session,
            conversation_id=first.conversation_id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
        )
        assert open_row is not None
        assert open_row.status == AmocrmEntityLinkStatus.RECONCILE_REQUIRED.value

    second = await worker.claim_one(now=retry_at)
    assert second is not None
    with pytest.raises(AmoCrmMirrorRejected):
        await worker.process_claimed(second)
    assert len(_lead_posts(transport)) == 1


@pytest.mark.asyncio
async def test_config_off_mirrors_with_zero_crm_http(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        await InboundService(session).accept(_inbound("crm-msg-off"))

    transport = _FakeTransport()
    worker = _worker(
        session_factory,
        transport,
        config=AmoCrmDealCreateConfig(enabled=False),
    )
    claim = await worker.claim_one()
    assert claim is not None
    result = await worker.process_claimed(claim)
    assert result.status == AmoCrmMirrorStatus.MIRRORED.value
    assert transport.calls == []
    assert worker.adapter.last_http_calls == ()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_missing_oauth_token_zero_entity_writes_worker_stays_healthy(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(_inbound("crm-msg-notok"))
        conversation_id = accepted.conversation.id

    transport = _FakeTransport()
    worker = _worker(session_factory, transport)
    claim = await worker.claim_one()
    assert claim is not None
    with pytest.raises(AmoCrmMirrorRejected):
        await worker.process_claimed(claim)
    assert transport.calls == []
    async with session_scope(session_factory) as session:
        job = await mirror_repo.get_by_id(session, job_id=claim.job_id)
        assert job is not None
        assert job.status == AmoCrmMirrorStatus.FAILED.value
        assert job.error_code == "AMOCRM_CRM_OAUTH_NOT_FOUND"
        open_row = await entity_links.get_open(
            session,
            conversation_id=conversation_id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
        )
        assert open_row is None


@pytest.mark.asyncio
async def test_stale_lease_after_claim_zero_crm_http(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pre-adapter reclaim: require_processing_lease fails → zero CRM HTTP."""

    await _seed_oauth(session_factory)
    async with session_scope(session_factory) as session:
        await InboundService(session).accept(_inbound("crm-msg-stale"))

    transport = _FakeTransport()
    transport.responses.append(_lead_created(1))
    crashed = _worker(session_factory, transport, worker_id="crm-stale-a")
    claim = await crashed.claim_one()
    assert claim is not None

    async with session_scope(session_factory) as session:
        expired = await db_now(session) - timedelta(seconds=5)
        await session.execute(
            update(AmoCrmMirrorJob)
            .where(AmoCrmMirrorJob.id == claim.job_id)
            .values(lease_until=expired)
        )

    recovery = _worker(session_factory, transport, worker_id="crm-stale-b")
    reclaimed = await recovery.claim_one()
    assert reclaimed is not None
    assert reclaimed.lease_token != claim.lease_token

    with pytest.raises(StaleAmoCrmMirrorLeaseError):
        await crashed.process_claimed(claim)
    assert transport.calls == []
    assert crashed.adapter.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_mid_flight_reclaim_crm_ran_stale_cannot_complete_one_deal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CRM may run after fence; mid-flight RECLAIM blocks stale complete;

    second worker converges via GET on the one ACTIVE deal (no second POST).
    Expiry alone is not enough: reclaim must supersede lease_token/version.
    """

    await _seed_oauth(session_factory)
    async with session_scope(session_factory) as session:
        await InboundService(session).accept(_inbound("crm-msg-mid"))

    transport = _FakeTransport()
    # First worker create; second worker GET existing.
    transport.responses.append(_lead_created(4242))
    transport.responses.append(_lead_get(4242))

    first = _worker(session_factory, transport, worker_id="crm-mid-a")
    claim = await first.claim_one()
    assert claim is not None

    original_mirror = first.adapter.mirror  # type: ignore[attr-defined]
    reclaimed_box: dict[str, object] = {}

    async def _mirror_expire_and_reclaim(request):  # type: ignore[no-untyped-def]
        result = await original_mirror(request)
        async with session_scope(session_factory) as session:
            expired = await db_now(session) - timedelta(seconds=5)
            await session.execute(
                update(AmoCrmMirrorJob)
                .where(AmoCrmMirrorJob.id == claim.job_id)
                .values(lease_until=expired)
            )
        second_worker = _worker(session_factory, transport, worker_id="crm-mid-b")
        reclaimed = await second_worker.claim_one()
        assert reclaimed is not None
        assert reclaimed.job_id == claim.job_id
        assert reclaimed.lease_token != claim.lease_token
        assert reclaimed.lease_version == claim.lease_version + 1
        reclaimed_box["claim"] = reclaimed
        reclaimed_box["worker"] = second_worker
        return result

    first.adapter.mirror = _mirror_expire_and_reclaim  # type: ignore[method-assign]

    with pytest.raises(StaleAmoCrmMirrorLeaseError):
        await first.process_claimed(claim)

    assert len(_lead_posts(transport)) == 1
    assert len(first.adapter.calls) == 1  # type: ignore[attr-defined]
    assert "claim" in reclaimed_box

    async with session_scope(session_factory) as session:
        open_row = await entity_links.get_open(
            session,
            conversation_id=claim.conversation_id,
            entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
        )
        assert open_row is not None
        assert open_row.status == AmocrmEntityLinkStatus.ACTIVE.value
        assert open_row.external_id == "4242"
        job = await mirror_repo.get_by_id(session, job_id=claim.job_id)
        assert job is not None
        assert job.status == AmoCrmMirrorStatus.PROCESSING.value

    second = reclaimed_box["worker"]
    reclaimed = reclaimed_box["claim"]
    result = await second.process_claimed(reclaimed)  # type: ignore[attr-defined]
    assert result.status == AmoCrmMirrorStatus.MIRRORED.value
    assert len(_lead_posts(transport)) == 1
    assert any("/api/v4/leads/4242" in c.url for c in transport.calls)

    async with session_scope(session_factory) as session:
        rows = (
            await session.scalars(
                select(AmocrmEntityLink).where(
                    AmocrmEntityLink.conversation_id == claim.conversation_id,
                    AmocrmEntityLink.entity_kind
                    == AmocrmEntityKind.TECHNICAL_DEAL.value,
                    AmocrmEntityLink.status == AmocrmEntityLinkStatus.ACTIVE.value,
                )
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].external_id == "4242"
