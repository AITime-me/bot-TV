"""PostgreSQL behavioral and concurrency tests for CURSOR-27 master bindings."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.master_channel_binding import (
    DEFAULT_CONNECTION_SCOPE,
    BindMasterBindingOutcome,
    MasterBindingStatus,
    RebindMasterBindingOutcome,
    ResolveMasterBindingOutcome,
    RevokeMasterBindingOutcome,
)
from app.db.session import session_scope
from app.models.master_channel_binding import MasterChannelBinding
from app.services.master_channel_binding import MasterChannelBindingService
from tests.pg_harness import truncate_foundation_tables

_MASTER_A = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_MASTER_B = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
_ACCOUNT = "vk-user-10001"


@pytest_asyncio.fixture(autouse=True)
async def master_binding_row_cleanup(
    request: pytest.FixtureRequest,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    if request.node.get_closest_marker("no_foundation_row_cleanup"):
        yield
        return
    await truncate_foundation_tables(session_factory)
    try:
        yield
    finally:
        await truncate_foundation_tables(session_factory)


@pytest.mark.asyncio
async def test_migration_creates_master_channel_bindings_table(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        exists = await session.scalar(
            text("SELECT to_regclass('public.master_channel_bindings') IS NOT NULL")
        )
        assert exists is True
        partial = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'uq_master_channel_bindings_active_identity'"
            )
        )
        assert partial is not None
        assert "UNIQUE" in partial.upper()
        assert "ACTIVE" in partial


@pytest.mark.asyncio
async def test_resolve_active_binding(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = MasterChannelBindingService(session)
        bound = await svc.bind(
            channel="vk",
            external_account_id=_ACCOUNT,
            master_id=_MASTER_A,
        )
        assert bound.outcome is BindMasterBindingOutcome.BOUND
        resolved = await svc.resolve(
            channel="vk",
            external_account_id=_ACCOUNT,
        )
        assert resolved.outcome is ResolveMasterBindingOutcome.RESOLVED
        assert resolved.master_id == _MASTER_A
        assert resolved.binding is not None
        assert resolved.binding.status is MasterBindingStatus.ACTIVE


@pytest.mark.asyncio
async def test_revoked_binding_does_not_resolve(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = MasterChannelBindingService(session)
        await svc.bind(
            channel="vk",
            external_account_id=_ACCOUNT,
            master_id=_MASTER_A,
        )
        revoked = await svc.revoke(
            channel="vk",
            external_account_id=_ACCOUNT,
        )
        assert revoked.outcome is RevokeMasterBindingOutcome.REVOKED
        assert revoked.binding is not None
        assert revoked.binding.status is MasterBindingStatus.REVOKED
        assert revoked.binding.revoked_at is not None

        resolved = await svc.resolve(
            channel="vk",
            external_account_id=_ACCOUNT,
        )
        assert resolved.outcome is ResolveMasterBindingOutcome.NOT_FOUND
        assert resolved.master_id is None


@pytest.mark.asyncio
async def test_bind_conflict_different_master(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = MasterChannelBindingService(session)
        first = await svc.bind(
            channel="max",
            external_account_id=_ACCOUNT,
            master_id=_MASTER_A,
            connection_scope="bot-1",
        )
        assert first.outcome is BindMasterBindingOutcome.BOUND
        second = await svc.bind(
            channel="max",
            external_account_id=_ACCOUNT,
            master_id=_MASTER_B,
            connection_scope="bot-1",
        )
        assert second.outcome is BindMasterBindingOutcome.CONFLICT
        resolved = await svc.resolve(
            channel="max",
            external_account_id=_ACCOUNT,
            connection_scope="bot-1",
        )
        assert resolved.outcome is ResolveMasterBindingOutcome.RESOLVED
        assert resolved.master_id == _MASTER_A


@pytest.mark.asyncio
async def test_bind_idempotent_same_master(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = MasterChannelBindingService(session)
        first = await svc.bind(
            channel="synthetic",
            external_account_id="acct-1",
            master_id=_MASTER_A,
        )
        second = await svc.bind(
            channel="synthetic",
            external_account_id="acct-1",
            master_id=_MASTER_A,
        )
        assert first.outcome is BindMasterBindingOutcome.BOUND
        assert second.outcome is BindMasterBindingOutcome.ALREADY_BOUND
        count = await session.scalar(
            select(func.count()).select_from(MasterChannelBinding).where(
                MasterChannelBinding.status == "ACTIVE"
            )
        )
        assert int(count or 0) == 1


@pytest.mark.asyncio
async def test_connection_scope_prevents_cross_connection_collision(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = MasterChannelBindingService(session)
        a = await svc.bind(
            channel="vk",
            external_account_id=_ACCOUNT,
            master_id=_MASTER_A,
            connection_scope="connection-a",
        )
        b = await svc.bind(
            channel="vk",
            external_account_id=_ACCOUNT,
            master_id=_MASTER_B,
            connection_scope="connection-b",
        )
        assert a.outcome is BindMasterBindingOutcome.BOUND
        assert b.outcome is BindMasterBindingOutcome.BOUND
        ra = await svc.resolve(
            channel="vk",
            external_account_id=_ACCOUNT,
            connection_scope="connection-a",
        )
        rb = await svc.resolve(
            channel="vk",
            external_account_id=_ACCOUNT,
            connection_scope="connection-b",
        )
        assert ra.master_id == _MASTER_A
        assert rb.master_id == _MASTER_B


@pytest.mark.asyncio
async def test_case_sensitive_account_ids_do_not_false_match(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = MasterChannelBindingService(session)
        await svc.bind(
            channel="vk",
            external_account_id="UserABC",
            master_id=_MASTER_A,
        )
        await svc.bind(
            channel="vk",
            external_account_id="userabc",
            master_id=_MASTER_B,
        )
        upper = await svc.resolve(channel="vk", external_account_id="UserABC")
        lower = await svc.resolve(channel="vk", external_account_id="userabc")
        assert upper.master_id == _MASTER_A
        assert lower.master_id == _MASTER_B


@pytest.mark.asyncio
async def test_rebind_revokes_previous_and_resolves_new(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = MasterChannelBindingService(session)
        first = await svc.bind(
            channel="vk",
            external_account_id=_ACCOUNT,
            master_id=_MASTER_A,
        )
        assert first.binding is not None
        rebound = await svc.rebind(
            channel="vk",
            external_account_id=_ACCOUNT,
            master_id=_MASTER_B,
        )
        assert rebound.outcome is RebindMasterBindingOutcome.REBOUND
        assert rebound.revoked_binding_id == first.binding.binding_id
        assert rebound.binding is not None
        assert rebound.binding.master_id == _MASTER_B

        resolved = await svc.resolve(channel="vk", external_account_id=_ACCOUNT)
        assert resolved.master_id == _MASTER_B

        active = await session.scalar(
            select(func.count()).select_from(MasterChannelBinding).where(
                MasterChannelBinding.status == "ACTIVE",
                MasterChannelBinding.external_account_id == _ACCOUNT,
            )
        )
        revoked = await session.scalar(
            select(func.count()).select_from(MasterChannelBinding).where(
                MasterChannelBinding.status == "REVOKED",
                MasterChannelBinding.external_account_id == _ACCOUNT,
            )
        )
        assert int(active or 0) == 1
        assert int(revoked or 0) == 1


@pytest.mark.asyncio
async def test_partial_unique_rejects_two_active_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        now = func.statement_timestamp()
        session.add(
            MasterChannelBinding(
                id=uuid.uuid4(),
                channel="vk",
                connection_scope=DEFAULT_CONNECTION_SCOPE,
                external_account_id=_ACCOUNT,
                master_id=_MASTER_A,
                status="ACTIVE",
                bound_at=now,
                revoked_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            MasterChannelBinding(
                id=uuid.uuid4(),
                channel="vk",
                connection_scope=DEFAULT_CONNECTION_SCOPE,
                external_account_id=_ACCOUNT,
                master_id=_MASTER_B,
                status="ACTIVE",
                bound_at=now,
                revoked_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_concurrent_bind_different_masters_single_active(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def _attempt(master_id: str) -> BindMasterBindingOutcome:
        async with session_scope(session_factory) as session:
            svc = MasterChannelBindingService(session)
            result = await svc.bind(
                channel="vk",
                external_account_id=_ACCOUNT,
                master_id=master_id,
            )
            return result.outcome

    first, second = await asyncio.gather(
        _attempt(_MASTER_A),
        _attempt(_MASTER_B),
    )
    outcomes = {first, second}
    assert BindMasterBindingOutcome.BOUND in outcomes
    assert BindMasterBindingOutcome.CONFLICT in outcomes
    # Exactly one ACTIVE row; never two masters active for one identity.
    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(MasterChannelBinding).where(
                        MasterChannelBinding.status == "ACTIVE",
                        MasterChannelBinding.external_account_id == _ACCOUNT,
                    )
                )
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].master_id in {_MASTER_A, _MASTER_B}


@pytest.mark.asyncio
async def test_concurrent_bind_same_master_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def _attempt() -> BindMasterBindingOutcome:
        async with session_scope(session_factory) as session:
            svc = MasterChannelBindingService(session)
            result = await svc.bind(
                channel="vk",
                external_account_id=_ACCOUNT,
                master_id=_MASTER_A,
            )
            return result.outcome

    first, second = await asyncio.gather(_attempt(), _attempt())
    assert {first, second} <= {
        BindMasterBindingOutcome.BOUND,
        BindMasterBindingOutcome.ALREADY_BOUND,
    }
    assert BindMasterBindingOutcome.BOUND in {first, second}
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(MasterChannelBinding).where(
                MasterChannelBinding.status == "ACTIVE",
                MasterChannelBinding.external_account_id == _ACCOUNT,
            )
        )
        assert int(count or 0) == 1


@pytest.mark.asyncio
async def test_repr_hides_identities(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = MasterChannelBindingService(session)
        result = await svc.bind(
            channel="vk",
            external_account_id=_ACCOUNT,
            master_id=_MASTER_A,
        )
        rendered = f"{result!r}{result.binding!r}"
        assert _ACCOUNT not in rendered
        assert _MASTER_A not in rendered
        assert "eeeeeeeeeeee" not in rendered


async def _active_rows(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account: str = _ACCOUNT,
) -> list[MasterChannelBinding]:
    async with session_factory() as session:
        return list(
            (
                await session.scalars(
                    select(MasterChannelBinding).where(
                        MasterChannelBinding.status == "ACTIVE",
                        MasterChannelBinding.external_account_id == account,
                    )
                )
            ).all()
        )


@pytest.mark.asyncio
async def test_concurrent_rebind_same_master(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = MasterChannelBindingService(session)
        bound = await svc.bind(
            channel="vk",
            external_account_id=_ACCOUNT,
            master_id=_MASTER_A,
        )
        assert bound.outcome is BindMasterBindingOutcome.BOUND

    async def _attempt() -> RebindMasterBindingOutcome:
        async with session_scope(session_factory) as session:
            svc = MasterChannelBindingService(session)
            result = await svc.rebind(
                channel="vk",
                external_account_id=_ACCOUNT,
                master_id=_MASTER_A,
            )
            return result.outcome

    first, second = await asyncio.gather(_attempt(), _attempt())
    assert first is RebindMasterBindingOutcome.ALREADY_BOUND
    assert second is RebindMasterBindingOutcome.ALREADY_BOUND
    rows = await _active_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].master_id == _MASTER_A


@pytest.mark.asyncio
async def test_cross_channel_same_scope_account_are_independent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Identical scope/account on vk vs max must not collide."""

    async def _bind(channel: str, master_id: str) -> BindMasterBindingOutcome:
        async with session_scope(session_factory) as session:
            svc = MasterChannelBindingService(session)
            result = await svc.bind(
                channel=channel,
                external_account_id=_ACCOUNT,
                master_id=master_id,
                connection_scope="shared-scope",
            )
            return result.outcome

    vk_out, max_out = await asyncio.gather(
        _bind("vk", _MASTER_A),
        _bind("max", _MASTER_B),
    )
    assert vk_out is BindMasterBindingOutcome.BOUND
    assert max_out is BindMasterBindingOutcome.BOUND

    async with session_scope(session_factory) as session:
        svc = MasterChannelBindingService(session)
        vk = await svc.resolve(
            channel="vk",
            external_account_id=_ACCOUNT,
            connection_scope="shared-scope",
        )
        mx = await svc.resolve(
            channel="max",
            external_account_id=_ACCOUNT,
            connection_scope="shared-scope",
        )
        assert vk.outcome is ResolveMasterBindingOutcome.RESOLVED
        assert mx.outcome is ResolveMasterBindingOutcome.RESOLVED
        assert vk.master_id == _MASTER_A
        assert mx.master_id == _MASTER_B

    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(MasterChannelBinding).where(
                        MasterChannelBinding.status == "ACTIVE",
                        MasterChannelBinding.external_account_id == _ACCOUNT,
                        MasterChannelBinding.connection_scope == "shared-scope",
                    )
                )
            ).all()
        )
        assert len(rows) == 2
        assert {row.channel for row in rows} == {"vk", "max"}


@pytest.mark.asyncio
async def test_concurrent_revoke_single_winner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = MasterChannelBindingService(session)
        await svc.bind(
            channel="vk",
            external_account_id=_ACCOUNT,
            master_id=_MASTER_A,
        )

    async def _revoke() -> RevokeMasterBindingOutcome:
        async with session_scope(session_factory) as session:
            svc = MasterChannelBindingService(session)
            result = await svc.revoke(
                channel="vk",
                external_account_id=_ACCOUNT,
            )
            assert result.outcome is not RevokeMasterBindingOutcome.INVALID_INPUT
            return result.outcome

    first, second = await asyncio.gather(_revoke(), _revoke())
    assert {first, second} == {
        RevokeMasterBindingOutcome.REVOKED,
        RevokeMasterBindingOutcome.NOT_FOUND,
    }
    rows = await _active_rows(session_factory)
    assert rows == []


@pytest.mark.asyncio
async def test_db_check_enforces_printable_ascii_identity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """DB CHECK must match service ^[\\x21-\\x7E]+$ (locale-independent)."""

    from sqlalchemy.exc import DBAPIError, IntegrityError

    async def _try_insert(*, account: str, scope: str = "default") -> None:
        async with session_scope(session_factory) as session:
            now = func.statement_timestamp()
            session.add(
                MasterChannelBinding(
                    id=uuid.uuid4(),
                    channel="vk",
                    connection_scope=scope,
                    external_account_id=account,
                    master_id=_MASTER_A,
                    status="ACTIVE",
                    bound_at=now,
                    revoked_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()

    await _try_insert(account="ascii-ok_Account.1")

    for bad in ("café", "кириллица", "has space", "has\ttab", "has\nline"):
        with pytest.raises((IntegrityError, DBAPIError)):
            await _try_insert(account=bad)
    with pytest.raises((IntegrityError, DBAPIError)):
        await _try_insert(account="ok-account", scope="bad scope")


@pytest.mark.asyncio
async def test_concurrent_rebind_different_masters(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = MasterChannelBindingService(session)
        await svc.bind(
            channel="vk",
            external_account_id=_ACCOUNT,
            master_id=_MASTER_A,
        )

    async def _attempt(master_id: str) -> RebindMasterBindingOutcome:
        async with session_scope(session_factory) as session:
            svc = MasterChannelBindingService(session)
            result = await svc.rebind(
                channel="vk",
                external_account_id=_ACCOUNT,
                master_id=master_id,
            )
            assert result.outcome is not RebindMasterBindingOutcome.INVALID_INPUT
            return result.outcome

    first, second = await asyncio.gather(
        _attempt(_MASTER_B),
        _attempt("cccccccc-dddd-4eee-8fff-000000000000"),
    )
    outcomes = {first, second}
    assert RebindMasterBindingOutcome.REBOUND in outcomes
    assert outcomes <= {
        RebindMasterBindingOutcome.REBOUND,
        RebindMasterBindingOutcome.CONFLICT,
        RebindMasterBindingOutcome.ALREADY_BOUND,
    }
    rows = await _active_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].master_id in {
        _MASTER_B,
        "cccccccc-dddd-4eee-8fff-000000000000",
    }


@pytest.mark.asyncio
async def test_concurrent_rebind_and_bind(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        svc = MasterChannelBindingService(session)
        await svc.bind(
            channel="vk",
            external_account_id=_ACCOUNT,
            master_id=_MASTER_A,
        )

    async def _rebind() -> RebindMasterBindingOutcome:
        async with session_scope(session_factory) as session:
            svc = MasterChannelBindingService(session)
            result = await svc.rebind(
                channel="vk",
                external_account_id=_ACCOUNT,
                master_id=_MASTER_B,
            )
            assert result.outcome is not RebindMasterBindingOutcome.INVALID_INPUT
            return result.outcome

    async def _bind() -> BindMasterBindingOutcome:
        async with session_scope(session_factory) as session:
            svc = MasterChannelBindingService(session)
            result = await svc.bind(
                channel="vk",
                external_account_id=_ACCOUNT,
                master_id=_MASTER_B,
            )
            assert result.outcome is not BindMasterBindingOutcome.INVALID_INPUT
            return result.outcome

    rebind_out, bind_out = await asyncio.gather(_rebind(), _bind())
    assert rebind_out in {
        RebindMasterBindingOutcome.REBOUND,
        RebindMasterBindingOutcome.ALREADY_BOUND,
        RebindMasterBindingOutcome.CONFLICT,
    }
    assert bind_out in {
        BindMasterBindingOutcome.BOUND,
        BindMasterBindingOutcome.ALREADY_BOUND,
        BindMasterBindingOutcome.CONFLICT,
    }
    rows = await _active_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].master_id in {_MASTER_A, _MASTER_B}


@pytest.mark.asyncio
async def test_rebind_insert_failure_keeps_previous_active(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacement failure must not commit revoke-only (0 ACTIVE)."""

    async with session_scope(session_factory) as session:
        svc = MasterChannelBindingService(session)
        first = await svc.bind(
            channel="vk",
            external_account_id=_ACCOUNT,
            master_id=_MASTER_A,
        )
        assert first.outcome is BindMasterBindingOutcome.BOUND

        async def _boom(*_a: object, **_k: object) -> object:
            raise RuntimeError("simulated replacement failure")

        monkeypatch.setattr(
            "app.repositories.master_channel_bindings.insert_active_binding",
            _boom,
        )
        with pytest.raises(RuntimeError, match="simulated replacement failure"):
            await svc.rebind(
                channel="vk",
                external_account_id=_ACCOUNT,
                master_id=_MASTER_B,
            )
        # Outer UoW still open; savepoint rollback must restore ACTIVE A.
        active = await session.scalar(
            select(func.count()).select_from(MasterChannelBinding).where(
                MasterChannelBinding.status == "ACTIVE",
                MasterChannelBinding.external_account_id == _ACCOUNT,
            )
        )
        assert int(active or 0) == 1
        row = (
            await session.scalars(
                select(MasterChannelBinding).where(
                    MasterChannelBinding.status == "ACTIVE",
                    MasterChannelBinding.external_account_id == _ACCOUNT,
                )
            )
        ).one()
        assert row.master_id == _MASTER_A
