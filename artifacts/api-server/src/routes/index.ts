import { Router, type IRouter } from "express";
import hazardRouter from "./hazard";
import healthRouter from "./health";

const router: IRouter = Router();

router.use(healthRouter);
router.use(hazardRouter);

export default router;
