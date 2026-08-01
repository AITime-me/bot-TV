"""Shared fakes and synthetic fixtures for attachment spool Stage 1A1 tests."""

from __future__ import annotations

import binascii
import struct
import zlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass
class TxnTracker:
    events: list[str] = field(default_factory=list)
    fail_commit: bool = False
    session: Any = field(default=None)

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = _NestedCapableSession()


class _NestedTxn:
    async def __aenter__(self) -> "_NestedTxn":
        return self

    async def __aexit__(self, *_a: Any) -> bool:
        return False


class _NestedCapableSession:
    def begin_nested(self) -> _NestedTxn:
        return _NestedTxn()

    async def flush(self) -> None:
        return None


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


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    length = struct.pack(">I", len(data))
    crc = binascii.crc32(chunk_type)
    crc = binascii.crc32(data, crc) & 0xFFFFFFFF
    return length + chunk_type + data + struct.pack(">I", crc)


def synthetic_minimal_png() -> bytes:
    """Minimal structurally valid 1x1 PNG with correct CRCs."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    # zlib-compressed empty scanline-ish payload (filter byte + one gray sample)
    raw = b"\x00\x00"
    idat = zlib.compress(raw, level=9)
    return (
        signature
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def synthetic_minimal_jpeg() -> bytes:
    """Minimal structurally valid 1x1 JPEG with SOF/SOS/EOI."""
    sof = bytes(
        [
            0xFF,
            0xC0,
            0x00,
            0x0B,
            0x08,
            0x00,
            0x01,
            0x00,
            0x01,
            0x01,
            0x01,
            0x11,
            0x00,
        ]
    )
    sos = bytes([0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01, 0x00, 0x00, 0x3F, 0x00])
    return b"\xff\xd8" + sof + sos + b"\x00" + b"\xff\xd9"
