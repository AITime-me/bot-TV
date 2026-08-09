"""Repository for canonical identity graph. No commit; caller owns UoW."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity_resolution import (
    CanonicalIdentityRecord,
    CanonicalIdentityStatus,
    IdentityEntityKind,
    IdentityLinkConfidence,
    IdentityLinkRecord,
    IdentityLinkStatus,
)
from app.models.canonical_identity import CanonicalIdentity, ExternalIdentityLink


def _db_uuid(value: object) -> uuid.UUID:
    if type(value) is uuid.UUID:
        return value
    return uuid.UUID(str(value))


def _to_link_record(row: ExternalIdentityLink) -> IdentityLinkRecord:
    return IdentityLinkRecord(
        link_id=_db_uuid(row.id),
        canonical_identity_id=_db_uuid(row.canonical_identity_id),
        provider=row.provider,
        connection_scope=row.connection_scope,
        entity_kind=IdentityEntityKind(row.entity_kind),
        external_id=row.external_id,
        status=IdentityLinkStatus(row.status),
        confidence=IdentityLinkConfidence(row.confidence),
        source=row.source,
        linked_at=row.linked_at,
        revoked_at=row.revoked_at,
    )


def _to_identity_record(row: CanonicalIdentity) -> CanonicalIdentityRecord:
    return CanonicalIdentityRecord(
        identity_id=_db_uuid(row.id),
        status=CanonicalIdentityStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def get_canonical(
    session: AsyncSession,
    *,
    identity_id: uuid.UUID,
) -> CanonicalIdentity | None:
    stmt = select(CanonicalIdentity).where(CanonicalIdentity.id == identity_id)
    return await session.scalar(stmt)


async def lock_canonical(
    session: AsyncSession,
    *,
    identity_id: uuid.UUID,
) -> CanonicalIdentity | None:
    stmt = (
        select(CanonicalIdentity)
        .where(CanonicalIdentity.id == identity_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def insert_canonical(
    session: AsyncSession,
    *,
    identity_id: uuid.UUID,
) -> CanonicalIdentity:
    now = func.statement_timestamp()
    row = CanonicalIdentity(
        id=identity_id,
        status=CanonicalIdentityStatus.ACTIVE.value,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_active_by_key(
    session: AsyncSession,
    *,
    provider: str,
    connection_scope: str,
    entity_kind: str,
    external_id: str,
) -> list[ExternalIdentityLink]:
    stmt = select(ExternalIdentityLink).where(
        ExternalIdentityLink.provider == provider,
        ExternalIdentityLink.connection_scope == connection_scope,
        ExternalIdentityLink.entity_kind == entity_kind,
        ExternalIdentityLink.external_id == external_id,
        ExternalIdentityLink.status == IdentityLinkStatus.ACTIVE.value,
    )
    result = await session.scalars(stmt)
    return list(result.all())


async def lock_active_by_key(
    session: AsyncSession,
    *,
    provider: str,
    connection_scope: str,
    entity_kind: str,
    external_id: str,
) -> ExternalIdentityLink | None:
    stmt = (
        select(ExternalIdentityLink)
        .where(
            ExternalIdentityLink.provider == provider,
            ExternalIdentityLink.connection_scope == connection_scope,
            ExternalIdentityLink.entity_kind == entity_kind,
            ExternalIdentityLink.external_id == external_id,
            ExternalIdentityLink.status == IdentityLinkStatus.ACTIVE.value,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await session.scalars(stmt)
    rows = list(result.all())
    if not rows:
        return None
    return rows[0]


async def count_active_by_key(
    session: AsyncSession,
    *,
    provider: str,
    connection_scope: str,
    entity_kind: str,
    external_id: str,
) -> int:
    stmt = select(func.count()).select_from(ExternalIdentityLink).where(
        ExternalIdentityLink.provider == provider,
        ExternalIdentityLink.connection_scope == connection_scope,
        ExternalIdentityLink.entity_kind == entity_kind,
        ExternalIdentityLink.external_id == external_id,
        ExternalIdentityLink.status == IdentityLinkStatus.ACTIVE.value,
    )
    value = await session.scalar(stmt)
    return int(value or 0)


async def list_active_by_kind_external(
    session: AsyncSession,
    *,
    entity_kind: str,
    external_id: str,
) -> list[ExternalIdentityLink]:
    """Cross-scope lookup for phone/email-style matches (kind + normalized id)."""

    stmt = select(ExternalIdentityLink).where(
        ExternalIdentityLink.entity_kind == entity_kind,
        ExternalIdentityLink.external_id == external_id,
        ExternalIdentityLink.status == IdentityLinkStatus.ACTIVE.value,
    )
    result = await session.scalars(stmt)
    return list(result.all())


async def list_links_for_canonical(
    session: AsyncSession,
    *,
    canonical_identity_id: uuid.UUID,
    active_only: bool = False,
) -> list[ExternalIdentityLink]:
    clauses = [
        ExternalIdentityLink.canonical_identity_id == canonical_identity_id,
    ]
    if active_only:
        clauses.append(
            ExternalIdentityLink.status == IdentityLinkStatus.ACTIVE.value
        )
    stmt = select(ExternalIdentityLink).where(*clauses)
    result = await session.scalars(stmt)
    return list(result.all())


async def list_active_buyer_cards(
    session: AsyncSession,
    *,
    canonical_identity_id: uuid.UUID,
) -> list[ExternalIdentityLink]:
    stmt = select(ExternalIdentityLink).where(
        ExternalIdentityLink.canonical_identity_id == canonical_identity_id,
        ExternalIdentityLink.entity_kind
        == IdentityEntityKind.AMOCRM_BUYER_CARD.value,
        ExternalIdentityLink.status == IdentityLinkStatus.ACTIVE.value,
    )
    result = await session.scalars(stmt)
    return list(result.all())


async def insert_active_link(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
    canonical_identity_id: uuid.UUID,
    provider: str,
    connection_scope: str,
    entity_kind: str,
    external_id: str,
    confidence: str,
    source: str,
) -> ExternalIdentityLink:
    now = func.statement_timestamp()
    row = ExternalIdentityLink(
        id=row_id,
        canonical_identity_id=canonical_identity_id,
        provider=provider,
        connection_scope=connection_scope,
        entity_kind=entity_kind,
        external_id=external_id,
        status=IdentityLinkStatus.ACTIVE.value,
        confidence=confidence,
        source=source,
        linked_at=now,
        revoked_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def mark_link_revoked(
    session: AsyncSession,
    *,
    link_id: uuid.UUID,
) -> ExternalIdentityLink | None:
    stmt = (
        update(ExternalIdentityLink)
        .where(
            ExternalIdentityLink.id == link_id,
            ExternalIdentityLink.status == IdentityLinkStatus.ACTIVE.value,
        )
        .values(
            status=IdentityLinkStatus.REVOKED.value,
            revoked_at=func.statement_timestamp(),
            updated_at=func.statement_timestamp(),
        )
        .returning(ExternalIdentityLink)
    )
    row = await session.scalar(stmt)
    if row is None:
        return None
    await session.flush()
    return row


async def list_active_amocrm_deal_roles(
    session: AsyncSession,
    *,
    provider: str,
    connection_scope: str,
    external_id: str,
) -> list[ExternalIdentityLink]:
    """ACTIVE Buyer Card or technical deal rows for the same amo external id."""

    stmt = select(ExternalIdentityLink).where(
        ExternalIdentityLink.provider == provider,
        ExternalIdentityLink.connection_scope == connection_scope,
        ExternalIdentityLink.external_id == external_id,
        ExternalIdentityLink.status == IdentityLinkStatus.ACTIVE.value,
        ExternalIdentityLink.entity_kind.in_(
            (
                IdentityEntityKind.AMOCRM_BUYER_CARD.value,
                IdentityEntityKind.AMOCRM_TECHNICAL_DEAL.value,
            )
        ),
    )
    result = await session.scalars(stmt)
    return list(result.all())


async def lock_active_amocrm_deal_roles(
    session: AsyncSession,
    *,
    provider: str,
    connection_scope: str,
    external_id: str,
) -> list[ExternalIdentityLink]:
    stmt = (
        select(ExternalIdentityLink)
        .where(
            ExternalIdentityLink.provider == provider,
            ExternalIdentityLink.connection_scope == connection_scope,
            ExternalIdentityLink.external_id == external_id,
            ExternalIdentityLink.status == IdentityLinkStatus.ACTIVE.value,
            ExternalIdentityLink.entity_kind.in_(
                (
                    IdentityEntityKind.AMOCRM_BUYER_CARD.value,
                    IdentityEntityKind.AMOCRM_TECHNICAL_DEAL.value,
                )
            ),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await session.scalars(stmt)
    return list(result.all())


def as_link_record(row: ExternalIdentityLink) -> IdentityLinkRecord:
    return _to_link_record(row)


def as_identity_record(row: CanonicalIdentity) -> CanonicalIdentityRecord:
    return _to_identity_record(row)
