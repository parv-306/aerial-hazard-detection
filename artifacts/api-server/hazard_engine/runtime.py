from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import socket
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

import asyncpg

from .grid import GRID_HEIGHT, GRID_WIDTH, HazardGrid, SITE_ORIGIN_LAT, SITE_ORIGIN_LON
from .protocol import FrameParser, TelemetryFrame, encode_frame

logger = logging.getLogger("hazard-engine")

REORDER_WINDOW_SECONDS = 2.0
HIGH_RISK_THRESHOLD = 65.0
ALERT_CLEAR_SECONDS = 300.0
STALE_AFTER_SECONDS = 10.0
NOTIFICATION_REPEAT_SECONDS = 30.0
SEQUENCE_SPACE = 65_536


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sequence_unwrap_distance(sequence: int, expected: int) -> int:
    return (sequence - expected) % SEQUENCE_SPACE


class SequenceReorderWindow:
    """Buffers forward gaps for up to two seconds and detects recent duplicates."""

    def __init__(self, window_seconds: float = REORDER_WINDOW_SECONDS) -> None:
        self.expected_sequence: int | None = None
        self.pending: dict[int, tuple[TelemetryFrame, float]] = {}
        self.seen: set[int] = set()
        self.seen_order: deque[int] = deque()
        self.window_seconds = window_seconds
        self.duplicates = 0
        self.reordered = 0

    def push(self, frame: TelemetryFrame, now: float) -> list[TelemetryFrame]:
        sequence = frame.sequence
        if sequence in self.seen or sequence in self.pending:
            self.duplicates += 1
            return []
        if self.expected_sequence is None:
            self.expected_sequence = sequence

        distance = _sequence_unwrap_distance(sequence, self.expected_sequence)
        if distance == 0:
            ready = [frame]
            self._remember(sequence)
            self.expected_sequence = (sequence + 1) % SEQUENCE_SPACE
            ready.extend(self._drain_consecutive())
            return ready
        if distance < SEQUENCE_SPACE // 2:
            self.pending[sequence] = (frame, now)
            self.reordered += 1
            return self._expire(now)

        # A packet older than the current cursor is only accepted while its
        # counterpart could still be inside the reorder window.
        self.duplicates += 1
        return []

    def _remember(self, sequence: int) -> None:
        self.seen.add(sequence)
        self.seen_order.append(sequence)
        while len(self.seen_order) > 4096:
            self.seen.discard(self.seen_order.popleft())

    def _drain_consecutive(self) -> list[TelemetryFrame]:
        ready: list[TelemetryFrame] = []
        while self.expected_sequence in self.pending:
            sequence = self.expected_sequence
            frame, _ = self.pending.pop(sequence)
            self._remember(sequence)
            ready.append(frame)
            self.expected_sequence = (sequence + 1) % SEQUENCE_SPACE
        return ready

    def _expire(self, now: float) -> list[TelemetryFrame]:
        ready: list[TelemetryFrame] = []
        while self.pending:
            expired = [
                sequence
                for sequence, (_, received_at) in self.pending.items()
                if now - received_at >= self.window_seconds
            ]
            if not expired:
                break
            sequence = min(
                expired,
                key=lambda item: _sequence_unwrap_distance(
                    item, self.expected_sequence if self.expected_sequence is not None else item
                ),
            )
            frame, _ = self.pending.pop(sequence)
            self._remember(sequence)
            self.expected_sequence = (sequence + 1) % SEQUENCE_SPACE
            ready.append(frame)
            ready.extend(self._drain_consecutive())
        return ready

    def flush_expired(self, now: float) -> list[TelemetryFrame]:
        return self._expire(now)


class FaultProxy(asyncio.DatagramProtocol):
    def __init__(self, engine: "HazardEngine", target: tuple[str, int]) -> None:
        self.engine = engine
        self.target = target
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, _address: tuple[str, int]) -> None:
        if self.engine.random.random() < 0.30:
            return
        forwarded = bytearray(data)
        if self.engine.corruption_enabled and self.engine.random.random() < 0.10 and forwarded:
            forwarded[self.engine.random.randrange(1, len(forwarded) - 2)] ^= 0x01
        delay = self.engine.random.uniform(0.0, 1.5)
        loop = asyncio.get_running_loop()
        loop.call_later(delay, self._forward, bytes(forwarded))
        if self.engine.random.random() < 0.10:
            duplicate_delay = min(1.5, delay + self.engine.random.uniform(0.01, 0.12))
            loop.call_later(duplicate_delay, self._forward, bytes(forwarded))

    def _forward(self, data: bytes) -> None:
        if self.transport is not None:
            self.transport.sendto(data, self.target)


class TelemetryReceiver(asyncio.DatagramProtocol):
    def __init__(self, engine: "HazardEngine") -> None:
        self.engine = engine

    def datagram_received(self, data: bytes, _address: tuple[str, int]) -> None:
        self.engine.received_datagrams += 1
        before_crc_failures = self.engine.parser.crc_failures
        frames = self.engine.parser.feed(data)
        self.engine.received_frames += len(frames)
        if self.engine.parser.crc_failures != before_crc_failures:
            self.engine.schedule_broadcast()
        now = time.monotonic()
        for frame in frames:
            for ordered in self.engine.reorder.push(frame, now):
                asyncio.create_task(self.engine.accept_frame(ordered))


class HazardEngine:
    def __init__(self) -> None:
        self.grid = HazardGrid()
        self.random = random.Random(2026)
        self.parser = FrameParser()
        self.reorder = SequenceReorderWindow()
        self.corruption_enabled = False
        self.simulator_task: asyncio.Task[None] | None = None
        self.background_tasks: list[asyncio.Task[None]] = []
        self.receiver_transport: asyncio.DatagramTransport | None = None
        self.proxy_transport: asyncio.DatagramTransport | None = None
        self.receiver_port = 0
        self.proxy_port = 0
        self.pool: asyncpg.Pool | None = None
        self.telemetry_queue: asyncio.Queue[tuple[Any, ...]] = asyncio.Queue(maxsize=2500)
        self.telemetry_queue_drops = 0
        self.telemetry_storage_failures = 0
        self.generated_packets = 0
        self.received_datagrams = 0
        self.received_frames = 0
        self.valid_frames = 0
        self.notification_events: deque[dict[str, Any]] = deque(maxlen=40)
        self.alerts: dict[str, dict[str, Any]] = {}
        self.active_alert_by_cell: dict[str, str] = {}
        self.high_frame_counts: dict[str, int] = {}
        self.websockets: set[Any] = set()
        self._lock = asyncio.Lock()
        self._shutdown = False
        self._route_cursor = 0
        self._sequence = 0
        self._drone_id = 1

    async def startup(self) -> None:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is required for durable alert and telemetry storage")
        self.pool = await asyncpg.create_pool(database_url, min_size=1, max_size=6)
        await self._recover_alerts()
        loop = asyncio.get_running_loop()
        receiver_transport, _ = await loop.create_datagram_endpoint(
            lambda: TelemetryReceiver(self),
            local_addr=("127.0.0.1", 0),
            family=socket.AF_INET,
        )
        self.receiver_transport = receiver_transport  # type: ignore[assignment]
        self.receiver_port = self.receiver_transport.get_extra_info("sockname")[1]
        proxy_transport, _ = await loop.create_datagram_endpoint(
            lambda: FaultProxy(self, ("127.0.0.1", self.receiver_port)),
            local_addr=("127.0.0.1", 0),
            family=socket.AF_INET,
        )
        self.proxy_transport = proxy_transport  # type: ignore[assignment]
        self.proxy_port = self.proxy_transport.get_extra_info("sockname")[1]
        self.background_tasks = [
            asyncio.create_task(self._telemetry_writer(), name="telemetry-batch-writer"),
            asyncio.create_task(self._maintenance_loop(), name="hazard-maintenance"),
        ]
        await self.start_simulator()
        logger.info("Hazard simulation engine started; synthetic predictions only")

    async def shutdown(self) -> None:
        self._shutdown = True
        await self.stop_simulator()
        for task in self.background_tasks:
            task.cancel()
        await asyncio.gather(*self.background_tasks, return_exceptions=True)
        for transport in (self.proxy_transport, self.receiver_transport):
            if transport is not None:
                transport.close()
        if self.pool is not None:
            await self.pool.close()

    async def _recover_alerts(self) -> None:
        assert self.pool is not None
        rows = await self.pool.fetch(
            """
            SELECT id, cell_id, state, level, risk_score, created_at,
                   acknowledged_at, acknowledged_by, last_notification_at,
                   notification_count, clearing_started_at
            FROM hazard_alerts
            ORDER BY created_at DESC
            LIMIT 100
            """
        )
        for row in reversed(rows):
            alert = self._alert_from_record(row)
            self.alerts[alert["id"]] = alert
            if alert["state"] != "NORMAL":
                self.active_alert_by_cell[alert["cellId"]] = alert["id"]

    @staticmethod
    def _alert_from_record(row: asyncpg.Record) -> dict[str, Any]:
        return {
            "id": row["id"],
            "cellId": row["cell_id"],
            "state": row["state"],
            "level": row["level"],
            "riskScore": float(row["risk_score"]),
            "createdAt": row["created_at"],
            "acknowledgedAt": row["acknowledged_at"],
            "acknowledgedBy": row["acknowledged_by"],
            "lastNotificationAt": row["last_notification_at"],
            "notificationCount": row["notification_count"],
            "clearingStartedAt": row["clearing_started_at"],
        }

    @staticmethod
    def _public_alert(alert: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": alert["id"],
            "cellId": alert["cellId"],
            "state": alert["state"],
            "level": alert["level"],
            "riskScore": alert["riskScore"],
            "createdAt": alert["createdAt"].isoformat(),
            "acknowledgedAt": (
                alert["acknowledgedAt"].isoformat() if alert["acknowledgedAt"] else None
            ),
            "acknowledgedBy": alert["acknowledgedBy"],
            "notificationCount": alert["notificationCount"],
        }

    async def _persist_alert(self, alert: dict[str, Any], *, connection: Any = None) -> None:
        assert self.pool is not None
        query = """
            INSERT INTO hazard_alerts (
                id, cell_id, state, level, risk_score, created_at, updated_at,
                acknowledged_at, acknowledged_by, last_notification_at,
                notification_count, clearing_started_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (id) DO UPDATE SET
                state = EXCLUDED.state,
                level = EXCLUDED.level,
                risk_score = EXCLUDED.risk_score,
                updated_at = EXCLUDED.updated_at,
                acknowledged_at = EXCLUDED.acknowledged_at,
                acknowledged_by = EXCLUDED.acknowledged_by,
                last_notification_at = EXCLUDED.last_notification_at,
                notification_count = EXCLUDED.notification_count,
                clearing_started_at = EXCLUDED.clearing_started_at
        """
        arguments = (
            alert["id"],
            alert["cellId"],
            alert["state"],
            alert["level"],
            alert["riskScore"],
            alert["createdAt"],
            _utc_now(),
            alert["acknowledgedAt"],
            alert["acknowledgedBy"],
            alert["lastNotificationAt"],
            alert["notificationCount"],
            alert["clearingStartedAt"],
        )
        if connection is not None:
            await connection.execute(query, *arguments)
        else:
            await self.pool.execute(query, *arguments)

    def _captured_datetime(self, timestamp_ms: int) -> datetime:
        current_ms = int(time.time() * 1000)
        current_mod = current_ms & 0xFFFFFFFF
        difference = ((timestamp_ms - current_mod + 0x80000000) & 0xFFFFFFFF) - 0x80000000
        return datetime.fromtimestamp((current_ms + difference) / 1000, timezone.utc)

    async def accept_frame(self, frame: TelemetryFrame) -> None:
        captured_at = self._captured_datetime(frame.timestamp_ms)
        async with self._lock:
            cell = self.grid.process_reading(
                latitude=frame.latitude,
                longitude=frame.longitude,
                raw_sensors=frame.sensors,
                captured_at=captured_at,
            )
            self.valid_frames += 1
            try:
                self.telemetry_queue.put_nowait(
                    (
                        frame.drone_id,
                        frame.sequence,
                        captured_at,
                        frame.latitude,
                        frame.longitude,
                        json.dumps(cell["sensors"]),
                        float(cell["riskScore"]),
                    )
                )
            except asyncio.QueueFull:
                self.telemetry_queue_drops += 1
            await self._evaluate_cell_alert(cell)
        self.schedule_broadcast()

    async def _evaluate_cell_alert(self, cell: dict[str, Any]) -> None:
        cell_id = cell["id"]
        risk_score = float(cell["riskScore"])
        alert_id = self.active_alert_by_cell.get(cell_id)
        high = risk_score >= HIGH_RISK_THRESHOLD

        if alert_id is None:
            self.high_frame_counts[cell_id] = (
                self.high_frame_counts.get(cell_id, 0) + 1 if high else 0
            )
            if self.high_frame_counts[cell_id] == 0:
                cell["alertState"] = "NORMAL"
            elif self.high_frame_counts[cell_id] < 3:
                cell["alertState"] = "PENDING"
            else:
                await self._create_alert(cell)
            return

        alert = self.alerts[alert_id]
        alert["riskScore"] = risk_score
        if high and alert["state"] == "CLEARING":
            alert["state"] = "ACKED"
            alert["clearingStartedAt"] = None
        if not high and alert["acknowledgedAt"] is not None:
            if alert["clearingStartedAt"] is None:
                alert["state"] = "CLEARING"
                alert["clearingStartedAt"] = _utc_now()
        cell["alertState"] = alert["state"]
        await self._persist_alert(alert)

    async def _create_alert(self, cell: dict[str, Any]) -> None:
        now = _utc_now()
        alert = {
            "id": str(uuid.uuid4()),
            "cellId": cell["id"],
            "state": "ALERT",
            "level": 1,
            "riskScore": float(cell["riskScore"]),
            "createdAt": now,
            "acknowledgedAt": None,
            "acknowledgedBy": None,
            "lastNotificationAt": now,
            "notificationCount": 1,
            "clearingStartedAt": None,
        }
        await self._persist_alert(alert)
        self.alerts[alert["id"]] = alert
        self.active_alert_by_cell[cell["id"]] = alert["id"]
        self.high_frame_counts[cell["id"]] = 3
        cell["alertState"] = "ALERT"
        self.notification_events.append(
            {
                "type": "alert",
                "alertId": alert["id"],
                "cellId": cell["id"],
                "level": 1,
                "message": f"Simulated elevated risk in {cell['id']}",
                "createdAt": now.isoformat(),
            }
        )

    async def acknowledge_alert(self, alert_id: str, acknowledged_by: str) -> dict[str, Any] | None:
        acknowledged_by = acknowledged_by.strip()
        if not acknowledged_by:
            raise ValueError("Operator name cannot be empty")
        alert = self.alerts.get(alert_id)
        if alert is None:
            return None
        async with self._lock:
            if alert["state"] == "NORMAL":
                return self._public_alert(alert)
            now = _utc_now()
            assert self.pool is not None
            async with self.pool.acquire() as connection:
                async with connection.transaction():
                    alert["acknowledgedAt"] = now
                    alert["acknowledgedBy"] = acknowledged_by[:80]
                    alert["state"] = "ACKED"
                    alert["clearingStartedAt"] = None
                    alert["lastNotificationAt"] = now
                    await self._persist_alert(alert, connection=connection)
            cell = self.grid.cells.get(alert["cellId"])
            if cell is not None:
                cell["alertState"] = "ACKED"
            self.notification_events.append(
                {
                    "type": "acknowledged",
                    "alertId": alert_id,
                    "cellId": alert["cellId"],
                    "message": f"Acknowledged by {acknowledged_by[:80]}",
                    "createdAt": now.isoformat(),
                }
            )
        self.schedule_broadcast()
        return self._public_alert(alert)

    async def _telemetry_writer(self) -> None:
        assert self.pool is not None
        while not self._shutdown:
            first = await self.telemetry_queue.get()
            batch = [first]
            deadline = time.monotonic() + 1.0
            while len(batch) < 100 and time.monotonic() < deadline:
                try:
                    batch.append(self.telemetry_queue.get_nowait())
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.02)
            try:
                async with self.pool.acquire() as connection:
                    async with connection.transaction():
                        await connection.executemany(
                            """
                            INSERT INTO hazard_telemetry (
                                drone_id, sequence, captured_at, latitude, longitude, sensors, risk_score
                            ) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7)
                            """,
                            batch,
                        )
            except Exception:
                self.telemetry_storage_failures += 1
                logger.exception("Telemetry batch write failed; batch discarded")
            finally:
                for _ in batch:
                    self.telemetry_queue.task_done()

    async def _maintenance_loop(self) -> None:
        while not self._shutdown:
            now_mono = time.monotonic()
            for frame in self.reorder.flush_expired(now_mono):
                await self.accept_frame(frame)
            self.grid.update_weather()
            await self._maintain_alerts(now_mono)
            await asyncio.sleep(0.25)

    async def _maintain_alerts(self, now_mono: float) -> None:
        now = _utc_now()
        now_timestamp = now.timestamp()
        any_changed = False
        for alert in list(self.alerts.values()):
            if alert["state"] == "NORMAL":
                continue
            cell = self.grid.cells.get(alert["cellId"])
            if cell is None:
                continue
            last_seen = cell["_lastSeen"]
            stale = last_seen is None or now_mono - last_seen >= STALE_AFTER_SECONDS
            if stale and alert["state"] == "CLEARING":
                alert["state"] = "ACKED"
                alert["clearingStartedAt"] = None
                await self._persist_alert(alert)
                cell["alertState"] = "ACKED"
                any_changed = True
                continue

            age = now_timestamp - alert["createdAt"].timestamp()
            changed = False
            if alert["acknowledgedAt"] is None:
                next_level = 3 if age >= 300 else 2 if age >= 120 else 1
                if next_level > alert["level"]:
                    alert["level"] = next_level
                    alert["state"] = "ESCALATED"
                    changed = True
                    self.notification_events.append(
                        {
                            "type": "escalated",
                            "alertId": alert["id"],
                            "cellId": alert["cellId"],
                            "level": next_level,
                            "message": f"Simulated risk alert escalated to level {next_level}",
                            "createdAt": now.isoformat(),
                        }
                    )
                last_notification = alert["lastNotificationAt"]
                if last_notification is None or now_timestamp - last_notification.timestamp() >= NOTIFICATION_REPEAT_SECONDS:
                    alert["notificationCount"] += 1
                    alert["lastNotificationAt"] = now
                    changed = True
                    self.notification_events.append(
                        {
                            "type": "repeat",
                            "alertId": alert["id"],
                            "cellId": alert["cellId"],
                            "level": alert["level"],
                            "message": f"Unacknowledged simulation alert · level {alert['level']}",
                            "createdAt": now.isoformat(),
                        }
                    )
            elif alert["state"] == "CLEARING" and alert["clearingStartedAt"] is not None:
                if now_timestamp - alert["clearingStartedAt"].timestamp() >= ALERT_CLEAR_SECONDS:
                    alert["state"] = "NORMAL"
                    alert["clearingStartedAt"] = None
                    self.active_alert_by_cell.pop(alert["cellId"], None)
                    cell["alertState"] = "NORMAL"
                    changed = True
                    self.notification_events.append(
                        {
                            "type": "cleared",
                            "alertId": alert["id"],
                            "cellId": alert["cellId"],
                            "message": f"Simulated alert cleared for {alert['cellId']}",
                            "createdAt": now.isoformat(),
                        }
                    )
            if changed:
                cell["alertState"] = alert["state"]
                await self._persist_alert(alert)
                any_changed = True
        if any_changed:
            self.schedule_broadcast()

    def _simulated_route(self) -> list[tuple[int, int]]:
        center = (GRID_WIDTH // 2, GRID_HEIGHT // 2)
        route = [
            (x, y)
            for y in range(GRID_HEIGHT)
            for x in range(GRID_WIDTH)
            if (x, y) != center
        ]
        return route + [center] * 4

    def _coordinates_for_cell(self, x: int, y: int) -> tuple[float, float]:
        latitude = SITE_ORIGIN_LAT + ((y + 0.5) * 10 / 110_540)
        longitude = SITE_ORIGIN_LON + (
            (x + 0.5) * 10 / (111_320 * math.cos(math.radians(SITE_ORIGIN_LAT)))
        )
        return latitude, longitude

    async def _run_simulator(self) -> None:
        loop = asyncio.get_running_loop()
        route = self._simulated_route()
        while not self._shutdown:
            x, y = route[self._route_cursor % len(route)]
            cell_id = f"R{y + 1}-C{x + 1}"
            self._route_cursor += 1
            hotspot = (x, y) == (GRID_WIDTH // 2, GRID_HEIGHT // 2)
            noise = self.random.uniform(-2.0, 2.0)
            if hotspot:
                values = (
                    26 + noise,
                    640 + noise * 8,
                    88 + noise * 2,
                    61 + noise,
                    230 + noise * 3,
                    470 + noise * 5,
                )
            else:
                values = (
                    5 + abs(noise) * 0.2,
                    90 + abs(noise) * 5,
                    18 + abs(noise),
                    12 + abs(noise) * 0.4,
                    72 + abs(noise) * 3,
                    130 + abs(noise) * 4,
                )
            raw_sensors = tuple(min(65_535, max(0, round(value * 100))) for value in values)
            latitude, longitude = self._coordinates_for_cell(x, y)
            frame = encode_frame(
                drone_id=self._drone_id,
                sequence=self._sequence,
                timestamp_ms=int(time.time() * 1000) & 0xFFFFFFFF,
                latitude=latitude,
                longitude=longitude,
                sensors=raw_sensors,  # type: ignore[arg-type]
                status_flags=1 if hotspot else 0,
            )
            self.generated_packets += 1
            self._sequence = (self._sequence + 1) & 0xFFFF
            self._drone_id = (self._drone_id % 4) + 1
            if self.proxy_transport is not None:
                self.proxy_transport.sendto(frame, ("127.0.0.1", self.proxy_port))
            await asyncio.sleep(0.32)

    async def start_simulator(self) -> dict[str, Any]:
        if self.simulator_task is None or self.simulator_task.done():
            self.simulator_task = asyncio.create_task(self._run_simulator(), name="drone-simulator")
        return self.simulator_status()

    async def stop_simulator(self) -> dict[str, Any]:
        if self.simulator_task is not None:
            self.simulator_task.cancel()
            await asyncio.gather(self.simulator_task, return_exceptions=True)
            self.simulator_task = None
        return self.simulator_status()

    def set_corruption(self, enabled: bool) -> dict[str, Any]:
        self.corruption_enabled = enabled
        return self.simulator_status("Optional packet corruption enabled" if enabled else "Optional packet corruption disabled")

    def simulator_status(self, message: str | None = None) -> dict[str, Any]:
        running = self.simulator_task is not None and not self.simulator_task.done()
        return {
            "running": running,
            "corruptionEnabled": self.corruption_enabled,
            "message": message or ("Simulator running" if running else "Simulator stopped"),
        }

    def network_stats(self) -> dict[str, Any]:
        estimated_loss = (
            max(0, self.generated_packets - self.valid_frames) / self.generated_packets * 100
            if self.generated_packets
            else 0.0
        )
        return {
            "received": self.received_frames,
            "valid": self.valid_frames,
            "crcFailures": self.parser.crc_failures,
            "duplicates": self.reorder.duplicates,
            "reordered": self.reorder.reordered,
            "estimatedLossPercent": round(min(100.0, estimated_loss), 1),
            "lastSequence": (self._sequence - 1) & 0xFFFF if self.generated_packets else None,
            "generated": self.generated_packets,
            "queueDrops": self.telemetry_queue_drops,
            "telemetryStorageFailures": self.telemetry_storage_failures,
        }

    def get_alerts(self) -> list[dict[str, Any]]:
        return [
            self._public_alert(alert)
            for alert in sorted(
                self.alerts.values(),
                key=lambda item: item["createdAt"],
                reverse=True,
            )[:50]
        ]

    def dashboard(self) -> dict[str, Any]:
        alerts = self.get_alerts()
        active_count = sum(alert["state"] != "NORMAL" for alert in alerts)
        return {
            "simulated": True,
            "simulatorRunning": self.simulator_task is not None and not self.simulator_task.done(),
            "corruptionEnabled": self.corruption_enabled,
            "modelStatus": "SVM trained on synthetic demo data · not field validated",
            "activeAlertCount": active_count,
            "cells": self.grid.snapshot(),
            "alerts": alerts,
            "network": self.network_stats(),
            "weather": dict(self.grid.weather),
            "notificationEvents": list(self.notification_events),
        }

    def schedule_broadcast(self) -> None:
        if not self.websockets:
            return
        asyncio.create_task(self.broadcast())

    async def broadcast(self) -> None:
        payload = self.dashboard()
        disconnected: list[Any] = []
        for websocket in list(self.websockets):
            try:
                await websocket.send_json(payload)
            except Exception:
                disconnected.append(websocket)
        for websocket in disconnected:
            self.websockets.discard(websocket)

    def legacy_status(self) -> dict[str, Any]:
        return {
            "available": False,
            "message": "No legacy pipeline was supplied with this demo project.",
        }
