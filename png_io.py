"""Minimal stdlib PNG reader/writer: 8-bit RGB/RGBA/grey, non-interlaced. Used by the overview stages."""
from __future__ import annotations

import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHANNELS = {0: 1, 2: 3, 6: 4}


class PngError(ValueError):
    pass


def decode(raw: bytes) -> tuple[int, int, int, bytearray]:
    """(width, height, channels, pixels) with pixels row-major, top row first."""
    if raw[:8] != SIGNATURE:
        raise PngError("not a PNG")
    pos, header, idat = 8, None, []
    while pos < len(raw):
        (length,) = struct.unpack(">I", raw[pos:pos + 4])
        kind = raw[pos + 4:pos + 8]
        data = raw[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", data)
        elif kind == b"IDAT":
            idat.append(data)
        elif kind == b"IEND":
            break
    if header is None:
        raise PngError("missing IHDR")
    width, height, depth, color, _, _, interlace = header
    if depth != 8 or color not in CHANNELS or interlace:
        raise PngError(f"unsupported PNG (depth {depth}, colour type {color}, interlace {interlace})")
    bpp = CHANNELS[color]
    stride = width * bpp
    data = zlib.decompress(b"".join(idat))
    if len(data) != (stride + 1) * height:
        raise PngError("PNG data length mismatch")
    out = bytearray(stride * height)
    prev = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind = data[start]
        row = bytearray(data[start + 1:start + 1 + stride])
        if kind == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif kind == 2:
            row = bytearray((a + b) & 0xFF for a, b in zip(row, prev))
        elif kind == 3:
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                row[i] = (row[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        elif kind != 0:
            raise PngError(f"unknown filter {kind}")
        out[y * stride:(y + 1) * stride] = row
        prev = row
    return width, height, bpp, out


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode(width: int, height: int, channels: int, pixels: bytes, level: int = 6) -> bytes:
    """PNG bytes; filter 0 per row (deterministic)."""
    color = {1: 0, 3: 2, 4: 6}[channels]
    stride = width * channels
    if len(pixels) != stride * height:
        raise PngError("pixel buffer size mismatch")
    body = b"".join(b"\x00" + bytes(pixels[y * stride:(y + 1) * stride]) for y in range(height))
    return (SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(body, level)) + _chunk(b"IEND", b""))
