"""Repository for durable encrypted amoCRM CRM OAuth tokens."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.amocrm_crm_oauth_crypto import decrypt_token, encrypt_token
from app.core.amocrm_crm_oauth_keys import AmoCrmOauthKeyProvider
from app.core.amocrm_crm_oauth_types import (
    CRYPTO_VERSION_V1,
    DEFAULT_CONNECTION_SCOPE,
    AmoCrmCrmOauthError,
    AmoCrmOauthAad,
    AmoCrmOauthCiphertext,
    AmoCrmOauthTokenKind,
)
from app.db.clock import resolve_moment
from app.models.amocrm_crm_oauth_token import AmocrmCrmOauthToken

DEFAULT_REFRESH_LEASE_SECONDS = 30
# Covers OAuth HTTP timeout (10s) + local persist retries after 200.
PRE_HTTP_REFRESH_LEASE_SECONDS = 60


@dataclass(frozen=True, repr=False)
class OauthRefreshLease:
    token_row_id: uuid.UUID
    connection_scope: str
    lease_owner: str
    lease_token: uuid.UUID
    lease_version: int
    lease_until: datetime

    def __repr__(self) -> str:
        return (
            "OauthRefreshLease("
            f"token_row_id={self.token_row_id!r}, "
            f"lease_version={self.lease_version!r}, "
            "connection_scope=<redacted>)"
        )


@dataclass(frozen=True, repr=False)
class OauthPreRefreshSnapshot:
    """Exact pre-refresh token pair + lease version used for one OAuth HTTP call.

    Plaintext tokens stay in-process only; __repr__ never prints them.
    """

    access_token: str
    refresh_token: str
    lease_version: int

    def __repr__(self) -> str:
        return (
            "OauthPreRefreshSnapshot("
            "access_token=<redacted>, refresh_token=<redacted>, "
            f"lease_version={self.lease_version!r})"
        )


@dataclass(frozen=True, repr=False)
class DecryptedOauthTokens:
    access_token: str
    refresh_token: str
    access_expires_at: datetime | None

    def __repr__(self) -> str:
        return (
            "DecryptedOauthTokens(access_token=<redacted>, "
            "refresh_token=<redacted>, "
            f"access_expires_at={self.access_expires_at!r})"
        )


async def get_by_scope(
    session: AsyncSession,
    *,
    connection_scope: str = DEFAULT_CONNECTION_SCOPE,
) -> AmocrmCrmOauthToken | None:
    return await session.scalar(
        select(AmocrmCrmOauthToken).where(
            AmocrmCrmOauthToken.connection_scope == connection_scope
        )
    )


def _encrypt_pair(
    *,
    access_token: str,
    refresh_token: str,
    connection_scope: str,
    key_provider: AmoCrmOauthKeyProvider,
) -> tuple[str, AmoCrmOauthCiphertext, AmoCrmOauthCiphertext]:
    active = key_provider.get_active_key()
    access_ct = encrypt_token(
        access_token,
        aad=AmoCrmOauthAad(
            crypto_version=CRYPTO_VERSION_V1,
            key_id=active.key_id,
            connection_scope=connection_scope,
            token_kind=AmoCrmOauthTokenKind.ACCESS,
        ),
        key_provider=key_provider,
        active_key=active,
    )
    refresh_ct = encrypt_token(
        refresh_token,
        aad=AmoCrmOauthAad(
            crypto_version=CRYPTO_VERSION_V1,
            key_id=active.key_id,
            connection_scope=connection_scope,
            token_kind=AmoCrmOauthTokenKind.REFRESH,
        ),
        key_provider=key_provider,
        active_key=active,
    )
    return active.key_id, access_ct, refresh_ct


async def insert_token_pair_if_absent(
    session: AsyncSession,
    *,
    access_token: str,
    refresh_token: str,
    key_provider: AmoCrmOauthKeyProvider,
    connection_scope: str = DEFAULT_CONNECTION_SCOPE,
    access_expires_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[AmocrmCrmOauthToken, bool]:
    """Encrypt and insert a token pair only when the scope row is absent.

    Returns ``(row, inserted)``. Existing scope => ``inserted=False`` and no
    overwrite (operator bootstrap refuse path). Never clears a live refresh lease.
    """

    if type(access_token) is not str or not access_token:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_VALUE_INVALID")
    if type(refresh_token) is not str or not refresh_token:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_VALUE_INVALID")
    if any(ch.isspace() for ch in access_token) or any(
        ch.isspace() for ch in refresh_token
    ):
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_VALUE_INVALID")

    moment = await resolve_moment(session, now)
    key_id, access_ct, refresh_ct = _encrypt_pair(
        access_token=access_token,
        refresh_token=refresh_token,
        connection_scope=connection_scope,
        key_provider=key_provider,
    )
    new_id = uuid.uuid4()
    stmt = (
        insert(AmocrmCrmOauthToken)
        .values(
            id=new_id,
            connection_scope=connection_scope,
            key_id=key_id,
            crypto_version=CRYPTO_VERSION_V1,
            access_nonce=access_ct.nonce,
            access_ciphertext=access_ct.ciphertext,
            refresh_nonce=refresh_ct.nonce,
            refresh_ciphertext=refresh_ct.ciphertext,
            access_expires_at=access_expires_at,
            lease_owner=None,
            lease_token=None,
            lease_version=0,
            lease_until=None,
            created_at=moment,
            updated_at=moment,
        )
        .on_conflict_do_nothing(
            constraint="uq_amocrm_crm_oauth_tokens_connection_scope"
        )
        .returning(AmocrmCrmOauthToken.id)
    )
    row_id = await session.scalar(stmt)
    if row_id is None:
        existing = await get_by_scope(session, connection_scope=connection_scope)
        if existing is None:
            raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_STORE_FAILED")
        return existing, False
    row = await session.get(AmocrmCrmOauthToken, row_id)
    if row is None:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_STORE_FAILED")
    return row, True


async def upsert_token_pair(
    session: AsyncSession,
    *,
    access_token: str,
    refresh_token: str,
    key_provider: AmoCrmOauthKeyProvider,
    connection_scope: str = DEFAULT_CONNECTION_SCOPE,
    access_expires_at: datetime | None = None,
    now: datetime | None = None,
) -> AmocrmCrmOauthToken:
    """Insert or atomically replace the encrypted token pair for a scope."""

    moment = await resolve_moment(session, now)
    key_id, access_ct, refresh_ct = _encrypt_pair(
        access_token=access_token,
        refresh_token=refresh_token,
        connection_scope=connection_scope,
        key_provider=key_provider,
    )
    new_id = uuid.uuid4()
    stmt = (
        insert(AmocrmCrmOauthToken)
        .values(
            id=new_id,
            connection_scope=connection_scope,
            key_id=key_id,
            crypto_version=CRYPTO_VERSION_V1,
            access_nonce=access_ct.nonce,
            access_ciphertext=access_ct.ciphertext,
            refresh_nonce=refresh_ct.nonce,
            refresh_ciphertext=refresh_ct.ciphertext,
            access_expires_at=access_expires_at,
            lease_owner=None,
            lease_token=None,
            lease_version=0,
            lease_until=None,
            created_at=moment,
            updated_at=moment,
        )
        .on_conflict_do_update(
            constraint="uq_amocrm_crm_oauth_tokens_connection_scope",
            set_={
                "key_id": key_id,
                "crypto_version": CRYPTO_VERSION_V1,
                "access_nonce": access_ct.nonce,
                "access_ciphertext": access_ct.ciphertext,
                "refresh_nonce": refresh_ct.nonce,
                "refresh_ciphertext": refresh_ct.ciphertext,
                "access_expires_at": access_expires_at,
                "lease_owner": None,
                "lease_token": None,
                "lease_until": None,
                "updated_at": moment,
            },
        )
        .returning(AmocrmCrmOauthToken.id)
    )
    row_id = await session.scalar(stmt)
    if row_id is None:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_STORE_FAILED")
    row = await session.get(AmocrmCrmOauthToken, row_id)
    if row is None:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_STORE_FAILED")
    return row


def decrypt_row(
    row: AmocrmCrmOauthToken,
    *,
    key_provider: AmoCrmOauthKeyProvider,
) -> DecryptedOauthTokens:
    access = decrypt_token(
        AmoCrmOauthCiphertext(
            crypto_version=row.crypto_version,
            key_id=row.key_id,
            nonce=row.access_nonce,
            ciphertext=row.access_ciphertext,
        ),
        aad=AmoCrmOauthAad(
            crypto_version=row.crypto_version,
            key_id=row.key_id,
            connection_scope=row.connection_scope,
            token_kind=AmoCrmOauthTokenKind.ACCESS,
        ),
        key_provider=key_provider,
    )
    refresh = decrypt_token(
        AmoCrmOauthCiphertext(
            crypto_version=row.crypto_version,
            key_id=row.key_id,
            nonce=row.refresh_nonce,
            ciphertext=row.refresh_ciphertext,
        ),
        aad=AmoCrmOauthAad(
            crypto_version=row.crypto_version,
            key_id=row.key_id,
            connection_scope=row.connection_scope,
            token_kind=AmoCrmOauthTokenKind.REFRESH,
        ),
        key_provider=key_provider,
    )
    return DecryptedOauthTokens(
        access_token=access,
        refresh_token=refresh,
        access_expires_at=row.access_expires_at,
    )


async def claim_refresh_lease(
    session: AsyncSession,
    *,
    worker_id: str,
    connection_scope: str = DEFAULT_CONNECTION_SCOPE,
    lease_seconds: int = DEFAULT_REFRESH_LEASE_SECONDS,
    now: datetime | None = None,
) -> OauthRefreshLease:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    moment = await resolve_moment(session, now)
    row = await session.scalar(
        select(AmocrmCrmOauthToken)
        .where(AmocrmCrmOauthToken.connection_scope == connection_scope)
        .with_for_update()
    )
    if row is None:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_NOT_FOUND")
    if (
        row.lease_until is not None
        and row.lease_until > moment
        and row.lease_owner is not None
        and row.lease_owner != worker_id
    ):
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_STALE_LEASE")

    lease_token = uuid.uuid4()
    lease_until = moment + timedelta(seconds=lease_seconds)
    stmt = (
        update(AmocrmCrmOauthToken)
        .where(
            AmocrmCrmOauthToken.id == row.id,
            AmocrmCrmOauthToken.lease_version == row.lease_version,
        )
        .values(
            lease_owner=worker_id,
            lease_token=lease_token,
            lease_version=AmocrmCrmOauthToken.lease_version + 1,
            lease_until=lease_until,
            updated_at=moment,
        )
        .returning(
            AmocrmCrmOauthToken.id,
            AmocrmCrmOauthToken.lease_version,
        )
    )
    updated = (await session.execute(stmt)).one_or_none()
    if updated is None:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_STALE_LEASE")
    return OauthRefreshLease(
        token_row_id=updated[0],
        connection_scope=connection_scope,
        lease_owner=worker_id,
        lease_token=lease_token,
        lease_version=int(updated[1]),
        lease_until=lease_until,
    )


async def renew_refresh_lease(
    session: AsyncSession,
    *,
    lease: OauthRefreshLease,
    lease_seconds: int = PRE_HTTP_REFRESH_LEASE_SECONDS,
    now: datetime | None = None,
) -> OauthRefreshLease:
    """Extend a held refresh lease immediately before OAuth HTTP.

    Does not bump ``lease_version`` so the post-200 rotate fence stays valid.
    """

    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    moment = await resolve_moment(session, now)
    lease_until = moment + timedelta(seconds=lease_seconds)
    stmt = (
        update(AmocrmCrmOauthToken)
        .where(
            AmocrmCrmOauthToken.id == lease.token_row_id,
            AmocrmCrmOauthToken.lease_token == lease.lease_token,
            AmocrmCrmOauthToken.lease_version == lease.lease_version,
            AmocrmCrmOauthToken.lease_owner == lease.lease_owner,
            AmocrmCrmOauthToken.lease_until.is_not(None),
            AmocrmCrmOauthToken.lease_until > moment,
        )
        .values(lease_until=lease_until, updated_at=moment)
        .returning(AmocrmCrmOauthToken.id)
    )
    row_id = await session.scalar(stmt)
    if row_id is None:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_STALE_LEASE")
    return OauthRefreshLease(
        token_row_id=lease.token_row_id,
        connection_scope=lease.connection_scope,
        lease_owner=lease.lease_owner,
        lease_token=lease.lease_token,
        lease_version=lease.lease_version,
        lease_until=lease_until,
    )


async def rotate_tokens_with_lease(
    session: AsyncSession,
    *,
    lease: OauthRefreshLease,
    access_token: str,
    refresh_token: str,
    key_provider: AmoCrmOauthKeyProvider,
    access_expires_at: datetime | None = None,
    now: datetime | None = None,
) -> AmocrmCrmOauthToken:
    """Atomically replace token pair under a held refresh lease."""

    moment = await resolve_moment(session, now)
    key_id, access_ct, refresh_ct = _encrypt_pair(
        access_token=access_token,
        refresh_token=refresh_token,
        connection_scope=lease.connection_scope,
        key_provider=key_provider,
    )
    stmt = (
        update(AmocrmCrmOauthToken)
        .where(
            AmocrmCrmOauthToken.id == lease.token_row_id,
            AmocrmCrmOauthToken.lease_token == lease.lease_token,
            AmocrmCrmOauthToken.lease_version == lease.lease_version,
            AmocrmCrmOauthToken.lease_owner == lease.lease_owner,
            AmocrmCrmOauthToken.lease_until.is_not(None),
            AmocrmCrmOauthToken.lease_until > moment,
        )
        .values(
            key_id=key_id,
            crypto_version=CRYPTO_VERSION_V1,
            access_nonce=access_ct.nonce,
            access_ciphertext=access_ct.ciphertext,
            refresh_nonce=refresh_ct.nonce,
            refresh_ciphertext=refresh_ct.ciphertext,
            access_expires_at=access_expires_at,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            updated_at=moment,
        )
        .returning(AmocrmCrmOauthToken.id)
    )
    row_id = await session.scalar(stmt)
    if row_id is None:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_STALE_LEASE")
    row = await session.get(AmocrmCrmOauthToken, row_id)
    if row is None:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_STORE_FAILED")
    return row


async def recover_rotate_if_pre_refresh_unchanged(
    session: AsyncSession,
    *,
    connection_scope: str,
    worker_id: str,
    pre_refresh: OauthPreRefreshSnapshot,
    access_token: str,
    refresh_token: str,
    key_provider: AmoCrmOauthKeyProvider,
    access_expires_at: datetime | None = None,
    lease_seconds: int = PRE_HTTP_REFRESH_LEASE_SECONDS,
    now: datetime | None = None,
) -> AmocrmCrmOauthToken:
    """Guarded post-200 recovery when the original refresh lease went stale.

    Persists the in-memory rotated pair only when the DB row still decrypts to
    the exact pre-refresh access+refresh pair used for that HTTP call (no newer
    rotation won). Never issues another remote refresh.
    """

    moment = await resolve_moment(session, now)
    row = await session.scalar(
        select(AmocrmCrmOauthToken)
        .where(AmocrmCrmOauthToken.connection_scope == connection_scope)
        .with_for_update()
    )
    if row is None:
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_NOT_FOUND")
    current = decrypt_row(row, key_provider=key_provider)
    if (
        current.refresh_token != pre_refresh.refresh_token
        or current.access_token != pre_refresh.access_token
    ):
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_ROTATE_SUPERSEDED")
    if (
        row.lease_until is not None
        and row.lease_until > moment
        and row.lease_owner is not None
        and row.lease_owner != worker_id
    ):
        raise AmoCrmCrmOauthError("AMOCRM_CRM_OAUTH_STALE_LEASE")

    lease = await claim_refresh_lease(
        session,
        worker_id=worker_id,
        connection_scope=connection_scope,
        lease_seconds=lease_seconds,
        now=moment,
    )
    return await rotate_tokens_with_lease(
        session,
        lease=lease,
        access_token=access_token,
        refresh_token=refresh_token,
        key_provider=key_provider,
        access_expires_at=access_expires_at,
        now=moment,
    )


async def release_refresh_lease(
    session: AsyncSession,
    *,
    lease: OauthRefreshLease,
    now: datetime | None = None,
) -> None:
    moment = await resolve_moment(session, now)
    await session.execute(
        update(AmocrmCrmOauthToken)
        .where(
            AmocrmCrmOauthToken.id == lease.token_row_id,
            AmocrmCrmOauthToken.lease_token == lease.lease_token,
            AmocrmCrmOauthToken.lease_version == lease.lease_version,
        )
        .values(
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            updated_at=moment,
        )
    )
