from __future__ import annotations

import dataclasses
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import CheckConstraint

from app.config import Settings
from app.models.amocrm_mirror import (
    ALLOWED_MIRROR_PAYLOAD_KEYS,
    AMOCRM_MIRROR_TRANSITIONS,
    FORBIDDEN_MIRROR_PAYLOAD_KEYS,
    MIRROR_KEY_MAX_LENGTH,
    MIRROR_PAYLOAD_SCHEMA,
    TERMINAL_AMOCRM_MIRROR_STATUSES,
    AmoCrmMirrorJob,
    AmoCrmMirrorJobType,
    AmoCrmMirrorSkipReason,
    AmoCrmMirrorStatus,
    AmoCrmMirrorSubjectKind,
    MirrorPayloadViolation,
    amocrm_mirror_transition_allowed,
    assert_mirror_payload_is_safe,
    client_message_mirror_key,
    manager_takeover_mirror_key,
    outbound_delivered_mirror_key,
    reply_plan_state_mirror_key,
    safe_mirror_payload,
)
from app.models.conversation import ConversationOwnership
from app.models.inbox import InboxMessage
from app.models.outbox import DeliveryStatus, OutboxMessage
from app.models.reply_plan import ReplyPlan, ReplyPlanStatus
from app.repositories import amocrm_mirror as mirror_repo
from app.repositories.amocrm_mirror import AmoCrmMirrorClaim, StaleAmoCrmMirrorLeaseError
from app.services.amocrm_adapter import (
    AmoCrmMirrorOutcome,
    AmoCrmMirrorRequest,
    NoopAmoCrmMirrorAdapter,
)
from app.services.amocrm_mirror import AmoCrmMirrorWorker

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXED_NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)

_MIRROR_MODULES = (
    ("app", "models", "amocrm_mirror.py"),
    ("app", "repositories", "amocrm_mirror.py"),
    ("app", "services", "amocrm_adapter.py"),
    ("app", "services", "amocrm_mirror.py"),
)


def _mirror_sources() -> dict[Path, str]:
    return {
        _REPO_ROOT.joinpath(*parts): _REPO_ROOT.joinpath(*parts).read_text(
            encoding="utf-8"
        )
        for parts in _MIRROR_MODULES
    }


def _claim(
    *,
    job_type: AmoCrmMirrorJobType,
    subject_kind: AmoCrmMirrorSubjectKind,
    subject_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    context_version: int | None = 1,
    subject_status: str | None = None,
) -> AmoCrmMirrorClaim:
    subject = subject_id if subject_id is not None else uuid.uuid4()
    conversation = conversation_id if conversation_id is not None else uuid.uuid4()
    payload = safe_mirror_payload(
        job_type=job_type,
        subject_kind=subject_kind,
        subject_id=subject,
        conversation_id=conversation,
        context_version=context_version,
        subject_status=subject_status,
    )
    return AmoCrmMirrorClaim(
        job_id=uuid.uuid4(),
        job_type=job_type.value,
        subject_kind=subject_kind.value,
        subject_id=subject,
        conversation_id=conversation,
        context_version=context_version,
        mirror_key=f"unit:{subject}",
        status=AmoCrmMirrorStatus.PROCESSING.value,
        attempt_count=1,
        max_attempts=5,
        lease_owner="unit-worker",
        lease_token=uuid.uuid4(),
        lease_version=1,
        lease_until=_FIXED_NOW + timedelta(seconds=30),
        correlation_id=uuid.uuid4(),
        payload_json=payload,
    )


def _bot_conversation(*, context_version: int = 1) -> MagicMock:
    conversation = MagicMock()
    conversation.ownership = ConversationOwnership.BOT.value
    conversation.manager_takeover_at = None
    conversation.context_version = context_version
    return conversation


def test_transition_map_covers_every_status_and_closes_terminals() -> None:
    assert set(AMOCRM_MIRROR_TRANSITIONS) == set(AmoCrmMirrorStatus)
    for targets in AMOCRM_MIRROR_TRANSITIONS.values():
        assert targets <= set(AmoCrmMirrorStatus)
    for terminal in TERMINAL_AMOCRM_MIRROR_STATUSES:
        assert AMOCRM_MIRROR_TRANSITIONS[terminal] == frozenset()
    assert TERMINAL_AMOCRM_MIRROR_STATUSES == frozenset(
        {
            AmoCrmMirrorStatus.MIRRORED,
            AmoCrmMirrorStatus.SKIPPED,
            AmoCrmMirrorStatus.DEAD,
        }
    )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AmoCrmMirrorStatus.PENDING, AmoCrmMirrorStatus.PROCESSING),
        (AmoCrmMirrorStatus.PROCESSING, AmoCrmMirrorStatus.MIRRORED),
        (AmoCrmMirrorStatus.PROCESSING, AmoCrmMirrorStatus.SKIPPED),
        (AmoCrmMirrorStatus.PROCESSING, AmoCrmMirrorStatus.FAILED),
        (AmoCrmMirrorStatus.PROCESSING, AmoCrmMirrorStatus.DEAD),
        (AmoCrmMirrorStatus.FAILED, AmoCrmMirrorStatus.PROCESSING),
        (AmoCrmMirrorStatus.FAILED, AmoCrmMirrorStatus.DEAD),
    ],
)
def test_allowed_transitions(
    current: AmoCrmMirrorStatus,
    target: AmoCrmMirrorStatus,
) -> None:
    assert amocrm_mirror_transition_allowed(current, target) is True
    assert amocrm_mirror_transition_allowed(current.value, target.value) is True


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AmoCrmMirrorStatus.PENDING, AmoCrmMirrorStatus.MIRRORED),
        (AmoCrmMirrorStatus.PENDING, AmoCrmMirrorStatus.SKIPPED),
        (AmoCrmMirrorStatus.MIRRORED, AmoCrmMirrorStatus.PROCESSING),
        (AmoCrmMirrorStatus.SKIPPED, AmoCrmMirrorStatus.PROCESSING),
        (AmoCrmMirrorStatus.DEAD, AmoCrmMirrorStatus.PROCESSING),
        (AmoCrmMirrorStatus.DEAD, AmoCrmMirrorStatus.MIRRORED),
        (AmoCrmMirrorStatus.FAILED, AmoCrmMirrorStatus.MIRRORED),
    ],
)
def test_denied_transitions(
    current: AmoCrmMirrorStatus,
    target: AmoCrmMirrorStatus,
) -> None:
    assert amocrm_mirror_transition_allowed(current, target) is False


def test_stage_events_are_limited_to_the_agreed_minimum() -> None:
    assert {item.value for item in AmoCrmMirrorJobType} == {
        "CLIENT_MESSAGE_RECEIVED_META",
        "REPLY_PLAN_STATE_CHANGED",
        "MANAGER_TAKEOVER",
        "OUTBOUND_DELIVERED_META",
    }
    # Subjects are bot-TV rows, never amoCRM entities.
    assert {item.value for item in AmoCrmMirrorSubjectKind} == {
        "CONVERSATION",
        "INBOX_MESSAGE",
        "REPLY_PLAN",
        "OUTBOX_MESSAGE",
    }
    for banned in ("LEAD", "CONTACT", "NOTE", "TASK", "CONVERSATION_UPSERT"):
        assert banned not in {item.value for item in AmoCrmMirrorJobType}
        assert banned not in {item.value for item in AmoCrmMirrorSubjectKind}


def test_check_constraint_text_matches_enum_values() -> None:
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in AmoCrmMirrorJob.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name
    }
    pairs = (
        ("ck_amocrm_mirror_job_type", AmoCrmMirrorJobType),
        ("ck_amocrm_mirror_subject_kind", AmoCrmMirrorSubjectKind),
        ("ck_amocrm_mirror_status", AmoCrmMirrorStatus),
    )
    for name, enum_cls in pairs:
        literals = set(re.findall(r"'([A-Z_]+)'", checks[name]))
        assert literals == {item.value for item in enum_cls}, name
    assert "SENT" not in " ".join(checks.values())


def test_mirror_keys_are_deterministic_and_internal_only() -> None:
    inbox_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    outbound_id = uuid.uuid4()

    assert client_message_mirror_key(inbox_id) == client_message_mirror_key(inbox_id)
    assert client_message_mirror_key(inbox_id) == f"client-message-meta:{inbox_id}"
    assert client_message_mirror_key(uuid.uuid4()) != client_message_mirror_key(inbox_id)
    assert reply_plan_state_mirror_key(plan_id, "DISPATCHED") != (
        reply_plan_state_mirror_key(plan_id, "DEAD")
    )
    assert manager_takeover_mirror_key(conversation_id) == (
        f"manager-takeover:{conversation_id}"
    )
    assert outbound_delivered_mirror_key(outbound_id) == (
        f"outbound-delivered:{outbound_id}"
    )

    keys = (
        client_message_mirror_key(inbox_id),
        reply_plan_state_mirror_key(plan_id, ReplyPlanStatus.DISPATCHED.value),
        manager_takeover_mirror_key(conversation_id),
        outbound_delivered_mirror_key(outbound_id),
    )
    for key in keys:
        assert len(key) <= MIRROR_KEY_MAX_LENGTH
        # Provider-side identifiers must never become part of the identity.
        assert "synth-" not in key
        assert "@" not in key


def test_safe_payload_contains_only_whitelisted_technical_keys() -> None:
    inbox_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    payload = safe_mirror_payload(
        job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META,
        subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE,
        subject_id=inbox_id,
        conversation_id=conversation_id,
        context_version=3,
    )
    assert set(payload) <= ALLOWED_MIRROR_PAYLOAD_KEYS
    assert payload["schema"] == MIRROR_PAYLOAD_SCHEMA
    assert payload["subject_id"] == str(inbox_id)
    assert payload["conversation_id"] == str(conversation_id)
    assert payload["context_version"] == 3
    assert not set(payload) & FORBIDDEN_MIRROR_PAYLOAD_KEYS
    assert "subject_status" not in payload


def test_takeover_payload_carries_no_context_version() -> None:
    conversation_id = uuid.uuid4()
    payload = safe_mirror_payload(
        job_type=AmoCrmMirrorJobType.MANAGER_TAKEOVER,
        subject_kind=AmoCrmMirrorSubjectKind.CONVERSATION,
        subject_id=conversation_id,
        conversation_id=conversation_id,
    )
    assert "context_version" not in payload


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": MIRROR_PAYLOAD_SCHEMA, "text": "клиентский текст"},
        {"schema": MIRROR_PAYLOAD_SCHEMA, "draft_text": "черновик"},
        {"schema": MIRROR_PAYLOAD_SCHEMA, "phone": "+70000000000"},
        {"schema": MIRROR_PAYLOAD_SCHEMA, "email": "a@b.c"},
        {"schema": MIRROR_PAYLOAD_SCHEMA, "client_name": "Иван"},
        {"schema": MIRROR_PAYLOAD_SCHEMA, "external_conversation_id": "synth-conv"},
        {"schema": MIRROR_PAYLOAD_SCHEMA, "external_message_id": "synth-msg"},
        {"schema": MIRROR_PAYLOAD_SCHEMA, "unexpected_key": "x"},
        {"schema": "other.schema.v1"},
        {"schema": MIRROR_PAYLOAD_SCHEMA, "context_version": {"nested": "text"}},
        {"schema": MIRROR_PAYLOAD_SCHEMA, "context_version": ["text"]},
        {"schema": MIRROR_PAYLOAD_SCHEMA, "subject_status": None},
    ],
)
def test_unsafe_payloads_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(MirrorPayloadViolation):
        assert_mirror_payload_is_safe(payload)


@pytest.mark.asyncio
async def test_enqueue_refuses_unsafe_payload_without_touching_the_database() -> None:
    session = AsyncMock()
    with pytest.raises(MirrorPayloadViolation):
        await mirror_repo.enqueue_if_absent(
            session,
            job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META,
            subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE,
            subject_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            mirror_key="client-message-meta:unsafe",
            payload_json={"schema": MIRROR_PAYLOAD_SCHEMA, "text": "секрет"},
            correlation_id=uuid.uuid4(),
        )
    session.scalar.assert_not_awaited()
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mirror_key", ["", "x" * (MIRROR_KEY_MAX_LENGTH + 1)])
async def test_enqueue_refuses_invalid_mirror_key(mirror_key: str) -> None:
    session = AsyncMock()
    with pytest.raises(ValueError):
        await mirror_repo.enqueue_if_absent(
            session,
            job_type=AmoCrmMirrorJobType.MANAGER_TAKEOVER,
            subject_kind=AmoCrmMirrorSubjectKind.CONVERSATION,
            subject_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            mirror_key=mirror_key,
            payload_json=safe_mirror_payload(
                job_type=AmoCrmMirrorJobType.MANAGER_TAKEOVER,
                subject_kind=AmoCrmMirrorSubjectKind.CONVERSATION,
                subject_id=uuid.uuid4(),
                conversation_id=uuid.uuid4(),
            ),
            correlation_id=uuid.uuid4(),
        )
    session.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_noop_adapter_records_calls_and_has_no_external_effect() -> None:
    adapter = NoopAmoCrmMirrorAdapter()
    request = AmoCrmMirrorRequest(
        job_id=str(uuid.uuid4()),
        job_type=AmoCrmMirrorJobType.MANAGER_TAKEOVER.value,
        subject_kind=AmoCrmMirrorSubjectKind.CONVERSATION.value,
        subject_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
        context_version=None,
        correlation_id=str(uuid.uuid4()),
        _payload_schema=MIRROR_PAYLOAD_SCHEMA,
    )
    result = await adapter.mirror(request)
    assert result.outcome is AmoCrmMirrorOutcome.SUCCESS
    assert result.error_code is None
    assert adapter.calls == [request]
    assert "payload=<redacted>" in repr(request)
    assert MIRROR_PAYLOAD_SCHEMA not in repr(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("forced", "error_code"),
    [
        (AmoCrmMirrorOutcome.TRANSIENT_ERROR, "AMOCRM_MIRROR_TRANSIENT"),
        (AmoCrmMirrorOutcome.PERMANENT_ERROR, "AMOCRM_MIRROR_PERMANENT"),
    ],
)
async def test_noop_adapter_failure_outcomes(
    forced: AmoCrmMirrorOutcome,
    error_code: str,
) -> None:
    adapter = NoopAmoCrmMirrorAdapter(forced_outcome=forced)
    result = await adapter.mirror(
        AmoCrmMirrorRequest(
            job_id=str(uuid.uuid4()),
            job_type=AmoCrmMirrorJobType.OUTBOUND_DELIVERED_META.value,
            subject_kind=AmoCrmMirrorSubjectKind.OUTBOX_MESSAGE.value,
            subject_id=str(uuid.uuid4()),
            conversation_id=str(uuid.uuid4()),
            context_version=1,
            correlation_id=str(uuid.uuid4()),
            _payload_schema=MIRROR_PAYLOAD_SCHEMA,
        )
    )
    assert result.outcome is forced
    assert result.error_code == error_code


def test_worker_defaults_to_the_noop_adapter() -> None:
    worker = AmoCrmMirrorWorker(AsyncMock(), worker_id="unit")
    assert isinstance(worker.adapter, NoopAmoCrmMirrorAdapter)
    assert worker.adapter.calls == []


@pytest.mark.asyncio
async def test_revalidation_skips_bot_action_after_takeover() -> None:
    worker = AmoCrmMirrorWorker(AsyncMock(), worker_id="unit")
    conversation = _bot_conversation()
    conversation.ownership = ConversationOwnership.MANAGER.value
    conversation.manager_takeover_at = _FIXED_NOW
    claim = _claim(
        job_type=AmoCrmMirrorJobType.REPLY_PLAN_STATE_CHANGED,
        subject_kind=AmoCrmMirrorSubjectKind.REPLY_PLAN,
        subject_status=ReplyPlanStatus.DISPATCHED.value,
    )
    session = AsyncMock()
    reason = await worker._revalidate(session, claim, conversation)
    assert reason is AmoCrmMirrorSkipReason.MANAGER_TAKEOVER
    session.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_revalidation_skips_bot_action_on_stale_context() -> None:
    worker = AmoCrmMirrorWorker(AsyncMock(), worker_id="unit")
    claim = _claim(
        job_type=AmoCrmMirrorJobType.OUTBOUND_DELIVERED_META,
        subject_kind=AmoCrmMirrorSubjectKind.OUTBOX_MESSAGE,
        context_version=1,
        subject_status=DeliveryStatus.DELIVERED.value,
    )
    reason = await worker._revalidate(
        AsyncMock(),
        claim,
        _bot_conversation(context_version=2),
    )
    assert reason is AmoCrmMirrorSkipReason.STALE_CONTEXT


@pytest.mark.asyncio
async def test_revalidation_keeps_domain_facts_after_takeover() -> None:
    """A client message and the takeover itself stay true for the CRM."""
    worker = AmoCrmMirrorWorker(AsyncMock(), worker_id="unit")
    conversation = _bot_conversation(context_version=7)
    conversation.ownership = ConversationOwnership.MANAGER.value
    conversation.manager_takeover_at = _FIXED_NOW

    message_claim = _claim(
        job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META,
        subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE,
        context_version=1,
    )
    session = AsyncMock()
    session.get = AsyncMock(return_value=MagicMock(spec=InboxMessage))
    assert await worker._revalidate(session, message_claim, conversation) is None

    takeover_claim = _claim(
        job_type=AmoCrmMirrorJobType.MANAGER_TAKEOVER,
        subject_kind=AmoCrmMirrorSubjectKind.CONVERSATION,
        context_version=None,
    )
    assert await worker._revalidate(AsyncMock(), takeover_claim, conversation) is None


@pytest.mark.asyncio
async def test_revalidation_skips_missing_conversation() -> None:
    worker = AmoCrmMirrorWorker(AsyncMock(), worker_id="unit")
    claim = _claim(
        job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META,
        subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE,
    )
    reason = await worker._revalidate(AsyncMock(), claim, None)
    assert reason is AmoCrmMirrorSkipReason.SUBJECT_STATE_CHANGED


@pytest.mark.asyncio
async def test_revalidation_skips_when_subject_state_diverged() -> None:
    worker = AmoCrmMirrorWorker(AsyncMock(), worker_id="unit")
    conversation = _bot_conversation()

    missing_inbox = AsyncMock()
    missing_inbox.get = AsyncMock(return_value=None)
    message_claim = _claim(
        job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META,
        subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE,
    )
    assert await worker._revalidate(missing_inbox, message_claim, conversation) is (
        AmoCrmMirrorSkipReason.SUBJECT_STATE_CHANGED
    )

    plan = MagicMock(spec=ReplyPlan)
    plan.status = ReplyPlanStatus.SUPERSEDED.value
    plan_session = AsyncMock()
    plan_session.get = AsyncMock(return_value=plan)
    plan_claim = _claim(
        job_type=AmoCrmMirrorJobType.REPLY_PLAN_STATE_CHANGED,
        subject_kind=AmoCrmMirrorSubjectKind.REPLY_PLAN,
        subject_status=ReplyPlanStatus.DISPATCHED.value,
    )
    assert await worker._revalidate(plan_session, plan_claim, conversation) is (
        AmoCrmMirrorSkipReason.SUBJECT_STATE_CHANGED
    )

    outbound = MagicMock(spec=OutboxMessage)
    outbound.delivery_status = DeliveryStatus.FAILED.value
    outbound_session = AsyncMock()
    outbound_session.get = AsyncMock(return_value=outbound)
    outbound_claim = _claim(
        job_type=AmoCrmMirrorJobType.OUTBOUND_DELIVERED_META,
        subject_kind=AmoCrmMirrorSubjectKind.OUTBOX_MESSAGE,
        subject_status=DeliveryStatus.DELIVERED.value,
    )
    assert await worker._revalidate(
        outbound_session,
        outbound_claim,
        conversation,
    ) is AmoCrmMirrorSkipReason.SUBJECT_STATE_CHANGED


@pytest.mark.asyncio
async def test_revalidation_admits_matching_bot_action() -> None:
    worker = AmoCrmMirrorWorker(AsyncMock(), worker_id="unit")
    plan = MagicMock(spec=ReplyPlan)
    plan.status = ReplyPlanStatus.DISPATCHED.value
    session = AsyncMock()
    session.get = AsyncMock(return_value=plan)
    claim = _claim(
        job_type=AmoCrmMirrorJobType.REPLY_PLAN_STATE_CHANGED,
        subject_kind=AmoCrmMirrorSubjectKind.REPLY_PLAN,
        subject_status=ReplyPlanStatus.DISPATCHED.value,
    )
    assert await worker._revalidate(session, claim, _bot_conversation()) is None


@pytest.mark.asyncio
async def test_require_processing_lease_rejects_mismatched_fencing() -> None:
    claim = _claim(
        job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META,
        subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE,
    )
    session = AsyncMock()

    cases = (
        None,
        MagicMock(
            status=AmoCrmMirrorStatus.FAILED.value,
            lease_token=claim.lease_token,
            lease_version=claim.lease_version,
            lease_owner=claim.lease_owner,
        ),
        MagicMock(
            status=AmoCrmMirrorStatus.PROCESSING.value,
            lease_token=uuid.uuid4(),
            lease_version=claim.lease_version,
            lease_owner=claim.lease_owner,
        ),
        MagicMock(
            status=AmoCrmMirrorStatus.PROCESSING.value,
            lease_token=claim.lease_token,
            lease_version=claim.lease_version + 1,
            lease_owner=claim.lease_owner,
        ),
        MagicMock(
            status=AmoCrmMirrorStatus.PROCESSING.value,
            lease_token=claim.lease_token,
            lease_version=claim.lease_version,
            lease_owner="other-worker",
        ),
    )
    for job in cases:
        session.scalar = AsyncMock(return_value=job)
        with pytest.raises(StaleAmoCrmMirrorLeaseError):
            await mirror_repo.require_processing_lease(
                session,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                lease_version=claim.lease_version,
                lease_owner=claim.lease_owner,
            )


@pytest.mark.asyncio
async def test_process_claimed_rejects_stale_lease_before_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Superseded fencing must fail closed before any sink side-effect."""
    from contextlib import asynccontextmanager

    adapter = NoopAmoCrmMirrorAdapter()
    worker = AmoCrmMirrorWorker(AsyncMock(), worker_id="unit", adapter=adapter)
    claim = _claim(
        job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META,
        subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE,
    )
    session = AsyncMock()

    @asynccontextmanager
    async def _fake_scope(_factory):  # type: ignore[no-untyped-def]
        yield session

    async def _lock_conversation(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return _bot_conversation()

    async def _stale_lease(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise StaleAmoCrmMirrorLeaseError("AMOCRM_MIRROR_STALE_LEASE")

    monkeypatch.setattr(
        "app.services.amocrm_mirror.session_scope",
        _fake_scope,
    )
    monkeypatch.setattr(
        "app.services.amocrm_mirror.conversation_repo.get_by_id_for_update",
        _lock_conversation,
    )
    monkeypatch.setattr(
        "app.services.amocrm_mirror.mirror_repo.require_processing_lease",
        _stale_lease,
    )

    with pytest.raises(StaleAmoCrmMirrorLeaseError):
        await worker.process_claimed(claim)

    assert adapter.calls == []


def test_settings_still_ignore_amocrm_credentials() -> None:
    settings = Settings.from_env(
        {
            "AMOCRM_CLIENT_ID": "unit-client-id",
            "AMOCRM_CLIENT_SECRET": "unit-client-secret",
            "AMOCRM_BASE_URL": "https://example.invalid",
        }
    )
    assert settings == Settings()
    field_names = {field.name for field in dataclasses.fields(Settings)}
    assert not any("amocrm" in name for name in field_names)
    config_source = (_REPO_ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert "AMOCRM" not in config_source


def test_mirror_modules_never_read_configuration_directly() -> None:
    for path, source in _mirror_sources().items():
        assert "os.environ" not in source, path
        assert "AMOCRM_CLIENT" not in source, path
        assert "getenv" not in source, path


def test_mirror_modules_have_no_http_or_amocrm_transport() -> None:
    banned = (
        "httpx",
        "aiohttp",
        "requests.",
        "urllib",
        "amocrm.ru",
        "oauth",
        "OAuth",
        "client_secret",
        "access_token",
        "refresh_token",
        "def send_to_client",
        "DeliveryStatus.SENT",
    )
    for path, source in _mirror_sources().items():
        for token in banned:
            assert token not in source, f"{path}: {token}"


def test_mirror_modules_cannot_mark_outbound_delivered() -> None:
    """Only OutboundArbiter may admit a synthetic outbound message."""
    forbidden = "mark_delivered" + "_with_lease"
    for path, source in _mirror_sources().items():
        assert forbidden not in source, path
        assert "OutboxMessage(" not in source, path


def test_requirements_have_no_http_client() -> None:
    requirements = (_REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    for banned in ("httpx", "aiohttp", "requests"):
        assert banned not in requirements


def test_mirror_pg_suite_does_not_mix_clocks() -> None:
    source = (_REPO_ROOT / "tests" / "test_amocrm_mirror_pg.py").read_text(
        encoding="utf-8"
    )
    assert "datetime.now(" not in source
    assert "utcnow(" not in source


def test_mirror_job_repr_redacts_payload() -> None:
    job = AmoCrmMirrorJob(
        id=uuid.uuid4(),
        job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META.value,
        subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE.value,
        subject_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        context_version=1,
        mirror_key="client-message-meta:unit",
        status=AmoCrmMirrorStatus.PENDING.value,
        payload_json={"schema": MIRROR_PAYLOAD_SCHEMA, "context_version": 1},
        correlation_id=uuid.uuid4(),
    )
    rendered = repr(job)
    assert "payload=<redacted>" in rendered
    assert MIRROR_PAYLOAD_SCHEMA not in rendered
    assert "client-message-meta" not in rendered


def test_mirror_claim_repr_redacts_payload() -> None:
    claim = _claim(
        job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META,
        subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE,
    )
    rendered = repr(claim)
    assert "payload=<redacted>" in rendered
    assert MIRROR_PAYLOAD_SCHEMA not in rendered
