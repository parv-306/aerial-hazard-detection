import asyncio
import math
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from hazard_engine.grid import HazardGrid, SITE_ORIGIN_LAT, SITE_ORIGIN_LON
from hazard_engine.protocol import TelemetryFrame
from hazard_engine.runtime import HazardEngine, SequenceReorderWindow


def frame(sequence: int) -> TelemetryFrame:
    return TelemetryFrame(
        drone_id=1,
        sequence=sequence,
        timestamp_ms=123_000,
        latitude=SITE_ORIGIN_LAT + (25 / 110_540),
        longitude=SITE_ORIGIN_LON
        + (25 / (111_320 * math.cos(math.radians(SITE_ORIGIN_LAT)))),
        sensors=(500, 20_000, 7_000, 5_000, 10_000, 15_000),
        status_flags=0,
    )


class ProtocolWindowTests(unittest.TestCase):
    def test_reorders_within_window_and_detects_duplicate(self) -> None:
        window = SequenceReorderWindow(window_seconds=2)
        self.assertEqual([f.sequence for f in window.push(frame(10), 0.0)], [10])
        self.assertEqual(window.push(frame(12), 0.2), [])
        self.assertEqual([f.sequence for f in window.push(frame(11), 0.4)], [11, 12])
        self.assertEqual(window.push(frame(11), 0.5), [])
        self.assertEqual(window.reordered, 1)
        self.assertEqual(window.duplicates, 1)

    def test_gap_is_flushed_after_two_second_window(self) -> None:
        window = SequenceReorderWindow(window_seconds=2)
        window.push(frame(10), 0.0)
        self.assertEqual(window.push(frame(12), 0.2), [])
        self.assertEqual([f.sequence for f in window.flush_expired(2.3)], [12])

    def test_sequence_wraparound_is_ordered(self) -> None:
        window = SequenceReorderWindow()
        self.assertEqual([f.sequence for f in window.push(frame(65_534), 0)], [65_534])
        self.assertEqual(window.push(frame(0), 0.1), [])
        self.assertEqual([f.sequence for f in window.push(frame(65_535), 0.2)], [65_535, 0])


class GridTests(unittest.TestCase):
    def test_local_projection_indexes_ten_metre_cells(self) -> None:
        grid = HazardGrid()
        center = grid.cell_for_coordinates(
            SITE_ORIGIN_LAT + (25 / 110_540),
            SITE_ORIGIN_LON
            + (25 / (111_320 * math.cos(math.radians(SITE_ORIGIN_LAT)))),
        )
        self.assertEqual(center["id"], "R3-C3")

    def test_eight_neighbor_risk_is_used_as_a_feature(self) -> None:
        grid = HazardGrid()
        now = time.monotonic()
        center = grid.cells["R3-C3"]
        neighbor = grid.cells["R2-C2"]
        neighbor["riskScore"] = 80.0
        neighbor["_lastSeen"] = now
        self.assertEqual(grid._neighbor_risk(center, now), 0.8)

    def test_freshness_confidence_decays_and_stale_cells_are_explicit(self) -> None:
        grid = HazardGrid()
        cell = grid.cells["R3-C3"]
        cell["_lastSeen"] = time.monotonic() - 5
        cell["sensorUpdatedAt"]["mq3Alcohol"] = datetime.now(timezone.utc).isoformat()
        snapshot = grid.cell_snapshot("R3-C3")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertFalse(snapshot["stale"])
        self.assertAlmostEqual(snapshot["confidence"], math.exp(-0.5), places=2)
        cell["_lastSeen"] = time.monotonic() - 11
        stale = grid.cell_snapshot("R3-C3")
        assert stale is not None
        self.assertTrue(stale["stale"])
        self.assertIn("unknown", stale["prediction"].lower())


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _FakeConnection:
    def transaction(self):
        return _FakeTransaction()


class _FakeAcquire:
    async def __aenter__(self):
        return _FakeConnection()

    async def __aexit__(self, *_args):
        return False


class _FakePool:
    def acquire(self):
        return _FakeAcquire()


class AlertTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = HazardEngine()
        self.engine._persist_alert = AsyncMock()  # type: ignore[method-assign]
        self.cell = {
            "id": "R3-C3",
            "riskScore": 82.0,
            "alertState": "NORMAL",
            "_lastSeen": time.monotonic(),
        }

    async def test_alert_triggers_after_three_consecutive_high_frames(self) -> None:
        await self.engine._evaluate_cell_alert(self.cell)
        await self.engine._evaluate_cell_alert(self.cell)
        self.assertEqual(self.cell["alertState"], "PENDING")
        await self.engine._evaluate_cell_alert(self.cell)
        self.assertEqual(self.cell["alertState"], "ALERT")
        self.assertEqual(len(self.engine.alerts), 1)
        self.assertEqual(next(iter(self.engine.alerts.values()))["notificationCount"], 1)

    async def test_acknowledgement_is_transactional_and_records_operator(self) -> None:
        await self.engine._evaluate_cell_alert(self.cell)
        await self.engine._evaluate_cell_alert(self.cell)
        await self.engine._evaluate_cell_alert(self.cell)
        alert_id = next(iter(self.engine.alerts))
        self.engine.pool = _FakePool()  # type: ignore[assignment]
        result = await self.engine.acknowledge_alert(alert_id, "Shift lead")
        assert result is not None
        self.assertEqual(result["state"], "ACKED")
        self.assertEqual(result["acknowledgedBy"], "Shift lead")

    async def test_high_reading_resets_continuous_clear_timer(self) -> None:
        alert = {
            "id": "alert-1",
            "cellId": "R3-C3",
            "state": "CLEARING",
            "level": 1,
            "riskScore": 20.0,
            "createdAt": datetime.now(timezone.utc),
            "acknowledgedAt": datetime.now(timezone.utc),
            "acknowledgedBy": "Operator",
            "lastNotificationAt": datetime.now(timezone.utc),
            "notificationCount": 1,
            "clearingStartedAt": datetime.now(timezone.utc) - timedelta(seconds=20),
        }
        self.engine.alerts[alert["id"]] = alert
        self.engine.active_alert_by_cell[alert["cellId"]] = alert["id"]
        await self.engine._evaluate_cell_alert(self.cell)
        self.assertEqual(alert["state"], "ACKED")
        self.assertIsNone(alert["clearingStartedAt"])

    async def test_stale_cell_cannot_clear_alert(self) -> None:
        now = datetime.now(timezone.utc)
        alert = {
            "id": "alert-2",
            "cellId": "R3-C3",
            "state": "CLEARING",
            "level": 1,
            "riskScore": 20.0,
            "createdAt": now - timedelta(minutes=8),
            "acknowledgedAt": now - timedelta(minutes=7),
            "acknowledgedBy": "Operator",
            "lastNotificationAt": now,
            "notificationCount": 1,
            "clearingStartedAt": now - timedelta(minutes=6),
        }
        self.engine.alerts[alert["id"]] = alert
        self.engine.active_alert_by_cell[alert["cellId"]] = alert["id"]
        self.engine.grid.cells[alert["cellId"]]["_lastSeen"] = time.monotonic() - 11
        await self.engine._maintain_alerts(time.monotonic())
        self.assertEqual(alert["state"], "ACKED")
        self.assertIn(alert["id"], self.engine.active_alert_by_cell.values())

    async def test_escalation_and_level_appropriate_repeat_notification(self) -> None:
        now = datetime.now(timezone.utc)
        alert = {
            "id": "alert-3",
            "cellId": "R3-C3",
            "state": "ALERT",
            "level": 1,
            "riskScore": 82.0,
            "createdAt": now - timedelta(seconds=121),
            "acknowledgedAt": None,
            "acknowledgedBy": None,
            "lastNotificationAt": now - timedelta(seconds=40),
            "notificationCount": 1,
            "clearingStartedAt": None,
        }
        self.engine.alerts[alert["id"]] = alert
        self.engine.active_alert_by_cell[alert["cellId"]] = alert["id"]
        await self.engine._maintain_alerts(time.monotonic())
        self.assertEqual(alert["state"], "ESCALATED")
        self.assertEqual(alert["level"], 2)
        self.assertEqual(alert["notificationCount"], 2)
        alert["createdAt"] = datetime.now(timezone.utc) - timedelta(seconds=301)
        alert["lastNotificationAt"] = datetime.now(timezone.utc)
        await self.engine._maintain_alerts(time.monotonic())
        self.assertEqual(alert["level"], 3)

    async def test_acknowledged_low_risk_alert_clears_after_five_minutes(self) -> None:
        now = datetime.now(timezone.utc)
        alert = {
            "id": "alert-4",
            "cellId": "R3-C3",
            "state": "CLEARING",
            "level": 1,
            "riskScore": 10.0,
            "createdAt": now - timedelta(minutes=10),
            "acknowledgedAt": now - timedelta(minutes=9),
            "acknowledgedBy": "Operator",
            "lastNotificationAt": now,
            "notificationCount": 1,
            "clearingStartedAt": now - timedelta(seconds=301),
        }
        self.engine.alerts[alert["id"]] = alert
        self.engine.active_alert_by_cell[alert["cellId"]] = alert["id"]
        self.engine.grid.cells[alert["cellId"]]["_lastSeen"] = time.monotonic()
        await self.engine._maintain_alerts(time.monotonic())
        self.assertEqual(alert["state"], "NORMAL")
        self.assertNotIn(alert["cellId"], self.engine.active_alert_by_cell)


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_queue_reports_overflow(self) -> None:
        engine = HazardEngine()
        engine.telemetry_queue = asyncio.Queue(maxsize=1)
        engine.telemetry_queue.put_nowait(("already",))
        cell = {"id": "R3-C3", "riskScore": 10.0, "sensors": {}}
        with patch.object(engine.grid, "process_reading", return_value=cell), patch.object(
            engine, "_evaluate_cell_alert", new=AsyncMock()
        ):
            await engine.accept_frame(frame(7))
        self.assertEqual(engine.telemetry_queue_drops, 1)

    async def test_telemetry_storage_failure_is_counted(self) -> None:
        class FailingConnection(_FakeConnection):
            async def executemany(self, *_args):
                raise RuntimeError("injected database failure")

        class FailingAcquire(_FakeAcquire):
            async def __aenter__(self):
                return FailingConnection()

        class FailingPool(_FakePool):
            def acquire(self):
                return FailingAcquire()

        engine = HazardEngine()
        engine.pool = FailingPool()  # type: ignore[assignment]
        engine.telemetry_queue.put_nowait((1, 1, datetime.now(timezone.utc), 0, 0, "{}", 0.0))
        writer = asyncio.create_task(engine._telemetry_writer())
        await asyncio.sleep(1.1)
        engine._shutdown = True
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        self.assertEqual(engine.telemetry_storage_failures, 1)


if __name__ == "__main__":
    unittest.main()
