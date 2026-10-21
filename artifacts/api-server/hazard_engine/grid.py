from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from typing import Any

import numpy as np
from sklearn.pipeline import Pipeline
from sklearn.calibration import CalibratedClassifierCV
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

CELL_SIZE_M = 10
GRID_WIDTH = 5
GRID_HEIGHT = 5
STALE_AFTER_SECONDS = float(os.environ.get("HAZARD_STALE_TIMEOUT_SECONDS", "10"))
CONFIDENCE_TAU_SECONDS = 10
FEATURE_SCHEMA = (
    "mq3Alcohol",
    "mq4Methane",
    "mq7CarbonMonoxide",
    "mq8Hydrogen",
    "mq9CarbonMonoxide",
    "mq135AirQuality",
    "neighborRisk",
    "temperatureC",
    "windSin",
    "windCos",
)
SENSOR_KEYS = FEATURE_SCHEMA[:6]

# Demonstration origin only; this is not a surveyed or operational facility.
SITE_ORIGIN_LAT = float(os.environ.get("HAZARD_SITE_ORIGIN_LAT", "19.0"))
SITE_ORIGIN_LON = float(os.environ.get("HAZARD_SITE_ORIGIN_LON", "72.0"))
_WIND_ANGLES = {
    "N": 0,
    "NNE": 22.5,
    "NE": 45,
    "ENE": 67.5,
    "E": 90,
    "ESE": 112.5,
    "SE": 135,
    "SSE": 157.5,
    "S": 180,
    "SSW": 202.5,
    "SW": 225,
    "WSW": 247.5,
    "W": 270,
    "WNW": 292.5,
    "NW": 315,
    "NNW": 337.5,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _train_synthetic_demo_svm() -> Pipeline:
    rng = np.random.default_rng(2026)
    sensors = rng.lognormal(mean=2.5, sigma=1.0, size=(900, 6))
    sensors[:, 1] *= 2.4
    sensors[:, 4] *= 1.8
    neighbors = rng.uniform(0, 1, size=(900, 1))
    temperature = rng.normal(29, 6, size=(900, 1))
    wind_angle = rng.uniform(0, math.tau, size=(900, 1))
    weather = np.column_stack(
        (
            neighbors[:, 0],
            temperature[:, 0],
            np.sin(wind_angle[:, 0]),
            np.cos(wind_angle[:, 0]),
        )
    )
    features = np.column_stack((sensors, weather))

    normalized = sensors / np.array([18, 260, 65, 45, 180, 320])
    synthetic_risk = (
        normalized[:, 0] * 0.08
        + normalized[:, 1] * 0.21
        + normalized[:, 2] * 0.19
        + normalized[:, 3] * 0.12
        + normalized[:, 4] * 0.12
        + normalized[:, 5] * 0.11
        + neighbors[:, 0] * 0.11
        + np.clip((temperature[:, 0] - 25) / 25, 0, 1) * 0.06
        + rng.normal(0, 0.07, size=900)
    )
    labels = (synthetic_risk >= 0.76).astype(int)
    return Pipeline(
        [
            ("scale", StandardScaler()),
            (
                "svm",
                CalibratedClassifierCV(
                    SVC(kernel="rbf", random_state=2026),
                    method="sigmoid",
                    cv=3,
                ),
            ),
        ]
    ).fit(features, labels)


class HazardGrid:
    def __init__(self) -> None:
        self.model = _train_synthetic_demo_svm()
        self.weather: dict[str, Any] = {
            "temperatureC": 29.1,
            "windDirection": "ENE",
            "windSpeedMps": 2.3,
        }
        self.weather_updated_at = 0.0
        self._last_known_weather = dict(self.weather)
        self.cells: dict[str, dict[str, Any]] = {}
        for y in range(GRID_HEIGHT):
            for x in range(GRID_WIDTH):
                cell_id = f"R{y + 1}-C{x + 1}"
                self.cells[cell_id] = {
                    "id": cell_id,
                    "x": x,
                    "y": y,
                    "latitude": SITE_ORIGIN_LAT + (y * 10 / 110_540),
                    "longitude": SITE_ORIGIN_LON
                    + (x * 10 / (111_320 * math.cos(math.radians(SITE_ORIGIN_LAT)))),
                    "riskScore": 0.0,
                    "confidence": 0.0,
                    "prediction": "Awaiting telemetry",
                    "stale": True,
                    "alertState": "NORMAL",
                    "updatedAt": _utc_now().isoformat(),
                    "sensors": {key: 0.0 for key in SENSOR_KEYS},
                    "sensorUpdatedAt": {key: None for key in SENSOR_KEYS},
                    "_lastSeen": None,
                }

    def cell_for_coordinates(self, latitude: float, longitude: float) -> dict[str, Any]:
        local_x = (
            (longitude - SITE_ORIGIN_LON)
            * 111_320
            * math.cos(math.radians(SITE_ORIGIN_LAT))
        )
        local_y = (latitude - SITE_ORIGIN_LAT) * 110_540
        x = min(GRID_WIDTH - 1, max(0, int(local_x // CELL_SIZE_M)))
        y = min(GRID_HEIGHT - 1, max(0, int(local_y // CELL_SIZE_M)))
        return self.cells[f"R{y + 1}-C{x + 1}"]

    def update_weather(self, provider: Any | None = None) -> None:
        now = time.monotonic()
        if now - self.weather_updated_at < 60:
            return
        self.weather_updated_at = now
        try:
            # A deterministic mock provider is used until a real weather source is configured.
            candidate = provider() if provider else {
                "temperatureC": 29.1,
                "windDirection": "ENE",
                "windSpeedMps": 2.3,
            }
            self.weather = candidate
            self._last_known_weather = dict(candidate)
        except Exception:
            self.weather = dict(self._last_known_weather)

    def _neighbor_risk(self, cell: dict[str, Any], now: float) -> float:
        scores: list[float] = []
        for other in self.cells.values():
            if other is cell or other["_lastSeen"] is None:
                continue
            if now - other["_lastSeen"] > STALE_AFTER_SECONDS:
                continue
            if max(abs(other["x"] - cell["x"]), abs(other["y"] - cell["y"])) == 1:
                scores.append(float(other["riskScore"]) / 100)
        return sum(scores) / len(scores) if scores else 0.0

    def _feature_vector(self, cell: dict[str, Any], now: float) -> list[float]:
        angle = math.radians(_WIND_ANGLES.get(self.weather["windDirection"], 0))
        return [
            *(float(cell["sensors"][key]) for key in SENSOR_KEYS),
            self._neighbor_risk(cell, now),
            float(self.weather["temperatureC"]),
            math.sin(angle),
            math.cos(angle),
        ]

    def process_reading(
        self,
        *,
        latitude: float,
        longitude: float,
        raw_sensors: tuple[int, int, int, int, int, int],
        captured_at: datetime,
    ) -> dict[str, Any]:
        now = time.monotonic()
        self.update_weather()
        cell = self.cell_for_coordinates(latitude, longitude)
        cell["sensors"] = {
            key: value / 100
            for key, value in zip(SENSOR_KEYS, raw_sensors, strict=True)
        }
        cell["sensorUpdatedAt"] = {
            key: captured_at.isoformat() for key in SENSOR_KEYS
        }
        cell["updatedAt"] = captured_at.isoformat()
        cell["_lastSeen"] = now
        probabilities = self.model.predict_proba(
            np.asarray([self._feature_vector(cell, now)], dtype=float)
        )[0]
        hazard_index = list(self.model.classes_).index(1)
        cell["riskScore"] = round(float(probabilities[hazard_index]) * 100, 1)
        cell["confidence"] = round(math.exp(-0 / CONFIDENCE_TAU_SECONDS), 3)
        cell["prediction"] = (
            "Elevated demo risk" if cell["riskScore"] >= 65 else "Lower demo risk"
        )
        cell["stale"] = False
        return cell

    def snapshot(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        result: list[dict[str, Any]] = []
        for cell in self.cells.values():
            age = (
                now - cell["_lastSeen"]
                if cell["_lastSeen"] is not None
                else math.inf
            )
            public_cell = {key: value for key, value in cell.items() if not key.startswith("_")}
            public_cell["stale"] = age >= STALE_AFTER_SECONDS
            public_cell["confidence"] = (
                round(math.exp(-age / CONFIDENCE_TAU_SECONDS), 3)
                if math.isfinite(age)
                else 0.0
            )
            if public_cell["stale"]:
                public_cell["prediction"] = (
                    "Awaiting telemetry" if not any(cell["sensorUpdatedAt"].values()) else "Stale — risk unknown"
                )
            result.append(public_cell)
        return result

    def cell_snapshot(self, cell_id: str) -> dict[str, Any] | None:
        return next((cell for cell in self.snapshot() if cell["id"] == cell_id), None)
