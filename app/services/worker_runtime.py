from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.booking_eligibility_factory import (
    build_booking_flow_from_settings,
    build_booking_s2s_config,
    rebind_booking_flow_to_runtime_settings,
)
from app.core.booking_method_http import BookingMethodHttpClient
from app.core.acquisition_source_http import AcquisitionSourceHttpClient
from app.core.booking_request_http import BookingRequestHttpClient
from app.core.ephemeral_pii_types import EphemeralPiiError
from app.core.live_facts_http import LiveFactsHttpClient
from app.core.s2s_http_stdlib import S2sHttpStdlibTransport
from app.core.s2s_rate_limit import (
    RATE_LIMITED_CODE,
    is_expected_s2s_rate_limited,
)
from app.core.yandex_llm_factory import build_text_generation_port
from app.db.session import session_scope
from app.models.worker_heartbeat import (
    ACQUISITION_SOURCE_ANALYTICS_LOOP,
    AMOCRM_CRM_OAUTH_LIFECYCLE_LOOP,
    AMOCRM_MIRROR_LOOP,
    BOOKING_METHOD_ANALYTICS_LOOP,
    CONTROL_PLANE_SNAPSHOT_LOOP,
    HANDOFF_EXPIRY_LOOP,
    INGRESS_LOOP,
    OUTBOUND_LOOP,
    REPLY_PLAN_LOOP,
    REQUIRED_WORKER_LOOPS,
    SELF_BOOKING_CREATE_LOOP,
    TEYA_REQUEST_ORCHESTRATOR_LOOP,
    TEYA_REQUEST_RECONCILIATION_LOOP,
)
from app.repositories import worker_heartbeats as heartbeat_repo
from app.repositories.amocrm_mirror import StaleAmoCrmMirrorLeaseError
from app.repositories.amocrm_message_projections import StaleAmocrmProjectionLeaseError
from app.repositories.ingress import StaleIngressLeaseError
from app.repositories.outbound import StaleOutboundLeaseError
from app.repositories.reply_plans import StaleReplyPlanLeaseError
from app.services.amocrm_crm_mirror_adapter import CrmRestMirrorAdapter
from app.services.amocrm_crm_oauth_lifecycle_worker import (
    AmoCrmCrmOauthLifecycleError,
    AmoCrmCrmOauthLifecycleWorker,
)
from app.services.amocrm_mirror import AmoCrmMirrorRejected, AmoCrmMirrorWorker
from app.services.amocrm_chat_projection import AmocrmChatProjectionWorker
from app.services.booking_flow import BookingFlowService
from app.services.ephemeral_pii_store import build_ephemeral_pii_store_from_env
from app.services.handoff_expiry import HandoffExpiryWorker
from app.services.ingress import IngressWorker
from app.services.outbound_arbiter import OutboundArbiter
from app.services.outbound_arbiter import OutboundArbiterDenied
from app.services.reply_outbound import OutboundWorker, ReplyPlanWorker
from app.services.self_booking_create_execution_worker import (
    SelfBookingCreateExecutionWorker,
)
from app.services.booking_method_analytics_worker import (
    BookingMethodAnalyticsWorker,
)
from app.services.acquisition_source_analytics_worker import (
    AcquisitionSourceAnalyticsWorker,
)
from app.core.control_plane_http import ControlPlaneHttpClient
from app.services.control_plane_snapshot_service import ControlPlaneSnapshotService
from app.services.control_plane_snapshot_worker import ControlPlaneSnapshotWorker
from app.services.runtime_context_builder import RuntimeContextBuilder
from app.services.shadow_draft_generation import build_shadow_draft_generation_service
from app.services.shadow_draft_ingress_hook import run_shadow_draft_after_client_inbound
from app.services.teya_request_crm_wiring import build_teya_request_crm_service
from app.services.teya_request_orchestrator_worker import (
    TeyaRequestOrchestratorWorker,
)
from app.services.teya_request_reconciliation_worker import (
    TeyaRequestReconciliationWorker,
)

logger = logging.getLogger(__name__)
_LEASE_OWNER_MAX_LENGTH = 128


class WorkerRuntimeFatal(RuntimeError):
    """One required loop can no longer make progress; supervisor must restart."""


@dataclass(frozen=True)
class WorkerLoopSpec:
    name: str
    poll_seconds: int
    tick: Callable[[], Awaitable[None]]


class WorkerHeartbeatStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def register(
        self,
        *,
        generation_id: uuid.UUID,
        worker_id: str,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            await heartbeat_repo.register_generation(
                session,
                generation_id=generation_id,
                worker_id=worker_id,
            )

    async def tick_started(
        self,
        *,
        loop_name: str,
        generation_id: uuid.UUID,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            await heartbeat_repo.record_tick_started(
                session,
                loop_name=loop_name,
                generation_id=generation_id,
            )

    async def tick_succeeded(
        self,
        *,
        loop_name: str,
        generation_id: uuid.UUID,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            await heartbeat_repo.record_tick_succeeded(
                session,
                loop_name=loop_name,
                generation_id=generation_id,
            )

    async def tick_failed(
        self,
        *,
        loop_name: str,
        generation_id: uuid.UUID,
        error_code: str,
    ) -> int:
        async with session_scope(self._session_factory) as session:
            return await heartbeat_repo.record_tick_failed(
                session,
                loop_name=loop_name,
                generation_id=generation_id,
                error_code=error_code,
            )


class WorkerRuntime:
    """Run every durable queue loop and persist generation-fenced health."""

    def __init__(
        self,
        *,
        settings: Settings,
        worker_id: str,
        heartbeat_store: WorkerHeartbeatStore,
        loops: tuple[WorkerLoopSpec, ...],
        generation_id: uuid.UUID | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        settings.validate_worker_runtime()
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must contain 1..128 characters")
        names = tuple(spec.name for spec in loops)
        if len(names) != len(set(names)):
            raise ValueError("worker loop names must be unique")
        if set(names) != set(REQUIRED_WORKER_LOOPS):
            raise ValueError("worker runtime must register every required loop")
        if any(spec.poll_seconds <= 0 for spec in loops):
            raise ValueError("worker poll seconds must be positive")
        self._settings = settings
        self._worker_id = worker_id
        self._heartbeat_store = heartbeat_store
        self._loops = loops
        self._generation_id = generation_id or uuid.uuid4()
        self._monotonic = monotonic

    @property
    def generation_id(self) -> uuid.UUID:
        return self._generation_id

    async def run(self, stop_event: asyncio.Event) -> None:
        await self._heartbeat_store.register(
            generation_id=self._generation_id,
            worker_id=self._worker_id,
        )
        tasks = [
            asyncio.create_task(
                self._run_loop(spec, stop_event),
                name=f"bot-tv-{spec.name}",
            )
            for spec in self._loops
        ]
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_EXCEPTION,
            )
            failure = next(
                (
                    task.exception()
                    for task in done
                    if not task.cancelled() and task.exception() is not None
                ),
                None,
            )
            if failure is not None:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise failure
            await asyncio.gather(*pending)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_loop(
        self,
        spec: WorkerLoopSpec,
        stop_event: asyncio.Event,
    ) -> None:
        last_heartbeat = float("-inf")
        consecutive_failures = 0

        while not stop_event.is_set():
            now = self._monotonic()
            heartbeat_due = (
                now - last_heartbeat
                >= self._settings.worker_heartbeat_interval_seconds
            )
            if heartbeat_due:
                await self._heartbeat_store.tick_started(
                    loop_name=spec.name,
                    generation_id=self._generation_id,
                )

            try:
                await asyncio.wait_for(
                    spec.tick(),
                    timeout=self._settings.worker_tick_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if is_expected_s2s_rate_limited(exc):
                    logger.info(
                        "worker loop rate limited loop=%s error_code=%s",
                        spec.name,
                        RATE_LIMITED_CODE,
                    )
                    consecutive_failures = 0
                    if heartbeat_due:
                        await self._heartbeat_store.tick_succeeded(
                            loop_name=spec.name,
                            generation_id=self._generation_id,
                        )
                        last_heartbeat = self._monotonic()
                else:
                    error_code = (
                        exc.code
                        if isinstance(exc, AmoCrmCrmOauthLifecycleError)
                        else type(exc).__name__[:64]
                    )
                    logger.error(
                        "worker loop failed loop=%s error_code=%s",
                        spec.name,
                        error_code,
                    )
                    consecutive_failures = await self._heartbeat_store.tick_failed(
                        loop_name=spec.name,
                        generation_id=self._generation_id,
                        error_code=error_code,
                    )
                    last_heartbeat = self._monotonic()
                    if (
                        consecutive_failures
                        >= self._settings.worker_max_consecutive_failures
                    ):
                        raise WorkerRuntimeFatal(
                            f"WORKER_LOOP_FAILURE_LIMIT:{spec.name}"
                        ) from None
            else:
                if heartbeat_due or consecutive_failures > 0:
                    await self._heartbeat_store.tick_succeeded(
                        loop_name=spec.name,
                        generation_id=self._generation_id,
                    )
                    last_heartbeat = self._monotonic()
                consecutive_failures = 0

            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=spec.poll_seconds,
                )
            except TimeoutError:
                pass


def build_booking_flow_for_worker(settings: Settings) -> BookingFlowService:
    """Compose BookingFlowService for the worker process (no app.state)."""

    return build_booking_flow_from_settings(settings)


def build_default_loop_specs(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    worker_id: str,
    booking_flow: BookingFlowService | None = None,
) -> tuple[WorkerLoopSpec, ...]:
    resolved_booking_flow = (
        rebind_booking_flow_to_runtime_settings(settings, booking_flow)
        if booking_flow is not None
        else build_booking_flow_for_worker(settings)
    )
    try:
        ingress_pii_store = build_ephemeral_pii_store_from_env(session_factory)
    except EphemeralPiiError:
        # Partial/invalid EPHEMERAL_PII_* must not abort the worker process.
        ingress_pii_store = None

    vk_outbound_config = None
    vk_sender = None
    try:
        from app.channels.vk_client_config import VkClientCallbackConfig
        from app.channels.vk_client_outbound_config import VkClientOutboundConfig
        from app.channels.vk_client_outbound_http import (
            NullVkClientSender,
            VkClientHttpSender,
        )

        callback_cfg = VkClientCallbackConfig.from_env()
        vk_outbound_config = VkClientOutboundConfig.from_env(callback=callback_cfg)
        if vk_outbound_config.outbound_enabled and vk_outbound_config.send_config_complete():
            vk_sender = VkClientHttpSender(vk_outbound_config)
        else:
            vk_sender = NullVkClientSender()
    except Exception:
        # Incomplete/invalid VK client outbound config must not abort the worker.
        vk_outbound_config = None
        from app.channels.vk_client_outbound_http import NullVkClientSender

        vk_sender = NullVkClientSender()

    ingress = IngressWorker(
        session_factory,
        worker_id=_lease_worker_id(worker_id, "ingress"),
        handoff_pause_seconds=settings.handoff_pause_seconds,
        pii_store=ingress_pii_store,
        settings=settings,
        vk_outbound_config=vk_outbound_config,
    )
    handoff = HandoffExpiryWorker(session_factory)
    reply_plan = ReplyPlanWorker(
        session_factory,
        worker_id=_lease_worker_id(worker_id, "reply"),
        booking_flow=resolved_booking_flow,
    )
    arbiter = OutboundArbiter(
        session_factory,
        settings=settings,
        vk_config=vk_outbound_config,
        vk_sender=vk_sender,
    )
    outbound = OutboundWorker(
        session_factory,
        worker_id=_lease_worker_id(worker_id, "outbound"),
        arbiter=arbiter,
    )
    mirror_lease_owner = _lease_worker_id(worker_id, "amocrm")
    mirror = AmoCrmMirrorWorker(
        session_factory,
        worker_id=mirror_lease_owner,
        adapter=CrmRestMirrorAdapter(
            session_factory,
            worker_id=mirror_lease_owner,
        ),
    )
    chat_projection = AmocrmChatProjectionWorker(
        session_factory,
        worker_id=_lease_worker_id(worker_id, "amocht"),
    )
    self_booking_create = SelfBookingCreateExecutionWorker(
        session_factory,
        booking_flow=resolved_booking_flow,
        pii_store=ingress_pii_store,
    )
    booking_s2s_config = None
    try:
        booking_s2s_config = build_booking_s2s_config(settings)
    except ValueError:
        booking_s2s_config = None
    teya_remote = (
        BookingRequestHttpClient(booking_s2s_config, S2sHttpStdlibTransport())
        if booking_s2s_config is not None
        else None
    )
    teya_crm = None
    try:
        teya_crm = build_teya_request_crm_service(
            session_factory, worker_id=_lease_worker_id(worker_id, "teya")
        )
    except (ValueError, TypeError, RuntimeError):
        # CRM REST / business-write misconfiguration must not abort the worker.
        teya_crm = None
    teya_request_orchestrator = TeyaRequestOrchestratorWorker(
        session_factory,
        remote=teya_remote,
        crm=teya_crm,
    )
    teya_request_reconciliation = TeyaRequestReconciliationWorker(
        session_factory,
        remote=teya_remote,
        crm=teya_crm,
    )
    booking_method_remote = (
        BookingMethodHttpClient(booking_s2s_config, S2sHttpStdlibTransport())
        if booking_s2s_config is not None
        else None
    )
    booking_method_analytics = BookingMethodAnalyticsWorker(
        session_factory,
        remote=booking_method_remote,
        crm=teya_crm,
    )
    acquisition_source_remote = (
        AcquisitionSourceHttpClient(booking_s2s_config, S2sHttpStdlibTransport())
        if booking_s2s_config is not None
        else None
    )
    acquisition_source_analytics = AcquisitionSourceAnalyticsWorker(
        session_factory,
        remote=acquisition_source_remote,
        crm=teya_crm,
    )
    amocrm_oauth_lifecycle = AmoCrmCrmOauthLifecycleWorker(
        session_factory,
        worker_id=_lease_worker_id(worker_id, "oauth"),
    )
    control_plane_remote = (
        ControlPlaneHttpClient(booking_s2s_config, S2sHttpStdlibTransport())
        if booking_s2s_config is not None
        else None
    )
    control_plane_service = ControlPlaneSnapshotService(
        session_factory,
        remote=control_plane_remote,
        max_stale_seconds=settings.control_plane_max_stale_seconds,
    )
    control_plane_snapshot = ControlPlaneSnapshotWorker(control_plane_service)

    # Shadow draft stack (internal only). Never feeds ReplyPlan / outbox / CRM.
    live_facts_remote = (
        LiveFactsHttpClient(booking_s2s_config, S2sHttpStdlibTransport())
        if booking_s2s_config is not None
        else None
    )
    shadow_runtime_builder = RuntimeContextBuilder(
        session_factory=session_factory,
        local_settings=settings,
        control_plane=control_plane_service,
        live_facts_remote=live_facts_remote,
    )
    try:
        shadow_text_port = build_text_generation_port()
    except (ValueError, TypeError, RuntimeError):
        # Partial/invalid Yandex config must not abort the worker process.
        shadow_text_port = None
    shadow_draft_service = build_shadow_draft_generation_service(
        port=shadow_text_port,
    )

    async def ingress_tick() -> None:
        for _ in range(settings.worker_batch_size):
            claim = await ingress.claim_one()
            if claim is None:
                return
            try:
                result = await ingress.process_claimed(claim)
            except StaleIngressLeaseError:
                continue
            # After durable inbound + lease completion: fail-soft shadow only.
            if (
                shadow_draft_service.shadow_feature_enabled
                and result.conversation_id is not None
                and result.inbox_id is not None
                and not result.duplicate_business
            ):
                try:
                    await run_shadow_draft_after_client_inbound(
                        conversation_id=result.conversation_id,
                        inbox_message_id=result.inbox_id,
                        session_factory=session_factory,
                        builder=shadow_runtime_builder,
                        service=shadow_draft_service,
                    )
                except Exception as exc:
                    try:
                        logger.info(
                            "shadow_draft event=ingress_hook_failed error_type=%s",
                            type(exc).__name__,
                        )
                    except Exception:
                        pass

    async def handoff_tick() -> None:
        await handoff.tick(max_items=settings.worker_batch_size)

    async def reply_plan_tick() -> None:
        for _ in range(settings.worker_batch_size):
            claim = await reply_plan.claim_one()
            if claim is None:
                return
            try:
                await reply_plan.dispatch_claimed(claim)
            except StaleReplyPlanLeaseError:
                continue

    async def outbound_tick() -> None:
        for _ in range(settings.worker_batch_size):
            claim = await outbound.claim_one()
            if claim is None:
                return
            try:
                await outbound.process_claimed(claim)
            except StaleOutboundLeaseError:
                continue
            except OutboundArbiterDenied as denied:
                if denied.is_expected_fence_outcome:
                    continue
                raise

    async def mirror_tick() -> None:
        for _ in range(settings.worker_batch_size):
            claim = await mirror.claim_one()
            if claim is None:
                break
            try:
                await mirror.process_claimed(claim)
            except StaleAmoCrmMirrorLeaseError:
                continue
            except AmoCrmMirrorRejected:
                # Transient/permanent CRM outcomes already persisted on the job.
                continue
        for _ in range(settings.worker_batch_size):
            claim = await chat_projection.claim_one()
            if claim is None:
                return
            try:
                await chat_projection.process_claimed(claim)
            except StaleAmocrmProjectionLeaseError:
                continue
            except RuntimeError:
                # Permanent/transient failures already persisted on the row.
                continue

    async def self_booking_create_tick() -> None:
        # CREATE HTTP can be slow; drain a small batch per tick.
        for _ in range(min(settings.worker_batch_size, 5)):
            pending_id = await self_booking_create.claim_one()
            if pending_id is None:
                return
            # Expected outcomes are returned as result objects, not exceptions.
            await self_booking_create.process_one(pending_id)

    async def teya_request_orchestrator_tick() -> None:
        await teya_request_orchestrator.ingest_feed()
        for _ in range(min(settings.worker_batch_size, 5)):
            pending_id = await teya_request_orchestrator.claim_one()
            if pending_id is None:
                return
            await teya_request_orchestrator.process_one(pending_id)

    async def teya_request_reconciliation_tick() -> None:
        await teya_request_reconciliation.tick()

    async def booking_method_analytics_tick() -> None:
        await booking_method_analytics.ingest_feed()
        for _ in range(min(settings.worker_batch_size, 5)):
            pending_id = await booking_method_analytics.claim_one()
            if pending_id is None:
                return
            await booking_method_analytics.process_one(pending_id)

    async def acquisition_source_analytics_tick() -> None:
        await acquisition_source_analytics.ingest_feed()
        for _ in range(min(settings.worker_batch_size, 5)):
            pending_id = await acquisition_source_analytics.claim_one()
            if pending_id is None:
                return
            await acquisition_source_analytics.process_one(pending_id)

    async def amocrm_oauth_lifecycle_tick() -> None:
        await amocrm_oauth_lifecycle.tick()

    async def control_plane_snapshot_tick() -> None:
        await control_plane_snapshot.tick()

    return (
        WorkerLoopSpec(
            name=INGRESS_LOOP,
            poll_seconds=settings.worker_poll_seconds,
            tick=ingress_tick,
        ),
        WorkerLoopSpec(
            name=HANDOFF_EXPIRY_LOOP,
            poll_seconds=settings.handoff_expiry_poll_seconds,
            tick=handoff_tick,
        ),
        WorkerLoopSpec(
            name=REPLY_PLAN_LOOP,
            poll_seconds=settings.worker_poll_seconds,
            tick=reply_plan_tick,
        ),
        WorkerLoopSpec(
            name=OUTBOUND_LOOP,
            poll_seconds=settings.worker_poll_seconds,
            tick=outbound_tick,
        ),
        WorkerLoopSpec(
            name=AMOCRM_MIRROR_LOOP,
            poll_seconds=settings.worker_poll_seconds,
            tick=mirror_tick,
        ),
        WorkerLoopSpec(
            name=SELF_BOOKING_CREATE_LOOP,
            poll_seconds=settings.worker_poll_seconds,
            tick=self_booking_create_tick,
        ),
        WorkerLoopSpec(
            name=TEYA_REQUEST_ORCHESTRATOR_LOOP,
            poll_seconds=settings.teya_request_poll_seconds,
            tick=teya_request_orchestrator_tick,
        ),
        WorkerLoopSpec(
            name=TEYA_REQUEST_RECONCILIATION_LOOP,
            poll_seconds=settings.teya_request_reconciliation_poll_seconds,
            tick=teya_request_reconciliation_tick,
        ),
        WorkerLoopSpec(
            name=BOOKING_METHOD_ANALYTICS_LOOP,
            poll_seconds=settings.booking_method_analytics_poll_seconds,
            tick=booking_method_analytics_tick,
        ),
        WorkerLoopSpec(
            name=ACQUISITION_SOURCE_ANALYTICS_LOOP,
            poll_seconds=settings.acquisition_source_analytics_poll_seconds,
            tick=acquisition_source_analytics_tick,
        ),
        WorkerLoopSpec(
            name=AMOCRM_CRM_OAUTH_LIFECYCLE_LOOP,
            poll_seconds=settings.worker_poll_seconds,
            tick=amocrm_oauth_lifecycle_tick,
        ),
        WorkerLoopSpec(
            name=CONTROL_PLANE_SNAPSHOT_LOOP,
            # CONTROL_PLANE_POLL_SECONDS / CONTROL_PLANE_REFRESH_SECONDS, not
            # the generic WORKER_POLL_SECONDS used by queue loops.
            poll_seconds=settings.control_plane_refresh_seconds,
            tick=control_plane_snapshot_tick,
        ),
    )


def _lease_worker_id(worker_id: str, suffix: str) -> str:
    reserved = len(suffix) + 1
    if reserved >= _LEASE_OWNER_MAX_LENGTH:
        raise ValueError("worker suffix is too long")
    return f"{worker_id[: _LEASE_OWNER_MAX_LENGTH - reserved]}:{suffix}"
