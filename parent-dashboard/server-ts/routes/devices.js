/**
 * Guardian Shield — Device Routes
 * CRUD for monitored devices.
 */

const express = require("express");
const db = require("../models/db");
const { authMiddleware } = require("./auth");

const router = express.Router();
router.use(authMiddleware);

/**
 * GET /api/devices/:id/telemetry
 * Returns latest in-memory telemetry for a device.
 */
router.get("/:id/telemetry", (req, res) => {
  const deviceId = Number.parseInt(req.params.id, 10);
  if (Number.isNaN(deviceId)) {
    return res.status(400).json({ error: "Invalid device id" });
  }

  const telemetryMap = req.app.get("deviceTelemetry");
  const telemetry = telemetryMap?.get?.(deviceId) || null;
  if (!telemetry) {
    return res.status(404).json({ error: "Telemetry not available" });
  }

  res.json(telemetry);
});

/**
 * GET /api/devices
 * Returns all registered devices with their status
 */
router.get("/", (req, res) => {
  const devices = db
    .prepare(
      `SELECT id, device_name, platform, hostname, status, last_seen, created_at
       FROM devices ORDER BY last_seen DESC`
    )
    .all();
  res.json(devices);
});

/**
 * GET /api/devices/:id
 * Returns a single device
 */
router.get("/:id", (req, res) => {
  const device = db
    .prepare(
      `SELECT id, device_name, platform, hostname, status, last_seen, created_at
       FROM devices WHERE id = ?`
    )
    .get(req.params.id);

  if (!device) {
    return res.status(404).json({ error: "Device not found" });
  }
  res.json(device);
});

/**
 * DELETE /api/devices/:id
 * Remove a device and its screenshots
 */
router.delete("/:id", (req, res) => {
  const result = db.prepare("DELETE FROM devices WHERE id = ?").run(req.params.id);
  if (result.changes === 0) {
    return res.status(404).json({ error: "Device not found" });
  }
  res.json({ message: "Device removed" });
});

/**
 * PUT /api/devices/:id/rename
 * Body: { name }
 */
router.put("/:id/rename", (req, res) => {
  const { name } = req.body;
  if (!name) return res.status(400).json({ error: "Name required" });

  const result = db
    .prepare("UPDATE devices SET device_name = ? WHERE id = ?")
    .run(name, req.params.id);

  if (result.changes === 0) {
    return res.status(404).json({ error: "Device not found" });
  }
  res.json({ message: "Device renamed" });
});

module.exports = router;
