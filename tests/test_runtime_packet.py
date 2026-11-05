from __future__ import annotations

import struct
import sys
import unittest
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.png_capture import PNG_SIGNATURE, encode_bgra_png
from core.runtime_packet import (
    MAX_HEADER_BYTES, MAX_PACKET_BYTES, PROTOCOL_VERSION, RuntimePacketError,
    decode_runtime_packet, encode_runtime_packet, read_runtime_packet,
    runtime_packet_self_check,
)


REQUEST_ID = "2a62b388-3339-4e2c-8ff3-57314cb971ee"
SESSION_ID = "9053ad6d-604c-4f7f-8f3b-0b6058a60bfe"
BASE_HEADER = {"protocol_version": PROTOCOL_VERSION, "request_id": REQUEST_ID,
               "session_id": SESSION_ID, "action": "observe"}
PNG = encode_bgra_png(bytes((0, 0, 0, 0, 0, 0, 255, 0,
                            0, 255, 0, 0, 255, 0, 0, 0)), 2, 2)


class RuntimePacketTests(unittest.TestCase):
    def test_metadata_only_packet_roundtrips_without_payload(self):
        packet = encode_runtime_packet({**BASE_HEADER, "state": "ready"})
        header, payload = decode_runtime_packet(packet)
        self.assertEqual(payload, b"")
        self.assertEqual(header["request_id"], REQUEST_ID)
        self.assertEqual(header["session_id"], SESSION_ID)
        self.assertEqual(header["payload_bytes"], 0)
        self.assertNotIn("content_type", header)

    def test_png_packet_roundtrips_binary_bytes_and_validated_hash(self):
        packet = encode_runtime_packet(BASE_HEADER, PNG)
        header, payload = decode_runtime_packet(packet)
        self.assertEqual(payload, PNG)
        self.assertEqual(header["content_type"], "image/png")
        self.assertEqual(header["payload_bytes"], len(PNG))
        self.assertEqual(len(packet), struct.unpack_from(">I", packet)[0] + 4)

    def test_utf8_header_roundtrips_korean_text(self):
        packet = encode_runtime_packet({**BASE_HEADER, "status_text": "작업을 준비 중입니다"})
        header, _ = decode_runtime_packet(packet)
        self.assertEqual(header["status_text"], "작업을 준비 중입니다")

    def test_rejects_invalid_envelope_identity_and_version(self):
        cases = [
            ({**BASE_HEADER, "protocol_version": True}, b""),
            ({**BASE_HEADER, "request_id": "nope"}, b""),
            ({**BASE_HEADER, "session_id": "nope"}, b""),
            ({**BASE_HEADER, "action": ""}, b""),
        ]
        for header, payload in cases:
            with self.subTest(header=header), self.assertRaises(RuntimePacketError):
                encode_runtime_packet(header, payload)

    def test_rejects_non_png_payload_and_non_bytes(self):
        for payload in (b"not a png", PNG_SIGNATURE, bytearray(PNG), "image"):
            with self.subTest(payload_type=type(payload).__name__), self.assertRaises(RuntimePacketError):
                encode_runtime_packet(BASE_HEADER, payload)

    def test_rejects_truncation_length_mismatch_and_oversize_packet(self):
        packet = encode_runtime_packet(BASE_HEADER, PNG)
        cases = [packet[:5], packet[:-1], struct.pack(">I", MAX_PACKET_BYTES + 1) + b"\0" * 8]
        for malformed in cases:
            with self.subTest(length=len(malformed)), self.assertRaises(RuntimePacketError):
                decode_runtime_packet(malformed)

    def test_rejects_header_payload_length_and_hash_tampering(self):
        packet = bytearray(encode_runtime_packet(BASE_HEADER, PNG))
        packet[-1] ^= 1
        with self.assertRaises(RuntimePacketError):
            decode_runtime_packet(bytes(packet))
        empty_packet = encode_runtime_packet(BASE_HEADER)
        tampered = bytearray(empty_packet)
        header_len = struct.unpack_from(">I", tampered, 4)[0]
        header = tampered[8:8 + header_len].replace(b'"payload_bytes":0', b'"payload_bytes":1')
        tampered = bytearray(struct.pack(">I", 4 + len(header)) + struct.pack(">I", len(header)) + header)
        with self.assertRaises(RuntimePacketError):
            decode_runtime_packet(bytes(tampered))

    def test_header_limit_is_bounded_on_encode_and_decode(self):
        header = {**BASE_HEADER, "padding": "x" * MAX_HEADER_BYTES}
        with self.assertRaises(RuntimePacketError):
            encode_runtime_packet(header)
        malformed = struct.pack(">II", 4 + MAX_HEADER_BYTES + 1,
                                MAX_HEADER_BYTES + 1) + b"x" * (MAX_HEADER_BYTES + 1)
        with self.assertRaises(RuntimePacketError):
            decode_runtime_packet(malformed)

    def test_candidate_codec_self_check_emits_metadata_only_receipt(self):
        report = runtime_packet_self_check()
        self.assertTrue(report["passed"])
        self.assertEqual((report["width"], report["height"]), (2, 2))
        self.assertGreater(report["packet_bytes"], report["payload_bytes"])
        self.assertEqual(len(report["payload_sha256"]), 64)
        self.assertNotIn("image", report)

    def test_stream_reader_handles_one_byte_reads_and_preserves_packet(self):
        class OneByteReader:
            def __init__(self, data):
                self.data = BytesIO(data)

            def read(self, _size=-1):
                return self.data.read(1)

        packet = encode_runtime_packet(BASE_HEADER, PNG)
        expected = decode_runtime_packet(packet)
        self.assertEqual(read_runtime_packet(OneByteReader(packet)), expected)

    def test_stream_reader_rejects_truncated_prefix_body_and_oversized_length(self):
        for data in (b"\0\0", struct.pack(">I", 20) + b"short",
                     struct.pack(">I", MAX_PACKET_BYTES + 1)):
            with self.subTest(data_len=len(data)), self.assertRaises(RuntimePacketError):
                read_runtime_packet(BytesIO(data))

    def test_stream_reader_rejects_a_non_binary_stream(self):
        class InvalidReader:
            def read(self, _size):
                return "not bytes"
        with self.assertRaises(RuntimePacketError):
            read_runtime_packet(InvalidReader())

    def test_stream_reader_rejects_a_stream_that_over_returns(self):
        class OverreadReader:
            def read(self, _size):
                return b"x" * 1024
        with self.assertRaisesRegex(RuntimePacketError, "more bytes than requested"):
            read_runtime_packet(OverreadReader())


if __name__ == "__main__":
    unittest.main(verbosity=2)
