from __future__ import annotations

import struct
from dataclasses import dataclass

SYNC_BYTE = 0xA5
FRAME_LENGTH = 32
_FRAME_BODY = struct.Struct("!BBHIii6HH")
_FRAME = struct.Struct("!BBHIii6HHH")


@dataclass(frozen=True, slots=True)
class TelemetryFrame:
    drone_id: int
    sequence: int
    timestamp_ms: int
    latitude: float
    longitude: float
    sensors: tuple[int, int, int, int, int, int]
    status_flags: int


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xffff, no reflection/xorout."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode_frame(
    *,
    drone_id: int,
    sequence: int,
    timestamp_ms: int,
    latitude: float,
    longitude: float,
    sensors: tuple[int, int, int, int, int, int],
    status_flags: int = 0,
) -> bytes:
    if len(sensors) != 6:
        raise ValueError("Exactly six sensor values are required")
    body = _FRAME_BODY.pack(
        SYNC_BYTE,
        drone_id,
        sequence & 0xFFFF,
        timestamp_ms & 0xFFFFFFFF,
        round(latitude * 10_000_000),
        round(longitude * 10_000_000),
        *sensors,
        status_flags & 0xFFFF,
    )
    frame = body + struct.pack("!H", crc16_ccitt_false(body))
    if len(frame) != FRAME_LENGTH:
        raise AssertionError(f"Encoded frame must be {FRAME_LENGTH} bytes, got {len(frame)}")
    return frame


def decode_frame(raw: bytes) -> TelemetryFrame:
    if len(raw) != FRAME_LENGTH:
        raise ValueError(f"Expected {FRAME_LENGTH} bytes, got {len(raw)}")
    if raw[0] != SYNC_BYTE:
        raise ValueError("Invalid sync byte")
    if crc16_ccitt_false(raw[:30]) != struct.unpack("!H", raw[30:])[0]:
        raise ValueError("CRC-16/CCITT-FALSE mismatch")
    values = _FRAME.unpack(raw)
    return TelemetryFrame(
        drone_id=values[1],
        sequence=values[2],
        timestamp_ms=values[3],
        latitude=values[4] / 10_000_000,
        longitude=values[5] / 10_000_000,
        sensors=tuple(values[6:12]),  # type: ignore[arg-type]
        status_flags=values[12],
    )


class FrameParser:
    """Incremental byte parser with CRC rejection and sync-byte resynchronization."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.crc_failures = 0
        self.discarded_bytes = 0

    def feed(self, data: bytes) -> list[TelemetryFrame]:
        frames: list[TelemetryFrame] = []
        for byte in data:
            self._buffer.append(byte)
            while True:
                try:
                    sync_index = self._buffer.index(SYNC_BYTE)
                except ValueError:
                    self.discarded_bytes += len(self._buffer)
                    self._buffer.clear()
                    break
                if sync_index:
                    self.discarded_bytes += sync_index
                    del self._buffer[:sync_index]
                if len(self._buffer) < FRAME_LENGTH:
                    break
                candidate = bytes(self._buffer[:FRAME_LENGTH])
                try:
                    frame = decode_frame(candidate)
                except ValueError as error:
                    if "CRC" in str(error):
                        self.crc_failures += 1
                    self.discarded_bytes += 1
                    del self._buffer[0]
                    continue
                frames.append(frame)
                del self._buffer[:FRAME_LENGTH]
        return frames
