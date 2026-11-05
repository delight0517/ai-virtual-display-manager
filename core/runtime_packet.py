"""Bounded v1 binary envelopes for task-scoped UI observation messages."""
from __future__ import annotations

import hashlib
import json
import re
import struct

from core.png_capture import (MAX_PNG_BYTES, encode_bgra_png, validate_encoded_png)

PROTOCOL_VERSION = 1
MAX_HEADER_BYTES = 64 * 1024
MAX_PACKET_BYTES = MAX_HEADER_BYTES + MAX_PNG_BYTES + 4
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class RuntimePacketError(ValueError):
    """Raised when a v1 observation packet is malformed or exceeds a bound."""


def _validate_header(header: object) -> dict:
    if not isinstance(header, dict):
        raise RuntimePacketError("Packet header must be an object")
    if type(header.get("protocol_version")) is not int or header["protocol_version"] != PROTOCOL_VERSION:
        raise RuntimePacketError("Unsupported runtime packet version")
    request_id = header.get("request_id")
    if not isinstance(request_id, str) or not UUID_RE.fullmatch(request_id):
        raise RuntimePacketError("Invalid request_id")
    session_id = header.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or not UUID_RE.fullmatch(session_id)):
        raise RuntimePacketError("Invalid session_id")
    action = header.get("action")
    if not isinstance(action, str) or not action or len(action) > 48:
        raise RuntimePacketError("Invalid action")
    return dict(header)


def encode_runtime_packet(header: dict, png: bytes = b"") -> bytes:
    """Encode a JSON v1 envelope plus optional PNG bytes without disk staging."""
    clean_header = _validate_header(header)
    if not isinstance(png, bytes):
        raise RuntimePacketError("PNG payload must be bytes")
    if len(png) > MAX_PNG_BYTES:
        raise RuntimePacketError("PNG payload exceeds the byte limit")
    if png:
        try:
            validate_encoded_png(png)
        except ValueError as exc:
            raise RuntimePacketError("Observation payload is not a valid bounded PNG") from exc

    clean_header["payload_bytes"] = len(png)
    if png:
        clean_header["content_type"] = "image/png"
        clean_header["payload_sha256"] = hashlib.sha256(png).hexdigest()
    else:
        clean_header.pop("content_type", None)
        clean_header.pop("payload_sha256", None)
    try:
        encoded_header = json.dumps(clean_header, ensure_ascii=False, allow_nan=False,
                                    separators=(",", ":"), sort_keys=True).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimePacketError("Packet metadata is not JSON encodable") from exc
    if len(encoded_header) > MAX_HEADER_BYTES:
        raise RuntimePacketError("Packet header exceeds the byte limit")
    body = struct.pack(">I", len(encoded_header)) + encoded_header + png
    if len(body) > MAX_PACKET_BYTES:
        raise RuntimePacketError("Packet exceeds the total byte limit")
    return struct.pack(">I", len(body)) + body


def decode_runtime_packet(packet: bytes) -> tuple[dict, bytes]:
    """Validate a complete framed packet and return its metadata and PNG bytes."""
    if not isinstance(packet, bytes) or len(packet) < 8:
        raise RuntimePacketError("Packet is truncated")
    packet_length = struct.unpack_from(">I", packet, 0)[0]
    if packet_length > MAX_PACKET_BYTES:
        raise RuntimePacketError("Packet exceeds the total byte limit")
    if packet_length != len(packet) - 4:
        raise RuntimePacketError("Packet length prefix does not match the received bytes")
    header_length = struct.unpack_from(">I", packet, 4)[0]
    if header_length < 1 or header_length > MAX_HEADER_BYTES or header_length > packet_length - 4:
        raise RuntimePacketError("Packet header length is invalid")
    header_end = 8 + header_length
    try:
        header = json.loads(packet[8:header_end].decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimePacketError("Packet header is not valid UTF-8 JSON") from exc
    header = _validate_header(header)
    png = packet[header_end:]
    if type(header.get("payload_bytes")) is not int or header["payload_bytes"] != len(png):
        raise RuntimePacketError("Packet payload length does not match its header")
    if png:
        if len(png) > MAX_PNG_BYTES or header.get("content_type") != "image/png":
            raise RuntimePacketError("Packet payload type or size is invalid")
        try:
            validate_encoded_png(png)
        except ValueError as exc:
            raise RuntimePacketError("Observation payload is not a valid bounded PNG") from exc
        digest = header.get("payload_sha256")
        if not isinstance(digest, str) or digest != hashlib.sha256(png).hexdigest():
            raise RuntimePacketError("Packet payload hash does not match")
    elif header.get("content_type") is not None or header.get("payload_sha256") is not None:
        raise RuntimePacketError("Empty packet cannot declare a media payload")
    return header, png


def read_runtime_packet(reader) -> tuple[dict, bytes]:
    """Read one bounded length-prefixed packet from a partial-read stream."""
    def read_exact(size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            part = reader.read(size - len(chunks))
            if not isinstance(part, bytes):
                raise RuntimePacketError("Packet stream returned a non-bytes chunk")
            if not part:
                raise RuntimePacketError("Packet stream ended before the frame was complete")
            if len(part) > size - len(chunks):
                raise RuntimePacketError("Packet stream returned more bytes than requested")
            chunks.extend(part)
        return bytes(chunks)

    prefix = read_exact(4)
    packet_length = struct.unpack(">I", prefix)[0]
    if packet_length < 4 or packet_length > MAX_PACKET_BYTES:
        raise RuntimePacketError("Packet stream length is outside the allowed bound")
    body = read_exact(packet_length)
    return decode_runtime_packet(prefix + body)


def runtime_packet_self_check() -> dict:
    """Exercise PNG validation and binary framing without persisting image bytes."""
    pixels = bytes((0, 0, 0, 255, 0, 0, 255, 255,
                    0, 255, 0, 255, 255, 0, 0, 255))
    png = encode_bgra_png(pixels, 2, 2)
    packet = encode_runtime_packet({
        "protocol_version": PROTOCOL_VERSION,
        "request_id": "2a62b388-3339-4e2c-8ff3-57314cb971ee",
        "session_id": "9053ad6d-604c-4f7f-8f3b-0b6058a60bfe",
        "action": "observe",
    }, png)
    import io
    header, decoded_png = read_runtime_packet(io.BytesIO(packet))
    width, height = validate_encoded_png(decoded_png)
    return {"passed": decoded_png == png and header["payload_bytes"] == len(png),
            "packet_bytes": len(packet), "payload_bytes": len(decoded_png),
            "width": width, "height": height,
            "payload_sha256": header["payload_sha256"]}
