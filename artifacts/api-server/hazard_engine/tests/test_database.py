import asyncio
import json
import os
import unittest
import uuid
from datetime import datetime, timezone

import asyncpg

from hazard_engine.runtime import HazardEngine


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL is required for PostgreSQL integration tests")
class PostgreSQLIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def test_alert_acknowledgement_survives_engine_restart(self) -> None:
        alert_id = f"test-{uuid.uuid4()}"
        created_at = datetime.now(timezone.utc)
        await self.pool.execute(
            """
            INSERT INTO hazard_alerts (
                id, cell_id, state, level, risk_score, created_at, updated_at,
                acknowledged_at, acknowledged_by, last_notification_at,
                notification_count, clearing_started_at
            ) VALUES ($1,$2,'ALERT',1,78,$3,$3,NULL,NULL,$3,1,NULL)
            """,
            alert_id,
            "R3-C3",
            created_at,
        )
        try:
            first_process = HazardEngine()
            first_process.pool = self.pool
            await first_process._recover_alerts()
            self.assertIn(alert_id, first_process.alerts)

            acknowledged = await first_process.acknowledge_alert(alert_id, "Integration operator")
            self.assertIsNotNone(acknowledged)
            self.assertEqual(acknowledged["state"], "ACKED")

            restored_process = HazardEngine()
            restored_process.pool = self.pool
            await restored_process._recover_alerts()
            restored = restored_process.alerts[alert_id]
            self.assertEqual(restored["state"], "ACKED")
            self.assertEqual(restored["acknowledgedBy"], "Integration operator")

            row = await self.pool.fetchrow(
                "SELECT state, acknowledged_by FROM hazard_alerts WHERE id = $1",
                alert_id,
            )
            self.assertEqual(row["state"], "ACKED")
            self.assertEqual(row["acknowledged_by"], "Integration operator")
        finally:
            await self.pool.execute("DELETE FROM hazard_alerts WHERE id = $1", alert_id)

    async def test_telemetry_batch_writer_persists_a_reading(self) -> None:
        drone_id = 250
        sequence = 65_000
        captured_at = datetime.now(timezone.utc)
        sensors = {
            "mq3Alcohol": 4.2,
            "mq4Methane": 84.0,
            "mq7CarbonMonoxide": 17.0,
            "mq8Hydrogen": 9.0,
            "mq9CarbonMonoxide": 43.0,
            "mq135AirQuality": 126.0,
        }
        engine = HazardEngine()
        engine.pool = self.pool
        engine.telemetry_queue.put_nowait(
            (
                drone_id,
                sequence,
                captured_at,
                19.0001,
                72.0001,
                json.dumps(sensors),
                12.0,
            )
        )
        writer = asyncio.create_task(engine._telemetry_writer())
        try:
            await asyncio.wait_for(engine.telemetry_queue.join(), timeout=3)
            row = await self.pool.fetchrow(
                """
                SELECT sequence, sensors, risk_score
                FROM hazard_telemetry
                WHERE drone_id = $1 AND sequence = $2 AND captured_at = $3
                """,
                drone_id,
                sequence,
                captured_at,
            )
            self.assertIsNotNone(row)
            self.assertEqual(row["sequence"], sequence)
            self.assertEqual(row["risk_score"], 12.0)
            stored_sensors = (
                json.loads(row["sensors"])
                if isinstance(row["sensors"], str)
                else row["sensors"]
            )
            self.assertEqual(stored_sensors["mq4Methane"], 84.0)
        finally:
            engine._shutdown = True
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            await self.pool.execute(
                """
                DELETE FROM hazard_telemetry
                WHERE drone_id = $1 AND sequence = $2 AND captured_at = $3
                """,
                drone_id,
                sequence,
                captured_at,
            )


if __name__ == "__main__":
    unittest.main()
