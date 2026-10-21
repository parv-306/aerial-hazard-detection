import unittest

from hazard_engine.protocol import (
    FRAME_LENGTH,
    FrameParser,
    crc16_ccitt_false,
    decode_frame,
    encode_frame,
)


def make_frame(sequence: int = 7) -> bytes:
    return encode_frame(
        drone_id=3,
        sequence=sequence,
        timestamp_ms=1_700_000_123,
        latitude=19.076,
        longitude=72.8777,
        sensors=(100, 200, 300, 400, 500, 600),
        status_flags=0x0003,
    )


class ProtocolTests(unittest.TestCase):
    def test_crc_known_vector(self) -> None:
        self.assertEqual(crc16_ccitt_false(b"123456789"), 0x29B1)

    def test_frame_is_exactly_32_bytes_and_round_trips(self) -> None:
        raw = make_frame()
        self.assertEqual(len(raw), FRAME_LENGTH)
        frame = decode_frame(raw)
        self.assertEqual(frame.sequence, 7)
        self.assertEqual(frame.drone_id, 3)
        self.assertEqual(frame.latitude, 19.076)
        self.assertEqual(frame.longitude, 72.8777)
        self.assertEqual(frame.sensors, (100, 200, 300, 400, 500, 600))
        self.assertEqual(frame.status_flags, 3)

    def test_parser_accepts_incremental_chunks(self) -> None:
        raw = make_frame()
        parser = FrameParser()
        self.assertEqual(parser.feed(raw[:8]), [])
        self.assertEqual(parser.feed(raw[8:21]), [])
        frames = parser.feed(raw[21:])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].sequence, 7)

    def test_parser_rejects_corruption_and_resynchronizes(self) -> None:
        damaged = bytearray(make_frame(8))
        damaged[19] ^= 0x10
        parser = FrameParser()
        frames = parser.feed(b"\x00\x10" + bytes(damaged) + make_frame(9))
        self.assertEqual([frame.sequence for frame in frames], [9])
        self.assertEqual(parser.crc_failures, 1)
        self.assertGreaterEqual(parser.discarded_bytes, FRAME_LENGTH + 2)

    def test_sequence_wraparound_encodes_as_uint16(self) -> None:
        self.assertEqual(decode_frame(make_frame(65_536)).sequence, 0)


if __name__ == "__main__":
    unittest.main()
