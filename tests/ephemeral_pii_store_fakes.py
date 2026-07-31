"""Shared fakes/spies for ephemeral PII store unit orchestration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ephemeral_pii_types import (
    CRYPTO_VERSION_V1,
    EphemeralPiiKind,
    EphemeralPiiPurpose,
)
from app.repositories.ephemeral_pii import EphemeralPiiLockedRow


@dataclass
class TxnTracker:
    events: list[str] = field(default_factory=list)
    fail_commit: bool = False
    session: Any = field(default_factory=lambda: object())


def make_observing_session_scope(
    tracker: TxnTracker,
) -> Callable[[async_sessionmaker[AsyncSession]], Any]:
    @asynccontextmanager
    async def _scope(
        _factory: async_sessionmaker[AsyncSession],
    ) -> AsyncIterator[Any]:
        tracker.events.append("enter")
        try:
            yield tracker.session
        except (KeyboardInterrupt, SystemExit):
            tracker.events.append("rollback")
            raise
        except Exception:
            tracker.events.append("rollback")
            raise
        else:
            if tracker.fail_commit:
                tracker.events.append("rollback")
                raise RuntimeError("synthetic commit failure")
            tracker.events.append("commit")
        finally:
            tracker.events.append("exit")

    return _scope


def sample_locked_row(
    *,
    conversation_id: UUID | None = None,
    pii_kind: str = EphemeralPiiKind.PHONE.value,
    allowed_purpose: str = EphemeralPiiPurpose.BOOKING_PHONE_WRITE.value,
    crypto_version: int = CRYPTO_VERSION_V1,
    ciphertext: bytes | None = None,
    nonce: bytes | None = None,
    key_id: str = "TESTK1",
    row_id: UUID | None = None,
) -> EphemeralPiiLockedRow:
    return EphemeralPiiLockedRow(
        id=row_id or uuid4(),
        conversation_id=conversation_id or uuid4(),
        pii_kind=pii_kind,
        allowed_purpose=allowed_purpose,
        ciphertext=ciphertext if ciphertext is not None else b"x" * 16,
        nonce=nonce if nonce is not None else b"y" * 12,
        key_id=key_id,
        crypto_version=crypto_version,
    )
