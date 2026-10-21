from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from .runtime import HazardEngine

engine = HazardEngine()


class AcknowledgeBody(BaseModel):
    acknowledgedBy: str = Field(min_length=1, max_length=80)


class CorruptionBody(BaseModel):
    enabled: bool


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await engine.startup()
    try:
        yield
    finally:
        await engine.shutdown()


app = FastAPI(
    title="Aerial Hazard Simulation API",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def protect_mutations(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.headers.get("x-hazard-internal") != "true":
            origin = request.headers.get("origin")
            host = request.headers.get("host")
            if not origin or urlsplit(origin).netloc != host:
                return JSONResponse(
                    status_code=403,
                    content={"error": "Same-origin request required"},
                )
    return await call_next(request)


@app.get("/hazard-sim/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/hazard-sim/hazard/dashboard")
async def dashboard() -> dict:
    return engine.dashboard()


@app.get("/hazard-sim/hazard/grid")
async def grid() -> list[dict]:
    return engine.grid.snapshot()


@app.get("/hazard-sim/hazard/cells/{cell_id}")
async def cell_detail(cell_id: str) -> dict:
    cell = engine.grid.cell_snapshot(cell_id)
    if cell is None:
        raise HTTPException(status_code=404, detail="Cell not found")
    return cell


@app.get("/hazard-sim/hazard/alerts")
async def alerts() -> list[dict]:
    return engine.get_alerts()


@app.post("/hazard-sim/hazard/alerts/{alert_id}/ack")
async def acknowledge(alert_id: str, body: AcknowledgeBody) -> dict:
    alert = await engine.acknowledge_alert(alert_id, body.acknowledgedBy)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return alert


@app.get("/hazard-sim/hazard/network")
async def network() -> dict:
    return engine.network_stats()


@app.get("/hazard-sim/hazard/legacy/status")
async def legacy_status() -> dict:
    return engine.legacy_status()


@app.post("/hazard-sim/hazard/simulator/start")
async def start_simulator() -> dict:
    status = await engine.start_simulator()
    engine.schedule_broadcast()
    return status


@app.post("/hazard-sim/hazard/simulator/stop")
async def stop_simulator() -> dict:
    status = await engine.stop_simulator()
    engine.schedule_broadcast()
    return status


@app.post("/hazard-sim/hazard/simulator/corruption")
async def set_corruption(body: CorruptionBody) -> dict:
    status = engine.set_corruption(body.enabled)
    engine.schedule_broadcast()
    return status


@app.websocket("/hazard-sim/ws")
async def websocket_feed(websocket: WebSocket) -> None:
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if origin and urlsplit(origin).netloc != host:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    engine.websockets.add(websocket)
    try:
        await websocket.send_json(engine.dashboard())
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        engine.websockets.discard(websocket)
