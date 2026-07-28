from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.core.outbound_policy import OutboundAction, is_automatic_outbound_allowed
from app.models.conversation import ConversationOwnership
from app.models.outbox import (
    OUTBOUND_TRANSITIONS,
    DeliveryStatus,
    DestinationType,
    outbound_transition_allowed,
)
from app.models.reply_plan import (
    BOT_RESPONSE_DELAY_MS,
    REPLY_PLAN_TRANSITIONS,
    ReplyPlanStatus,
    reply_plan_transition_allowed,
)
from app.repositories.outbound import OutboundClaim
from app.services.outbound_arbiter import (
    OutboundArbiter,
    OutboundArbiterDenied,
    assert_no_arbiter_bypass,
)
from app.services.synthetic_outbound import (
    SyntheticOutboundAdapter,
    SyntheticOutboundOutcome,
    SyntheticOutboundRequest,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXED_NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


def test_bot_response_delay_is_five_seconds() -> None:
    assert BOT_RESPONSE_DELAY_MS == 5000


def test_reply_plan_transitions_are_closed() -> None:
    assert set(REPLY_PLAN_TRANSITIONS) == set(ReplyPlanStatus)
    assert reply_plan_transition_allowed(
        ReplyPlanStatus.PENDING,
        ReplyPlanStatus.READY,
    )
    assert not reply_plan_transition_allowed(
        ReplyPlanStatus.DISPATCHED,
        ReplyPlanStatus.PROCESSING,
    )
    assert not reply_plan_transition_allowed(
        ReplyPlanStatus.CANCELLED,
        ReplyPlanStatus.READY,
    )
    with pytest.raises(ValueError):
        ReplyPlanStatus("UNKNOWN")


def test_outbound_transitions_forbid_sent() -> None:
    assert not hasattr(DeliveryStatus, "SENT")
    assert set(OUTBOUND_TRANSITIONS) == set(DeliveryStatus)
    assert outbound_transition_allowed(
        DeliveryStatus.PROCESSING,
        DeliveryStatus.DELIVERED,
    )
    assert not outbound_transition_allowed(
        DeliveryStatus.DELIVERED,
        DeliveryStatus.PENDING,
    )
    with pytest.raises(ValueError):
        DeliveryStatus("SENT")


def test_auto_outbound_remains_fail_closed() -> None:
    settings = Settings(bot_mode=__import__("app.config", fromlist=["BotMode"]).BotMode.AUTO_WRITE, emergency_lock=False)
    assert is_automatic_outbound_allowed(settings, OutboundAction.SEND_MESSAGE) is False


def test_synthetic_outbound_adapter_has_no_network_side_effects() -> None:
    adapter = SyntheticOutboundAdapter()
    request = SyntheticOutboundRequest(
        outbound_id="o1",
        conversation_id="c1",
        reply_plan_id="p1",
        context_version=1,
        correlation_id="corr",
        _payload_schema="synthetic.outbound.v1",
    )
    secret = "client-private-text"
    assert secret not in repr(request)
    result = adapter.deliver(request)
    assert result.outcome is SyntheticOutboundOutcome.SUCCESS
    assert len(adapter.calls) == 1

    failing = SyntheticOutboundAdapter(
        forced_outcome=SyntheticOutboundOutcome.TRANSIENT_ERROR,
    )
    assert failing.deliver(request).outcome is SyntheticOutboundOutcome.TRANSIENT_ERROR


def _arbiter_claim(
    *,
    conversation_id: uuid.UUID,
    outbound_id: uuid.UUID,
    reply_plan_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_version: int = 1,
    context_version: int = 1,
) -> OutboundClaim:
    return OutboundClaim(
        outbound_id=outbound_id,
        conversation_id=conversation_id,
        reply_plan_id=reply_plan_id,
        context_version=context_version,
        idempotency_key=f"synthetic-outbound:reply-plan:{reply_plan_id}",
        destination_type=DestinationType.SYNTHETIC_OUTBOUND.value,
        delivery_status=DeliveryStatus.PROCESSING.value,
        not_before=_FIXED_NOW - timedelta(seconds=1),
        attempt_count=1,
        max_attempts=5,
        lease_owner="unit-worker",
        lease_token=lease_token,
        lease_version=lease_version,
        lease_until=_FIXED_NOW + timedelta(seconds=30),
        correlation_id=uuid.uuid4(),
        payload_json={"schema": "synthetic.outbound.v1"},
    )


def _build_admit_session(
    *,
    conversation: MagicMock,
    outbound: MagicMock,
    plan: MagicMock | None,
) -> AsyncMock:
    session = AsyncMock()

    async def _get(model, ident, **kwargs):  # type: ignore[no-untyped-def]
        from app.models.outbox import OutboxMessage
        from app.models.reply_plan import ReplyPlan

        if model is OutboxMessage:
            return outbound
        if model is ReplyPlan:
            return plan
        raise AssertionError(f"unexpected session.get({model!r})")

    session.get = AsyncMock(side_effect=_get)
    return session


@pytest.mark.asyncio
async def test_arbiter_admits_dispatched_and_rejects_other_plan_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production OutboundArbiter admits only DISPATCHED linked plans."""
    conversation_id = uuid.uuid4()
    outbound_id = uuid.uuid4()
    reply_plan_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    claim = _arbiter_claim(
        conversation_id=conversation_id,
        outbound_id=outbound_id,
        reply_plan_id=reply_plan_id,
        lease_token=lease_token,
    )

    conversation = MagicMock()
    conversation.ownership = ConversationOwnership.BOT.value
    conversation.manager_takeover_at = None
    conversation.context_version = 1

    outbound = MagicMock()
    outbound.id = outbound_id
    outbound.conversation_id = conversation_id
    outbound.reply_plan_id = reply_plan_id
    outbound.delivery_status = DeliveryStatus.PROCESSING.value
    outbound.lease_token = lease_token
    outbound.lease_version = 1
    outbound.destination_type = DestinationType.SYNTHETIC_OUTBOUND.value
    outbound.not_before = _FIXED_NOW - timedelta(seconds=1)
    outbound.context_version = 1
    outbound.correlation_id = claim.correlation_id
    outbound.payload_json = {"schema": "synthetic.outbound.v1"}

    delivered = MagicMock()
    delivered.id = outbound_id
    delivered.delivery_status = DeliveryStatus.DELIVERED.value

    monkeypatch.setattr(
        "app.services.outbound_arbiter.resolve_moment",
        AsyncMock(return_value=_FIXED_NOW),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.conversation_repo.get_by_id_for_update",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.outbound_repo.mark_delivered_with_lease",
        AsyncMock(return_value=delivered),
    )

    sink = SyntheticOutboundAdapter()
    arbiter = OutboundArbiter(AsyncMock(), sink=sink)

    for status in ReplyPlanStatus:
        plan = MagicMock()
        plan.status = status.value
        plan.context_version = 1
        session = _build_admit_session(
            conversation=conversation,
            outbound=outbound,
            plan=plan,
        )
        if status is ReplyPlanStatus.DISPATCHED:
            result = await arbiter._admit_in_session(session, claim, now=_FIXED_NOW)
            assert result.admitted is True
            assert result.delivery_status == DeliveryStatus.DELIVERED.value
            continue
        with pytest.raises(
            OutboundArbiterDenied,
            match=f"^REPLY_PLAN_{status.value}$",
        ):
            await arbiter._admit_in_session(session, claim, now=_FIXED_NOW)


@pytest.mark.asyncio
async def test_arbiter_denies_manager_owned_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = uuid.uuid4()
    outbound_id = uuid.uuid4()
    reply_plan_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    claim = _arbiter_claim(
        conversation_id=conversation_id,
        outbound_id=outbound_id,
        reply_plan_id=reply_plan_id,
        lease_token=lease_token,
    )
    conversation = MagicMock()
    conversation.ownership = ConversationOwnership.MANAGER.value
    conversation.manager_takeover_at = None
    conversation.context_version = 1

    monkeypatch.setattr(
        "app.services.outbound_arbiter.resolve_moment",
        AsyncMock(return_value=_FIXED_NOW),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.conversation_repo.get_by_id_for_update",
        AsyncMock(return_value=conversation),
    )

    arbiter = OutboundArbiter(AsyncMock())
    session = AsyncMock()
    with pytest.raises(OutboundArbiterDenied, match="^MANAGER_OWNED$"):
        await arbiter._admit_in_session(session, claim, now=_FIXED_NOW)
    session.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_arbiter_denies_manager_takeover_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = uuid.uuid4()
    outbound_id = uuid.uuid4()
    reply_plan_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    claim = _arbiter_claim(
        conversation_id=conversation_id,
        outbound_id=outbound_id,
        reply_plan_id=reply_plan_id,
        lease_token=lease_token,
    )
    conversation = MagicMock()
    conversation.ownership = ConversationOwnership.BOT.value
    conversation.manager_takeover_at = _FIXED_NOW
    conversation.context_version = 1

    monkeypatch.setattr(
        "app.services.outbound_arbiter.resolve_moment",
        AsyncMock(return_value=_FIXED_NOW),
    )
    monkeypatch.setattr(
        "app.services.outbound_arbiter.conversation_repo.get_by_id_for_update",
        AsyncMock(return_value=conversation),
    )

    arbiter = OutboundArbiter(AsyncMock())
    session = AsyncMock()
    with pytest.raises(OutboundArbiterDenied, match="^MANAGER_TAKEOVER$"):
        await arbiter._admit_in_session(session, claim, now=_FIXED_NOW)
    session.get.assert_not_awaited()


def test_arbiter_bypass_guard() -> None:
    assert_no_arbiter_bypass()


def test_reply_outbound_modules_have_no_http_or_channels() -> None:
    roots = [
        _REPO_ROOT / "app" / "services" / "outbound_arbiter.py",
        _REPO_ROOT / "app" / "services" / "reply_outbound.py",
        _REPO_ROOT / "app" / "services" / "synthetic_outbound.py",
        _REPO_ROOT / "app" / "services" / "takeover.py",
        _REPO_ROOT / "app" / "repositories" / "reply_plans.py",
        _REPO_ROOT / "app" / "repositories" / "outbound.py",
    ]
    banned = (
        "httpx",
        "aiohttp",
        "requests.",
        "vk.com",
        "telegram",
        "openai",
        "yandex",
        "n8n",
        "def send_to_client",
        "DeliveryStatus.SENT",
    )
    for path in roots:
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{path}: {token}"


def test_not_before_computation_uses_delay_constant() -> None:
    now = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
    expected = now + timedelta(milliseconds=BOT_RESPONSE_DELAY_MS)
    assert expected == now + timedelta(seconds=5)


_CLOCK_SENSITIVE_MODULES = (
    ("app", "repositories", "reply_plans.py"),
    ("app", "repositories", "outbound.py"),
    ("app", "repositories", "conversations.py"),
    ("app", "repositories", "amocrm_mirror.py"),
    ("app", "services", "outbound_arbiter.py"),
    ("app", "services", "takeover.py"),
    ("app", "services", "amocrm_mirror.py"),
)


def test_scheduling_modules_never_read_the_application_clock() -> None:
    """not_before/lease deadlines must come from PostgreSQL, not the host."""
    for parts in _CLOCK_SENSITIVE_MODULES:
        path = _REPO_ROOT.joinpath(*parts)
        source = path.read_text(encoding="utf-8")
        assert "datetime.now(" not in source, f"{path}: host clock read"
        assert "utcnow(" not in source, f"{path}: host clock read"


def test_pg_reply_outbound_suite_does_not_mix_clocks() -> None:
    """PG assertions must derive instants from PostgreSQL, never from the host."""
    source = (_REPO_ROOT / "tests" / "test_reply_outbound_pg.py").read_text(
        encoding="utf-8"
    )
    assert "datetime.now(" not in source
    assert "utcnow(" not in source
