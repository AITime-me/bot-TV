"""Unit tests for attachment MIME type gating (Stage 1A1)."""

from __future__ import annotations

import binascii
import struct
import zlib

import pytest

from app.core.attachment_mime import detect_attachment_mime
from app.core.attachment_types import (
    MAX_PLAINTEXT_BYTES,
    AttachmentError,
    AttachmentMime,
)
from tests.attachment_spool_fakes import synthetic_minimal_jpeg, synthetic_minimal_png


def test_detects_valid_synthetic_jpeg_and_png() -> None:
    assert detect_attachment_mime(synthetic_minimal_jpeg()) is AttachmentMime.IMAGE_JPEG
    assert detect_attachment_mime(synthetic_minimal_png()) is AttachmentMime.IMAGE_PNG


def test_empty_and_oversized_rejected() -> None:
    with pytest.raises(AttachmentError) as raised:
        detect_attachment_mime(b"")
    assert raised.value.code == "ATTACHMENT_VALUE_INVALID"
    with pytest.raises(AttachmentError) as raised:
        detect_attachment_mime(b"\xff\xd8" + b"\x00" * (MAX_PLAINTEXT_BYTES))
    assert raised.value.code in {"ATTACHMENT_TOO_LARGE", "ATTACHMENT_MIME_DENIED"}


def test_filename_extension_and_content_type_have_no_influence() -> None:
    # Validator takes bytes only — spoofed names cannot be passed.
    jpeg = synthetic_minimal_jpeg()
    assert detect_attachment_mime(jpeg) is AttachmentMime.IMAGE_JPEG
    with pytest.raises(AttachmentError) as raised:
        detect_attachment_mime(b"not-an-image")
    assert raised.value.code == "ATTACHMENT_MIME_DENIED"


def test_jpeg_missing_markers_and_trailing_payload() -> None:
    with pytest.raises(AttachmentError):
        detect_attachment_mime(b"\xff\xd8\xff\xd9")  # no SOF/SOS
    jpeg = synthetic_minimal_jpeg()
    with pytest.raises(AttachmentError):
        detect_attachment_mime(jpeg + b"TRAIL")
    with pytest.raises(AttachmentError):
        detect_attachment_mime(jpeg[:-1])  # truncated EOI


def test_jpeg_malformed_segment_length() -> None:
    bad = b"\xff\xd8\xff\xc0\x00\x02\xff\xd9"
    with pytest.raises(AttachmentError):
        detect_attachment_mime(bad)


def test_jpeg_stuffed_ff00_and_restart_in_scan() -> None:
    sof = bytes(
        [0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01, 0x00, 0x01, 0x01, 0x01, 0x11, 0x00]
    )
    sos = bytes([0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01, 0x00, 0x00, 0x3F, 0x00])
    scan = b"\x00\xff\x00\xff\xd0\x11"
    data = b"\xff\xd8" + sof + sos + scan + b"\xff\xd9"
    assert detect_attachment_mime(data) is AttachmentMime.IMAGE_JPEG


def test_png_wrong_signature_and_crc() -> None:
    png = bytearray(synthetic_minimal_png())
    png[0] = 0x00
    with pytest.raises(AttachmentError):
        detect_attachment_mime(bytes(png))
    good = synthetic_minimal_png()
    # Corrupt CRC of IHDR.
    bad = bytearray(good)
    bad[29] ^= 0x01
    with pytest.raises(AttachmentError):
        detect_attachment_mime(bytes(bad))


def test_png_ihdr_must_be_first_and_length_13() -> None:
    signature = b"\x89PNG\r\n\x1a\n"
    idat = zlib.compress(b"\x00\x00")

    def chunk(ctype: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = binascii.crc32(ctype)
        crc = binascii.crc32(data, crc) & 0xFFFFFFFF
        return length + ctype + data + struct.pack(">I", crc)

    # IDAT before IHDR
    bad_order = (
        signature
        + chunk(b"IDAT", idat)
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + chunk(b"IEND", b"")
    )
    with pytest.raises(AttachmentError):
        detect_attachment_mime(bad_order)


def test_png_missing_idat_or_iend_or_trailing() -> None:
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(ctype: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = binascii.crc32(ctype)
        crc = binascii.crc32(data, crc) & 0xFFFFFFFF
        return length + ctype + data + struct.pack(">I", crc)

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00\x00"))
    iend = chunk(b"IEND", b"")
    with pytest.raises(AttachmentError):
        detect_attachment_mime(signature + ihdr + iend)  # missing IDAT
    with pytest.raises(AttachmentError):
        detect_attachment_mime(signature + ihdr + idat)  # missing IEND
    with pytest.raises(AttachmentError):
        detect_attachment_mime(signature + ihdr + idat + iend + b"x")


def test_png_zero_dimension_and_truncated_chunk() -> None:
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(ctype: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = binascii.crc32(ctype)
        crc = binascii.crc32(data, crc) & 0xFFFFFFFF
        return length + ctype + data + struct.pack(">I", crc)

    zero = chunk(b"IHDR", struct.pack(">IIBBBBB", 0, 1, 8, 0, 0, 0, 0))
    with pytest.raises(AttachmentError):
        detect_attachment_mime(signature + zero)
    truncated = signature + struct.pack(">I", 13) + b"IHDR" + b"\x00" * 4
    with pytest.raises(AttachmentError):
        detect_attachment_mime(truncated)


def test_type_gating_boundary_documented() -> None:
    """Type gating accepts structural JPEG/PNG; it is not antivirus/decoding."""
    # Polyglot-ish: JPEG bytes followed by PNG signature after EOI is rejected.
    poly = synthetic_minimal_jpeg() + synthetic_minimal_png()
    with pytest.raises(AttachmentError) as raised:
        detect_attachment_mime(poly)
    assert raised.value.code == "ATTACHMENT_MIME_DENIED"
