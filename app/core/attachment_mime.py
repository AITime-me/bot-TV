"""Server-side JPEG/PNG type gating for attachment spool Stage 1A1.

This is allowlist type gating only — not antivirus, not a full image decoder,
and not a guarantee against all exotic polyglots.
"""

from __future__ import annotations

import binascii
import struct
from typing import Final

from app.core.attachment_types import (
    MAX_PLAINTEXT_BYTES,
    AttachmentError,
    AttachmentMime,
)

_PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"
_JPEG_SOI: Final[bytes] = b"\xff\xd8"
_JPEG_EOI: Final[bytes] = b"\xff\xd9"

# SOF markers that define a frame (baseline/progressive/etc.).
_JPEG_SOF_MARKERS: Final[frozenset[int]] = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)

# Markers without a length field.
_JPEG_STANDALONE: Final[frozenset[int]] = frozenset(
    {
        0xD0,
        0xD1,
        0xD2,
        0xD3,
        0xD4,
        0xD5,
        0xD6,
        0xD7,  # RST
        0xD8,  # SOI
        0xD9,  # EOI
        0x01,  # TEM
    }
)


def detect_attachment_mime(data: object) -> AttachmentMime:
    """Detect MIME from bytes. Filename/extension/Content-Type are ignored."""
    if type(data) is not bytes:
        raise AttachmentError("ATTACHMENT_VALUE_INVALID") from None
    if data == b"":
        raise AttachmentError("ATTACHMENT_VALUE_INVALID") from None
    if len(data) > MAX_PLAINTEXT_BYTES:
        raise AttachmentError("ATTACHMENT_TOO_LARGE") from None
    if data.startswith(_JPEG_SOI):
        if _is_valid_jpeg(data):
            return AttachmentMime.IMAGE_JPEG
        raise AttachmentError("ATTACHMENT_MIME_DENIED") from None
    if data.startswith(_PNG_SIGNATURE):
        if _is_valid_png(data):
            return AttachmentMime.IMAGE_PNG
        raise AttachmentError("ATTACHMENT_MIME_DENIED") from None
    raise AttachmentError("ATTACHMENT_MIME_DENIED") from None


def _is_valid_jpeg(data: bytes) -> bool:
    if len(data) < 4 or data[:2] != _JPEG_SOI:
        return False
    i = 2
    saw_sof = False
    while i < len(data):
        if data[i] != 0xFF:
            return False
        # Skip fill bytes.
        while i < len(data) and data[i] == 0xFF:
            i += 1
        if i >= len(data):
            return False
        marker = data[i]
        i += 1
        if marker == 0xD9:  # EOI
            # No trailing payload after EOI.
            return saw_sof and i == len(data)
        if marker == 0xDA:  # SOS
            if not saw_sof:
                return False
            if i + 2 > len(data):
                return False
            seg_len = struct.unpack(">H", data[i : i + 2])[0]
            if seg_len < 2 or i + seg_len > len(data):
                return False
            i += seg_len
            # Entropy-coded segment until EOI, respecting stuffed FF 00 and RST.
            while i < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                if i + 1 >= len(data):
                    return False
                nxt = data[i + 1]
                if nxt == 0x00:
                    i += 2
                    continue
                if 0xD0 <= nxt <= 0xD7:
                    i += 2
                    continue
                if nxt == 0xD9:
                    return (i + 2) == len(data)
                # Unexpected marker inside scan.
                return False
            return False
        if marker in _JPEG_STANDALONE:
            # SOI already consumed at start; unexpected standalone here.
            return False
        if i + 2 > len(data):
            return False
        seg_len = struct.unpack(">H", data[i : i + 2])[0]
        if seg_len < 2 or i + seg_len > len(data):
            return False
        if marker in _JPEG_SOF_MARKERS:
            # Minimal SOF: Lf(2)+P(1)+Y(2)+X(2)+Nf(1) = 8.
            if seg_len < 8:
                return False
            saw_sof = True
        i += seg_len
    return False


def _is_valid_png(data: bytes) -> bool:
    if len(data) < 8 or data[:8] != _PNG_SIGNATURE:
        return False
    offset = 8
    saw_ihdr = False
    saw_idat = False
    saw_iend = False
    first_chunk = True
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        ctype = data[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(data):
            return False
        chunk_data = data[data_start:data_end]
        declared_crc = struct.unpack(">I", data[data_end:crc_end])[0]
        actual_crc = binascii.crc32(ctype)
        actual_crc = binascii.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if declared_crc != actual_crc:
            return False

        if first_chunk:
            if ctype != b"IHDR" or length != 13:
                return False
            width, height = struct.unpack(">II", chunk_data[:8])
            if width == 0 or height == 0:
                return False
            saw_ihdr = True
            first_chunk = False
        else:
            if ctype == b"IHDR":
                return False
            if ctype == b"IDAT":
                if length == 0:
                    return False
                saw_idat = True
            if ctype == b"IEND":
                if length != 0 or saw_iend:
                    return False
                saw_iend = True
                # No trailing payload after IEND.
                return saw_ihdr and saw_idat and crc_end == len(data)

        offset = crc_end

    return False
