"""Small, bounded PNG encoding helpers for task-owned window captures."""
from __future__ import annotations

import struct
import zlib

MAX_CAPTURE_PIXELS = 4_000_000
MAX_PNG_BYTES = 4 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MIN_CAPTURE_SAMPLE_COLORS = 3


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def encode_bgra_png(pixels: bytes, width: int, height: int) -> bytes:
    """Encode top-down 32-bit BGRA pixels to bounded 8-bit RGBA PNG bytes."""
    if type(width) is not int or type(height) is not int or width < 1 or height < 1:
        raise ValueError("Capture dimensions must be positive integers")
    pixel_count = width * height
    if pixel_count > MAX_CAPTURE_PIXELS:
        raise ValueError("Capture exceeds the pixel limit")
    if not isinstance(pixels, bytes) or len(pixels) != pixel_count * 4:
        raise ValueError("BGRA buffer length does not match the capture dimensions")

    rows = bytearray()
    for y in range(height):
        rows.append(0)  # PNG filter: None
        offset = y * width * 4
        row = pixels[offset:offset + width * 4]
        for x in range(0, len(row), 4):
            rows.extend((row[x + 2], row[x + 1], row[x], 255))

    compressed = zlib.compress(bytes(rows), level=6)
    data = (PNG_SIGNATURE +
            _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) +
            _chunk(b"IDAT", compressed) +
            _chunk(b"IEND", b""))
    if len(data) > MAX_PNG_BYTES:
        raise ValueError("Encoded screenshot exceeds the byte limit")
    return data


def sampled_bgra_color_count(pixels: bytes, width: int, height: int,
                             columns: int = 12, rows: int = 12) -> int:
    """Count distinct sampled RGB colors from a validated top-down BGRA frame."""
    if type(width) is not int or type(height) is not int or width < 1 or height < 1:
        raise ValueError("Capture dimensions must be positive integers")
    if width * height > MAX_CAPTURE_PIXELS:
        raise ValueError("Capture exceeds the pixel limit")
    if not isinstance(pixels, bytes) or len(pixels) != width * height * 4:
        raise ValueError("BGRA buffer length does not match the capture dimensions")
    if type(columns) is not int or type(rows) is not int or columns < 1 or rows < 1:
        raise ValueError("Sample grid must have positive dimensions")

    colors = set()
    for row in range(rows):
        y = round(row * (height - 1) / max(1, rows - 1))
        for column in range(columns):
            x = round(column * (width - 1) / max(1, columns - 1))
            offset = (y * width + x) * 4
            blue, green, red = pixels[offset:offset + 3]
            color = (red, green, blue)
            if color != (255, 255, 255):
                colors.add(color)
    return len(colors)


def require_contentful_bgra_capture(pixels: bytes, width: int, height: int) -> int:
    """Reject blank or near-uniform captures before returning them to a consumer."""
    count = sampled_bgra_color_count(pixels, width, height)
    if count < MIN_CAPTURE_SAMPLE_COLORS:
        raise ValueError("CAPTURE_CONTENT_UNVERIFIED")
    return count


def validate_encoded_png(png: bytes) -> tuple[int, int]:
    """Validate the exact bounded RGBA/8-bit PNG profile emitted by this module."""
    if not isinstance(png, bytes) or len(png) > MAX_PNG_BYTES or not png.startswith(PNG_SIGNATURE):
        raise ValueError("PNG signature or byte length is invalid")
    offset = len(PNG_SIGNATURE)
    width = height = None
    compressed = bytearray()
    seen_idat = False
    idat_closed = False
    seen_iend = False
    while offset < len(png):
        if len(png) - offset < 12:
            raise ValueError("PNG chunk is truncated")
        size = struct.unpack_from(">I", png, offset)[0]
        kind = png[offset + 4:offset + 8]
        end = offset + 12 + size
        if end > len(png):
            raise ValueError("PNG chunk length is invalid")
        data = png[offset + 8:offset + 8 + size]
        expected_crc = struct.unpack_from(">I", png, offset + 8 + size)[0]
        if expected_crc != (zlib.crc32(kind + data) & 0xFFFFFFFF):
            raise ValueError("PNG chunk checksum is invalid")
        if width is None:
            if kind != b"IHDR" or size != 13:
                raise ValueError("PNG must start with one valid IHDR")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", data)
            if (width < 1 or height < 1 or width * height > MAX_CAPTURE_PIXELS or
                    (depth, color, compression, filtering, interlace) != (8, 6, 0, 0, 0)):
                raise ValueError("PNG dimensions or pixel format are unsupported")
        elif kind == b"IDAT":
            if idat_closed or seen_iend:
                raise ValueError("PNG IDAT chunks are out of order")
            seen_idat = True
            if len(compressed) + size > MAX_PNG_BYTES:
                raise ValueError("PNG compressed data exceeds the byte limit")
            compressed.extend(data)
        elif kind == b"IEND":
            if size != 0 or not seen_idat or seen_iend:
                raise ValueError("PNG IEND is invalid")
            seen_iend = True
            if end != len(png):
                raise ValueError("PNG has bytes after IEND")
        else:
            if kind == b"IHDR" or seen_iend:
                raise ValueError("PNG contains an invalid or unsupported chunk")
            if seen_idat:
                idat_closed = True
            # This encoder emits no ancillary or unknown chunks.
            raise ValueError("PNG contains an unsupported chunk")
        offset = end
    if width is None or height is None or not seen_iend:
        raise ValueError("PNG is missing required chunks")

    expected_raw_bytes = height * (1 + width * 4)
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(bytes(compressed), expected_raw_bytes + 1)
    except zlib.error as exc:
        raise ValueError("PNG image data is not valid zlib data") from exc
    if (len(raw) != expected_raw_bytes or inflater.unconsumed_tail or
            inflater.unused_data or not inflater.eof):
        raise ValueError("PNG decompressed image size does not match its dimensions")
    stride = 1 + width * 4
    if any(raw[row * stride] != 0 for row in range(height)):
        raise ValueError("PNG uses an unsupported row filter")
    return width, height
