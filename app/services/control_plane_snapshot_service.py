"""Control-plane snapshot refresh + readiness (BOT-CONTROL-PLANE-04).

desiredAdminState from published settings is owner intent only. It must never
mutate bot-TV ``BOT_MODE`` / ``EMERGENCY_LOCK`` effective runtime ownership.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.control_plane_http import (
    ControlPlaneFetchCode,
    ControlPlaneHttpClient,
    ControlPlaneKnowledgeFetchResult,
    ControlPlaneSettingsFetchResult,
)
from app.core.control_plane_types import (
    ControlPlaneKindReadiness,
    ControlPlaneKindState,
    ControlPlaneOverallReadiness,
    ControlPlaneParseError,
    ControlPlaneSnapshotKind,
    ControlPlaneSnapshotState,
    KnowledgePublicationV1,
    PublicationIdentity,
    SettingsPublicationV1,
    knowledge_publication_to_payload_dict,
    parse_knowledge_publication_v1,
    parse_settings_publication_v1,
    settings_publication_to_payload_dict,
)
from app.db.clock import db_statement_now
from app.db.session import session_scope
from app.models.control_plane_snapshot import ControlPlaneSnapshot
from app.repositories import control_plane_snapshots as snapshot_repo

logger = logging.getLogger(__name__)

_ALLOWED_LOG_CODES: frozenset[str] = frozenset(
    {
        "CONTROL_PLANE_REFRESH_SKIPPED_LOCK",
        "CONTROL_PLANE_REFRESH_OK",
        "CONTROL_PLANE_REFRESH_PARTIAL",
        "CONTROL_PLANE_NOT_CONFIGURED",
        "CONTROL_PLANE_CACHE_CORRUPT",
        "CONTROL_PLANE_MARK_UNUSABLE",
        "CONTROL_PLANE_STALE_GRACE",
        "CONTROL_PLANE_STALE_EXPIRED",
    }
)


class _ControlPlaneRemote(Protocol):
    def fetch_settings(self) -> ControlPlaneSettingsFetchResult: ...

    def fetch_knowledge(self) -> ControlPlaneKnowledgeFetchResult: ...


def _log(event: str) -> None:
    if type(event) is not str or event not in _ALLOWED_LOG_CODES:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(now: datetime, then: datetime | None) -> int | None:
    if then is None:
        return None
    delta = int((now - then).total_seconds())
    return max(0, delta)


def _identity_from_settings(
    publication: SettingsPublicationV1,
) -> PublicationIdentity:
    return PublicationIdentity(
        kind=ControlPlaneSnapshotKind.SETTINGS,
        publication_id=publication.publication_id,
        version=publication.version,
        checksum=publication.checksum,
        schema_version=publication.schema_version,
        published_at=publication.published_at,
    )


def _identity_from_knowledge(
    publication: KnowledgePublicationV1,
) -> PublicationIdentity:
    return PublicationIdentity(
        kind=ControlPlaneSnapshotKind.KNOWLEDGE,
        publication_id=publication.knowledge_publication_id,
        version=publication.version,
        checksum=publication.checksum,
        schema_version=publication.schema_version,
        published_at=publication.published_at,
    )


def _kind_state(
    *,
    kind: ControlPlaneSnapshotKind,
    readiness: ControlPlaneKindReadiness,
    usable: bool,
    identity: PublicationIdentity | None = None,
    error_code: str | None = None,
    verified_at: datetime | None = None,
    fetched_at: datetime | None = None,
    stale_age_seconds: int | None = None,
    stale_reason: str | None = None,
) -> ControlPlaneKindState:
    return ControlPlaneKindState(
        kind=kind,
        readiness=readiness,
        usable=usable,
        identity=identity,
        error_code=error_code,
        verified_at=verified_at,
        fetched_at=fetched_at,
        stale_age_seconds=stale_age_seconds,
        stale_reason=stale_reason,
    )


def _overall(
    settings: ControlPlaneKindState,
    knowledge: ControlPlaneKindState,
    *,
    last_successful_refresh_at: datetime | None,
) -> ControlPlaneSnapshotState:
    both_usable = settings.usable and knowledge.usable
    overall = (
        ControlPlaneOverallReadiness.READY
        if both_usable
        else ControlPlaneOverallReadiness.NOT_READY
    )
    error_code = None
    if not both_usable:
        error_code = (
            settings.error_code
            or knowledge.error_code
            or "NOT_READY"
        )
    return ControlPlaneSnapshotState(
        settings=settings,
        knowledge=knowledge,
        overall=overall,
        last_successful_refresh_at=last_successful_refresh_at,
        error_code=error_code,
    )


def load_settings_from_row(
    row: ControlPlaneSnapshot,
) -> SettingsPublicationV1 | None:
    if not row.usable:
        return None
    try:
        return parse_settings_publication_v1(row.payload)
    except ControlPlaneParseError:
        return None


def load_knowledge_from_row(
    row: ControlPlaneSnapshot,
) -> KnowledgePublicationV1 | None:
    if not row.usable:
        return None
    try:
        return parse_knowledge_publication_v1(row.payload)
    except ControlPlaneParseError:
        return None


@dataclass
class ControlPlaneSnapshotService:
    """Refresh + readiness for settings/knowledge publications.

    Does not interpret ``desiredAdminState`` as effective bot-TV runtime mode.
    """

    session_factory: async_sessionmaker[AsyncSession]
    remote: _ControlPlaneRemote | None
    max_stale_seconds: int
    _last_successful_refresh_at: datetime | None = None
    _last_state: ControlPlaneSnapshotState | None = None

    def __post_init__(self) -> None:
        if type(self.max_stale_seconds) is not int or isinstance(
            self.max_stale_seconds, bool
        ):
            raise ValueError("CONTROL_PLANE_MAX_STALE_SECONDS invalid")
        if not 30 <= self.max_stale_seconds <= 3600:
            raise ValueError("CONTROL_PLANE_MAX_STALE_SECONDS out of bounds")

    def get_state(self) -> ControlPlaneSnapshotState:
        if self._last_state is not None:
            return self._last_state
        return self._empty_state(error_code="NOT_READY")

    async def load_state_from_cache(self) -> ControlPlaneSnapshotState:
        """Rebuild readiness from durable cache without remote I/O."""

        now = _utc_now()
        async with session_scope(self.session_factory) as session:
            settings_row = await snapshot_repo.get_by_kind(
                session, kind=ControlPlaneSnapshotKind.SETTINGS
            )
            knowledge_row = await snapshot_repo.get_by_kind(
                session, kind=ControlPlaneSnapshotKind.KNOWLEDGE
            )
            state = self._state_from_cache_rows(
                settings_row=settings_row,
                knowledge_row=knowledge_row,
                now=now,
                transport_failed=False,
            )
            self._last_state = state
            if state.overall is ControlPlaneOverallReadiness.READY:
                verified_candidates = [
                    ts
                    for ts in (
                        state.settings.verified_at,
                        state.knowledge.verified_at,
                    )
                    if ts is not None
                ]
                if verified_candidates:
                    self._last_successful_refresh_at = max(verified_candidates)
            return state

    async def refresh(self) -> ControlPlaneSnapshotState:
        if self.remote is None:
            _log("CONTROL_PLANE_NOT_CONFIGURED")
            state = self._empty_state(error_code="NOT_CONFIGURED")
            self._last_state = state
            return state

        async with session_scope(self.session_factory) as session:
            locked = await snapshot_repo.try_acquire_refresh_lock(session)
            if not locked:
                _log("CONTROL_PLANE_REFRESH_SKIPPED_LOCK")
                # Always rebuild from durable cache — never trust in-memory
                # _last_state after a peer may have marked rows unusable.
                return await self._state_unlocked_from_cache(session)

            now = await db_statement_now(session)
            settings_result = self.remote.fetch_settings()
            knowledge_result = self.remote.fetch_knowledge()

            settings_state = await self._apply_settings_result(
                session,
                result=settings_result,
                now=now,
            )
            knowledge_state = await self._apply_knowledge_result(
                session,
                result=knowledge_result,
                now=now,
            )

            if (
                settings_state.usable
                and knowledge_state.usable
                and settings_result.code is ControlPlaneFetchCode.OK
                and knowledge_result.code is ControlPlaneFetchCode.OK
            ):
                self._last_successful_refresh_at = now
                _log("CONTROL_PLANE_REFRESH_OK")
            else:
                _log("CONTROL_PLANE_REFRESH_PARTIAL")

            state = _overall(
                settings_state,
                knowledge_state,
                last_successful_refresh_at=self._last_successful_refresh_at,
            )
            self._last_state = state
            return state

    async def _state_unlocked_from_cache(
        self, session: AsyncSession
    ) -> ControlPlaneSnapshotState:
        now = await db_statement_now(session)
        settings_row = await snapshot_repo.get_by_kind(
            session, kind=ControlPlaneSnapshotKind.SETTINGS
        )
        knowledge_row = await snapshot_repo.get_by_kind(
            session, kind=ControlPlaneSnapshotKind.KNOWLEDGE
        )
        state = self._state_from_cache_rows(
            settings_row=settings_row,
            knowledge_row=knowledge_row,
            now=now,
            transport_failed=False,
        )
        self._last_state = state
        return state

    async def _apply_settings_result(
        self,
        session: AsyncSession,
        *,
        result: ControlPlaneSettingsFetchResult,
        now: datetime,
    ) -> ControlPlaneKindState:
        kind = ControlPlaneSnapshotKind.SETTINGS
        if result.code is ControlPlaneFetchCode.OK and result.publication is not None:
            publication = result.publication
            await snapshot_repo.upsert_verified(
                session,
                kind=kind,
                schema_version=publication.schema_version,
                publication_id=publication.publication_id,
                version=publication.version,
                checksum=publication.checksum,
                payload=settings_publication_to_payload_dict(publication),
                published_at=publication.published_at,
                verified_at=now,
                fetched_at=now,
            )
            return _kind_state(
                kind=kind,
                readiness=ControlPlaneKindReadiness.READY_FRESH,
                usable=True,
                identity=_identity_from_settings(publication),
                verified_at=now,
                fetched_at=now,
            )

        if result.code is ControlPlaneFetchCode.NOT_PUBLISHED:
            await snapshot_repo.mark_unusable(
                session, kind=kind, error_code="NOT_PUBLISHED", fetched_at=now
            )
            _log("CONTROL_PLANE_MARK_UNUSABLE")
            return _kind_state(
                kind=kind,
                readiness=ControlPlaneKindReadiness.NOT_READY,
                usable=False,
                error_code="NOT_PUBLISHED",
                fetched_at=now,
            )

        if result.code is ControlPlaneFetchCode.INVALID:
            await snapshot_repo.mark_unusable(
                session, kind=kind, error_code="INVALID", fetched_at=now
            )
            _log("CONTROL_PLANE_MARK_UNUSABLE")
            return _kind_state(
                kind=kind,
                readiness=ControlPlaneKindReadiness.INVALID,
                usable=False,
                error_code="INVALID",
                fetched_at=now,
            )

        if result.code is ControlPlaneFetchCode.AUTH_ERROR:
            await snapshot_repo.mark_unusable(
                session, kind=kind, error_code="AUTH_ERROR", fetched_at=now
            )
            _log("CONTROL_PLANE_MARK_UNUSABLE")
            return _kind_state(
                kind=kind,
                readiness=ControlPlaneKindReadiness.AUTH_ERROR,
                usable=False,
                error_code="AUTH_ERROR",
                fetched_at=now,
            )

        if result.code is ControlPlaneFetchCode.RESPONSE_INVALID:
            await snapshot_repo.mark_unusable(
                session,
                kind=kind,
                error_code="RESPONSE_INVALID",
                fetched_at=now,
            )
            _log("CONTROL_PLANE_MARK_UNUSABLE")
            return _kind_state(
                kind=kind,
                readiness=ControlPlaneKindReadiness.INVALID,
                usable=False,
                error_code="RESPONSE_INVALID",
                fetched_at=now,
            )

        # UNAVAILABLE → controlled stale grace only.
        return await self._stale_or_not_ready(
            session, kind=kind, now=now, error_code=result.code.value
        )

    async def _apply_knowledge_result(
        self,
        session: AsyncSession,
        *,
        result: ControlPlaneKnowledgeFetchResult,
        now: datetime,
    ) -> ControlPlaneKindState:
        kind = ControlPlaneSnapshotKind.KNOWLEDGE
        if result.code is ControlPlaneFetchCode.OK and result.publication is not None:
            publication = result.publication
            await snapshot_repo.upsert_verified(
                session,
                kind=kind,
                schema_version=publication.schema_version,
                publication_id=publication.knowledge_publication_id,
                version=publication.version,
                checksum=publication.checksum,
                payload=knowledge_publication_to_payload_dict(publication),
                published_at=publication.published_at,
                verified_at=now,
                fetched_at=now,
            )
            return _kind_state(
                kind=kind,
                readiness=ControlPlaneKindReadiness.READY_FRESH,
                usable=True,
                identity=_identity_from_knowledge(publication),
                verified_at=now,
                fetched_at=now,
            )

        if result.code is ControlPlaneFetchCode.NOT_PUBLISHED:
            await snapshot_repo.mark_unusable(
                session, kind=kind, error_code="NOT_PUBLISHED", fetched_at=now
            )
            _log("CONTROL_PLANE_MARK_UNUSABLE")
            return _kind_state(
                kind=kind,
                readiness=ControlPlaneKindReadiness.NOT_READY,
                usable=False,
                error_code="NOT_PUBLISHED",
                fetched_at=now,
            )

        if result.code is ControlPlaneFetchCode.INVALID:
            await snapshot_repo.mark_unusable(
                session, kind=kind, error_code="INVALID", fetched_at=now
            )
            _log("CONTROL_PLANE_MARK_UNUSABLE")
            return _kind_state(
                kind=kind,
                readiness=ControlPlaneKindReadiness.INVALID,
                usable=False,
                error_code="INVALID",
                fetched_at=now,
            )

        if result.code is ControlPlaneFetchCode.AUTH_ERROR:
            await snapshot_repo.mark_unusable(
                session, kind=kind, error_code="AUTH_ERROR", fetched_at=now
            )
            _log("CONTROL_PLANE_MARK_UNUSABLE")
            return _kind_state(
                kind=kind,
                readiness=ControlPlaneKindReadiness.AUTH_ERROR,
                usable=False,
                error_code="AUTH_ERROR",
                fetched_at=now,
            )

        if result.code is ControlPlaneFetchCode.RESPONSE_INVALID:
            await snapshot_repo.mark_unusable(
                session,
                kind=kind,
                error_code="RESPONSE_INVALID",
                fetched_at=now,
            )
            _log("CONTROL_PLANE_MARK_UNUSABLE")
            return _kind_state(
                kind=kind,
                readiness=ControlPlaneKindReadiness.INVALID,
                usable=False,
                error_code="RESPONSE_INVALID",
                fetched_at=now,
            )

        return await self._stale_or_not_ready(
            session, kind=kind, now=now, error_code=result.code.value
        )

    async def _stale_or_not_ready(
        self,
        session: AsyncSession,
        *,
        kind: ControlPlaneSnapshotKind,
        now: datetime,
        error_code: str,
    ) -> ControlPlaneKindState:
        row = await snapshot_repo.get_by_kind(session, kind=kind)
        if row is None or not row.usable:
            return _kind_state(
                kind=kind,
                readiness=ControlPlaneKindReadiness.NOT_READY,
                usable=False,
                error_code=error_code,
                fetched_at=now,
            )

        publication: SettingsPublicationV1 | KnowledgePublicationV1 | None
        identity: PublicationIdentity | None
        if kind is ControlPlaneSnapshotKind.SETTINGS:
            publication = load_settings_from_row(row)
            if publication is None:
                _log("CONTROL_PLANE_CACHE_CORRUPT")
                await snapshot_repo.mark_unusable(
                    session,
                    kind=kind,
                    error_code="CACHE_CORRUPT",
                    fetched_at=now,
                )
                return _kind_state(
                    kind=kind,
                    readiness=ControlPlaneKindReadiness.NOT_READY,
                    usable=False,
                    error_code="CACHE_CORRUPT",
                    fetched_at=now,
                )
            identity = _identity_from_settings(publication)
        else:
            publication = load_knowledge_from_row(row)
            if publication is None:
                _log("CONTROL_PLANE_CACHE_CORRUPT")
                await snapshot_repo.mark_unusable(
                    session,
                    kind=kind,
                    error_code="CACHE_CORRUPT",
                    fetched_at=now,
                )
                return _kind_state(
                    kind=kind,
                    readiness=ControlPlaneKindReadiness.NOT_READY,
                    usable=False,
                    error_code="CACHE_CORRUPT",
                    fetched_at=now,
                )
            identity = _identity_from_knowledge(publication)

        age = _age_seconds(now, row.verified_at)
        if age is None or age > self.max_stale_seconds:
            _log("CONTROL_PLANE_STALE_EXPIRED")
            return _kind_state(
                kind=kind,
                readiness=ControlPlaneKindReadiness.NOT_READY,
                usable=False,
                identity=identity,
                error_code="STALE_EXPIRED",
                verified_at=row.verified_at,
                fetched_at=now,
                stale_age_seconds=age,
                stale_reason="STALE_EXPIRED",
            )

        await snapshot_repo.touch_fetched_at(
            session, kind=kind, fetched_at=now
        )
        _log("CONTROL_PLANE_STALE_GRACE")
        return _kind_state(
            kind=kind,
            readiness=ControlPlaneKindReadiness.READY_STALE,
            usable=True,
            identity=identity,
            error_code=error_code,
            verified_at=row.verified_at,
            fetched_at=now,
            stale_age_seconds=age,
            stale_reason="TRANSPORT_UNAVAILABLE",
        )

    def _state_from_cache_rows(
        self,
        *,
        settings_row: ControlPlaneSnapshot | None,
        knowledge_row: ControlPlaneSnapshot | None,
        now: datetime,
        transport_failed: bool,
    ) -> ControlPlaneSnapshotState:
        settings = self._kind_from_cache_row(
            kind=ControlPlaneSnapshotKind.SETTINGS,
            row=settings_row,
            now=now,
            allow_stale=transport_failed,
        )
        knowledge = self._kind_from_cache_row(
            kind=ControlPlaneSnapshotKind.KNOWLEDGE,
            row=knowledge_row,
            now=now,
            allow_stale=transport_failed,
        )
        return _overall(
            settings,
            knowledge,
            last_successful_refresh_at=self._last_successful_refresh_at,
        )

    def _kind_from_cache_row(
        self,
        *,
        kind: ControlPlaneSnapshotKind,
        row: ControlPlaneSnapshot | None,
        now: datetime,
        allow_stale: bool,
    ) -> ControlPlaneKindState:
        if row is None:
            return _kind_state(
                kind=kind,
                readiness=ControlPlaneKindReadiness.NOT_READY,
                usable=False,
                error_code="NOT_READY",
            )
        if not row.usable:
            readiness = ControlPlaneKindReadiness.NOT_READY
            if row.last_error_code == "INVALID":
                readiness = ControlPlaneKindReadiness.INVALID
            elif row.last_error_code == "AUTH_ERROR":
                readiness = ControlPlaneKindReadiness.AUTH_ERROR
            return _kind_state(
                kind=kind,
                readiness=readiness,
                usable=False,
                error_code=row.last_error_code or "NOT_READY",
                verified_at=row.verified_at,
                fetched_at=row.fetched_at,
            )

        if kind is ControlPlaneSnapshotKind.SETTINGS:
            publication = load_settings_from_row(row)
            if publication is None:
                return _kind_state(
                    kind=kind,
                    readiness=ControlPlaneKindReadiness.NOT_READY,
                    usable=False,
                    error_code="CACHE_CORRUPT",
                )
            identity = _identity_from_settings(publication)
        else:
            publication = load_knowledge_from_row(row)
            if publication is None:
                return _kind_state(
                    kind=kind,
                    readiness=ControlPlaneKindReadiness.NOT_READY,
                    usable=False,
                    error_code="CACHE_CORRUPT",
                )
            identity = _identity_from_knowledge(publication)

        age = _age_seconds(now, row.verified_at)
        if age is not None and age > self.max_stale_seconds:
            return _kind_state(
                kind=kind,
                readiness=ControlPlaneKindReadiness.NOT_READY,
                usable=False,
                identity=identity,
                error_code="STALE_EXPIRED",
                verified_at=row.verified_at,
                fetched_at=row.fetched_at,
                stale_age_seconds=age,
                stale_reason="STALE_EXPIRED",
            )

        readiness = (
            ControlPlaneKindReadiness.READY_STALE
            if allow_stale
            else ControlPlaneKindReadiness.READY_FRESH
        )
        # Cache load without a fresh remote OK is at best READY_STALE when
        # still within grace; treat restart load as READY_STALE when age > 0.
        if age is not None and age > 0:
            readiness = ControlPlaneKindReadiness.READY_STALE
        return _kind_state(
            kind=kind,
            readiness=readiness,
            usable=True,
            identity=identity,
            verified_at=row.verified_at,
            fetched_at=row.fetched_at,
            stale_age_seconds=age,
            stale_reason="CACHE_LOAD" if age and age > 0 else None,
        )

    def _empty_state(self, *, error_code: str) -> ControlPlaneSnapshotState:
        settings = _kind_state(
            kind=ControlPlaneSnapshotKind.SETTINGS,
            readiness=ControlPlaneKindReadiness.NOT_READY,
            usable=False,
            error_code=error_code,
        )
        knowledge = _kind_state(
            kind=ControlPlaneSnapshotKind.KNOWLEDGE,
            readiness=ControlPlaneKindReadiness.NOT_READY,
            usable=False,
            error_code=error_code,
        )
        return _overall(
            settings,
            knowledge,
            last_successful_refresh_at=self._last_successful_refresh_at,
        )


def build_control_plane_http_client(
    remote: ControlPlaneHttpClient | None,
) -> ControlPlaneHttpClient | None:
    return remote
