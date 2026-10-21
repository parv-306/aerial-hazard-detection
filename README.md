# Aerial Hazard Detection

A simulation-only industrial monitoring demo. It exercises binary sensor-frame ingestion, a 10-metre hazard grid, synthetic SVM predictions, durable alert transitions, packet faults, and a live operations dashboard.

> **Safety notice:** Every reading, prediction, alert, and weather value in this project is simulated. The model uses synthetic training data. This application is not a validated safety system and must not be used to make real-world safety decisions.

## What is included

- A 32-byte telemetry frame parser with CRC-16/CCITT-FALSE validation, stream resynchronization, duplicate detection, 16-bit sequence wraparound, and a 2-second reorder window.
- An asyncio UDP simulator and fault proxy with deterministic random behavior, packet loss, duplication, delay, and optional corruption.
- A 5×5 local grid of 10-metre cells with freshness decay, neighbor features, simulated weather, and a synthetic-trained SVM. Risk scores shown in the UI are 0–100.
- A persistent alert lifecycle: three consecutive high frames to trigger, escalation after 2 and 5 minutes, 30-second repeat notices, operator acknowledgement, and clearing only after five minutes of fresh low readings. Stale cells cannot clear an alert.
- A bounded asynchronous telemetry writer and PostgreSQL-backed telemetry and alert state.
- An Express JSON gateway, FastAPI REST/WebSocket service, and responsive React/Vite operations dashboard.

Weather is mocked and refreshed every 60 seconds. There is no real sensor, weather-provider, or operational alert integration.

## Run in Replit

Start these three configured workflows together:

1. `artifacts/api-server: Hazard Engine`
2. `artifacts/api-server: API Server`
3. `artifacts/aerial-hazard-detection: web`

The project uses the configured `DATABASE_URL` secret for PostgreSQL. Push development schema changes before first use or after changing the Drizzle schema:

```sh
pnpm --filter @workspace/db run push
```

The dashboard is available at the `Aerial Hazard Detection` artifact preview. The Express API is mounted under `/api`; the engine WebSocket is `/hazard-sim/ws`.

## Local setup

Requirements: Node.js 24, pnpm, Python 3.13 with `uv`, and PostgreSQL.

```sh
pnpm install
cp .env.example .env
```

Set `DATABASE_URL` in your shell to a development PostgreSQL database, then run `pnpm --filter @workspace/db run push` once to create the tables. Never commit `.env` or use the example password outside a local demo.

Run each command in a separate terminal:

```sh
uv sync
uv run --no-sync python -m uvicorn hazard_engine.main:app --app-dir artifacts/api-server --host 0.0.0.0 --port 8000
```

```sh
PORT=8080 HAZARD_ENGINE_URL=http://127.0.0.1:8000/hazard-sim pnpm --filter @workspace/api-server run dev
```

```sh
PORT=5173 BASE_PATH=/ pnpm --filter @workspace/aerial-hazard-detection run dev
```

On Windows PowerShell, set variables before running the corresponding command:

```powershell
$env:DATABASE_URL = "postgresql://hazard_demo:hazard_demo@localhost:5432/hazard_demo"
$env:PORT = "8080"
$env:HAZARD_ENGINE_URL = "http://127.0.0.1:8000/hazard-sim"
pnpm --filter @workspace/api-server run dev
```

For the web workflow, use `$env:PORT = "5173"` and `$env:BASE_PATH = "/"`. Run the Python service with `uv run --no-sync python -m uvicorn hazard_engine.main:app --app-dir artifacts/api-server --host 0.0.0.0 --port 8000`.

### Docker Compose

For a local Compose demo, copy `.env.example` to `.env`, change the example password if the database port is exposed beyond your machine, and run:

```sh
docker compose up --build
```

Open `http://localhost:8090`. Compose starts PostgreSQL, synchronizes the development schema, starts the engine and API, and serves the web app through Nginx. The Compose configuration is for local demonstration only; use Replit's configured workflows for this workspace. Container builds were not run in the Replit environment.

## Tests and checks

```sh
pnpm run typecheck
PORT=5173 BASE_PATH=/ pnpm run build
PYTHONPATH=artifacts/api-server uv run --no-sync python -m unittest discover -s artifacts/api-server/hazard_engine/tests -v
```

PostgreSQL integration tests require `DATABASE_URL`; without it, those tests are skipped. The unit tests cover frame CRC/parsing, ordering and wraparound, grid freshness/features, alert transitions, queue limits, and telemetry storage failures. PostgreSQL tests verify persisted telemetry and alert acknowledgement recovery.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL URL. Required for the engine and persistence tests. |
| `HAZARD_ENGINE_URL` | Express-to-FastAPI base URL; defaults to `http://127.0.0.1:8000/hazard-sim`. |
| `HAZARD_SITE_ORIGIN_LAT` / `HAZARD_SITE_ORIGIN_LON` | Local-projection origin for the demo grid. |
| `HAZARD_STALE_TIMEOUT_SECONDS` | Reading freshness threshold; defaults to 10 seconds. |
| `PORT` / `BASE_PATH` | Web workflow settings; Replit supplies these for its artifact. |

## Project structure

- `artifacts/api-server/hazard_engine/` — Python frame parser, grid/model, simulator, alert state, persistence, and tests.
- `artifacts/api-server/src/routes/hazard.ts` — Express API gateway.
- `artifacts/aerial-hazard-detection/` — React/Vite dashboard.
- `lib/api-spec/openapi.yaml` — source API contract.
- `lib/db/src/schema/` — Drizzle PostgreSQL schema.
