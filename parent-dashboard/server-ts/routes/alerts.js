/**
 * Guardian Shield — Alert Routes
 * Manage alerts from monitored devices.
 */

const express = require("express");
const db = require("../models/db");
const { authMiddleware } = require("./auth");

const router = express.Router();
router.use(authMiddleware);

/**
 * GET /api/alerts?limit=50&device_id=1
 * Returns recent alerts
 */
router.get("/", (req, res) => {
  const limit = Math.min(parseInt(req.query.limit || "50", 10), 200);
  const deviceId = req.query.device_id;

  let query, params;
  if (deviceId) {
    query = `SELECT a.*, d.device_name FROM alerts a
             LEFT JOIN devices d ON a.device_id = d.id
             WHERE a.device_id = ?
             ORDER BY a.created_at DESC LIMIT ?`;
    params = [deviceId, limit];
  } else {
    query = `SELECT a.*, d.device_name FROM alerts a
             LEFT JOIN devices d ON a.device_id = d.id
             ORDER BY a.created_at DESC LIMIT ?`;
    params = [limit];
  }

  const alerts = db.prepare(query).all(...params);
  res.json(alerts);
});

/**
 * PUT /api/alerts/:id/read
 * Mark an alert as read
 */
router.put("/:id/read", (req, res) => {
  db.prepare("UPDATE alerts SET is_read = 1 WHERE id = ?").run(req.params.id);
  res.json({ message: "Alert marked as read" });
});

/**
 * PUT /api/alerts/read-all
 * Mark all alerts as read
 */
router.put("/read-all", (req, res) => {
  db.prepare("UPDATE alerts SET is_read = 1 WHERE is_read = 0").run();
  res.json({ message: "All alerts marked as read" });
});

/**
 * GET /api/alerts/unread-count
 * Get count of unread alerts
 */
router.get("/unread-count", (req, res) => {
  const result = db
    .prepare("SELECT COUNT(*) as count FROM alerts WHERE is_read = 0")
    .get();
  res.json({ count: result.count });
});

module.exports = router;
