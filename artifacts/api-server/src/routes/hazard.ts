import {
  AcknowledgeHazardAlertBody,
  AcknowledgeHazardAlertParams,
  AcknowledgeHazardAlertResponse,
  GetHazardAlertsResponse,
  GetHazardCellParams,
  GetHazardCellResponse,
  GetHazardDashboardResponse,
  GetHazardGridResponse,
  GetHazardNetworkStatsResponse,
  GetLegacyPipelineStatusResponse,
  SetHazardSimulatorCorruptionBody,
  SetHazardSimulatorCorruptionResponse,
  StartHazardSimulatorResponse,
  StopHazardSimulatorResponse,
} from "@workspace/api-zod";
import { Router, type IRouter, type Request, type Response } from "express";

const router: IRouter = Router();
const ENGINE_URL = (
  process.env.HAZARD_ENGINE_URL ?? "http://127.0.0.1:8000/hazard-sim"
).replace(/\/$/, "");

type ResponseSchema<T> = {
  safeParse(
    value: unknown,
  ):
    | { success: true; data: T }
    | { success: false; error: { issues: unknown; message: string } };
};

function requireSameOrigin(req: Request, res: Response): boolean {
  const origin = req.get("origin");
  const host = req.get("host");
  if (!origin || !host) {
    res.status(403).json({ error: "Same-origin request required" });
    return false;
  }
  try {
    if (new URL(origin).host !== host) {
      res.status(403).json({ error: "Same-origin request required" });
      return false;
    }
  } catch {
    res.status(403).json({ error: "Invalid request origin" });
    return false;
  }
  return true;
}

async function forwardJson<T>(
  req: Request,
  res: Response,
  endpoint: string,
  schema: ResponseSchema<T>,
  method: "GET" | "POST" = "GET",
  body?: unknown,
): Promise<void> {
  try {
    const upstream = await fetch(`${ENGINE_URL}${endpoint}`, {
      method,
      headers: {
        "content-type": "application/json",
        "x-hazard-internal": "true",
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: AbortSignal.timeout(5_000),
    });
    const result: unknown = await upstream.json().catch(() => null);
    if (!upstream.ok) {
      const detail =
        result && typeof result === "object" && "detail" in result
          ? String(result.detail)
          : "Hazard engine request failed";
      res.status(upstream.status).json({ error: detail });
      return;
    }
    const parsed = schema.safeParse(result);
    if (!parsed.success) {
      req.log.error(
        { issues: parsed.error.issues },
        "Hazard engine returned a response outside the API contract",
      );
      res.status(502).json({ error: "Hazard engine response validation failed" });
      return;
    }
    res.json(parsed.data);
  } catch (error) {
    req.log.error({ err: error }, "Hazard engine is unavailable");
    res.status(503).json({ error: "Hazard engine is unavailable" });
  }
}

router.get("/hazard/dashboard", async (req, res): Promise<void> => {
  await forwardJson(req, res, "/hazard/dashboard", GetHazardDashboardResponse);
});

router.get("/hazard/grid", async (req, res): Promise<void> => {
  await forwardJson(req, res, "/hazard/grid", GetHazardGridResponse);
});

router.get("/hazard/cells/:cellId", async (req, res): Promise<void> => {
  const params = GetHazardCellParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  await forwardJson(
    req,
    res,
    `/hazard/cells/${encodeURIComponent(params.data.cellId)}`,
    GetHazardCellResponse,
  );
});

router.get("/hazard/alerts", async (req, res): Promise<void> => {
  await forwardJson(req, res, "/hazard/alerts", GetHazardAlertsResponse);
});

router.post("/hazard/alerts/:alertId/ack", async (req, res): Promise<void> => {
  if (!requireSameOrigin(req, res)) return;
  const params = AcknowledgeHazardAlertParams.safeParse(req.params);
  const body = AcknowledgeHazardAlertBody.safeParse(req.body);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  if (!body.success) {
    res.status(400).json({ error: body.error.message });
    return;
  }
  await forwardJson(
    req,
    res,
    `/hazard/alerts/${encodeURIComponent(params.data.alertId)}/ack`,
    AcknowledgeHazardAlertResponse,
    "POST",
    body.data,
  );
});

router.get("/hazard/network", async (req, res): Promise<void> => {
  await forwardJson(req, res, "/hazard/network", GetHazardNetworkStatsResponse);
});

router.get("/hazard/legacy/status", async (req, res): Promise<void> => {
  await forwardJson(
    req,
    res,
    "/hazard/legacy/status",
    GetLegacyPipelineStatusResponse,
  );
});

router.post("/hazard/simulator/start", async (req, res): Promise<void> => {
  if (!requireSameOrigin(req, res)) return;
  await forwardJson(
    req,
    res,
    "/hazard/simulator/start",
    StartHazardSimulatorResponse,
    "POST",
  );
});

router.post("/hazard/simulator/stop", async (req, res): Promise<void> => {
  if (!requireSameOrigin(req, res)) return;
  await forwardJson(
    req,
    res,
    "/hazard/simulator/stop",
    StopHazardSimulatorResponse,
    "POST",
  );
});

router.post("/hazard/simulator/corruption", async (req, res): Promise<void> => {
  if (!requireSameOrigin(req, res)) return;
  const body = SetHazardSimulatorCorruptionBody.safeParse(req.body);
  if (!body.success) {
    res.status(400).json({ error: body.error.message });
    return;
  }
  await forwardJson(
    req,
    res,
    "/hazard/simulator/corruption",
    SetHazardSimulatorCorruptionResponse,
    "POST",
    body.data,
  );
});

export default router;
