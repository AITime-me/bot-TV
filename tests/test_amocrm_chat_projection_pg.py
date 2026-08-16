"""AMO-01B1 PostgreSQL: CLIENT_INBOUND (B1a) + BOT_OUTBOUND (B1b) Chat projection."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_chat_egress_config import AmoCrmChatEgressConfig
from app.core.amocrm_chat_egress_http import (
    AmoCrmChatEgressHttpClient,
    AmoCrmChatEgressOutcome,
    AmoCrmChatHistoryScan,
    AmoCrmChatHistoryScanResult,
    AmoCrmChatSendResult,
)
from app.core.amocrm_manager_ids import amocrm_manager_namespaced_id
from app.core.booking_types import BookingDialogAction
from app.db.session import session_scope
from app.models.amocrm_message_projection import (
    AmocrmMessageProjection,
    AmocrmProjectionSourceKind,
    AmocrmProjectionStatus,
)
from app.models.conversation import Conversation, HandoffState
from app.models.ingress import IngressStatus
from app.models.manager_message import ManagerMessage, ManagerMessageStatus
from app.models.outbox import DeliveryStatus, DestinationType, OutboxMessage
from app.repositories import amocrm_chat_bindings as binding_repo
from app.repositories import amocrm_message_projections as projection_repo
from app.repositories import outbound as outbound_repo
from app.repositories.amocrm_message_projections import StaleAmocrmProjectionLeaseError
from app.schemas.amocrm_manager_ingress import AmoCrmManagerIngressEvent
from app.schemas.inbound import SyntheticInboundEvent
from app.services.amocrm_chat_projection import (
    AmocrmChatProjectionWorker,
    enqueue_bot_outbound_projection,
)
from app.services.amocrm_manager_ingress import AmoCrmManagerIngressAdapter
from app.services.inbound import InboundService
from app.services.ingress import IngressWorker
from tests.pg_harness import truncate_foundation_tables

_SECRET = "t" * 32
_SCOPE = "scope-pg-test-01"
_INTEG_CID = "integ-conv-pg-1"


@pytest_asyncio.fixture(autouse=True)
async def cleanup(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


class _ScriptedClient(AmoCrmChatEgressHttpClient):
    """Bypass require_runtime HTTP; script send/history outcomes."""

    def __init__(self) -> None:
        self.send_results: list[AmoCrmChatSendResult] = []
        self.scan_results: list[AmoCrmChatHistoryScanResult] = []
        self.send_calls = 0
        self.history_calls = 0
        self.send_texts: list[str] = []
        self.last_send_kwargs: dict[str, object] | None = None
        self.last_history_kwargs: dict[str, object] | None = None

    def send_silent_text(self, **kwargs: object) -> AmoCrmChatSendResult:
        self.send_calls += 1
        self.last_send_kwargs = dict(kwargs)
        text = kwargs.get("text")
        if type(text) is str:
            self.send_texts.append(text)
        if not self.send_results:
            raise AssertionError("no send result")
        return self.send_results.pop(0)

    def scan_msgid_in_history(self, **kwargs: object) -> AmoCrmChatHistoryScanResult:
        self.history_calls += 1
        self.last_history_kwargs = dict(kwargs)
        if not self.scan_results:
            return AmoCrmChatHistoryScanResult(scan=AmoCrmChatHistoryScan.ABSENT)
        return self.scan_results.pop(0)


async def _seed_bound(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    chat_id: str = "amo-chat-proj-1",
    integration_conversation_id: str | None = _INTEG_CID,
) -> Conversation:
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id="synth-proj-1",
                external_message_id=f"client-{uuid4().hex[:10]}",
                text="client seed",
            )
        )
        conversation = accepted.conversation
        await binding_repo.insert_active_if_absent(
            session,
            conversation_id=conversation.id,
            amocrm_chat_id=chat_id,
            integration_conversation_id=integration_conversation_id,
        )
        return conversation


@pytest.mark.asyncio
async def test_enqueue_off_by_default(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_bound(session_factory)
    async with session_factory() as session:
        async with session.begin():
            count = await session.scalar(
                select(func.count()).select_from(AmocrmMessageProjection)
            )
            assert count == 0


@pytest.mark.asyncio
async def test_success_and_idempotent_enqueue(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seed binding while egress is OFF so the fixture inbound does not enqueue.
    conversation = await _seed_bound(session_factory)
    monkeypatch.setenv("AMOCRM_CHAT_EGRESS_ENABLED", "true")
    monkeypatch.setenv("AMOCRM_CHAT_CHANNEL_SECRET", _SECRET)
    monkeypatch.setenv("AMOCRM_CHAT_SCOPE_ID", _SCOPE)

    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-proj-1",
                text="project me",
            )
        )
        inbox_id = accepted.inbox.id
        await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-proj-1",
                text="project me",
            )
        )

    async with session_factory() as session:
        async with session.begin():
            rows = (await session.scalars(select(AmocrmMessageProjection))).all()
            assert len(rows) == 1
            assert rows[0].source_id == inbox_id
            assert rows[0].status == AmocrmProjectionStatus.PENDING.value
            assert "project me" not in json.dumps(
                {
                    c.name: getattr(rows[0], c.name)
                    for c in rows[0].__table__.columns
                    if c.name
                    not in {"id", "conversation_id", "source_id", "correlation_id"}
                },
                default=str,
            )

    fake = _ScriptedClient()
    fake.send_results.append(
        AmoCrmChatSendResult(
            outcome=AmoCrmChatEgressOutcome.SUCCESS,
            amocrm_message_id="amo-projected-1",
        )
    )
    worker = AmocrmChatProjectionWorker(
        session_factory,
        worker_id="proj-worker-1",
        config=AmoCrmChatEgressConfig(
            enabled=True,
            channel_secret=_SECRET,
            scope_id=_SCOPE,
        ),
        http_client=fake,
    )
    claim = await worker.claim_one()
    assert claim is not None
    result = await worker.process_claimed(claim)
    assert result.projected is True
    assert fake.send_calls == 1

    async with session_factory() as session:
        async with session.begin():
            row = (await session.scalars(select(AmocrmMessageProjection))).one()
            assert row.status == AmocrmProjectionStatus.PROJECTED.value
            assert row.amocrm_message_id == "amo-projected-1"


@pytest.mark.asyncio
async def test_stale_lease_wrong_version_zero_http(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.models.amocrm_message_projection import AmocrmProjectionSourceKind

    conversation = await _seed_bound(session_factory)
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-stale-1",
                text="stale path",
            )
        )
        await projection_repo.enqueue_if_absent(
            session,
            conversation_id=conversation.id,
            source_kind=AmocrmProjectionSourceKind.CLIENT_INBOUND,
            source_id=accepted.inbox.id,
            correlation_id=uuid4(),
        )

    fake = _ScriptedClient()
    worker = AmocrmChatProjectionWorker(
        session_factory,
        worker_id="proj-stale",
        config=AmoCrmChatEgressConfig(
            enabled=True,
            channel_secret=_SECRET,
            scope_id=_SCOPE,
        ),
        http_client=fake,
    )
    claim = await worker.claim_one()
    assert claim is not None
    bad = claim.__class__(
        projection_id=claim.projection_id,
        conversation_id=claim.conversation_id,
        source_kind=claim.source_kind,
        source_id=claim.source_id,
        integration_msgid=claim.integration_msgid,
        amocrm_message_id=claim.amocrm_message_id,
        status=claim.status,
        attempt_count=claim.attempt_count,
        max_attempts=claim.max_attempts,
        lease_owner=claim.lease_owner,
        lease_token=claim.lease_token,
        lease_version=claim.lease_version + 99,
        lease_until=claim.lease_until,
        correlation_id=claim.correlation_id,
    )
    with pytest.raises(StaleAmocrmProjectionLeaseError):
        await worker.process_claimed(bad)
    assert fake.send_calls == 0
    assert fake.history_calls == 0


@pytest.mark.asyncio
async def test_expired_lease_zero_http(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.models.amocrm_message_projection import AmocrmProjectionSourceKind

    conversation = await _seed_bound(session_factory)
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-expire-1",
                text="expire path",
            )
        )
        await projection_repo.enqueue_if_absent(
            session,
            conversation_id=conversation.id,
            source_kind=AmocrmProjectionSourceKind.CLIENT_INBOUND,
            source_id=accepted.inbox.id,
            correlation_id=uuid4(),
        )

    fake = _ScriptedClient()
    worker = AmocrmChatProjectionWorker(
        session_factory,
        worker_id="proj-expire",
        config=AmoCrmChatEgressConfig(
            enabled=True,
            channel_secret=_SECRET,
            scope_id=_SCOPE,
        ),
        http_client=fake,
    )
    claim = await worker.claim_one()
    assert claim is not None
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE amocrm_message_projections "
                    "SET lease_until=:past WHERE id=:id"
                ),
                {"past": past, "id": claim.projection_id},
            )
    expired = claim.__class__(
        projection_id=claim.projection_id,
        conversation_id=claim.conversation_id,
        source_kind=claim.source_kind,
        source_id=claim.source_id,
        integration_msgid=claim.integration_msgid,
        amocrm_message_id=claim.amocrm_message_id,
        status=claim.status,
        attempt_count=claim.attempt_count,
        max_attempts=claim.max_attempts,
        lease_owner=claim.lease_owner,
        lease_token=claim.lease_token,
        lease_version=claim.lease_version,
        lease_until=past,
        correlation_id=claim.correlation_id,
    )
    with pytest.raises(StaleAmocrmProjectionLeaseError):
        await worker.process_claimed(expired)
    assert fake.send_calls == 0
    assert fake.history_calls == 0


@pytest.mark.asyncio
async def test_ambiguous_send_reconcile_no_blind_resend(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_bound(session_factory)
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-reconcile-1",
                text="reconcile me",
            )
        )
        from app.models.amocrm_message_projection import AmocrmProjectionSourceKind

        await projection_repo.enqueue_if_absent(
            session,
            conversation_id=conversation.id,
            source_kind=AmocrmProjectionSourceKind.CLIENT_INBOUND,
            source_id=accepted.inbox.id,
            correlation_id=uuid4(),
        )

    fake = _ScriptedClient()
    fake.send_results.append(
        AmoCrmChatSendResult(
            outcome=AmoCrmChatEgressOutcome.TRANSIENT_ERROR,
            error_code="AMOCRM_CHAT_HTTP_503",
        )
    )
    worker = AmocrmChatProjectionWorker(
        session_factory,
        worker_id="proj-recon",
        config=AmoCrmChatEgressConfig(
            enabled=True,
            channel_secret=_SECRET,
            scope_id=_SCOPE,
        ),
        http_client=fake,
    )
    claim1 = await worker.claim_one()
    assert claim1 is not None
    with pytest.raises(RuntimeError):
        await worker.process_claimed(claim1)
    assert fake.send_calls == 1
    assert fake.history_calls == 0  # no post-send blind history/resend

    fake.scan_results.append(
        AmoCrmChatHistoryScanResult(
            scan=AmoCrmChatHistoryScan.FOUND,
            amocrm_message_id="amo-from-history",
        )
    )
    # Transient fail schedules next_attempt_at (+DEFAULT_RETRY_DELAY_SECONDS).
    # Advance claim clock past that fence; do not sleep or weaken production delay.
    later = datetime.now(timezone.utc) + timedelta(seconds=2)
    claim2 = await worker.claim_one(now=later)
    assert claim2 is not None
    assert claim2.attempt_count >= 2
    result = await worker.process_claimed(claim2, now=later)
    assert result.projected is True
    assert fake.send_calls == 1
    assert fake.history_calls == 1
    assert fake.last_history_kwargs is not None
    assert fake.last_history_kwargs.get("amocrm_chat_id") == "amo-chat-proj-1"
    assert "integration_conversation_id" not in fake.last_history_kwargs
    assert fake.last_send_kwargs is not None
    assert fake.last_send_kwargs.get("integration_conversation_id") == _INTEG_CID
    assert fake.last_send_kwargs.get("conversation_ref_id") == "amo-chat-proj-1"


@pytest.mark.asyncio
async def test_ambiguous_retry_absent_allows_one_resend(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_bound(session_factory)
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-absent-1",
                text="absent then send",
            )
        )
        from app.models.amocrm_message_projection import AmocrmProjectionSourceKind

        await projection_repo.enqueue_if_absent(
            session,
            conversation_id=conversation.id,
            source_kind=AmocrmProjectionSourceKind.CLIENT_INBOUND,
            source_id=accepted.inbox.id,
            correlation_id=uuid4(),
        )

    fake = _ScriptedClient()
    fake.send_results.append(
        AmoCrmChatSendResult(
            outcome=AmoCrmChatEgressOutcome.TRANSIENT_ERROR,
            error_code="AMOCRM_CHAT_HTTP_503",
        )
    )
    worker = AmocrmChatProjectionWorker(
        session_factory,
        worker_id="proj-absent",
        config=AmoCrmChatEgressConfig(
            enabled=True,
            channel_secret=_SECRET,
            scope_id=_SCOPE,
        ),
        http_client=fake,
    )
    claim1 = await worker.claim_one()
    assert claim1 is not None
    with pytest.raises(RuntimeError):
        await worker.process_claimed(claim1)
    assert fake.send_calls == 1

    fake.scan_results.append(
        AmoCrmChatHistoryScanResult(scan=AmoCrmChatHistoryScan.ABSENT)
    )
    fake.send_results.append(
        AmoCrmChatSendResult(
            outcome=AmoCrmChatEgressOutcome.SUCCESS,
            amocrm_message_id="amo-after-absent",
        )
    )
    later = datetime.now(timezone.utc) + timedelta(seconds=2)
    claim2 = await worker.claim_one(now=later)
    assert claim2 is not None
    assert claim2.attempt_count >= 2
    result = await worker.process_claimed(claim2, now=later)
    assert result.projected is True
    assert fake.history_calls == 1
    assert fake.send_calls == 2
    assert fake.last_history_kwargs is not None
    assert fake.last_history_kwargs.get("amocrm_chat_id") == "amo-chat-proj-1"


@pytest.mark.asyncio
async def test_ambiguous_retry_uncertain_fail_closed_no_resend(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_bound(session_factory)
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-uncertain-1",
                text="uncertain history",
            )
        )
        from app.models.amocrm_message_projection import AmocrmProjectionSourceKind

        await projection_repo.enqueue_if_absent(
            session,
            conversation_id=conversation.id,
            source_kind=AmocrmProjectionSourceKind.CLIENT_INBOUND,
            source_id=accepted.inbox.id,
            correlation_id=uuid4(),
        )

    fake = _ScriptedClient()
    fake.send_results.append(
        AmoCrmChatSendResult(
            outcome=AmoCrmChatEgressOutcome.TRANSIENT_ERROR,
            error_code="AMOCRM_CHAT_HTTP_503",
        )
    )
    worker = AmocrmChatProjectionWorker(
        session_factory,
        worker_id="proj-uncertain",
        config=AmoCrmChatEgressConfig(
            enabled=True,
            channel_secret=_SECRET,
            scope_id=_SCOPE,
        ),
        http_client=fake,
    )
    claim1 = await worker.claim_one()
    assert claim1 is not None
    with pytest.raises(RuntimeError):
        await worker.process_claimed(claim1)
    assert fake.send_calls == 1

    fake.scan_results.append(
        AmoCrmChatHistoryScanResult(
            scan=AmoCrmChatHistoryScan.UNCERTAIN,
            error_code="AMOCRM_CHAT_HISTORY_TRANSIENT",
        )
    )
    later = datetime.now(timezone.utc) + timedelta(seconds=2)
    claim2 = await worker.claim_one(now=later)
    assert claim2 is not None
    assert claim2.attempt_count >= 2
    with pytest.raises(RuntimeError, match="AMOCRM_CHAT_HISTORY"):
        await worker.process_claimed(claim2, now=later)
    assert fake.history_calls == 1
    assert fake.send_calls == 1
    assert fake.last_history_kwargs is not None
    assert fake.last_history_kwargs.get("amocrm_chat_id") == "amo-chat-proj-1"


@pytest.mark.asyncio
async def test_processing_with_amo_id_echo_suppress(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_bound(session_factory)
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-echo-proc-1",
                text="echo src",
            )
        )
        from app.models.amocrm_message_projection import AmocrmProjectionSourceKind

        row, _ = await projection_repo.enqueue_if_absent(
            session,
            conversation_id=conversation.id,
            source_kind=AmocrmProjectionSourceKind.CLIENT_INBOUND,
            source_id=accepted.inbox.id,
            correlation_id=uuid4(),
        )
        await session.execute(
            text(
                "UPDATE amocrm_message_projections "
                "SET status='PROCESSING', amocrm_message_id=:amo, "
                "lease_owner='w', lease_token=:tok, lease_version=1, "
                "lease_until=statement_timestamp() + interval '30 seconds' "
                "WHERE id=:id"
            ),
            {"amo": "amo-echo-proc-1", "id": row.id, "tok": uuid4()},
        )
        epoch_before = conversation.manager_epoch

    namespaced = amocrm_manager_namespaced_id(
        amocrm_chat_id="amo-chat-proj-1",
        amocrm_message_id="amo-echo-proc-1",
    )
    adapter = AmoCrmManagerIngressAdapter(session_factory)
    await adapter.accept(
        AmoCrmManagerIngressEvent(
            amocrm_chat_id="amo-chat-proj-1",
            amocrm_message_id="amo-echo-proc-1",
            external_message_id=namespaced,
            provider_sequence=1,
            text="echo should be ignored",
            conversation_client_id=_INTEG_CID,
        )
    )
    worker = IngressWorker(session_factory, worker_id="echo-proc-worker")
    claim = await worker.claim_one()
    assert claim is not None
    result = await worker.process_claimed(claim)
    assert result.status == IngressStatus.PROCESSED.value

    async with session_factory() as session:
        async with session.begin():
            conv = await session.get(Conversation, conversation.id)
            assert conv is not None
            assert conv.manager_epoch == epoch_before
            assert conv.handoff_state == HandoffState.BOT_ACTIVE.value


@pytest.mark.asyncio
async def test_projected_webhook_no_manager_epoch_change(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_bound(session_factory)
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-echo-1",
                text="echo src",
            )
        )
        from app.models.amocrm_message_projection import AmocrmProjectionSourceKind

        row, _ = await projection_repo.enqueue_if_absent(
            session,
            conversation_id=conversation.id,
            source_kind=AmocrmProjectionSourceKind.CLIENT_INBOUND,
            source_id=accepted.inbox.id,
            correlation_id=uuid4(),
        )
        await session.execute(
            text(
                "UPDATE amocrm_message_projections "
                "SET status='PROJECTED', amocrm_message_id=:amo "
                "WHERE id=:id"
            ),
            {"amo": "amo-echo-msg-1", "id": row.id},
        )
        epoch_before = conversation.manager_epoch

    namespaced = amocrm_manager_namespaced_id(
        amocrm_chat_id="amo-chat-proj-1",
        amocrm_message_id="amo-echo-msg-1",
    )
    adapter = AmoCrmManagerIngressAdapter(session_factory)
    await adapter.accept(
        AmoCrmManagerIngressEvent(
            amocrm_chat_id="amo-chat-proj-1",
            amocrm_message_id="amo-echo-msg-1",
            external_message_id=namespaced,
            provider_sequence=1,
            text="echo should be ignored",
        )
    )
    worker = IngressWorker(session_factory, worker_id="echo-worker")
    claim = await worker.claim_one()
    assert claim is not None
    result = await worker.process_claimed(claim)
    assert result.status == IngressStatus.PROCESSED.value

    async with session_factory() as session:
        async with session.begin():
            conv = await session.get(Conversation, conversation.id)
            assert conv is not None
            assert conv.manager_epoch == epoch_before
            assert conv.handoff_state == HandoffState.BOT_ACTIVE.value
            applied = await session.scalar(
                select(func.count())
                .select_from(ManagerMessage)
                .where(ManagerMessage.status == ManagerMessageStatus.APPLIED.value)
            )
            assert applied == 0


@pytest.mark.asyncio
async def test_real_manager_webhook_takeover(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await _seed_bound(session_factory)
    namespaced = amocrm_manager_namespaced_id(
        amocrm_chat_id="amo-chat-proj-1",
        amocrm_message_id="amo-real-mgr-1",
    )
    adapter = AmoCrmManagerIngressAdapter(session_factory)
    await adapter.accept(
        AmoCrmManagerIngressEvent(
            amocrm_chat_id="amo-chat-proj-1",
            amocrm_message_id="amo-real-mgr-1",
            external_message_id=namespaced,
            provider_sequence=5,
            text="real manager",
            conversation_client_id=_INTEG_CID,
        )
    )
    worker = IngressWorker(session_factory, worker_id="mgr-worker")
    claim = await worker.claim_one()
    assert claim is not None
    await worker.process_claimed(claim)

    async with session_factory() as session:
        async with session.begin():
            conv = await session.get(Conversation, conversation.id)
            assert conv is not None
            assert conv.handoff_state == HandoffState.HUMAN_ACTIVE.value
            assert conv.manager_epoch == 1


@pytest.mark.asyncio
async def test_missing_binding_or_integ_cid_fail_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id="unbound-conv",
                external_message_id="client-unbound-1",
                text="no binding",
            )
        )
        from app.models.amocrm_message_projection import AmocrmProjectionSourceKind

        await projection_repo.enqueue_if_absent(
            session,
            conversation_id=accepted.conversation.id,
            source_kind=AmocrmProjectionSourceKind.CLIENT_INBOUND,
            source_id=accepted.inbox.id,
            correlation_id=uuid4(),
        )

    fake = _ScriptedClient()
    worker = AmocrmChatProjectionWorker(
        session_factory,
        worker_id="proj-unbound",
        config=AmoCrmChatEgressConfig(
            enabled=True,
            channel_secret=_SECRET,
            scope_id=_SCOPE,
        ),
        http_client=fake,
    )
    claim = await worker.claim_one()
    assert claim is not None
    result = await worker.process_claimed(claim)
    assert result.projected is False
    assert result.skip_reason == "BINDING_UNKNOWN"
    assert fake.send_calls == 0

    conversation = await _seed_bound(
        session_factory,
        chat_id="amo-chat-no-integ",
        integration_conversation_id=None,
    )
    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-no-integ-1",
                text="missing integ",
            )
        )
        from app.models.amocrm_message_projection import AmocrmProjectionSourceKind

        await projection_repo.enqueue_if_absent(
            session,
            conversation_id=conversation.id,
            source_kind=AmocrmProjectionSourceKind.CLIENT_INBOUND,
            source_id=accepted.inbox.id,
            correlation_id=uuid4(),
        )
    claim2 = await worker.claim_one()
    assert claim2 is not None
    result2 = await worker.process_claimed(claim2)
    assert result2.projected is False
    assert result2.skip_reason == "BINDING_INTEGRATION_CONVERSATION_MISSING"
    assert fake.send_calls == 0


@pytest.mark.asyncio
async def test_token_only_delivered_outbound_no_bot_projection_or_http(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AMO-01B1b: token-only / missing text never creates/sends bot projection."""

    monkeypatch.setenv("AMOCRM_CHAT_EGRESS_ENABLED", "true")
    monkeypatch.setenv("AMOCRM_CHAT_CHANNEL_SECRET", _SECRET)
    monkeypatch.setenv("AMOCRM_CHAT_SCOPE_ID", _SCOPE)

    conversation = await _seed_bound(session_factory)
    fake = _ScriptedClient()
    worker = AmocrmChatProjectionWorker(
        session_factory,
        worker_id="proj-no-bot",
        config=AmoCrmChatEgressConfig(
            enabled=True,
            channel_secret=_SECRET,
            scope_id=_SCOPE,
        ),
        http_client=fake,
    )

    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-for-bot-defer-1",
                text="client stays projectable",
            )
        )
        assert accepted.reply_plan is not None
        outbound, created = await outbound_repo.insert_synthetic_outbound_if_absent(
            session,
            conversation_id=conversation.id,
            reply_plan_id=accepted.reply_plan.id,
            context_version=accepted.context_version,
            manager_epoch=accepted.conversation.manager_epoch,
            event_seq_hwm=accepted.conversation.current_event_seq,
            payload_json={
                "schema": "synthetic.outbound.v1",
                "synthetic_token": "SYNTHETIC_OK",
            },
            correlation_id=uuid4(),
            not_before=datetime.now(timezone.utc),
        )
        assert created is True
        outbound.delivery_status = DeliveryStatus.DELIVERED.value
        outbound.admitted_at = datetime.now(timezone.utc)
        assert (
            await enqueue_bot_outbound_projection(
                session,
                conversation_id=conversation.id,
                outbound_id=outbound.id,
                correlation_id=uuid4(),
                egress_enabled=True,
            )
            is None
        )

    async with session_factory() as session:
        async with session.begin():
            bot_count = await session.scalar(
                select(func.count())
                .select_from(AmocrmMessageProjection)
                .where(
                    AmocrmMessageProjection.source_kind
                    == AmocrmProjectionSourceKind.BOT_OUTBOUND.value
                )
            )
            assert bot_count == 0
            client_count = await session.scalar(
                select(func.count())
                .select_from(AmocrmMessageProjection)
                .where(
                    AmocrmMessageProjection.source_kind
                    == AmocrmProjectionSourceKind.CLIENT_INBOUND.value
                )
            )
            assert client_count >= 1

    # Drain CLIENT_INBOUND only; no bot HTTP.
    while True:
        claim = await worker.claim_one()
        if claim is None:
            break
        assert claim.source_kind == AmocrmProjectionSourceKind.CLIENT_INBOUND.value
        fake.send_results.append(
            AmoCrmChatSendResult(
                outcome=AmoCrmChatEgressOutcome.SUCCESS,
                amocrm_message_id=f"amo-client-only-{fake.send_calls + 1}",
            )
        )
        result = await worker.process_claimed(claim)
        assert result.projected is True
    assert fake.send_calls >= 1
    assert fake.history_calls == 0

    # Defense: if a BOT_OUTBOUND row is force-enqueued with token-only payload, skip.
    async with session_scope(session_factory) as session:
        outbound_row = (
            await session.scalars(
                select(OutboxMessage).where(
                    OutboxMessage.destination_type
                    == DestinationType.SYNTHETIC_OUTBOUND.value
                )
            )
        ).one()
        await projection_repo.enqueue_if_absent(
            session,
            conversation_id=conversation.id,
            source_kind=AmocrmProjectionSourceKind.BOT_OUTBOUND,
            source_id=outbound_row.id,
            correlation_id=uuid4(),
        )
    before_sends = fake.send_calls
    claim_bot = await worker.claim_one()
    assert claim_bot is not None
    assert claim_bot.source_kind == AmocrmProjectionSourceKind.BOT_OUTBOUND.value
    skipped = await worker.process_claimed(claim_bot)
    assert skipped.projected is False
    assert skipped.skip_reason == "SOURCE_EMPTY_TEXT"
    assert fake.send_calls == before_sends


async def _insert_delivered_bot_outbound(
    session: AsyncSession,
    *,
    conversation: Conversation,
    reply_plan_id: object,
    context_version: int,
    manager_epoch: int,
    event_seq_hwm: int,
    payload_json: dict[str, object],
) -> OutboxMessage:
    outbound, created = await outbound_repo.insert_synthetic_outbound_if_absent(
        session,
        conversation_id=conversation.id,
        reply_plan_id=reply_plan_id,  # type: ignore[arg-type]
        context_version=context_version,
        manager_epoch=manager_epoch,
        event_seq_hwm=event_seq_hwm,
        payload_json=payload_json,
        correlation_id=uuid4(),
        not_before=datetime.now(timezone.utc),
    )
    assert created is True
    outbound.delivery_status = DeliveryStatus.DELIVERED.value
    outbound.admitted_at = datetime.now(timezone.utc)
    return outbound


@pytest.mark.asyncio
async def test_b1b_delivered_text_projects_exact_outbox_body(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELIVERED persisted text в†’ one BOT_OUTBOUND; Chat body == DB text."""

    monkeypatch.setenv("AMOCRM_CHAT_EGRESS_ENABLED", "true")
    monkeypatch.setenv("AMOCRM_CHAT_CHANNEL_SECRET", _SECRET)
    monkeypatch.setenv("AMOCRM_CHAT_SCOPE_ID", _SCOPE)

    conversation = await _seed_bound(session_factory)
    durable = "exact durable bot reply body for amo chat"
    outbound_id = None

    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-b1b-exact-1",
                text="client inbound stays separate",
            )
        )
        assert accepted.reply_plan is not None
        outbound = await _insert_delivered_bot_outbound(
            session,
            conversation=accepted.conversation,
            reply_plan_id=accepted.reply_plan.id,
            context_version=accepted.context_version,
            manager_epoch=accepted.conversation.manager_epoch,
            event_seq_hwm=accepted.conversation.current_event_seq,
            payload_json={
                "schema": "synthetic.outbound.v1",
                "synthetic_token": "SYNTHETIC_OK",
                "draft_text": "must never project draft",
                "text": durable,
            },
        )
        outbound_id = outbound.id
        enqueued = await enqueue_bot_outbound_projection(
            session,
            conversation_id=conversation.id,
            outbound_id=outbound.id,
            correlation_id=uuid4(),
            egress_enabled=True,
        )
        assert enqueued is not None
        row, created = enqueued
        assert created is True
        assert row.source_kind == AmocrmProjectionSourceKind.BOT_OUTBOUND.value
        # Idempotent second enqueue.
        again = await enqueue_bot_outbound_projection(
            session,
            conversation_id=conversation.id,
            outbound_id=outbound.id,
            correlation_id=uuid4(),
            egress_enabled=True,
        )
        assert again is not None
        assert again[0].id == row.id
        assert again[1] is False

    async with session_factory() as session:
        async with session.begin():
            bot_rows = (
                await session.scalars(
                    select(AmocrmMessageProjection).where(
                        AmocrmMessageProjection.source_kind
                        == AmocrmProjectionSourceKind.BOT_OUTBOUND.value
                    )
                )
            ).all()
            assert len(bot_rows) == 1
            assert bot_rows[0].source_id == outbound_id
            assert durable not in json.dumps(
                {
                    c.name: getattr(bot_rows[0], c.name)
                    for c in bot_rows[0].__table__.columns
                    if c.name
                    not in {"id", "conversation_id", "source_id", "correlation_id"}
                },
                default=str,
            )
            outbox = await session.get(OutboxMessage, outbound_id)
            assert outbox is not None
            assert outbox.payload_json.get("text") == durable

    fake = _ScriptedClient()
    worker = AmocrmChatProjectionWorker(
        session_factory,
        worker_id="proj-b1b-exact",
        config=AmoCrmChatEgressConfig(
            enabled=True,
            channel_secret=_SECRET,
            scope_id=_SCOPE,
        ),
        http_client=fake,
    )
    # Drain CLIENT_INBOUND first; BOT_OUTBOUND body must equal DB text.
    while True:
        claim = await worker.claim_one()
        if claim is None:
            break
        if claim.source_kind == AmocrmProjectionSourceKind.CLIENT_INBOUND.value:
            fake.send_results.append(
                AmoCrmChatSendResult(
                    outcome=AmoCrmChatEgressOutcome.SUCCESS,
                    amocrm_message_id=f"amo-client-{fake.send_calls + 1}",
                )
            )
            await worker.process_claimed(claim)
            continue
        assert claim.source_kind == AmocrmProjectionSourceKind.BOT_OUTBOUND.value
        assert claim.source_id == outbound_id
        fake.send_results.append(
            AmoCrmChatSendResult(
                outcome=AmoCrmChatEgressOutcome.SUCCESS,
                amocrm_message_id="amo-bot-exact-1",
            )
        )
        result = await worker.process_claimed(claim)
        assert result.projected is True
        assert fake.send_texts[-1] == durable
        assert "must never project draft" not in fake.send_texts[-1]
        break
    else:
        raise AssertionError("BOT_OUTBOUND claim was never processed")

    async with session_factory() as session:
        async with session.begin():
            bot = (
                await session.scalars(
                    select(AmocrmMessageProjection).where(
                        AmocrmMessageProjection.source_kind
                        == AmocrmProjectionSourceKind.BOT_OUTBOUND.value
                    )
                )
            ).one()
            assert bot.status == AmocrmProjectionStatus.PROJECTED.value
            assert bot.amocrm_message_id == "amo-bot-exact-1"


@pytest.mark.asyncio
async def test_b1b_ambiguous_retry_reconcile_at_most_one_remote(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate/retry/reclaim в†’ history reconcile, at most one Chat POST."""

    monkeypatch.setenv("AMOCRM_CHAT_EGRESS_ENABLED", "true")
    monkeypatch.setenv("AMOCRM_CHAT_CHANNEL_SECRET", _SECRET)
    monkeypatch.setenv("AMOCRM_CHAT_SCOPE_ID", _SCOPE)

    conversation = await _seed_bound(session_factory)
    durable = "bot reply for reconcile"

    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-b1b-recon-1",
                text="client",
            )
        )
        assert accepted.reply_plan is not None
        outbound = await _insert_delivered_bot_outbound(
            session,
            conversation=accepted.conversation,
            reply_plan_id=accepted.reply_plan.id,
            context_version=accepted.context_version,
            manager_epoch=accepted.conversation.manager_epoch,
            event_seq_hwm=accepted.conversation.current_event_seq,
            payload_json={
                "schema": "synthetic.outbound.v1",
                "synthetic_token": "SYNTHETIC_OK",
                "text": durable,
            },
        )
        await enqueue_bot_outbound_projection(
            session,
            conversation_id=conversation.id,
            outbound_id=outbound.id,
            correlation_id=uuid4(),
            egress_enabled=True,
        )

    fake = _ScriptedClient()
    worker = AmocrmChatProjectionWorker(
        session_factory,
        worker_id="proj-b1b-recon",
        config=AmoCrmChatEgressConfig(
            enabled=True,
            channel_secret=_SECRET,
            scope_id=_SCOPE,
        ),
        http_client=fake,
    )

    # Drain CLIENT_INBOUND projections first.
    while True:
        claim = await worker.claim_one()
        if claim is None:
            break
        if claim.source_kind != AmocrmProjectionSourceKind.BOT_OUTBOUND.value:
            fake.send_results.append(
                AmoCrmChatSendResult(
                    outcome=AmoCrmChatEgressOutcome.SUCCESS,
                    amocrm_message_id=f"amo-cli-{fake.send_calls + 1}",
                )
            )
            await worker.process_claimed(claim)
            continue

        # Baselines after CLIENT_INBOUND drain; BOT_OUTBOUND deltas only.
        sends_before = fake.send_calls
        history_before = fake.history_calls
        texts_before = len(fake.send_texts)

        fake.send_results.append(
            AmoCrmChatSendResult(
                outcome=AmoCrmChatEgressOutcome.TRANSIENT_ERROR,
                error_code="AMOCRM_CHAT_HTTP_503",
            )
        )
        with pytest.raises(RuntimeError):
            await worker.process_claimed(claim)
        assert fake.send_calls == sends_before + 1
        assert fake.history_calls == history_before
        assert len(fake.send_texts) == texts_before + 1
        assert fake.send_texts[-1] == durable

        fake.scan_results.append(
            AmoCrmChatHistoryScanResult(
                scan=AmoCrmChatHistoryScan.FOUND,
                amocrm_message_id="amo-bot-from-history",
            )
        )
        later = datetime.now(timezone.utc) + timedelta(seconds=2)
        claim2 = await worker.claim_one(now=later)
        assert claim2 is not None
        assert claim2.source_kind == AmocrmProjectionSourceKind.BOT_OUTBOUND.value
        assert claim2.attempt_count >= 2
        result = await worker.process_claimed(claim2, now=later)
        assert result.projected is True
        # History reconcile: no second BOT_OUTBOUND POST.
        assert fake.send_calls == sends_before + 1
        assert fake.history_calls == history_before + 1
        assert len(fake.send_texts) == texts_before + 1
        assert fake.send_texts[-1] == durable
        assert fake.last_history_kwargs is not None
        assert fake.last_history_kwargs.get("amocrm_chat_id") == "amo-chat-proj-1"
        assert "integration_conversation_id" not in fake.last_history_kwargs
        break
    else:
        raise AssertionError("BOT_OUTBOUND claim missing")


@pytest.mark.asyncio
async def test_b1b_machine_only_and_missing_text_zero_projection(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AMOCRM_CHAT_EGRESS_ENABLED", "true")
    monkeypatch.setenv("AMOCRM_CHAT_CHANNEL_SECRET", _SECRET)
    monkeypatch.setenv("AMOCRM_CHAT_SCOPE_ID", _SCOPE)

    conversation = await _seed_bound(session_factory)

    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-b1b-machine-1",
                text="client",
            )
        )
        assert accepted.reply_plan is not None
        machine = await _insert_delivered_bot_outbound(
            session,
            conversation=accepted.conversation,
            reply_plan_id=accepted.reply_plan.id,
            context_version=accepted.context_version,
            manager_epoch=accepted.conversation.manager_epoch,
            event_seq_hwm=accepted.conversation.current_event_seq,
            payload_json={
                "schema": "synthetic.outbound.v1",
                "synthetic_token": "SYNTHETIC_OK",
                "booking_action": BookingDialogAction.OFFER_DAYS.value,
                "booking_available_date_keys": ["2026-08-06"],
            },
        )
        assert (
            await enqueue_bot_outbound_projection(
                session,
                conversation_id=conversation.id,
                outbound_id=machine.id,
                correlation_id=uuid4(),
                egress_enabled=True,
            )
            is None
        )

        accepted2 = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-b1b-draft-1",
                text="client again",
            )
        )
        assert accepted2.reply_plan is not None
        draft_only = await _insert_delivered_bot_outbound(
            session,
            conversation=accepted2.conversation,
            reply_plan_id=accepted2.reply_plan.id,
            context_version=accepted2.context_version,
            manager_epoch=accepted2.conversation.manager_epoch,
            event_seq_hwm=accepted2.conversation.current_event_seq,
            payload_json={
                "schema": "synthetic.outbound.v1",
                "synthetic_token": "SYNTHETIC_OK",
                "draft_text": "INTERNAL_DRAFT only",
            },
        )
        assert (
            await enqueue_bot_outbound_projection(
                session,
                conversation_id=conversation.id,
                outbound_id=draft_only.id,
                correlation_id=uuid4(),
                egress_enabled=True,
            )
            is None
        )

    async with session_factory() as session:
        async with session.begin():
            bot_count = await session.scalar(
                select(func.count())
                .select_from(AmocrmMessageProjection)
                .where(
                    AmocrmMessageProjection.source_kind
                    == AmocrmProjectionSourceKind.BOT_OUTBOUND.value
                )
            )
            assert bot_count == 0


@pytest.mark.asyncio
async def test_b1b_stale_lease_cannot_complete(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AMOCRM_CHAT_EGRESS_ENABLED", "true")
    monkeypatch.setenv("AMOCRM_CHAT_CHANNEL_SECRET", _SECRET)
    monkeypatch.setenv("AMOCRM_CHAT_SCOPE_ID", _SCOPE)

    conversation = await _seed_bound(session_factory)

    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-b1b-stale-1",
                text="client",
            )
        )
        assert accepted.reply_plan is not None
        outbound = await _insert_delivered_bot_outbound(
            session,
            conversation=accepted.conversation,
            reply_plan_id=accepted.reply_plan.id,
            context_version=accepted.context_version,
            manager_epoch=accepted.conversation.manager_epoch,
            event_seq_hwm=accepted.conversation.current_event_seq,
            payload_json={
                "schema": "synthetic.outbound.v1",
                "synthetic_token": "SYNTHETIC_OK",
                "text": "stale lease body",
            },
        )
        await enqueue_bot_outbound_projection(
            session,
            conversation_id=conversation.id,
            outbound_id=outbound.id,
            correlation_id=uuid4(),
            egress_enabled=True,
        )

    fake = _ScriptedClient()
    worker = AmocrmChatProjectionWorker(
        session_factory,
        worker_id="proj-b1b-stale",
        config=AmoCrmChatEgressConfig(
            enabled=True,
            channel_secret=_SECRET,
            scope_id=_SCOPE,
        ),
        http_client=fake,
    )

    bot_claim = None
    while True:
        claim = await worker.claim_one()
        if claim is None:
            break
        if claim.source_kind != AmocrmProjectionSourceKind.BOT_OUTBOUND.value:
            fake.send_results.append(
                AmoCrmChatSendResult(
                    outcome=AmoCrmChatEgressOutcome.SUCCESS,
                    amocrm_message_id=f"amo-cli-{fake.send_calls + 1}",
                )
            )
            await worker.process_claimed(claim)
            continue
        bot_claim = claim
        break
    assert bot_claim is not None

    sends_before = fake.send_calls
    history_before = fake.history_calls
    bad = bot_claim.__class__(
        projection_id=bot_claim.projection_id,
        conversation_id=bot_claim.conversation_id,
        source_kind=bot_claim.source_kind,
        source_id=bot_claim.source_id,
        integration_msgid=bot_claim.integration_msgid,
        amocrm_message_id=bot_claim.amocrm_message_id,
        status=bot_claim.status,
        attempt_count=bot_claim.attempt_count,
        max_attempts=bot_claim.max_attempts,
        lease_owner=bot_claim.lease_owner,
        lease_token=bot_claim.lease_token,
        lease_version=bot_claim.lease_version + 99,
        lease_until=bot_claim.lease_until,
        correlation_id=bot_claim.correlation_id,
    )
    with pytest.raises(StaleAmocrmProjectionLeaseError):
        await worker.process_claimed(bad)
    assert fake.send_calls == sends_before
    assert fake.history_calls == history_before


@pytest.mark.asyncio
async def test_b1_delivered_survives_projection_enqueue_failure_and_repair(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1/H1: DELIVERED commits before projection; failure в†’ repair restores row."""

    from app.services.amocrm_chat_projection import repair_bot_outbound_projection
    from app.services.outbound_arbiter import OutboundArbiter
    from app.services.reply_outbound import OutboundWorker
    from app.services.synthetic_outbound import SyntheticOutboundAdapter
    from app.models.reply_plan import ReplyPlanStatus

    monkeypatch.setenv("AMOCRM_CHAT_EGRESS_ENABLED", "true")
    monkeypatch.setenv("AMOCRM_CHAT_CHANNEL_SECRET", _SECRET)
    monkeypatch.setenv("AMOCRM_CHAT_SCOPE_ID", _SCOPE)

    conversation = await _seed_bound(session_factory)
    durable = "arbiter delivered body for projection isolation"
    due = datetime.now(timezone.utc)

    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-b1-isolate-1",
                text="client",
            )
        )
        assert accepted.reply_plan is not None
        accepted.reply_plan.status = ReplyPlanStatus.DISPATCHED.value
        outbound, created = await outbound_repo.insert_synthetic_outbound_if_absent(
            session,
            conversation_id=accepted.conversation.id,
            reply_plan_id=accepted.reply_plan.id,
            context_version=accepted.context_version,
            manager_epoch=accepted.conversation.manager_epoch,
            event_seq_hwm=accepted.conversation.current_event_seq,
            payload_json={
                "schema": "synthetic.outbound.v1",
                "synthetic_token": "SYNTHETIC_OK",
                "text": durable,
            },
            correlation_id=uuid4(),
            not_before=due - timedelta(seconds=1),
        )
        assert created is True
        outbound_id = outbound.id

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("INJECTED_PROJECTION_ENQUEUE_FAIL")

    monkeypatch.setattr(
        "app.services.outbound_arbiter.enqueue_bot_outbound_projection",
        _boom,
    )

    sink = SyntheticOutboundAdapter()
    arbiter = OutboundArbiter(session_factory, sink=sink)
    worker = OutboundWorker(
        session_factory,
        worker_id="out-b1-isolate",
        arbiter=arbiter,
    )
    claim = await worker.claim_one(now=due)
    assert claim is not None
    assert claim.outbound_id == outbound_id
    result = await worker.process_claimed(claim, now=due)
    assert result.delivery_status == DeliveryStatus.DELIVERED.value
    assert len(sink.calls) == 1
    assert sink.calls[0]._text == durable

    async with session_factory() as session:
        async with session.begin():
            row = await session.get(OutboxMessage, outbound_id)
            assert row is not None
            assert row.delivery_status == DeliveryStatus.DELIVERED.value
            bot_count = await session.scalar(
                select(func.count())
                .select_from(AmocrmMessageProjection)
                .where(
                    AmocrmMessageProjection.source_kind
                    == AmocrmProjectionSourceKind.BOT_OUTBOUND.value
                )
            )
            assert bot_count == 0

    # Reclaim must not pick DELIVERED; sink must not run again.
    reclaim = await worker.claim_one(now=due + timedelta(seconds=5))
    if reclaim is not None:
        assert reclaim.outbound_id != outbound_id
    assert len(sink.calls) == 1

    repaired = await repair_bot_outbound_projection(
        session_factory,
        outbound_id=outbound_id,
        egress_enabled=True,
    )
    assert repaired.enqueued is True
    assert repaired.created is True
    assert repaired.projection_id is not None
    assert repaired.error_code is None

    again = await repair_bot_outbound_projection(
        session_factory,
        outbound_id=outbound_id,
        egress_enabled=True,
    )
    assert again.enqueued is True
    assert again.created is False
    assert again.projection_id == repaired.projection_id

    async with session_factory() as session:
        async with session.begin():
            bot_rows = (
                await session.scalars(
                    select(AmocrmMessageProjection).where(
                        AmocrmMessageProjection.source_kind
                        == AmocrmProjectionSourceKind.BOT_OUTBOUND.value
                    )
                )
            ).all()
            assert len(bot_rows) == 1
            assert bot_rows[0].source_id == outbound_id
            assert bot_rows[0].status == AmocrmProjectionStatus.PENDING.value
            outbox = await session.get(OutboxMessage, outbound_id)
            assert outbox is not None
            assert outbox.delivery_status == DeliveryStatus.DELIVERED.value
            assert outbox.payload_json.get("text") == durable

    assert len(sink.calls) == 1


@pytest.mark.asyncio
async def test_m1_non_delivered_source_with_text_zero_chat_http(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-DELIVERED outbox with valid text в†’ no enqueue / zero Chat HTTP."""

    monkeypatch.setenv("AMOCRM_CHAT_EGRESS_ENABLED", "true")
    monkeypatch.setenv("AMOCRM_CHAT_CHANNEL_SECRET", _SECRET)
    monkeypatch.setenv("AMOCRM_CHAT_SCOPE_ID", _SCOPE)

    conversation = await _seed_bound(session_factory)

    async with session_scope(session_factory) as session:
        accepted = await InboundService(session).accept(
            SyntheticInboundEvent(
                external_conversation_id=conversation.external_conversation_id,
                external_message_id="client-m1-nondel-1",
                text="client",
            )
        )
        assert accepted.reply_plan is not None
        outbound, created = await outbound_repo.insert_synthetic_outbound_if_absent(
            session,
            conversation_id=accepted.conversation.id,
            reply_plan_id=accepted.reply_plan.id,
            context_version=accepted.context_version,
            manager_epoch=accepted.conversation.manager_epoch,
            event_seq_hwm=accepted.conversation.current_event_seq,
            payload_json={
                "schema": "synthetic.outbound.v1",
                "synthetic_token": "SYNTHETIC_OK",
                "text": "valid text but still ADMITTED",
            },
            correlation_id=uuid4(),
            not_before=datetime.now(timezone.utc),
        )
        assert created is True
        outbound.delivery_status = DeliveryStatus.ADMITTED.value
        outbound.admitted_at = datetime.now(timezone.utc)
        assert (
            await enqueue_bot_outbound_projection(
                session,
                conversation_id=conversation.id,
                outbound_id=outbound.id,
                correlation_id=uuid4(),
                egress_enabled=True,
            )
            is None
        )
        # Defense: force-enqueued BOT_OUTBOUND from non-DELIVERED must skip.
        await projection_repo.enqueue_if_absent(
            session,
            conversation_id=conversation.id,
            source_kind=AmocrmProjectionSourceKind.BOT_OUTBOUND,
            source_id=outbound.id,
            correlation_id=uuid4(),
        )

    fake = _ScriptedClient()
    worker = AmocrmChatProjectionWorker(
        session_factory,
        worker_id="proj-m1-nondel",
        config=AmoCrmChatEgressConfig(
            enabled=True,
            channel_secret=_SECRET,
            scope_id=_SCOPE,
        ),
        http_client=fake,
    )
    while True:
        claim = await worker.claim_one()
        if claim is None:
            break
        if claim.source_kind == AmocrmProjectionSourceKind.CLIENT_INBOUND.value:
            fake.send_results.append(
                AmoCrmChatSendResult(
                    outcome=AmoCrmChatEgressOutcome.SUCCESS,
                    amocrm_message_id=f"amo-cli-{fake.send_calls + 1}",
                )
            )
            await worker.process_claimed(claim)
            continue
        assert claim.source_kind == AmocrmProjectionSourceKind.BOT_OUTBOUND.value
        sends_before = fake.send_calls
        skipped = await worker.process_claimed(claim)
        assert skipped.projected is False
        assert skipped.skip_reason == "SOURCE_NOT_DELIVERED"
        assert fake.send_calls == sends_before
        assert fake.history_calls == 0
        break
    else:
        raise AssertionError("BOT_OUTBOUND claim missing")
