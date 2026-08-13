"""Repository for conversation-scoped amoCRM entity links + deal create fence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.clock import resolve_moment
from app.models.amocrm_entity_link import (
    AmocrmEntityKind,
    AmocrmEntityLink,
    AmocrmEntityLinkStatus,
)

DEFAULT_CREATE_LEASE_SECONDS = 45

_OPEN_STATUSES = (
    AmocrmEntityLinkStatus.ACTIVE.value,
    AmocrmEntityLinkStatus.RESERVED.value,
    AmocrmEntityLinkStatus.RECONCILE_REQUIRED.value,
)


class AmocrmEntityLinkConflictError(RuntimeError):
    """ACTIVE/open uniqueness violated — fail closed."""


class AmocrmEntityLinkStaleLeaseError(RuntimeError):
    """Create reservation fence rejected."""


@dataclass(frozen=True, repr=False)
class DealCreateReservation:
    link_id: uuid.UUID
    conversation_id: uuid.UUID
    lease_owner: str
    lease_token: uuid.UUID
    lease_version: int
    lease_until: datetime

    def __repr__(self) -> str:
        return (
            "DealCreateReservation("
            f"link_id={self.link_id!r}, "
            f"lease_version={self.lease_version!r}, "
            "conversation_id=<redacted>)"
        )


async def get_active(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    entity_kind: AmocrmEntityKind,
) -> AmocrmEntityLink | None:
    return await session.scalar(
        select(AmocrmEntityLink).where(
            AmocrmEntityLink.conversation_id == conversation_id,
            AmocrmEntityLink.entity_kind == entity_kind.value,
            AmocrmEntityLink.status == AmocrmEntityLinkStatus.ACTIVE.value,
        )
    )


async def get_open(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    entity_kind: AmocrmEntityKind,
) -> AmocrmEntityLink | None:
    return await session.scalar(
        select(AmocrmEntityLink)
        .where(
            AmocrmEntityLink.conversation_id == conversation_id,
            AmocrmEntityLink.entity_kind == entity_kind.value,
            AmocrmEntityLink.status.in_(_OPEN_STATUSES),
        )
        .with_for_update()
    )


async def insert_active_if_absent(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    entity_kind: AmocrmEntityKind,
    external_id: str,
    now: datetime | None = None,
) -> tuple[AmocrmEntityLink, bool]:
    if type(external_id) is not str or not external_id.strip():
        raise ValueError("EXTERNAL_ID_INVALID")
    external_id = external_id.strip()
    existing = await get_active(
        session,
        conversation_id=conversation_id,
        entity_kind=entity_kind,
    )
    if existing is not None:
        if existing.external_id != external_id:
            raise AmocrmEntityLinkConflictError("ENTITY_LINK_CONFLICT")
        return existing, False

    moment = await resolve_moment(session, now)
    row = AmocrmEntityLink(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        entity_kind=entity_kind.value,
        external_id=external_id,
        status=AmocrmEntityLinkStatus.ACTIVE.value,
        lease_owner=None,
        lease_token=None,
        lease_version=0,
        lease_until=None,
        create_submitted_at=None,
        created_at=moment,
        updated_at=moment,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        raise AmocrmEntityLinkConflictError("ENTITY_LINK_CONFLICT") from exc

    refreshed = await get_active(
        session,
        conversation_id=conversation_id,
        entity_kind=entity_kind,
    )
    if refreshed is None:
        raise AmocrmEntityLinkConflictError("ENTITY_LINK_LOOKUP_FAILED")
    if refreshed.external_id != external_id:
        raise AmocrmEntityLinkConflictError("ENTITY_LINK_CONFLICT")
    return refreshed, True


async def revoke_active(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    entity_kind: AmocrmEntityKind,
    now: datetime | None = None,
) -> AmocrmEntityLink | None:
    moment = await resolve_moment(session, now)
    stmt = (
        update(AmocrmEntityLink)
        .where(
            AmocrmEntityLink.conversation_id == conversation_id,
            AmocrmEntityLink.entity_kind == entity_kind.value,
            AmocrmEntityLink.status == AmocrmEntityLinkStatus.ACTIVE.value,
        )
        .values(
            status=AmocrmEntityLinkStatus.REVOKED.value,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            create_submitted_at=None,
            updated_at=moment,
        )
        .returning(AmocrmEntityLink.id)
    )
    row_id = await session.scalar(stmt)
    if row_id is None:
        return None
    return await session.get(AmocrmEntityLink, row_id)


async def rebind_active(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    entity_kind: AmocrmEntityKind,
    external_id: str,
    now: datetime | None = None,
) -> AmocrmEntityLink:
    await revoke_active(
        session,
        conversation_id=conversation_id,
        entity_kind=entity_kind,
        now=now,
    )
    row, _ = await insert_active_if_absent(
        session,
        conversation_id=conversation_id,
        entity_kind=entity_kind,
        external_id=external_id,
        now=now,
    )
    return row


async def activate_reconcile_required(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    external_id: str,
    now: datetime | None = None,
) -> AmocrmEntityLink:
    """RECONCILE_REQUIRED → ACTIVE with a confirmed external deal id.

    Caller must have already validated the deal via CRM GET. Never creates.
    Conflicting ACTIVE external id fails closed.
    """

    if type(external_id) is not str or not external_id.strip():
        raise ValueError("EXTERNAL_ID_INVALID")
    external_id = external_id.strip()
    if not external_id.isdigit():
        raise ValueError("EXTERNAL_ID_INVALID")

    moment = await resolve_moment(session, now)
    open_row = await get_open(
        session,
        conversation_id=conversation_id,
        entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
    )
    if open_row is None:
        raise AmocrmEntityLinkConflictError("ENTITY_LINK_RECONCILE_MISSING")
    if open_row.status != AmocrmEntityLinkStatus.RECONCILE_REQUIRED.value:
        raise AmocrmEntityLinkConflictError("ENTITY_LINK_NOT_RECONCILE_REQUIRED")

    conflict = await session.scalar(
        select(AmocrmEntityLink).where(
            AmocrmEntityLink.entity_kind == AmocrmEntityKind.TECHNICAL_DEAL.value,
            AmocrmEntityLink.status == AmocrmEntityLinkStatus.ACTIVE.value,
            AmocrmEntityLink.external_id == external_id,
            AmocrmEntityLink.conversation_id != conversation_id,
        )
    )
    if conflict is not None:
        raise AmocrmEntityLinkConflictError("ENTITY_LINK_EXTERNAL_ACTIVE_CONFLICT")

    stmt = (
        update(AmocrmEntityLink)
        .where(
            AmocrmEntityLink.id == open_row.id,
            AmocrmEntityLink.status == AmocrmEntityLinkStatus.RECONCILE_REQUIRED.value,
            AmocrmEntityLink.conversation_id == conversation_id,
            AmocrmEntityLink.entity_kind == AmocrmEntityKind.TECHNICAL_DEAL.value,
        )
        .values(
            status=AmocrmEntityLinkStatus.ACTIVE.value,
            external_id=external_id,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            create_submitted_at=None,
            updated_at=moment,
        )
        .returning(AmocrmEntityLink.id)
    )
    try:
        row_id = await session.scalar(stmt)
    except IntegrityError as exc:
        raise AmocrmEntityLinkConflictError("ENTITY_LINK_EXTERNAL_ACTIVE_CONFLICT") from exc
    if row_id is None:
        raise AmocrmEntityLinkConflictError("ENTITY_LINK_NOT_RECONCILE_REQUIRED")
    row = await session.get(AmocrmEntityLink, row_id)
    if row is None:
        raise AmocrmEntityLinkConflictError("ENTITY_LINK_LOOKUP_FAILED")
    return row


async def claim_deal_create_reservation(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    worker_id: str,
    lease_seconds: int = DEFAULT_CREATE_LEASE_SECONDS,
    now: datetime | None = None,
) -> DealCreateReservation:
    """Exclusive RESERVED fence BEFORE any create POST.

    Expired RESERVED without create_submitted_at may be reclaimed.
    Expired RESERVED with create_submitted_at → RECONCILE_REQUIRED (no resend).
    """

    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    moment = await resolve_moment(session, now)
    open_row = await get_open(
        session,
        conversation_id=conversation_id,
        entity_kind=AmocrmEntityKind.TECHNICAL_DEAL,
    )
    if open_row is not None:
        if open_row.status == AmocrmEntityLinkStatus.ACTIVE.value:
            raise AmocrmEntityLinkConflictError("ENTITY_LINK_ALREADY_ACTIVE")
        if open_row.status == AmocrmEntityLinkStatus.RECONCILE_REQUIRED.value:
            raise AmocrmEntityLinkStaleLeaseError("ENTITY_LINK_RECONCILE_REQUIRED")
        if open_row.status == AmocrmEntityLinkStatus.RESERVED.value:
            lease_live = (
                open_row.lease_until is not None
                and open_row.lease_until > moment
                and open_row.lease_owner is not None
            )
            if lease_live and open_row.lease_owner != worker_id:
                raise AmocrmEntityLinkStaleLeaseError("ENTITY_LINK_STALE_LEASE")
            if open_row.create_submitted_at is not None:
                await _mark_reconcile_required(session, row=open_row, now=moment)
                raise AmocrmEntityLinkStaleLeaseError("ENTITY_LINK_RECONCILE_REQUIRED")
            return await _take_reservation_lease(
                session,
                row=open_row,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                moment=moment,
            )

    moment = await resolve_moment(session, now)
    lease_token = uuid.uuid4()
    lease_until = moment + timedelta(seconds=lease_seconds)
    row = AmocrmEntityLink(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        entity_kind=AmocrmEntityKind.TECHNICAL_DEAL.value,
        external_id=None,
        status=AmocrmEntityLinkStatus.RESERVED.value,
        lease_owner=worker_id,
        lease_token=lease_token,
        lease_version=1,
        lease_until=lease_until,
        create_submitted_at=None,
        created_at=moment,
        updated_at=moment,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        raise AmocrmEntityLinkStaleLeaseError("ENTITY_LINK_STALE_LEASE") from exc
    return DealCreateReservation(
        link_id=row.id,
        conversation_id=conversation_id,
        lease_owner=worker_id,
        lease_token=lease_token,
        lease_version=1,
        lease_until=lease_until,
    )


async def mark_create_submitted(
    session: AsyncSession,
    *,
    reservation: DealCreateReservation,
    now: datetime | None = None,
) -> None:
    """Commit 'about to POST' marker under the held fence."""

    moment = await resolve_moment(session, now)
    stmt = (
        update(AmocrmEntityLink)
        .where(
            AmocrmEntityLink.id == reservation.link_id,
            AmocrmEntityLink.status == AmocrmEntityLinkStatus.RESERVED.value,
            AmocrmEntityLink.lease_token == reservation.lease_token,
            AmocrmEntityLink.lease_version == reservation.lease_version,
            AmocrmEntityLink.lease_owner == reservation.lease_owner,
            AmocrmEntityLink.lease_until.is_not(None),
            AmocrmEntityLink.lease_until > moment,
            AmocrmEntityLink.create_submitted_at.is_(None),
        )
        .values(create_submitted_at=moment, updated_at=moment)
    )
    result = await session.execute(stmt)
    if result.rowcount != 1:
        raise AmocrmEntityLinkStaleLeaseError("ENTITY_LINK_STALE_LEASE")


async def complete_reservation_to_active(
    session: AsyncSession,
    *,
    reservation: DealCreateReservation,
    external_id: str,
    now: datetime | None = None,
) -> AmocrmEntityLink:
    if type(external_id) is not str or not external_id.strip():
        raise ValueError("EXTERNAL_ID_INVALID")
    external_id = external_id.strip()
    moment = await resolve_moment(session, now)
    stmt = (
        update(AmocrmEntityLink)
        .where(
            AmocrmEntityLink.id == reservation.link_id,
            AmocrmEntityLink.status == AmocrmEntityLinkStatus.RESERVED.value,
            AmocrmEntityLink.lease_token == reservation.lease_token,
            AmocrmEntityLink.lease_version == reservation.lease_version,
            AmocrmEntityLink.lease_owner == reservation.lease_owner,
            AmocrmEntityLink.lease_until.is_not(None),
            AmocrmEntityLink.lease_until > moment,
        )
        .values(
            status=AmocrmEntityLinkStatus.ACTIVE.value,
            external_id=external_id,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            create_submitted_at=None,
            updated_at=moment,
        )
        .returning(AmocrmEntityLink.id)
    )
    row_id = await session.scalar(stmt)
    if row_id is None:
        raise AmocrmEntityLinkStaleLeaseError("ENTITY_LINK_STALE_LEASE")
    row = await session.get(AmocrmEntityLink, row_id)
    if row is None:
        raise AmocrmEntityLinkConflictError("ENTITY_LINK_LOOKUP_FAILED")
    return row


async def mark_reservation_reconcile_required(
    session: AsyncSession,
    *,
    reservation: DealCreateReservation,
    now: datetime | None = None,
) -> AmocrmEntityLink:
    moment = await resolve_moment(session, now)
    stmt = (
        update(AmocrmEntityLink)
        .where(
            AmocrmEntityLink.id == reservation.link_id,
            AmocrmEntityLink.status == AmocrmEntityLinkStatus.RESERVED.value,
            AmocrmEntityLink.lease_token == reservation.lease_token,
            AmocrmEntityLink.lease_version == reservation.lease_version,
            AmocrmEntityLink.lease_owner == reservation.lease_owner,
        )
        .values(
            status=AmocrmEntityLinkStatus.RECONCILE_REQUIRED.value,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            updated_at=moment,
        )
        .returning(AmocrmEntityLink.id)
    )
    row_id = await session.scalar(stmt)
    if row_id is None:
        raise AmocrmEntityLinkStaleLeaseError("ENTITY_LINK_STALE_LEASE")
    row = await session.get(AmocrmEntityLink, row_id)
    if row is None:
        raise AmocrmEntityLinkConflictError("ENTITY_LINK_LOOKUP_FAILED")
    return row


async def release_reservation_for_retry(
    session: AsyncSession,
    *,
    reservation: DealCreateReservation,
    now: datetime | None = None,
    allow_after_submit: bool = False,
) -> None:
    """Drop a reservation so create may retry after an explicit failed create.

    ``allow_after_submit=True`` only when the HTTP response proves the lead was
    not created (explicit 4xx). Ambiguous outcomes must use reconcile instead.
    """

    moment = await resolve_moment(session, now)
    clauses = [
        AmocrmEntityLink.id == reservation.link_id,
        AmocrmEntityLink.status == AmocrmEntityLinkStatus.RESERVED.value,
        AmocrmEntityLink.lease_token == reservation.lease_token,
        AmocrmEntityLink.lease_version == reservation.lease_version,
        AmocrmEntityLink.lease_owner == reservation.lease_owner,
    ]
    if not allow_after_submit:
        clauses.append(AmocrmEntityLink.create_submitted_at.is_(None))
    stmt = (
        update(AmocrmEntityLink)
        .where(*clauses)
        .values(
            status=AmocrmEntityLinkStatus.REVOKED.value,
            external_id="reservation-released",
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            create_submitted_at=None,
            updated_at=moment,
        )
    )
    result = await session.execute(stmt)
    if result.rowcount != 1:
        await mark_reservation_reconcile_required(
            session, reservation=reservation, now=moment
        )


async def _take_reservation_lease(
    session: AsyncSession,
    *,
    row: AmocrmEntityLink,
    worker_id: str,
    lease_seconds: int,
    moment: datetime,
) -> DealCreateReservation:
    lease_token = uuid.uuid4()
    lease_until = moment + timedelta(seconds=lease_seconds)
    stmt = (
        update(AmocrmEntityLink)
        .where(
            AmocrmEntityLink.id == row.id,
            AmocrmEntityLink.status == AmocrmEntityLinkStatus.RESERVED.value,
            AmocrmEntityLink.lease_version == row.lease_version,
            AmocrmEntityLink.create_submitted_at.is_(None),
        )
        .values(
            lease_owner=worker_id,
            lease_token=lease_token,
            lease_version=AmocrmEntityLink.lease_version + 1,
            lease_until=lease_until,
            updated_at=moment,
        )
        .returning(AmocrmEntityLink.lease_version)
    )
    new_version = await session.scalar(stmt)
    if new_version is None:
        raise AmocrmEntityLinkStaleLeaseError("ENTITY_LINK_STALE_LEASE")
    return DealCreateReservation(
        link_id=row.id,
        conversation_id=row.conversation_id,
        lease_owner=worker_id,
        lease_token=lease_token,
        lease_version=int(new_version),
        lease_until=lease_until,
    )


async def _mark_reconcile_required(
    session: AsyncSession,
    *,
    row: AmocrmEntityLink,
    now: datetime,
) -> None:
    await session.execute(
        update(AmocrmEntityLink)
        .where(
            AmocrmEntityLink.id == row.id,
            AmocrmEntityLink.status == AmocrmEntityLinkStatus.RESERVED.value,
        )
        .values(
            status=AmocrmEntityLinkStatus.RECONCILE_REQUIRED.value,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            updated_at=now,
        )
    )
