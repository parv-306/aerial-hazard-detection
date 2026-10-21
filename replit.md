# Aerial Hazard Detection

A simulation-only dashboard demonstrating telemetry parsing, hazard-grid predictions, alerting, and network fault handling for aerial sensor readings.

## Run & Operate

- Start the `artifacts/api-server: API Server`, `artifacts/api-server: Hazard Engine`, and `artifacts/aerial-hazard-detection: web` workflows together.
- `pnpm --filter @workspace/api-server run dev` — run the Express API gateway
- `uv run --no-sync python -m uvicorn hazard_engine.main:app --app-dir artifacts/api-server --host 0.0.0.0 --port 8000` — run the Python telemetry engine
- `pnpm --filter @workspace/aerial-hazard-detection run dev` — run the Vite dashboard
- `pnpm run typecheck` — full typecheck across all packages
- `PORT=5173 BASE_PATH=/ pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes to the development database
- `PYTHONPATH=artifacts/api-server uv run --no-sync python -m unittest discover -s artifacts/api-server/hazard_engine/tests -v` — Python unit and PostgreSQL integration tests
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5 gateway plus a FastAPI/asyncio UDP service
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Model: synthetic-labeled demo data, scikit-learn SVM

## Where things live

- `artifacts/aerial-hazard-detection/` — React/Vite control-room dashboard
- `artifacts/api-server/hazard_engine/` — Python protocol, UDP simulator, grid, alerts, and tests
- `artifacts/api-server/src/routes/hazard.ts` — same-origin Express API gateway and response validation
- `lib/api-spec/openapi.yaml` — REST contract and generated client source
- `lib/db/src/schema/` — telemetry and durable alert schemas
- `README.md`, `compose.yaml`, and `.env.example` — setup and local deployment references

## Architecture decisions

- Keep the dashboard, sensor values, weather, and SVM outputs explicitly simulated; this is not an operational safety system.
- The Python engine owns frame ingestion and alert lifecycle; Express is the browser-facing JSON gateway.
- State-changing gateway requests require same-origin validation, and engine responses are checked against generated Zod schemas.

## Product

- View a 5×5 local 10-metre risk grid, select cells for sensor freshness details, inspect network diagnostics, acknowledge alerts, and control simulated packet faults.
- The legacy pipeline is represented as unavailable, rather than returning fabricated live data.

## User preferences

No additional project preferences recorded.

## Gotchas

- Start the Hazard Engine and API Server workflows along with the web workflow; the browser API uses the Express gateway and the WebSocket uses `/hazard-sim/ws`.
- Push the development database schema before first startup. Never treat simulated alerts or model outputs as validated hazard detection.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
