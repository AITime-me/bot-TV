"""Master command application flow (CURSOR-28).

Channel adapter → binding (C27) → parse → durable confirm/idempotency → C26 S2S.
No live VK/MAX wiring. master_id never appears in user-facing results.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ephemeral_pii_types import (
    EphemeralPiiKind,
    EphemeralPiiPurpose,
    EphemeralPiiReference,
)
from app.core.master_command_http import (
    MasterCommandHttpClient,
    MasterCommandHttpError,
)
from app.core.master_command_parser import (
    MasterCommandControlIntent,
    MasterCommandParseStatus,
    parse_master_command_text,
)
from app.core.master_command_types import (
    CANCEL_TEXT_TOKENS,
    CONFIRMATION_TTL_SECONDS,
    CONFIRM_TEXT_TOKENS,
    EXECUTION_LEASE_SECONDS,
    MasterCommandClarificationNeed,
    MasterCommandEnvelope,
    MasterCommandFlowOutcome,
    MasterCommandFlowResult,
    MasterCommandKind,
    MasterCommandPendingState,
    MasterCommandPreview,
    MasterCommandSafePayload,
    master_command_pii_conversation_id,
)
from app.repositories import master_command_pendings as pending_repo
from app.services.master_channel_binding import MasterChannelBindingService

logger = logging.getLogger(__name__)

_ALLOWED_LOG_CODES: frozenset[str] = frozenset(
    {
        "MASTER_CMD_BINDING_REQUIRED",
        "MASTER_CMD_BINDING_AMBIGUOUS",
        "MASTER_CMD_BINDING_INVALID",
        "MASTER_CMD_DUPLICATE",
        "MASTER_CMD_UNKNOWN",
        "MASTER_CMD_CLARIFICATION",
        "MASTER_CMD_CONFIRMATION",
        "MASTER_CMD_CANCELLED",
        "MASTER_CMD_EXECUTE",
        "MASTER_CMD_SUCCESS",
        "MASTER_CMD_CONFLICT",
        "MASTER_CMD_UNAVAILABLE",
        "MASTER_CMD_REJECTED",
        "MASTER_CMD_MANUAL_HELP",
    }
)

_CONFLICT_REMOTE: frozenset[str] = frozenset(
    {
        "APPOINTMENT_CONFLICT",
        "BLOCK_CONFLICT",
        "SLOT_NO_LONGER_AVAILABLE",
        "CLIENT_AMBIGUOUS",
        "IDEMPOTENCY_CONFLICT",
        "EXTRA_WORK_IN_USE",
    }
)
# Unknown / in-flight remote outcomes: keep pending + PII + stable idempotency key.
# Includes post-receive codes where the server may already have accepted a mutation
# but the client cannot prove a terminal outcome (never treat as definitive failure).
_RETRYABLE_REMOTE: frozenset[str] = frozenset(
    {
        "TIMEOUT",
        "IDEMPOTENCY_IN_PROGRESS",
        "TRANSPORT_ERROR",
        "RESPONSE_INVALID",
        "RESPONSE_TOO_LARGE",
    }
)


class MasterCommandPiiStore(Protocol):
    async def store(
        self,
        plaintext: str,
        *,
        conversation_id: uuid.UUID,
        kind: EphemeralPiiKind,
        purpose: EphemeralPiiPurpose,
    ) -> object: ...

    async def read_plaintext(
        self,
        reference: EphemeralPiiReference,
        *,
        conversation_id: uuid.UUID,
        kind: EphemeralPiiKind,
        purpose: EphemeralPiiPurpose,
    ) -> str: ...


def _log(event: str) -> None:
    if type(event) is not str or event not in _ALLOWED_LOG_CODES:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MasterCommandFlowService:
    """Application boundary for master commands. Caller owns session UoW."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        master_client: MasterCommandHttpClient | None,
        pii_store: MasterCommandPiiStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._bindings = MasterChannelBindingService(session)
        self._client = master_client
        self._pii = pii_store
        self._clock = clock if clock is not None else _utc_now

    async def handle(self, envelope: MasterCommandEnvelope) -> MasterCommandFlowResult:
        if type(envelope) is not MasterCommandEnvelope:
            _log("MASTER_CMD_REJECTED")
            return MasterCommandFlowResult(outcome=MasterCommandFlowOutcome.REJECTED)

        now = self._clock()
        if type(now) is not datetime or now.tzinfo is None:
            _log("MASTER_CMD_REJECTED")
            return MasterCommandFlowResult(outcome=MasterCommandFlowOutcome.REJECTED)

        existing = await pending_repo.get_by_inbound(
            self._session,
            channel=envelope.channel.value,
            connection_scope=envelope.connection_scope,
            external_account_id=envelope.external_account_id,
            inbound_message_id=envelope.external_message_id,
        )
        if existing is not None:
            _log("MASTER_CMD_DUPLICATE")
            return _result_from_row(existing)

        resolved = await self._bindings.resolve(
            channel=envelope.channel,
            external_account_id=envelope.external_account_id,
            connection_scope=envelope.connection_scope,
        )
        from app.core.master_channel_binding import ResolveMasterBindingOutcome

        if resolved.outcome is ResolveMasterBindingOutcome.NOT_FOUND:
            _log("MASTER_CMD_BINDING_REQUIRED")
            return MasterCommandFlowResult(
                outcome=MasterCommandFlowOutcome.BINDING_REQUIRED
            )
        if resolved.outcome is ResolveMasterBindingOutcome.AMBIGUOUS:
            _log("MASTER_CMD_BINDING_AMBIGUOUS")
            return MasterCommandFlowResult(
                outcome=MasterCommandFlowOutcome.BINDING_AMBIGUOUS
            )
        if resolved.outcome is ResolveMasterBindingOutcome.INVALID_INPUT:
            _log("MASTER_CMD_BINDING_INVALID")
            return MasterCommandFlowResult(outcome=MasterCommandFlowOutcome.REJECTED)
        master_id = resolved.master_id
        if type(master_id) is not str or not master_id:
            _log("MASTER_CMD_BINDING_REQUIRED")
            return MasterCommandFlowResult(
                outcome=MasterCommandFlowOutcome.BINDING_REQUIRED
            )

        active = await pending_repo.lock_active_by_identity(
            self._session,
            channel=envelope.channel.value,
            connection_scope=envelope.connection_scope,
            external_account_id=envelope.external_account_id,
        )
        if active is not None:
            expired = await self._expire_if_needed(active, now)
            if expired:
                active = None

        parsed = parse_master_command_text(envelope.text, now=envelope.occurred_at)

        if parsed.status is MasterCommandParseStatus.CONTROL:
            return await self._handle_control(
                envelope=envelope,
                master_id=master_id,
                active=active,
                control=parsed.control,
                now=now,
            )

        if active is not None:
            if (
                active.state == MasterCommandPendingState.AWAITING_CLARIFICATION.value
                and parsed.kind is not None
                and active.command_kind == parsed.kind.value
            ):
                return await self._merge_clarification(
                    envelope=envelope,
                    master_id=master_id,
                    active=active,
                    parsed=parsed,
                    now=now,
                )
            if active.state in {
                MasterCommandPendingState.AWAITING_CONFIRMATION.value,
                MasterCommandPendingState.AWAITING_CLARIFICATION.value,
                MasterCommandPendingState.EXECUTING.value,
            }:
                _log("MASTER_CMD_CONFLICT")
                return MasterCommandFlowResult(
                    outcome=MasterCommandFlowOutcome.CONFLICT,
                    result_code="PENDING_COMMAND_ACTIVE",
                    command_kind=MasterCommandKind(active.command_kind),
                    command_version=active.command_version,
                )

        if parsed.status is MasterCommandParseStatus.UNKNOWN:
            _log("MASTER_CMD_UNKNOWN")
            return await self._store_terminal_unknown(envelope, master_id, now)

        if parsed.kind is MasterCommandKind.SCHEDULE_READ:
            return await self._execute_schedule(
                envelope=envelope,
                master_id=master_id,
                payload=parsed.payload or MasterCommandSafePayload(),
                now=now,
            )

        if parsed.status is MasterCommandParseStatus.CLARIFICATION_REQUIRED:
            return await self._start_clarification(
                envelope=envelope,
                master_id=master_id,
                parsed=parsed,
                now=now,
            )

        return await self._start_confirmation(
            envelope=envelope,
            master_id=master_id,
            parsed=parsed,
            now=now,
        )

    async def _expire_if_needed(self, row, now: datetime) -> bool:
        if row.state == MasterCommandPendingState.EXECUTING.value:
            lease_exp = row.execution_lease_expires_at
            if lease_exp is None or lease_exp > now:
                return False
            # Unblock identity: expired EXECUTING → confirmable again (same key/PII).
            recovered = await pending_repo.recover_expired_execution_to_confirmation(
                self._session,
                row=row,
                confirmation_expires_at=now
                + timedelta(seconds=CONFIRMATION_TTL_SECONDS),
                now=now,
            )
            if recovered:
                await self._session.refresh(row)
            return False

        if row.state not in {
            MasterCommandPendingState.AWAITING_CLARIFICATION.value,
            MasterCommandPendingState.AWAITING_CONFIRMATION.value,
        }:
            return False
        expires = row.confirmation_expires_at
        if expires is None or expires > now:
            return False
        await pending_repo.mark_terminal(
            self._session,
            row=row,
            state=MasterCommandPendingState.EXPIRED,
            result_code="EXPIRED",
            result_outcome=MasterCommandFlowOutcome.MANUAL_HELP.value,
            now=now,
        )
        # No synchronous PII delete: TTL/maintenance owns ciphertext cleanup.
        return True

    async def _handle_control(
        self,
        *,
        envelope: MasterCommandEnvelope,
        master_id: str,
        active,
        control: MasterCommandControlIntent,
        now: datetime,
    ) -> MasterCommandFlowResult:
        if active is None:
            _log("MASTER_CMD_MANUAL_HELP")
            return await self._insert_control_noop(
                envelope, master_id, now, MasterCommandFlowOutcome.MANUAL_HELP
            )

        if control is MasterCommandControlIntent.CANCEL:
            if active.state in {
                MasterCommandPendingState.AWAITING_CLARIFICATION.value,
                MasterCommandPendingState.AWAITING_CONFIRMATION.value,
            }:
                await pending_repo.mark_terminal(
                    self._session,
                    row=active,
                    state=MasterCommandPendingState.CANCELLED,
                    result_code="CANCELLED",
                    result_outcome=MasterCommandFlowOutcome.CANCELLED.value,
                    now=now,
                )
                # Record inbound dedupe row pointing at cancel.
                await self._insert_dedupe_mirror(
                    envelope,
                    master_id,
                    now,
                    kind=MasterCommandKind(active.command_kind),
                    outcome=MasterCommandFlowOutcome.CANCELLED,
                    version=active.command_version,
                    idempotency_key=active.idempotency_key,
                )
                _log("MASTER_CMD_CANCELLED")
                return MasterCommandFlowResult(
                    outcome=MasterCommandFlowOutcome.CANCELLED,
                    command_kind=MasterCommandKind(active.command_kind),
                    command_version=active.command_version,
                )
            _log("MASTER_CMD_MANUAL_HELP")
            return await self._insert_control_noop(
                envelope, master_id, now, MasterCommandFlowOutcome.MANUAL_HELP
            )

        # CONFIRM — only AWAITING_CONFIRMATION or reclaimable expired EXECUTING.
        if active.state == MasterCommandPendingState.EXECUTING.value:
            lease_exp = active.execution_lease_expires_at
            if lease_exp is not None and lease_exp > now:
                _log("MASTER_CMD_CONFLICT")
                return await self._insert_control_noop(
                    envelope, master_id, now, MasterCommandFlowOutcome.CONFLICT
                )
            # Expired lease: reclaim below inside _execute_confirmed.
        elif active.state != MasterCommandPendingState.AWAITING_CONFIRMATION.value:
            _log("MASTER_CMD_MANUAL_HELP")
            return await self._insert_control_noop(
                envelope, master_id, now, MasterCommandFlowOutcome.MANUAL_HELP
            )
        elif (
            active.confirmation_expires_at is None
            or active.confirmation_expires_at <= now
        ):
            await pending_repo.mark_terminal(
                self._session,
                row=active,
                state=MasterCommandPendingState.EXPIRED,
                result_code="EXPIRED",
                result_outcome=MasterCommandFlowOutcome.MANUAL_HELP.value,
                now=now,
            )
            _log("MASTER_CMD_MANUAL_HELP")
            return await self._insert_control_noop(
                envelope, master_id, now, MasterCommandFlowOutcome.MANUAL_HELP
            )

        return await self._execute_confirmed(
            envelope=envelope,
            master_id=master_id,
            active=active,
            now=now,
        )

    async def _execute_confirmed(
        self,
        *,
        envelope: MasterCommandEnvelope,
        master_id: str,
        active,
        now: datetime,
    ) -> MasterCommandFlowResult:
        if self._client is None:
            _log("MASTER_CMD_UNAVAILABLE")
            return await self._insert_control_noop(
                envelope, master_id, now, MasterCommandFlowOutcome.UNAVAILABLE
            )

        lease = uuid.uuid4()
        lease_exp = now + timedelta(seconds=EXECUTION_LEASE_SECONDS)
        if active.state == MasterCommandPendingState.EXECUTING.value:
            claimed = await pending_repo.reclaim_expired_execution(
                self._session,
                row=active,
                lease_token=lease,
                lease_expires_at=lease_exp,
                expected_version=active.command_version,
                now=now,
            )
        else:
            claimed = await pending_repo.claim_for_execution(
                self._session,
                row=active,
                lease_token=lease,
                lease_expires_at=lease_exp,
                expected_version=active.command_version,
                now=now,
            )
        if not claimed:
            _log("MASTER_CMD_CONFLICT")
            return await self._insert_control_noop(
                envelope, master_id, now, MasterCommandFlowOutcome.CONFLICT
            )

        await self._session.refresh(active)
        payload = MasterCommandSafePayload.from_json_dict(active.safe_payload)
        kind = MasterCommandKind(active.command_kind)
        idem = active.idempotency_key

        phone: str | None = None
        name: str | None = None
        if kind is MasterCommandKind.CREATE_BOOKING:
            # Non-destructive read: ciphertext retained until terminal success/failure.
            read = await self._read_booking_pii(active)
            if read is None:
                await pending_repo.complete_execution(
                    self._session,
                    row=active,
                    lease_token=lease,
                    state=MasterCommandPendingState.FAILED,
                    result_code="PII_UNAVAILABLE",
                    result_outcome=MasterCommandFlowOutcome.UNAVAILABLE.value,
                    now=now,
                )
                _log("MASTER_CMD_UNAVAILABLE")
                return await self._insert_dedupe_mirror(
                    envelope,
                    master_id,
                    now,
                    kind=kind,
                    outcome=MasterCommandFlowOutcome.UNAVAILABLE,
                    version=active.command_version,
                    result_code="PII_UNAVAILABLE",
                    idempotency_key=idem,
                )
            phone, name = read

        try:
            if kind is MasterCommandKind.CLOSE_INTERVAL:
                self._client.close_interval(
                    idempotency_key=idem,
                    master_id=master_id,
                    date_key=payload.date_key,
                    start_time=payload.start_time,
                    end_time=payload.end_time,
                    block_type=payload.block_type,
                )
            elif kind is MasterCommandKind.CLOSE_DAY:
                self._client.close_day(
                    idempotency_key=idem,
                    master_id=master_id,
                    date_key=payload.date_key,
                    block_type=payload.block_type,
                )
            elif kind is MasterCommandKind.CREATE_BOOKING:
                self._client.create_booking(
                    idempotency_key=idem,
                    master_id=master_id,
                    slot_id=payload.slot_id,
                    client_name=name,
                    phone=phone,
                )
            else:
                await pending_repo.complete_execution(
                    self._session,
                    row=active,
                    lease_token=lease,
                    state=MasterCommandPendingState.FAILED,
                    result_code="UNSUPPORTED",
                    result_outcome=MasterCommandFlowOutcome.REJECTED.value,
                    now=now,
                )
                return await self._insert_dedupe_mirror(
                    envelope,
                    master_id,
                    now,
                    kind=kind,
                    outcome=MasterCommandFlowOutcome.REJECTED,
                    version=active.command_version,
                    idempotency_key=idem,
                )
        except MasterCommandHttpError as exc:
            return await self._handle_remote_error(
                envelope=envelope,
                master_id=master_id,
                active=active,
                lease=lease,
                kind=kind,
                code=exc.code,
                now=now,
            )
        except Exception:
            # Unknown local failure before/around remote: keep retryable pending + PII.
            await pending_repo.release_execution_to_confirmation(
                self._session,
                row=active,
                lease_token=lease,
                confirmation_expires_at=now
                + timedelta(seconds=CONFIRMATION_TTL_SECONDS),
                now=now,
                result_code="INTERNAL_RETRYABLE",
            )
            _log("MASTER_CMD_UNAVAILABLE")
            return await self._insert_dedupe_mirror(
                envelope,
                master_id,
                now,
                kind=kind,
                outcome=MasterCommandFlowOutcome.UNAVAILABLE,
                version=active.command_version,
                result_code="INTERNAL_RETRYABLE",
                idempotency_key=idem,
            )

        await pending_repo.complete_execution(
            self._session,
            row=active,
            lease_token=lease,
            state=MasterCommandPendingState.SUCCEEDED,
            result_code="OK",
            result_outcome=MasterCommandFlowOutcome.SUCCESS.value,
            now=now,
        )
        # No synchronous PII delete (separate committed txn would reopen B1).
        # Ciphertext expires via ephemeral TTL/maintenance after durable terminal.
        _log("MASTER_CMD_SUCCESS")
        return await self._insert_dedupe_mirror(
            envelope,
            master_id,
            now,
            kind=kind,
            outcome=MasterCommandFlowOutcome.SUCCESS,
            version=active.command_version,
            result_code="OK",
            preview=_preview_for(kind, payload, active.command_version),
            idempotency_key=idem,
        )

    async def _handle_remote_error(
        self,
        *,
        envelope,
        master_id,
        active,
        lease,
        kind,
        code: str,
        now: datetime,
    ) -> MasterCommandFlowResult:
        if code in _RETRYABLE_REMOTE:
            await pending_repo.release_execution_to_confirmation(
                self._session,
                row=active,
                lease_token=lease,
                confirmation_expires_at=now
                + timedelta(seconds=CONFIRMATION_TTL_SECONDS),
                now=now,
                result_code=code,
            )
            _log("MASTER_CMD_UNAVAILABLE")
            return await self._insert_dedupe_mirror(
                envelope,
                master_id,
                now,
                kind=kind,
                outcome=MasterCommandFlowOutcome.UNAVAILABLE,
                version=active.command_version,
                result_code=code,
                idempotency_key=active.idempotency_key,
            )

        if code in _CONFLICT_REMOTE:
            outcome = MasterCommandFlowOutcome.CONFLICT
            _log("MASTER_CMD_CONFLICT")
        else:
            outcome = MasterCommandFlowOutcome.UNAVAILABLE
            _log("MASTER_CMD_UNAVAILABLE")
        await pending_repo.complete_execution(
            self._session,
            row=active,
            lease_token=lease,
            state=MasterCommandPendingState.FAILED,
            result_code=code,
            result_outcome=outcome.value,
            now=now,
        )
        return await self._insert_dedupe_mirror(
            envelope,
            master_id,
            now,
            kind=kind,
            outcome=outcome,
            version=active.command_version,
            result_code=code,
            idempotency_key=active.idempotency_key,
        )

    async def _execute_schedule(
        self,
        *,
        envelope,
        master_id: str,
        payload: MasterCommandSafePayload,
        now: datetime,
    ) -> MasterCommandFlowResult:
        if self._client is None:
            _log("MASTER_CMD_UNAVAILABLE")
            return await self._insert_schedule_terminal(
                envelope,
                master_id,
                payload,
                now,
                MasterCommandFlowOutcome.UNAVAILABLE,
                "CLIENT_UNCONFIGURED",
            )
        try:
            remote = self._client.read_schedule(
                master_id=master_id,
                from_date_key=payload.from_date_key,
                to_date_key=payload.to_date_key,
            )
        except MasterCommandHttpError as exc:
            outcome = (
                MasterCommandFlowOutcome.CONFLICT
                if exc.code in _CONFLICT_REMOTE
                else MasterCommandFlowOutcome.UNAVAILABLE
            )
            _log(
                "MASTER_CMD_CONFLICT"
                if outcome is MasterCommandFlowOutcome.CONFLICT
                else "MASTER_CMD_UNAVAILABLE"
            )
            return await self._insert_schedule_terminal(
                envelope,
                master_id,
                payload,
                now,
                outcome,
                exc.code,
            )

        summary = _schedule_summary_lines(remote.days)
        row_id = uuid.uuid4()
        try:
            await pending_repo.insert_pending(
                self._session,
                row_id=row_id,
                channel=envelope.channel.value,
                connection_scope=envelope.connection_scope,
                external_account_id=envelope.external_account_id,
                master_id=master_id,
                inbound_message_id=envelope.external_message_id,
                command_kind=MasterCommandKind.SCHEDULE_READ,
                state=MasterCommandPendingState.SUCCEEDED,
                command_version=1,
                idempotency_key=None,
                safe_payload=payload,
                phone_ref_token=None,
                name_ref_token=None,
                pii_conversation_id=None,
                confirmation_expires_at=None,
                now=now,
                result_code="OK",
                result_outcome=MasterCommandFlowOutcome.SUCCESS.value,
            )
        except IntegrityError:
            existing = await pending_repo.get_by_inbound(
                self._session,
                channel=envelope.channel.value,
                connection_scope=envelope.connection_scope,
                external_account_id=envelope.external_account_id,
                inbound_message_id=envelope.external_message_id,
            )
            if existing is not None:
                _log("MASTER_CMD_DUPLICATE")
                return _result_from_row(existing)
            raise
        _log("MASTER_CMD_SUCCESS")
        return MasterCommandFlowResult(
            outcome=MasterCommandFlowOutcome.SUCCESS,
            schedule_summary=summary,
            result_code="OK",
            command_kind=MasterCommandKind.SCHEDULE_READ,
            command_version=1,
        )

    async def _start_clarification(
        self, *, envelope, master_id, parsed, now
    ) -> MasterCommandFlowResult:
        phone_ref = None
        name_ref = None
        pii_conv = None
        if parsed.kind is MasterCommandKind.CREATE_BOOKING:
            if parsed.phone or parsed.client_name:
                if self._pii is None:
                    _log("MASTER_CMD_UNAVAILABLE")
                    return MasterCommandFlowResult(
                        outcome=MasterCommandFlowOutcome.UNAVAILABLE,
                        result_code="PII_STORE_UNCONFIGURED",
                        command_kind=parsed.kind,
                    )
                pii_conv = master_command_pii_conversation_id(
                    channel=envelope.channel,
                    connection_scope=envelope.connection_scope,
                    external_account_id=envelope.external_account_id,
                )
                if parsed.phone:
                    handle = await self._pii.store(
                        parsed.phone,
                        conversation_id=pii_conv,
                        kind=EphemeralPiiKind.PHONE,
                        purpose=EphemeralPiiPurpose.MASTER_BOOKING_CLIENT_WRITE,
                    )
                    phone_ref = handle.reference.to_token()
                if parsed.client_name:
                    handle = await self._pii.store(
                        parsed.client_name,
                        conversation_id=pii_conv,
                        kind=EphemeralPiiKind.CLIENT_NAME,
                        purpose=EphemeralPiiPurpose.MASTER_BOOKING_CLIENT_WRITE,
                    )
                    name_ref = handle.reference.to_token()

        expires = now + timedelta(seconds=CONFIRMATION_TTL_SECONDS)
        payload = parsed.payload or MasterCommandSafePayload()
        try:
            await pending_repo.insert_pending(
                self._session,
                row_id=uuid.uuid4(),
                channel=envelope.channel.value,
                connection_scope=envelope.connection_scope,
                external_account_id=envelope.external_account_id,
                master_id=master_id,
                inbound_message_id=envelope.external_message_id,
                command_kind=parsed.kind,
                state=MasterCommandPendingState.AWAITING_CLARIFICATION,
                command_version=1,
                idempotency_key=None,
                safe_payload=payload,
                phone_ref_token=phone_ref,
                name_ref_token=name_ref,
                pii_conversation_id=pii_conv,
                confirmation_expires_at=expires,
                now=now,
                result_code=None,
                result_outcome=MasterCommandFlowOutcome.CLARIFICATION_REQUIRED.value,
            )
        except IntegrityError:
            existing = await pending_repo.get_by_inbound(
                self._session,
                channel=envelope.channel.value,
                connection_scope=envelope.connection_scope,
                external_account_id=envelope.external_account_id,
                inbound_message_id=envelope.external_message_id,
            )
            if existing is not None:
                return _result_from_row(existing)
            _log("MASTER_CMD_CONFLICT")
            return MasterCommandFlowResult(
                outcome=MasterCommandFlowOutcome.CONFLICT,
                result_code="PENDING_COMMAND_ACTIVE",
                command_kind=parsed.kind,
            )
        _log("MASTER_CMD_CLARIFICATION")
        return MasterCommandFlowResult(
            outcome=MasterCommandFlowOutcome.CLARIFICATION_REQUIRED,
            clarification_needs=parsed.needs,
            command_kind=parsed.kind,
            command_version=1,
            details=tuple(n.value for n in parsed.needs),
        )

    async def _start_confirmation(
        self, *, envelope, master_id, parsed, now
    ) -> MasterCommandFlowResult:
        payload = parsed.payload or MasterCommandSafePayload()
        phone_ref = None
        name_ref = None
        pii_conv = None
        idem = str(uuid.uuid4())
        if parsed.kind is MasterCommandKind.CREATE_BOOKING:
            if self._pii is None or not parsed.phone or not parsed.client_name:
                _log("MASTER_CMD_UNAVAILABLE")
                return MasterCommandFlowResult(
                    outcome=MasterCommandFlowOutcome.UNAVAILABLE,
                    result_code="PII_REQUIRED",
                    command_kind=parsed.kind,
                )
            stored = await self._store_booking_pii(
                envelope, phone=parsed.phone, name=parsed.client_name
            )
            if stored is None:
                return MasterCommandFlowResult(
                    outcome=MasterCommandFlowOutcome.UNAVAILABLE,
                    result_code="PII_STORE_FAILED",
                    command_kind=parsed.kind,
                )
            phone_ref, name_ref = stored
            pii_conv = master_command_pii_conversation_id(
                channel=envelope.channel,
                connection_scope=envelope.connection_scope,
                external_account_id=envelope.external_account_id,
            )

        expires = now + timedelta(seconds=CONFIRMATION_TTL_SECONDS)
        preview = _preview_for(parsed.kind, payload, 1)
        try:
            await pending_repo.insert_pending(
                self._session,
                row_id=uuid.uuid4(),
                channel=envelope.channel.value,
                connection_scope=envelope.connection_scope,
                external_account_id=envelope.external_account_id,
                master_id=master_id,
                inbound_message_id=envelope.external_message_id,
                command_kind=parsed.kind,
                state=MasterCommandPendingState.AWAITING_CONFIRMATION,
                command_version=1,
                idempotency_key=idem,
                safe_payload=payload,
                phone_ref_token=phone_ref,
                name_ref_token=name_ref,
                pii_conversation_id=pii_conv,
                confirmation_expires_at=expires,
                now=now,
                result_code=None,
                result_outcome=MasterCommandFlowOutcome.CONFIRMATION_REQUIRED.value,
            )
        except IntegrityError:
            existing = await pending_repo.get_by_inbound(
                self._session,
                channel=envelope.channel.value,
                connection_scope=envelope.connection_scope,
                external_account_id=envelope.external_account_id,
                inbound_message_id=envelope.external_message_id,
            )
            if existing is not None:
                return _result_from_row(existing)
            _log("MASTER_CMD_CONFLICT")
            return MasterCommandFlowResult(
                outcome=MasterCommandFlowOutcome.CONFLICT,
                result_code="PENDING_COMMAND_ACTIVE",
                command_kind=parsed.kind,
            )
        _log("MASTER_CMD_CONFIRMATION")
        return MasterCommandFlowResult(
            outcome=MasterCommandFlowOutcome.CONFIRMATION_REQUIRED,
            preview=preview,
            command_kind=parsed.kind,
            command_version=1,
        )

    async def _merge_clarification(
        self, *, envelope, master_id, active, parsed, now
    ) -> MasterCommandFlowResult:
        current = MasterCommandSafePayload.from_json_dict(active.safe_payload)
        incoming = parsed.payload or MasterCommandSafePayload()
        merged = MasterCommandSafePayload(
            date_key=incoming.date_key or current.date_key,
            start_time=incoming.start_time or current.start_time,
            end_time=incoming.end_time or current.end_time,
            block_type=incoming.block_type or current.block_type,
            slot_id=incoming.slot_id or current.slot_id,
            from_date_key=incoming.from_date_key or current.from_date_key,
            to_date_key=incoming.to_date_key or current.to_date_key,
            missing=(),
        )
        phone_ref = active.phone_ref_token
        name_ref = active.name_ref_token
        if parsed.kind is MasterCommandKind.CREATE_BOOKING:
            if parsed.phone or parsed.client_name:
                if self._pii is None:
                    return MasterCommandFlowResult(
                        outcome=MasterCommandFlowOutcome.UNAVAILABLE,
                        result_code="PII_STORE_UNCONFIGURED",
                        command_kind=parsed.kind,
                    )
                if parsed.phone:
                    handle = await self._pii.store(
                        parsed.phone,
                        conversation_id=active.pii_conversation_id
                        or master_command_pii_conversation_id(
                            channel=envelope.channel,
                            connection_scope=envelope.connection_scope,
                            external_account_id=envelope.external_account_id,
                        ),
                        kind=EphemeralPiiKind.PHONE,
                        purpose=EphemeralPiiPurpose.MASTER_BOOKING_CLIENT_WRITE,
                    )
                    phone_ref = handle.reference.to_token()
                if parsed.client_name:
                    handle = await self._pii.store(
                        parsed.client_name,
                        conversation_id=active.pii_conversation_id
                        or master_command_pii_conversation_id(
                            channel=envelope.channel,
                            connection_scope=envelope.connection_scope,
                            external_account_id=envelope.external_account_id,
                        ),
                        kind=EphemeralPiiKind.CLIENT_NAME,
                        purpose=EphemeralPiiPurpose.MASTER_BOOKING_CLIENT_WRITE,
                    )
                    name_ref = handle.reference.to_token()

        needs = _missing_for_kind(MasterCommandKind(active.command_kind), merged)
        if MasterCommandKind(active.command_kind) is MasterCommandKind.CREATE_BOOKING:
            if not phone_ref:
                needs.append(MasterCommandClarificationNeed.PHONE)
            if not name_ref:
                needs.append(MasterCommandClarificationNeed.CLIENT_NAME)

        expires = now + timedelta(seconds=CONFIRMATION_TTL_SECONDS)
        if needs:
            merged = MasterCommandSafePayload(
                date_key=merged.date_key,
                start_time=merged.start_time,
                end_time=merged.end_time,
                block_type=merged.block_type,
                slot_id=merged.slot_id,
                from_date_key=merged.from_date_key,
                to_date_key=merged.to_date_key,
                missing=tuple(n.value for n in needs),
            )
            await pending_repo.update_clarification(
                self._session,
                row=active,
                safe_payload=merged,
                phone_ref_token=phone_ref,
                name_ref_token=name_ref,
                state=MasterCommandPendingState.AWAITING_CLARIFICATION,
                confirmation_expires_at=expires,
                idempotency_key=None,
                now=now,
            )
            await self._insert_dedupe_mirror(
                envelope,
                master_id,
                now,
                kind=MasterCommandKind(active.command_kind),
                outcome=MasterCommandFlowOutcome.CLARIFICATION_REQUIRED,
                version=active.command_version,
            )
            _log("MASTER_CMD_CLARIFICATION")
            return MasterCommandFlowResult(
                outcome=MasterCommandFlowOutcome.CLARIFICATION_REQUIRED,
                clarification_needs=tuple(needs),
                command_kind=MasterCommandKind(active.command_kind),
                command_version=active.command_version,
            )

        idem = active.idempotency_key or str(uuid.uuid4())
        await pending_repo.update_clarification(
            self._session,
            row=active,
            safe_payload=merged,
            phone_ref_token=phone_ref,
            name_ref_token=name_ref,
            state=MasterCommandPendingState.AWAITING_CONFIRMATION,
            confirmation_expires_at=expires,
            idempotency_key=idem,
            now=now,
        )
        await self._insert_dedupe_mirror(
            envelope,
            master_id,
            now,
            kind=MasterCommandKind(active.command_kind),
            outcome=MasterCommandFlowOutcome.CONFIRMATION_REQUIRED,
            version=active.command_version,
        )
        _log("MASTER_CMD_CONFIRMATION")
        return MasterCommandFlowResult(
            outcome=MasterCommandFlowOutcome.CONFIRMATION_REQUIRED,
            preview=_preview_for(
                MasterCommandKind(active.command_kind), merged, active.command_version
            ),
            command_kind=MasterCommandKind(active.command_kind),
            command_version=active.command_version,
        )

    async def _store_booking_pii(
        self, envelope, *, phone: str, name: str
    ) -> tuple[str, str] | None:
        if self._pii is None:
            return None
        conv = master_command_pii_conversation_id(
            channel=envelope.channel,
            connection_scope=envelope.connection_scope,
            external_account_id=envelope.external_account_id,
        )
        try:
            phone_h = await self._pii.store(
                phone,
                conversation_id=conv,
                kind=EphemeralPiiKind.PHONE,
                purpose=EphemeralPiiPurpose.MASTER_BOOKING_CLIENT_WRITE,
            )
            name_h = await self._pii.store(
                name,
                conversation_id=conv,
                kind=EphemeralPiiKind.CLIENT_NAME,
                purpose=EphemeralPiiPurpose.MASTER_BOOKING_CLIENT_WRITE,
            )
        except Exception:
            return None
        return phone_h.reference.to_token(), name_h.reference.to_token()

    async def _read_booking_pii(self, active) -> tuple[str, str] | None:
        """Non-destructive purpose-bound decrypt for CREATE_BOOKING execution."""

        if self._pii is None:
            return None
        if not active.phone_ref_token or not active.name_ref_token:
            return None
        if active.pii_conversation_id is None:
            return None
        try:
            phone = await self._pii.read_plaintext(
                EphemeralPiiReference.parse(active.phone_ref_token),
                conversation_id=active.pii_conversation_id,
                kind=EphemeralPiiKind.PHONE,
                purpose=EphemeralPiiPurpose.MASTER_BOOKING_CLIENT_WRITE,
            )
            name = await self._pii.read_plaintext(
                EphemeralPiiReference.parse(active.name_ref_token),
                conversation_id=active.pii_conversation_id,
                kind=EphemeralPiiKind.CLIENT_NAME,
                purpose=EphemeralPiiPurpose.MASTER_BOOKING_CLIENT_WRITE,
            )
        except Exception:
            return None
        return phone, name

    async def _store_terminal_unknown(self, envelope, master_id, now):
        try:
            await pending_repo.insert_pending(
                self._session,
                row_id=uuid.uuid4(),
                channel=envelope.channel.value,
                connection_scope=envelope.connection_scope,
                external_account_id=envelope.external_account_id,
                master_id=master_id,
                inbound_message_id=envelope.external_message_id,
                command_kind=MasterCommandKind.SCHEDULE_READ,
                state=MasterCommandPendingState.FAILED,
                command_version=1,
                idempotency_key=None,
                safe_payload=MasterCommandSafePayload(),
                phone_ref_token=None,
                name_ref_token=None,
                pii_conversation_id=None,
                confirmation_expires_at=None,
                now=now,
                result_code="UNKNOWN_COMMAND",
                result_outcome=MasterCommandFlowOutcome.MANUAL_HELP.value,
            )
        except IntegrityError:
            existing = await pending_repo.get_by_inbound(
                self._session,
                channel=envelope.channel.value,
                connection_scope=envelope.connection_scope,
                external_account_id=envelope.external_account_id,
                inbound_message_id=envelope.external_message_id,
            )
            if existing is not None:
                return _result_from_row(existing)
        return MasterCommandFlowResult(
            outcome=MasterCommandFlowOutcome.MANUAL_HELP,
            result_code="UNKNOWN_COMMAND",
        )

    async def _insert_control_noop(self, envelope, master_id, now, outcome):
        return await self._insert_dedupe_mirror(
            envelope,
            master_id,
            now,
            kind=MasterCommandKind.SCHEDULE_READ,
            outcome=outcome,
            version=1,
            result_code=outcome.value,
        )

    async def _insert_schedule_terminal(
        self, envelope, master_id, payload, now, outcome, code
    ):
        state = (
            MasterCommandPendingState.SUCCEEDED
            if outcome is MasterCommandFlowOutcome.SUCCESS
            else MasterCommandPendingState.FAILED
        )
        try:
            await pending_repo.insert_pending(
                self._session,
                row_id=uuid.uuid4(),
                channel=envelope.channel.value,
                connection_scope=envelope.connection_scope,
                external_account_id=envelope.external_account_id,
                master_id=master_id,
                inbound_message_id=envelope.external_message_id,
                command_kind=MasterCommandKind.SCHEDULE_READ,
                state=state,
                command_version=1,
                idempotency_key=None,
                safe_payload=payload,
                phone_ref_token=None,
                name_ref_token=None,
                pii_conversation_id=None,
                confirmation_expires_at=None,
                now=now,
                result_code=code,
                result_outcome=outcome.value,
            )
        except IntegrityError:
            existing = await pending_repo.get_by_inbound(
                self._session,
                channel=envelope.channel.value,
                connection_scope=envelope.connection_scope,
                external_account_id=envelope.external_account_id,
                inbound_message_id=envelope.external_message_id,
            )
            if existing is not None:
                return _result_from_row(existing)
        return MasterCommandFlowResult(
            outcome=outcome,
            result_code=code,
            command_kind=MasterCommandKind.SCHEDULE_READ,
            command_version=1,
        )

    async def _insert_dedupe_mirror(
        self,
        envelope,
        master_id,
        now,
        *,
        kind: MasterCommandKind,
        outcome: MasterCommandFlowOutcome,
        version: int,
        result_code: str | None = None,
        preview: MasterCommandPreview | None = None,
        idempotency_key: str | None = None,
    ) -> MasterCommandFlowResult:
        """Terminal/noop inbound row for dedupe (not an active pending).

        Persists the real command_kind. Mutation kinds require a non-null
        idempotency_key under DB CHECKs; when absent (orphan control), store as
        SCHEDULE_READ so constraints stay satisfied without inventing keys.
        """

        state = MasterCommandPendingState.SUCCEEDED
        if outcome is MasterCommandFlowOutcome.CANCELLED:
            state = MasterCommandPendingState.CANCELLED
        elif outcome in {
            MasterCommandFlowOutcome.UNAVAILABLE,
            MasterCommandFlowOutcome.CONFLICT,
            MasterCommandFlowOutcome.REJECTED,
            MasterCommandFlowOutcome.MANUAL_HELP,
            MasterCommandFlowOutcome.CLARIFICATION_REQUIRED,
            MasterCommandFlowOutcome.CONFIRMATION_REQUIRED,
            MasterCommandFlowOutcome.DUPLICATE_IGNORED,
        }:
            state = MasterCommandPendingState.FAILED

        stored_kind = kind
        stored_key = idempotency_key
        if kind is not MasterCommandKind.SCHEDULE_READ and stored_key is None:
            stored_kind = MasterCommandKind.SCHEDULE_READ
        try:
            await pending_repo.insert_pending(
                self._session,
                row_id=uuid.uuid4(),
                channel=envelope.channel.value,
                connection_scope=envelope.connection_scope,
                external_account_id=envelope.external_account_id,
                master_id=master_id,
                inbound_message_id=envelope.external_message_id,
                command_kind=stored_kind,
                state=state,
                command_version=version,
                idempotency_key=stored_key,
                safe_payload=MasterCommandSafePayload(),
                phone_ref_token=None,
                name_ref_token=None,
                pii_conversation_id=None,
                confirmation_expires_at=None,
                now=now,
                result_code=result_code or outcome.value,
                result_outcome=outcome.value,
            )
        except IntegrityError:
            existing = await pending_repo.get_by_inbound(
                self._session,
                channel=envelope.channel.value,
                connection_scope=envelope.connection_scope,
                external_account_id=envelope.external_account_id,
                inbound_message_id=envelope.external_message_id,
            )
            if existing is not None:
                return _result_from_row(existing)
        return MasterCommandFlowResult(
            outcome=outcome,
            preview=preview,
            result_code=result_code or outcome.value,
            command_kind=kind,
            command_version=version,
        )


def _preview_for(
    kind: MasterCommandKind, payload: MasterCommandSafePayload, version: int
) -> MasterCommandPreview:
    action = {
        MasterCommandKind.CLOSE_INTERVAL: "закрыть интервал",
        MasterCommandKind.CLOSE_DAY: "выходной",
        MasterCommandKind.CREATE_BOOKING: "запись клиенту",
        MasterCommandKind.SCHEDULE_READ: "расписание",
    }[kind]
    service_hint = None
    if payload.slot_id:
        service_hint = "слот указан"
    return MasterCommandPreview(
        action=action,
        date_key=payload.date_key,
        start_time=payload.start_time,
        end_time=payload.end_time,
        service_hint=service_hint,
        command_version=version,
    )


def _missing_for_kind(
    kind: MasterCommandKind, payload: MasterCommandSafePayload
) -> list[MasterCommandClarificationNeed]:
    needs: list[MasterCommandClarificationNeed] = []
    if kind is MasterCommandKind.CLOSE_DAY:
        if not payload.date_key:
            needs.append(MasterCommandClarificationNeed.DATE)
        if not payload.block_type:
            needs.append(MasterCommandClarificationNeed.BLOCK_TYPE)
    elif kind is MasterCommandKind.CLOSE_INTERVAL:
        if not payload.date_key:
            needs.append(MasterCommandClarificationNeed.DATE)
        if not payload.start_time:
            needs.append(MasterCommandClarificationNeed.TIME)
        if not payload.end_time:
            needs.append(MasterCommandClarificationNeed.END_TIME)
        if not payload.block_type:
            needs.append(MasterCommandClarificationNeed.BLOCK_TYPE)
    elif kind is MasterCommandKind.CREATE_BOOKING:
        if not payload.slot_id:
            needs.append(MasterCommandClarificationNeed.SLOT_ID)
    return needs


def _schedule_summary_lines(days: tuple) -> tuple[str, ...]:
    lines: list[str] = []
    for day in days:
        if type(day) is not dict:
            continue
        date_key = day.get("dateKey")
        if type(date_key) is not str:
            continue
        appts = day.get("appointments") or []
        blocks = day.get("scheduleBlocks") or []
        if type(appts) is not list:
            appts = []
        if type(blocks) is not list:
            blocks = []
        lines.append(
            f"{date_key}: записей {len(appts)}, блоков {len(blocks)}"
        )
        for appt in appts[:20]:
            if type(appt) is not dict:
                continue
            starts = appt.get("startsAt")
            service = appt.get("serviceName")
            if type(starts) is str:
                label = starts
                if type(service) is str and service:
                    label = f"{starts} · {service}"
                lines.append(f"  • {label}")
    return tuple(lines)


def _result_from_row(row) -> MasterCommandFlowResult:
    outcome_raw = row.result_outcome
    try:
        outcome = (
            MasterCommandFlowOutcome(outcome_raw)
            if type(outcome_raw) is str
            else MasterCommandFlowOutcome.DUPLICATE_IGNORED
        )
    except ValueError:
        outcome = MasterCommandFlowOutcome.DUPLICATE_IGNORED
    kind = None
    try:
        kind = MasterCommandKind(row.command_kind)
    except ValueError:
        kind = None
    preview = None
    if outcome is MasterCommandFlowOutcome.CONFIRMATION_REQUIRED:
        preview = _preview_for(
            kind or MasterCommandKind.CLOSE_DAY,
            MasterCommandSafePayload.from_json_dict(row.safe_payload),
            row.command_version,
        )
    return MasterCommandFlowResult(
        outcome=outcome,
        preview=preview,
        result_code=row.result_code,
        command_kind=kind,
        command_version=row.command_version,
    )


# Silence unused import warnings for tokens used by architecture guards / docs.
_ = (CONFIRM_TEXT_TOKENS, CANCEL_TEXT_TOKENS)
