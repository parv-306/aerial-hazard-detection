import { createInsertSchema } from "drizzle-zod";
import { doublePrecision, index, integer, jsonb, pgTable, serial, smallint, timestamp } from "drizzle-orm/pg-core";
import { z } from "zod/v4";

export type HazardSensorValues = {
  mq3Alcohol: number;
  mq4Methane: number;
  mq7CarbonMonoxide: number;
  mq8Hydrogen: number;
  mq9CarbonMonoxide: number;
  mq135AirQuality: number;
};

export const hazardTelemetryTable = pgTable(
  "hazard_telemetry",
  {
    id: serial("id").primaryKey(),
    droneId: smallint("drone_id").notNull(),
    sequence: integer("sequence").notNull(),
    capturedAt: timestamp("captured_at", { withTimezone: true }).notNull(),
    latitude: doublePrecision("latitude").notNull(),
    longitude: doublePrecision("longitude").notNull(),
    sensors: jsonb("sensors").$type<HazardSensorValues>().notNull(),
    riskScore: doublePrecision("risk_score").notNull(),
  },
  (table) => [index("hazard_telemetry_captured_at_idx").on(table.capturedAt)],
);

export const insertHazardTelemetrySchema = createInsertSchema(hazardTelemetryTable).omit({
  id: true,
});
export type InsertHazardTelemetry = z.infer<typeof insertHazardTelemetrySchema>;
export type HazardTelemetry = typeof hazardTelemetryTable.$inferSelect;
