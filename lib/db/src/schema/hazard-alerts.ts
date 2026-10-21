import { createInsertSchema } from "drizzle-zod";
import { doublePrecision, integer, pgTable, text, timestamp } from "drizzle-orm/pg-core";
import { z } from "zod/v4";

export const hazardAlertsTable = pgTable("hazard_alerts", {
  id: text("id").primaryKey(),
  cellId: text("cell_id").notNull(),
  state: text("state").notNull(),
  level: integer("level").notNull().default(1),
  riskScore: doublePrecision("risk_score").notNull(),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull(),
  updatedAt: timestamp("updated_at", { withTimezone: true }).notNull(),
  acknowledgedAt: timestamp("acknowledged_at", { withTimezone: true }),
  acknowledgedBy: text("acknowledged_by"),
  lastNotificationAt: timestamp("last_notification_at", { withTimezone: true }),
  notificationCount: integer("notification_count").notNull().default(0),
  clearingStartedAt: timestamp("clearing_started_at", { withTimezone: true }),
});

export const insertHazardAlertSchema = createInsertSchema(hazardAlertsTable);
export type InsertHazardAlert = z.infer<typeof insertHazardAlertSchema>;
export type HazardAlertRecord = typeof hazardAlertsTable.$inferSelect;
