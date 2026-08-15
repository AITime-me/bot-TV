"""AMO-01B2: CRM REST mirror adapter unit coverage."""

from __future__ import annotations

import inspect
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.amocrm_crm_deal_create_config import (
    AmoCrmDealCreateConfig,
    load_deal_create_config_fail_closed,
)
from app.core.amocrm_crm_rest_config import AmoCrmCrmRestConfig
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.models.amocrm_mirror import (
    AmoCrmMirrorJobType,
    AmoCrmMirrorStatus,
    AmoCrmMirrorSubjectKind,
    MIRROR_PAYLOAD_SCHEMA,
)
from app.repositories.amocrm_mirror import StaleAmoCrmMirrorLeaseError
from app.services.amocrm_adapter import AmoCrmMirrorOutcome, AmoCrmMirrorRequest
from app.services.amocrm_crm_mirror_adapter import CrmRestMirrorAdapter
from app.services.amocrm_mirror import AmoCrmMirrorWorker
from app.services.amocrm_technical_deal import (
    TechnicalDealEnsureResult,
    TechnicalDealOutcome,
)
from tests.docker_runtime_allowlist import (
    AMO01B2_DOCKER_RUNTIME_PATHS,
    assert_canonical_docker_runtime_allowlist,
    is_included_in_docker_build_context,
)

_REPO = Path(__file__).resolve().parents[1]
_MIRRORED_MEANING = (
    "required amoCRM entity state for this mirror job converged successfully"
)


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[S2sHttpRequest] = []

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        raise AssertionError("CRM HTTP must not run in this test")


class _FakeDeal:
    def __init__(self, result: TechnicalDealEnsureResult) -> None:
        self.result = result
        self.calls: list[uuid.UUID] = []

    async def ensure_technical_deal(
        self, conversation_id: uuid.UUID
    ) -> TechnicalDealEnsureResult:
        self.calls.append(conversation_id)
        return self.result


def _request(*, conversation_id: uuid.UUID | None = None) -> AmoCrmMirrorRequest:
    conv = conversation_id if conversation_id is not None else uuid.uuid4()
    return AmoCrmMirrorRequest(
        job_id=str(uuid.uuid4()),
        job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META.value,
        subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE.value,
        subject_id=str(uuid.uuid4()),
        conversation_id=str(conv),
        context_version=1,
        correlation_id=str(uuid.uuid4()),
        _payload_schema=MIRROR_PAYLOAD_SCHEMA,
    )


def _enabled_config() -> AmoCrmDealCreateConfig:
    return AmoCrmDealCreateConfig(
        enabled=True,
        pipeline_id=1001,
        status_id=2002,
        rest=AmoCrmCrmRestConfig(
            enabled=True,
            client_id="cid",
            client_secret="csecret12",
            api_base_url="https://example.amocrm.ru",
            redirect_uri="https://example.com/oauth",
        ),
    )


def test_mirrored_means_entity_convergence_not_message_copy() -> None:
    sources = (
        _REPO / "app" / "models" / "amocrm_mirror.py",
        _REPO / "app" / "services" / "amocrm_mirror.py",
        _REPO / "app" / "services" / "amocrm_crm_mirror_adapter.py",
        _REPO / "docs" / "adr" / "004-amocrm-mirror.md",
    )
    for path in sources:
        text = path.read_text(encoding="utf-8")
        assert _MIRRORED_MEANING in text, path
        lowered = text.lower()
        assert "copied to crm" in lowered or "текст сообщения" in lowered


@pytest.mark.asyncio
async def test_adapter_disabled_zero_http() -> None:
    transport = _FakeTransport()
    deal = _FakeDeal(TechnicalDealEnsureResult(outcome=TechnicalDealOutcome.ENSURED))
    adapter = CrmRestMirrorAdapter(
        object(),  # type: ignore[arg-type]
        config=AmoCrmDealCreateConfig(enabled=False),
        transport=transport,
        deal_service=deal,  # type: ignore[arg-type]
    )
    result = await adapter.mirror(_request())
    assert result.outcome is AmoCrmMirrorOutcome.SUCCESS
    assert deal.calls == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_invalid_config_fail_closed_zero_http() -> None:
    transport = _FakeTransport()
    config = load_deal_create_config_fail_closed(
        {"AMOCRM_CRM_DEAL_CREATE_ENABLED": "true"}
    )
    assert config.enabled is False
    adapter = CrmRestMirrorAdapter(
        object(),  # type: ignore[arg-type]
        config=config,
        transport=transport,
    )
    result = await adapter.mirror(_request())
    assert result.outcome is AmoCrmMirrorOutcome.SUCCESS
    assert adapter.last_http_calls == ()
    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deal_outcome", "mirror_outcome", "error_code"),
    [
        (TechnicalDealOutcome.ENSURED, AmoCrmMirrorOutcome.SUCCESS, None),
        (TechnicalDealOutcome.DISABLED, AmoCrmMirrorOutcome.SUCCESS, None),
        (
            TechnicalDealOutcome.RECONCILE_REQUIRED,
            AmoCrmMirrorOutcome.TRANSIENT_ERROR,
            "ENTITY_LINK_RECONCILE_REQUIRED",
        ),
        (
            TechnicalDealOutcome.TRANSIENT_ERROR,
            AmoCrmMirrorOutcome.TRANSIENT_ERROR,
            "AMOCRM_CRM_HTTP_403",
        ),
        (
            TechnicalDealOutcome.BUSY,
            AmoCrmMirrorOutcome.TRANSIENT_ERROR,
            "ENTITY_LINK_BUSY",
        ),
        (
            TechnicalDealOutcome.PERMANENT_ERROR,
            AmoCrmMirrorOutcome.PERMANENT_ERROR,
            "AMOCRM_CRM_HTTP_400",
        ),
    ],
)
async def test_adapter_maps_technical_deal_outcomes(
    deal_outcome: TechnicalDealOutcome,
    mirror_outcome: AmoCrmMirrorOutcome,
    error_code: str | None,
) -> None:
    deal = _FakeDeal(
        TechnicalDealEnsureResult(
            outcome=deal_outcome,
            error_code=error_code,
            http_calls=("GET_LEAD",),
        )
    )
    adapter = CrmRestMirrorAdapter(
        object(),  # type: ignore[arg-type]
        config=_enabled_config(),
        deal_service=deal,  # type: ignore[arg-type]
    )
    result = await adapter.mirror(_request())
    assert result.outcome is mirror_outcome
    assert result.error_code == error_code
    assert adapter.last_http_calls == ("GET_LEAD",)
    assert len(deal.calls) == 1


@pytest.mark.asyncio
async def test_pre_adapter_stale_mirror_lease_zero_crm_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale lease *before* the adapter must produce zero CRM HTTP."""

    transport = _FakeTransport()
    adapter = CrmRestMirrorAdapter(
        AsyncMock(),
        config=_enabled_config(),
        transport=transport,
    )
    worker = AmoCrmMirrorWorker(AsyncMock(), worker_id="unit", adapter=adapter)
    from app.models.amocrm_mirror import safe_mirror_payload
    from app.repositories.amocrm_mirror import AmoCrmMirrorClaim
    from datetime import datetime, timedelta, timezone

    subject = uuid.uuid4()
    conversation = uuid.uuid4()
    payload = safe_mirror_payload(
        job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META,
        subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE,
        subject_id=subject,
        conversation_id=conversation,
        context_version=1,
    )
    claim = AmoCrmMirrorClaim(
        job_id=uuid.uuid4(),
        job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META.value,
        subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE.value,
        subject_id=subject,
        conversation_id=conversation,
        context_version=1,
        mirror_key=f"unit:{subject}",
        status=AmoCrmMirrorStatus.PROCESSING.value,
        attempt_count=1,
        max_attempts=5,
        lease_owner="unit-worker",
        lease_token=uuid.uuid4(),
        lease_version=1,
        lease_until=datetime(2026, 8, 13, tzinfo=timezone.utc) + timedelta(seconds=30),
        correlation_id=uuid.uuid4(),
        payload_json=payload,
    )
    session = AsyncMock()

    @asynccontextmanager
    async def _fake_scope(_factory):  # type: ignore[no-untyped-def]
        yield session

    async def _lock_conversation(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        conv = MagicMock()
        conv.ownership = "BOT"
        conv.manager_takeover_at = None
        conv.context_version = 1
        return conv

    async def _stale_lease(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise StaleAmoCrmMirrorLeaseError("AMOCRM_MIRROR_STALE_LEASE")

    monkeypatch.setattr("app.services.amocrm_mirror.session_scope", _fake_scope)
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
    assert transport.calls == []


@pytest.mark.asyncio
async def test_mid_flight_reclaim_crm_may_run_stale_cannot_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After pre-CRM fence, mid-flight reclaim may leave CRM HTTP already started;

    stale worker still cannot complete the job. Deal fence (not mirror lease)
    prevents a second TECHNICAL_DEAL create under concurrency.
    """

    from app.models.amocrm_mirror import safe_mirror_payload
    from app.repositories.amocrm_mirror import AmoCrmMirrorClaim
    from datetime import datetime, timedelta, timezone

    crm_http_started: list[str] = []
    deal_creates: list[uuid.UUID] = []

    class _TrackingDeal:
        async def ensure_technical_deal(
            self, conversation_id: uuid.UUID
        ) -> TechnicalDealEnsureResult:
            crm_http_started.append("POST_LEAD")
            deal_creates.append(conversation_id)
            return TechnicalDealEnsureResult(
                outcome=TechnicalDealOutcome.ENSURED,
                external_deal_id="9001",
                http_calls=("POST_LEAD",),
            )

    adapter = CrmRestMirrorAdapter(
        AsyncMock(),
        config=_enabled_config(),
        deal_service=_TrackingDeal(),  # type: ignore[arg-type]
    )
    worker = AmoCrmMirrorWorker(AsyncMock(), worker_id="stale-unit", adapter=adapter)

    subject = uuid.uuid4()
    conversation = uuid.uuid4()
    payload = safe_mirror_payload(
        job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META,
        subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE,
        subject_id=subject,
        conversation_id=conversation,
        context_version=1,
    )
    claim = AmoCrmMirrorClaim(
        job_id=uuid.uuid4(),
        job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META.value,
        subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE.value,
        subject_id=subject,
        conversation_id=conversation,
        context_version=1,
        mirror_key=f"unit-mid:{subject}",
        status=AmoCrmMirrorStatus.PROCESSING.value,
        attempt_count=1,
        max_attempts=5,
        lease_owner="stale-unit",
        lease_token=uuid.uuid4(),
        lease_version=1,
        lease_until=datetime(2026, 8, 13, tzinfo=timezone.utc) + timedelta(seconds=30),
        correlation_id=uuid.uuid4(),
        payload_json=payload,
    )
    session = AsyncMock()
    scopes = {"n": 0}

    @asynccontextmanager
    async def _fake_scope(_factory):  # type: ignore[no-untyped-def]
        scopes["n"] += 1
        yield session

    async def _lock_conversation(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        conv = MagicMock()
        conv.ownership = "BOT"
        conv.manager_takeover_at = None
        conv.context_version = 1
        return conv

    async def _require_ok(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    async def _complete_stale(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise StaleAmoCrmMirrorLeaseError("AMOCRM_MIRROR_STALE_LEASE")

    monkeypatch.setattr("app.services.amocrm_mirror.session_scope", _fake_scope)
    monkeypatch.setattr(
        "app.services.amocrm_mirror.conversation_repo.get_by_id_for_update",
        _lock_conversation,
    )
    monkeypatch.setattr(
        "app.services.amocrm_mirror.mirror_repo.require_processing_lease",
        _require_ok,
    )
    monkeypatch.setattr(
        "app.services.amocrm_mirror.AmoCrmMirrorWorker._revalidate",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.amocrm_mirror.mirror_repo.complete_with_lease",
        _complete_stale,
    )

    with pytest.raises(StaleAmoCrmMirrorLeaseError):
        await worker.process_claimed(claim)

    assert crm_http_started == ["POST_LEAD"]
    assert len(deal_creates) == 1
    assert len(adapter.calls) == 1
    # Second worker / reclaim would hit deal reservation fence for the same
    # conversation — documented contract; this unit proves stale cannot complete
    # after CRM already ran.
    assert scopes["n"] >= 2


def test_mirror_lease_contract_docs_forbid_false_zero_http_claim() -> None:
    mirror_src = (_REPO / "app" / "services" / "amocrm_mirror.py").read_text(
        encoding="utf-8"
    )
    adr = (_REPO / "docs" / "adr" / "004-amocrm-mirror.md").read_text(encoding="utf-8")
    assert "reclaimed mid-flight" in mirror_src or "reclaimed mid-flight" in adr
    assert "never produces an adapter side-effect" not in mirror_src
    assert "deal reservation" in mirror_src.lower() or "TECHNICAL_DEAL" in adr


def test_chat_hmac_never_used_on_crm_mirror_path() -> None:
    for rel in (
        "app/services/amocrm_crm_mirror_adapter.py",
        "app/services/amocrm_technical_deal.py",
        "app/core/amocrm_crm_leads_http.py",
        "app/core/amocrm_crm_rest_http.py",
        "app/core/amocrm_crm_oauth_crypto.py",
    ):
        text = (_REPO / rel).read_text(encoding="utf-8")
        assert "AMOCRM_CHAT_" not in text or "separate" in text.lower()
        assert "channel_secret" not in text
        assert "X-Signature" not in text
        assert "build_amocrm_chat_signature" not in text
        assert "/api/v4/notes" not in text
        assert "/api/v4/tasks" not in text


def test_worker_runtime_wires_crm_adapter_and_swallows_reject() -> None:
    from app.services import worker_runtime as runtime

    source = inspect.getsource(runtime.build_default_loop_specs)
    assert "CrmRestMirrorAdapter" in source
    assert "AmoCrmMirrorRejected" in source
    assert "except AmoCrmMirrorRejected:" in source


def test_adapter_allowlisted() -> None:
    assert_canonical_docker_runtime_allowlist()
    rel = "app/services/amocrm_crm_mirror_adapter.py"
    assert rel in AMO01B2_DOCKER_RUNTIME_PATHS
    assert is_included_in_docker_build_context(rel, repo_root=_REPO)
    assert (_REPO / rel).is_file()
