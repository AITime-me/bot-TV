"""Master channel binding service (CURSOR-27).

Bind / rebind / revoke / resolve for channel-agnostic durable master identity.
Caller owns the unit of work (session_scope). No network I/O. No live channels.

Rebind replaces ACTIVE atomically inside a savepoint (revoke + insert). On
IntegrityError the savepoint rolls back so identity never commits with 0 ACTIVE;
the service re-reads and returns ALREADY_BOUND / CONFLICT / AMBIGUOUS — never
INVALID_INPUT for races.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.master_channel_binding import (
    DEFAULT_CONNECTION_SCOPE,
    BindMasterBindingOutcome,
    BindMasterBindingResult,
    MasterChannelBindingError,
    RebindMasterBindingOutcome,
    RebindMasterBindingResult,
    ResolveMasterBindingOutcome,
    ResolveMasterBindingResult,
    RevokeMasterBindingOutcome,
    RevokeMasterBindingResult,
    normalize_connection_scope,
    normalize_external_account_id,
    require_canonical_master_id,
    require_master_binding_channel,
)
from app.repositories import master_channel_bindings as repo

logger = logging.getLogger(__name__)

_ALLOWED_LOG_CODES: frozenset[str] = frozenset(
    {
        "MASTER_BINDING_RESOLVED",
        "MASTER_BINDING_NOT_FOUND",
        "MASTER_BINDING_AMBIGUOUS",
        "MASTER_BINDING_BOUND",
        "MASTER_BINDING_ALREADY_BOUND",
        "MASTER_BINDING_CONFLICT",
        "MASTER_BINDING_REBOUND",
        "MASTER_BINDING_REVOKED",
        "MASTER_BINDING_INVALID_INPUT",
    }
)


class _BindingStateAmbiguous(Exception):
    """Internal: unexpected ownership/state under lock; abort savepoint."""


def _log(event: str) -> None:
    if type(event) is not str or event not in _ALLOWED_LOG_CODES:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return


def _db_uuid(value: object) -> uuid.UUID:
    if type(value) is uuid.UUID:
        return value
    return uuid.UUID(str(value))


class MasterChannelBindingService:
    """Application API for durable master↔channel-account bindings."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(
        self,
        *,
        channel: object,
        external_account_id: object,
        connection_scope: object = DEFAULT_CONNECTION_SCOPE,
    ) -> ResolveMasterBindingResult:
        """Resolve only an ACTIVE unambiguous binding. Revoked → NOT_FOUND."""

        try:
            ch = require_master_binding_channel(channel).value
            scope = normalize_connection_scope(connection_scope)
            ext = normalize_external_account_id(external_account_id)
        except MasterChannelBindingError:
            _log("MASTER_BINDING_INVALID_INPUT")
            return ResolveMasterBindingResult(
                outcome=ResolveMasterBindingOutcome.INVALID_INPUT
            )

        rows = await repo.list_active_by_identity(
            self._session,
            channel=ch,
            connection_scope=scope,
            external_account_id=ext,
        )
        if not rows:
            _log("MASTER_BINDING_NOT_FOUND")
            return ResolveMasterBindingResult(
                outcome=ResolveMasterBindingOutcome.NOT_FOUND
            )
        if len(rows) > 1:
            _log("MASTER_BINDING_AMBIGUOUS")
            return ResolveMasterBindingResult(
                outcome=ResolveMasterBindingOutcome.AMBIGUOUS
            )
        record = repo.as_record(rows[0])
        _log("MASTER_BINDING_RESOLVED")
        return ResolveMasterBindingResult(
            outcome=ResolveMasterBindingOutcome.RESOLVED,
            master_id=record.master_id,
            binding=record,
        )

    async def bind(
        self,
        *,
        channel: object,
        external_account_id: object,
        master_id: object,
        connection_scope: object = DEFAULT_CONNECTION_SCOPE,
    ) -> BindMasterBindingResult:
        """Create ACTIVE binding. Conflict if identity already bound to another master."""

        try:
            ch = require_master_binding_channel(channel).value
            scope = normalize_connection_scope(connection_scope)
            ext = normalize_external_account_id(external_account_id)
            mid = require_canonical_master_id(master_id)
        except MasterChannelBindingError:
            _log("MASTER_BINDING_INVALID_INPUT")
            return BindMasterBindingResult(
                outcome=BindMasterBindingOutcome.INVALID_INPUT
            )

        existing = await repo.lock_active_by_identity(
            self._session,
            channel=ch,
            connection_scope=scope,
            external_account_id=ext,
        )
        if existing is not None:
            count = await repo.count_active_by_identity(
                self._session,
                channel=ch,
                connection_scope=scope,
                external_account_id=ext,
            )
            if count > 1:
                _log("MASTER_BINDING_CONFLICT")
                return BindMasterBindingResult(
                    outcome=BindMasterBindingOutcome.CONFLICT
                )
            if existing.master_id == mid:
                _log("MASTER_BINDING_ALREADY_BOUND")
                return BindMasterBindingResult(
                    outcome=BindMasterBindingOutcome.ALREADY_BOUND,
                    binding=repo.as_record(existing),
                )
            _log("MASTER_BINDING_CONFLICT")
            return BindMasterBindingResult(
                outcome=BindMasterBindingOutcome.CONFLICT
            )

        try:
            async with self._session.begin_nested():
                row = await repo.insert_active_binding(
                    self._session,
                    row_id=uuid.uuid4(),
                    channel=ch,
                    connection_scope=scope,
                    external_account_id=ext,
                    master_id=mid,
                )
        except IntegrityError:
            self._session.expire_all()
            raced = await repo.lock_active_by_identity(
                self._session,
                channel=ch,
                connection_scope=scope,
                external_account_id=ext,
            )
            if raced is not None and raced.master_id == mid:
                _log("MASTER_BINDING_ALREADY_BOUND")
                return BindMasterBindingResult(
                    outcome=BindMasterBindingOutcome.ALREADY_BOUND,
                    binding=repo.as_record(raced),
                )
            _log("MASTER_BINDING_CONFLICT")
            return BindMasterBindingResult(
                outcome=BindMasterBindingOutcome.CONFLICT
            )

        _log("MASTER_BINDING_BOUND")
        return BindMasterBindingResult(
            outcome=BindMasterBindingOutcome.BOUND,
            binding=repo.as_record(row),
        )

    async def rebind(
        self,
        *,
        channel: object,
        external_account_id: object,
        master_id: object,
        connection_scope: object = DEFAULT_CONNECTION_SCOPE,
    ) -> RebindMasterBindingResult:
        """Atomically replace ACTIVE binding for identity (or create if absent).

        Revoke + insert run in one savepoint. IntegrityError rolls back the
        savepoint (never leaves 0 ACTIVE on commit), then re-reads for typed
        ALREADY_BOUND / CONFLICT / AMBIGUOUS. Races never map to INVALID_INPUT.
        """

        try:
            ch = require_master_binding_channel(channel).value
            scope = normalize_connection_scope(connection_scope)
            ext = normalize_external_account_id(external_account_id)
            mid = require_canonical_master_id(master_id)
        except MasterChannelBindingError:
            _log("MASTER_BINDING_INVALID_INPUT")
            return RebindMasterBindingResult(
                outcome=RebindMasterBindingOutcome.INVALID_INPUT
            )

        existing = await repo.lock_active_by_identity(
            self._session,
            channel=ch,
            connection_scope=scope,
            external_account_id=ext,
        )
        if existing is not None:
            count = await repo.count_active_by_identity(
                self._session,
                channel=ch,
                connection_scope=scope,
                external_account_id=ext,
            )
            if count > 1:
                _log("MASTER_BINDING_AMBIGUOUS")
                return RebindMasterBindingResult(
                    outcome=RebindMasterBindingOutcome.AMBIGUOUS
                )
            if existing.master_id == mid:
                _log("MASTER_BINDING_ALREADY_BOUND")
                return RebindMasterBindingResult(
                    outcome=RebindMasterBindingOutcome.ALREADY_BOUND,
                    binding=repo.as_record(existing),
                )

            revoked_id: uuid.UUID | None = None
            try:
                async with self._session.begin_nested():
                    revoked = await repo.mark_revoked(
                        self._session, binding_id=_db_uuid(existing.id)
                    )
                    if revoked is None:
                        # Lost ACTIVE under lock — fail closed; savepoint aborts.
                        raise _BindingStateAmbiguous()
                    revoked_id = _db_uuid(revoked.id)
                    row = await repo.insert_active_binding(
                        self._session,
                        row_id=uuid.uuid4(),
                        channel=ch,
                        connection_scope=scope,
                        external_account_id=ext,
                        master_id=mid,
                    )
            except _BindingStateAmbiguous:
                _log("MASTER_BINDING_AMBIGUOUS")
                return RebindMasterBindingResult(
                    outcome=RebindMasterBindingOutcome.AMBIGUOUS
                )
            except IntegrityError:
                return await self._classify_rebind_integrity_race(
                    channel=ch,
                    connection_scope=scope,
                    external_account_id=ext,
                    master_id=mid,
                )

            _log("MASTER_BINDING_REBOUND")
            return RebindMasterBindingResult(
                outcome=RebindMasterBindingOutcome.REBOUND,
                binding=repo.as_record(row),
                revoked_binding_id=revoked_id,
            )

        try:
            async with self._session.begin_nested():
                row = await repo.insert_active_binding(
                    self._session,
                    row_id=uuid.uuid4(),
                    channel=ch,
                    connection_scope=scope,
                    external_account_id=ext,
                    master_id=mid,
                )
        except IntegrityError:
            return await self._classify_rebind_integrity_race(
                channel=ch,
                connection_scope=scope,
                external_account_id=ext,
                master_id=mid,
            )

        _log("MASTER_BINDING_BOUND")
        return RebindMasterBindingResult(
            outcome=RebindMasterBindingOutcome.BOUND,
            binding=repo.as_record(row),
        )

    async def _classify_rebind_integrity_race(
        self,
        *,
        channel: str,
        connection_scope: str,
        external_account_id: str,
        master_id: str,
    ) -> RebindMasterBindingResult:
        """After savepoint rollback: re-read ACTIVE and classify without INVALID_INPUT."""

        self._session.expire_all()
        raced = await repo.lock_active_by_identity(
            self._session,
            channel=channel,
            connection_scope=connection_scope,
            external_account_id=external_account_id,
        )
        if raced is None:
            _log("MASTER_BINDING_AMBIGUOUS")
            return RebindMasterBindingResult(
                outcome=RebindMasterBindingOutcome.AMBIGUOUS
            )
        count = await repo.count_active_by_identity(
            self._session,
            channel=channel,
            connection_scope=connection_scope,
            external_account_id=external_account_id,
        )
        if count > 1:
            _log("MASTER_BINDING_AMBIGUOUS")
            return RebindMasterBindingResult(
                outcome=RebindMasterBindingOutcome.AMBIGUOUS
            )
        if raced.master_id == master_id:
            _log("MASTER_BINDING_ALREADY_BOUND")
            return RebindMasterBindingResult(
                outcome=RebindMasterBindingOutcome.ALREADY_BOUND,
                binding=repo.as_record(raced),
            )
        _log("MASTER_BINDING_CONFLICT")
        return RebindMasterBindingResult(
            outcome=RebindMasterBindingOutcome.CONFLICT
        )

    async def revoke(
        self,
        *,
        channel: object,
        external_account_id: object,
        connection_scope: object = DEFAULT_CONNECTION_SCOPE,
    ) -> RevokeMasterBindingResult:
        """Revoke the ACTIVE binding for identity if present."""

        try:
            ch = require_master_binding_channel(channel).value
            scope = normalize_connection_scope(connection_scope)
            ext = normalize_external_account_id(external_account_id)
        except MasterChannelBindingError:
            _log("MASTER_BINDING_INVALID_INPUT")
            return RevokeMasterBindingResult(
                outcome=RevokeMasterBindingOutcome.INVALID_INPUT
            )

        existing = await repo.lock_active_by_identity(
            self._session,
            channel=ch,
            connection_scope=scope,
            external_account_id=ext,
        )
        if existing is None:
            _log("MASTER_BINDING_NOT_FOUND")
            return RevokeMasterBindingResult(
                outcome=RevokeMasterBindingOutcome.NOT_FOUND
            )
        count = await repo.count_active_by_identity(
            self._session,
            channel=ch,
            connection_scope=scope,
            external_account_id=ext,
        )
        if count > 1:
            _log("MASTER_BINDING_AMBIGUOUS")
            return RevokeMasterBindingResult(
                outcome=RevokeMasterBindingOutcome.AMBIGUOUS
            )

        revoked = await repo.mark_revoked(
            self._session, binding_id=_db_uuid(existing.id)
        )
        if revoked is None:
            # Concurrent revoke/rebind won the row; not an input error.
            _log("MASTER_BINDING_NOT_FOUND")
            return RevokeMasterBindingResult(
                outcome=RevokeMasterBindingOutcome.NOT_FOUND
            )
        _log("MASTER_BINDING_REVOKED")
        return RevokeMasterBindingResult(
            outcome=RevokeMasterBindingOutcome.REVOKED,
            binding=repo.as_record(revoked),
        )
