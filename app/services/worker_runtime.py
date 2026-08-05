from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.booking_eligibility_factory import build_booking_eligibility_client
from app.db.session import session_scope
from app.models.worker_heartbeat import (
    AMOCRM_MIRROR_LOOP,
    HANDOFF_EXPIRY_LOOP,
    INGRESS_LOOP,
    OUTBOUND_LOOP,
    REPLY_PLAN_LOOP,
)
from app.repositories import worker_heartbeats as heartbeat_repo
from app.repositories.amocrm_mirror import StaleAmoCrmMirrorLeaseError
from app.repositories.ingress import StaleIngressLeaseError
from app.repositories.outbound import StaleOutboundLeaseError
from app.repositories.reply_plans import StaleReplyPlanLeaseError
from app.services.amocrm_mirror import AmoCrmMirrorWorker
from app.services.booking_eligibility_flow import BookingEligibilityFlowService
from app.services.booking_flow import BookingFlowService
from app.services.handoff_expiry import HandoffExpiryWorker
from app.services.ingress import IngressWorker
from app.services.outbound_arbiter import OutboundArbiter
from app.services.outbound_arbiter import OutboundArbiterDenied
from app.services.reply_outbound import OutboundWorker, ReplyPlanWorker

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
        if set(names) != {
            INGRESS_LOOP,
            HANDOFF_EXPIRY_LOOP,
            REPLY_PLAN_LOOP,
            OUTBOUND_LOOP,
            AMOCRM_MIRROR_LOOP,
        }:
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
                error_code = type(exc).__name__[:64]
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

    client = build_booking_eligibility_client(settings)
    return BookingFlowService(BookingEligibilityFlowService(client))


def build_default_loop_specs(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    worker_id: str,
    booking_flow: BookingFlowService | None = None,
) -> tuple[WorkerLoopSpec, ...]:
    resolved_booking_flow = (
        booking_flow
        if booking_flow is not None
        else build_booking_flow_for_worker(settings)
    )
    ingress = IngressWorker(
        session_factory,
        worker_id=_lease_worker_id(worker_id, "ingress"),
        handoff_pause_seconds=settings.handoff_pause_seconds,
    )
    handoff = HandoffExpiryWorker(session_factory)
    reply_plan = ReplyPlanWorker(
        session_factory,
        worker_id=_lease_worker_id(worker_id, "reply"),
        booking_flow=resolved_booking_flow,
    )
    arbiter = OutboundArbiter(session_factory, settings=settings)
    outbound = OutboundWorker(
        session_factory,
        worker_id=_lease_worker_id(worker_id, "outbound"),
        arbiter=arbiter,
    )
    mirror = AmoCrmMirrorWorker(
        session_factory,
        worker_id=_lease_worker_id(worker_id, "amocrm"),
    )

    async def ingress_tick() -> None:
        for _ in range(settings.worker_batch_size):
            claim = await ingress.claim_one()
            if claim is None:
                return
            try:
                await ingress.process_claimed(claim)
            except StaleIngressLeaseError:
                continue

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
                return
            try:
                await mirror.process_claimed(claim)
            except StaleAmoCrmMirrorLeaseError:
                continue

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
    )


def _lease_worker_id(worker_id: str, suffix: str) -> str:
    reserved = len(suffix) + 1
    if reserved >= _LEASE_OWNER_MAX_LENGTH:
        raise ValueError("worker suffix is too long")
    return f"{worker_id[: _LEASE_OWNER_MAX_LENGTH - reserved]}:{suffix}"
