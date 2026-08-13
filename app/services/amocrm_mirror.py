from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import session_scope
from app.models.amocrm_mirror import (
    AmoCrmMirrorJob,
    AmoCrmMirrorJobType,
    AmoCrmMirrorSkipReason,
    AmoCrmMirrorStatus,
    AmoCrmMirrorSubjectKind,
    client_message_mirror_key,
    manager_takeover_mirror_key,
    outbound_delivered_mirror_key,
    reply_plan_state_mirror_key,
    safe_mirror_payload,
)
from app.models.conversation import Conversation, ConversationOwnership
from app.models.inbox import InboxMessage
from app.models.outbox import DeliveryStatus, OutboxMessage
from app.models.reply_plan import ReplyPlan
from app.repositories import amocrm_mirror as mirror_repo
from app.repositories import conversations as conversation_repo
from app.repositories.amocrm_mirror import (
    AmoCrmMirrorClaim,
    StaleAmoCrmMirrorLeaseError,
)
from app.services.amocrm_adapter import (
    AmoCrmMirrorAdapter,
    AmoCrmMirrorOutcome,
    AmoCrmMirrorRequest,
    NoopAmoCrmMirrorAdapter,
)

# Bot-action events are only meaningful for the context version they were
# produced in and only while the bot owns the dialog. Domain facts (a client
# message arrived, a manager took over) stay true afterwards and are therefore
# never gated by ownership or context.
_BOT_ACTION_JOB_TYPES = frozenset(
    {
        AmoCrmMirrorJobType.REPLY_PLAN_STATE_CHANGED.value,
        AmoCrmMirrorJobType.OUTBOUND_DELIVERED_META.value,
    }
)


class AmoCrmMirrorRejected(RuntimeError):
    """Local sink refused the job; the retry/DEAD path applies."""


@dataclass(frozen=True, repr=False)
class AmoCrmMirrorProcessResult:
    job_id: uuid.UUID
    status: str
    mirrored: bool
    skip_reason: str | None = None

    def __repr__(self) -> str:
        return (
            f"AmoCrmMirrorProcessResult(job_id={self.job_id!r}, "
            f"status={self.status!r}, mirrored={self.mirrored!r}, "
            f"skip_reason={self.skip_reason!r})"
        )


async def enqueue_client_message_received(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    inbox_id: uuid.UUID,
    context_version: int,
    correlation_id: uuid.UUID,
) -> tuple[AmoCrmMirrorJob, bool]:
    """Mirror the existence of a new client message — metadata only."""
    return await mirror_repo.enqueue_if_absent(
        session,
        job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META,
        subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE,
        subject_id=inbox_id,
        conversation_id=conversation_id,
        context_version=context_version,
        mirror_key=client_message_mirror_key(inbox_id),
        payload_json=safe_mirror_payload(
            job_type=AmoCrmMirrorJobType.CLIENT_MESSAGE_RECEIVED_META,
            subject_kind=AmoCrmMirrorSubjectKind.INBOX_MESSAGE,
            subject_id=inbox_id,
            conversation_id=conversation_id,
            context_version=context_version,
        ),
        correlation_id=correlation_id,
    )


async def enqueue_manager_takeover(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    correlation_id: uuid.UUID,
) -> tuple[AmoCrmMirrorJob, bool]:
    """Mirror the ownership transition. Not bound to a context version."""
    return await mirror_repo.enqueue_if_absent(
        session,
        job_type=AmoCrmMirrorJobType.MANAGER_TAKEOVER,
        subject_kind=AmoCrmMirrorSubjectKind.CONVERSATION,
        subject_id=conversation_id,
        conversation_id=conversation_id,
        context_version=None,
        mirror_key=manager_takeover_mirror_key(conversation_id),
        payload_json=safe_mirror_payload(
            job_type=AmoCrmMirrorJobType.MANAGER_TAKEOVER,
            subject_kind=AmoCrmMirrorSubjectKind.CONVERSATION,
            subject_id=conversation_id,
            conversation_id=conversation_id,
        ),
        correlation_id=correlation_id,
    )


async def enqueue_reply_plan_state_changed(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    plan_id: uuid.UUID,
    plan_status: str,
    context_version: int,
    correlation_id: uuid.UUID,
) -> tuple[AmoCrmMirrorJob, bool]:
    """Mirror a terminal reply-plan state reached by a lease-fenced worker."""
    return await mirror_repo.enqueue_if_absent(
        session,
        job_type=AmoCrmMirrorJobType.REPLY_PLAN_STATE_CHANGED,
        subject_kind=AmoCrmMirrorSubjectKind.REPLY_PLAN,
        subject_id=plan_id,
        conversation_id=conversation_id,
        context_version=context_version,
        mirror_key=reply_plan_state_mirror_key(plan_id, plan_status),
        payload_json=safe_mirror_payload(
            job_type=AmoCrmMirrorJobType.REPLY_PLAN_STATE_CHANGED,
            subject_kind=AmoCrmMirrorSubjectKind.REPLY_PLAN,
            subject_id=plan_id,
            conversation_id=conversation_id,
            context_version=context_version,
            subject_status=plan_status,
        ),
        correlation_id=correlation_id,
    )


async def enqueue_outbound_delivered(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    outbound_id: uuid.UUID,
    context_version: int | None,
    correlation_id: uuid.UUID,
) -> tuple[AmoCrmMirrorJob, bool]:
    """Mirror admission by the synthetic sink — metadata only, never a send."""
    return await mirror_repo.enqueue_if_absent(
        session,
        job_type=AmoCrmMirrorJobType.OUTBOUND_DELIVERED_META,
        subject_kind=AmoCrmMirrorSubjectKind.OUTBOX_MESSAGE,
        subject_id=outbound_id,
        conversation_id=conversation_id,
        context_version=context_version,
        mirror_key=outbound_delivered_mirror_key(outbound_id),
        payload_json=safe_mirror_payload(
            job_type=AmoCrmMirrorJobType.OUTBOUND_DELIVERED_META,
            subject_kind=AmoCrmMirrorSubjectKind.OUTBOX_MESSAGE,
            subject_id=outbound_id,
            conversation_id=conversation_id,
            context_version=context_version,
            subject_status=DeliveryStatus.DELIVERED.value,
        ),
        correlation_id=correlation_id,
    )


class AmoCrmMirrorWorker:
    """Drains amocrm_mirror_jobs through a CRM entity-convergence adapter.

    Each claimed job is revalidated against live dialog state under the
    conversation lock *before* the adapter runs.
    MIRRORED means required amoCRM entity state for this mirror job converged successfully
    — not that message content was copied to CRM.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        worker_id: str,
        adapter: AmoCrmMirrorAdapter | None = None,
        lease_seconds: int = mirror_repo.DEFAULT_LEASE_SECONDS,
        retry_delay_seconds: int = mirror_repo.DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._worker_id = worker_id
        self._adapter = adapter if adapter is not None else NoopAmoCrmMirrorAdapter()
        self._lease_seconds = lease_seconds
        self._retry_delay_seconds = retry_delay_seconds

    @property
    def adapter(self) -> AmoCrmMirrorAdapter:
        return self._adapter

    async def claim_one(
        self,
        *,
        now: datetime | None = None,
    ) -> AmoCrmMirrorClaim | None:
        async with session_scope(self._session_factory) as session:
            return await mirror_repo.claim_next(
                session,
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
                now=now,
            )

    async def process_claimed(
        self,
        claim: AmoCrmMirrorClaim,
        *,
        now: datetime | None = None,
    ) -> AmoCrmMirrorProcessResult:
        request = AmoCrmMirrorRequest(
            job_id=str(claim.job_id),
            job_type=claim.job_type,
            subject_kind=claim.subject_kind,
            subject_id=str(claim.subject_id),
            conversation_id=str(claim.conversation_id),
            context_version=claim.context_version,
            correlation_id=str(claim.correlation_id),
            _payload_schema=str(claim.payload_json.get("schema", "unknown")),
        )
        try:
            async with session_scope(self._session_factory) as session:
                # Lock order: conversations first, then the mirror job row.
                # Fence is required *before* any CRM side effect. The DB lock is
                # then released for the adapter call: mirror lease may be
                # reclaimed mid-flight while CRM HTTP runs. Stale completion is
                # still fenced by lease_token/version; deal reservation/fence
                # preserves at-most-one TECHNICAL_DEAL create semantics.
                conversation = await conversation_repo.get_by_id_for_update(
                    session,
                    conversation_id=claim.conversation_id,
                )
                await mirror_repo.require_processing_lease(
                    session,
                    job_id=claim.job_id,
                    lease_token=claim.lease_token,
                    lease_version=claim.lease_version,
                    lease_owner=claim.lease_owner,
                )
                skip_reason = await self._revalidate(session, claim, conversation)
                if skip_reason is not None:
                    job = await mirror_repo.skip_with_lease(
                        session,
                        job_id=claim.job_id,
                        lease_token=claim.lease_token,
                        lease_version=claim.lease_version,
                        skip_reason=skip_reason,
                        now=now,
                    )
                    return AmoCrmMirrorProcessResult(
                        job_id=job.id,
                        status=job.status,
                        mirrored=False,
                        skip_reason=job.skip_reason,
                    )

            result = await self._adapter.mirror(request)
            if result.outcome is not AmoCrmMirrorOutcome.SUCCESS:
                raise AmoCrmMirrorRejected(
                    result.error_code or "AMOCRM_MIRROR_REJECTED"
                )

            async with session_scope(self._session_factory) as session:
                await conversation_repo.get_by_id_for_update(
                    session,
                    conversation_id=claim.conversation_id,
                )
                job = await mirror_repo.complete_with_lease(
                    session,
                    job_id=claim.job_id,
                    lease_token=claim.lease_token,
                    lease_version=claim.lease_version,
                    now=now,
                )
                return AmoCrmMirrorProcessResult(
                    job_id=job.id,
                    status=job.status,
                    mirrored=job.status == AmoCrmMirrorStatus.MIRRORED.value,
                )
        except StaleAmoCrmMirrorLeaseError:
            raise
        except AmoCrmMirrorRejected as rejected:
            await self.fail_claimed(claim, error_code=str(rejected)[:64])
            raise
        except Exception as exc:
            await self.fail_claimed(claim, error_code=type(exc).__name__)
            raise

    async def fail_claimed(
        self,
        claim: AmoCrmMirrorClaim,
        *,
        error_code: str,
    ) -> AmoCrmMirrorJob:
        async with session_scope(self._session_factory) as session:
            return await mirror_repo.fail_with_lease(
                session,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                lease_version=claim.lease_version,
                error_code=error_code,
                retry_delay_seconds=self._retry_delay_seconds,
            )

    async def _revalidate(
        self,
        session: AsyncSession,
        claim: AmoCrmMirrorClaim,
        conversation: Conversation | None,
    ) -> AmoCrmMirrorSkipReason | None:
        if conversation is None:
            return AmoCrmMirrorSkipReason.SUBJECT_STATE_CHANGED

        if claim.job_type in _BOT_ACTION_JOB_TYPES:
            if (
                conversation.ownership != ConversationOwnership.BOT.value
                or conversation.manager_takeover_at is not None
            ):
                return AmoCrmMirrorSkipReason.MANAGER_TAKEOVER
            if (
                claim.context_version is not None
                and claim.context_version != conversation.context_version
            ):
                return AmoCrmMirrorSkipReason.STALE_CONTEXT

        expected_status = claim.payload_json.get("subject_status")
        if claim.subject_kind == AmoCrmMirrorSubjectKind.INBOX_MESSAGE.value:
            if await session.get(InboxMessage, claim.subject_id) is None:
                return AmoCrmMirrorSkipReason.SUBJECT_STATE_CHANGED
        elif claim.subject_kind == AmoCrmMirrorSubjectKind.REPLY_PLAN.value:
            plan = await session.get(ReplyPlan, claim.subject_id)
            if plan is None or plan.status != expected_status:
                return AmoCrmMirrorSkipReason.SUBJECT_STATE_CHANGED
        elif claim.subject_kind == AmoCrmMirrorSubjectKind.OUTBOX_MESSAGE.value:
            outbound = await session.get(OutboxMessage, claim.subject_id)
            if outbound is None or outbound.delivery_status != expected_status:
                return AmoCrmMirrorSkipReason.SUBJECT_STATE_CHANGED
        return None
