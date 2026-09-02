/**
 * Guardian Shield — Application Management Routes
 * REST API for live app tracking, app blocking, remote process closing,
 * app timers, installed apps catalog, and sending messages to the child.
 */

const express = require("express");
const db = require("../models/db");
const { authMiddleware } = require("./auth");

const router = express.Router();
router.use(authMiddleware);

// In-memory cache of latest running apps report per device
const runningAppsCache = new Map();
const activeAppCache = new Map();
const activeTimersCache = new Map();

/**
 * Helper to get device socket by device ID
 */
function getDeviceSocket(req, deviceId) {
  const io = req.app.get("io");
  if (!io) return null;

  const targetId = parseInt(deviceId, 10);
  for (const [, socket] of io.sockets.sockets) {
    if (socket.deviceId && parseInt(socket.deviceId, 10) === targetId) {
      return socket;
    }
  }
  return null;
}

/**
 * Broadcast current blocked apps list to a specific device or all devices.
 */
function broadcastBlockedApps(req, deviceId = null) {
  const io = req.app.get("io");
  if (!io) return;

  if (deviceId) {
    const socket = getDeviceSocket(req, deviceId);
    if (socket) {
      const rows = db
        .prepare(
          "SELECT process_name FROM blocked_apps WHERE device_id IS NULL OR device_id = ?"
        )
        .all(deviceId);
      socket.emit("update_blocked_apps", {
        blocked_apps: rows.map((r) => r.process_name),
      });
    }
  } else {
    // Broadcast to each connected socket with their respective device rules
    for (const [, socket] of io.sockets.sockets) {
      if (!socket.deviceId) continue;
      const rows = db
        .prepare(
          "SELECT process_name FROM blocked_apps WHERE device_id IS NULL OR device_id = ?"
        )
        .all(socket.deviceId);
      socket.emit("update_blocked_apps", {
        blocked_apps: rows.map((r) => r.process_name),
      });
    }
  }
}

// ─── App Overview & Running Apps ─────────────────────────────────────────────

/**
 * GET /api/apps/overview/:deviceId
 * Combined overview: active app, top running apps, active timers, blocked apps count
 */
router.get("/overview/:deviceId", (req, res) => {
  const deviceId = parseInt(req.params.deviceId, 10);
  if (!deviceId) return res.status(400).json({ error: "Invalid device ID" });

  const activeApp = activeAppCache.get(deviceId) || {
    app_name: "Desktop / System",
    process_name: "explorer.exe",
    window_title: "",
    duration_sec: 0,
  };

  const runningApps = runningAppsCache.get(deviceId) || [];
  const activeTimers = activeTimersCache.get(deviceId) || [];

  const blockedApps = db
    .prepare(
      "SELECT * FROM blocked_apps WHERE device_id IS NULL OR device_id = ? ORDER BY app_name ASC"
    )
    .all(deviceId);

  const installedCount = db
    .prepare("SELECT COUNT(*) as count FROM device_apps WHERE device_id = ?")
    .get(deviceId)?.count || 0;

  res.json({
    device_id: deviceId,
    active_app: activeApp,
    running_apps: runningApps,
    active_timers: activeTimers,
    blocked_apps: blockedApps,
    installed_apps_count: installedCount,
  });
});

/**
 * GET /api/apps/running/:deviceId
 * Returns real-time running applications for a device
 */
router.get("/running/:deviceId", (req, res) => {
  const deviceId = parseInt(req.params.deviceId, 10);
  if (!deviceId) return res.status(400).json({ error: "Invalid device ID" });

  // Trigger real-time refresh if device is connected
  const socket = getDeviceSocket(req, deviceId);
  if (socket) {
    socket.emit("request_running_apps");
  }

  const cached = runningAppsCache.get(deviceId) || [];
  const active = activeAppCache.get(deviceId) || null;
  const timers = activeTimersCache.get(deviceId) || [];

  res.json({
    device_id: deviceId,
    active_app: active,
    running_apps: cached,
    active_timers: timers,
  });
});

// ─── Installed Applications Library ──────────────────────────────────────────

/**
 * GET /api/apps/installed/:deviceId
 * Returns list of installed apps discovered on the target device
 */
router.get("/installed/:deviceId", (req, res) => {
  const deviceId = parseInt(req.params.deviceId, 10);
  if (!deviceId) return res.status(400).json({ error: "Invalid device ID" });

  const rows = db
    .prepare(
      `SELECT da.*,
              CASE WHEN ba.id IS NOT NULL THEN 1 ELSE 0 END as is_blocked
       FROM device_apps da
       LEFT JOIN blocked_apps ba ON (ba.device_id IS NULL OR ba.device_id = da.device_id)
                                AND LOWER(ba.process_name) = LOWER(da.process_name)
       WHERE da.device_id = ?
       ORDER BY da.app_name ASC`
    )
    .all(deviceId);

  res.json(rows);
});

/**
 * POST /api/apps/installed/refresh/:deviceId
 * Request agent to rescan installed applications
 */
router.post("/installed/refresh/:deviceId", (req, res) => {
  const deviceId = parseInt(req.params.deviceId, 10);
  const socket = getDeviceSocket(req, deviceId);

  if (!socket) {
    return res.status(404).json({ error: "Device is offline" });
  }

  socket.emit("request_installed_apps", { force: true });
  res.json({ message: "Scan request sent to device" });
});

// ─── App Blocklist Controls ──────────────────────────────────────────────────

/**
 * GET /api/apps/blocked
 * Returns all blocked applications
 */
router.get("/blocked", (req, res) => {
  const deviceId = req.query.deviceId ? parseInt(req.query.deviceId, 10) : null;
  let rows;
  if (deviceId) {
    rows = db
      .prepare(
        "SELECT * FROM blocked_apps WHERE device_id IS NULL OR device_id = ? ORDER BY app_name ASC"
      )
      .all(deviceId);
  } else {
    rows = db.prepare("SELECT * FROM blocked_apps ORDER BY app_name ASC").all();
  }
  res.json(rows);
});

/**
 * POST /api/apps/blocked
 * Body: { deviceId?, app_name, process_name, category? }
 * Block an application
 */
router.post("/blocked", (req, res) => {
  const { deviceId, app_name, process_name, category } = req.body;

  if (!process_name && !app_name) {
    return res.status(400).json({ error: "app_name or process_name required" });
  }

  const cleanName = (app_name || process_name).trim();
  const cleanProc = (process_name || app_name).trim().toLowerCase();
  const devId = deviceId ? parseInt(deviceId, 10) : null;
  const cleanCat = (category || "general").trim();

  try {
    const existing = db
      .prepare(
        `SELECT id FROM blocked_apps
         WHERE (device_id = ? OR (device_id IS NULL AND ? IS NULL))
           AND LOWER(process_name) = ?`
      )
      .get(devId, devId, cleanProc);

    if (existing) {
      return res.status(409).json({ error: "App is already blocked" });
    }

    const result = db
      .prepare(
        `INSERT INTO blocked_apps (device_id, app_name, process_name, category, added_by)
         VALUES (?, ?, ?, ?, ?)`
      )
      .run(devId, cleanName, cleanProc, cleanCat, req.user.username);

    // Broadcast updated blocklist to agents
    broadcastBlockedApps(req, devId);

    // Also send an alert record
    db.prepare(
      `INSERT INTO alerts (device_id, alert_type, message, details)
       VALUES (?, 'app_rule', ?, ?)`
    ).run(
      devId,
      `Application blocked: ${cleanName}`,
      JSON.stringify({ process_name: cleanProc, added_by: req.user.username })
    );

    res.status(201).json({
      id: result.lastInsertRowid,
      message: `Blocked: ${cleanName}`,
      app_name: cleanName,
      process_name: cleanProc,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

/**
 * DELETE /api/apps/blocked/:id
 * Unblock an application by ID or process_name
 */
router.delete("/blocked/:id", (req, res) => {
  const param = req.params.id;
  const numId = parseInt(param, 10);
  let existing = null;

  if (!isNaN(numId)) {
    existing = db.prepare("SELECT * FROM blocked_apps WHERE id = ?").get(numId);
  }
  if (!existing) {
    existing = db.prepare("SELECT * FROM blocked_apps WHERE LOWER(process_name) = ? OR LOWER(app_name) = ?").get(param.toLowerCase(), param.toLowerCase());
  }

  if (!existing) {
    return res.status(404).json({ error: "Blocked app rule not found" });
  }

  db.prepare("DELETE FROM blocked_apps WHERE id = ?").run(existing.id);

  // Broadcast update
  broadcastBlockedApps(req, existing.device_id);

  res.json({ message: `Unblocked: ${existing.app_name}` });
});

// ─── Remote Close App (Anonymous Termination) ───────────────────────────────

/**
 * POST /api/apps/close
 * Body: { deviceId, pid?, process_name?, app_name? }
 * Close an app immediately on the child's screen
 */
router.post("/close", (req, res) => {
  const { deviceId, pid, process_name, app_name } = req.body;

  if (!deviceId) return res.status(400).json({ error: "deviceId required" });
  if (!pid && !process_name) {
    return res.status(400).json({ error: "pid or process_name required" });
  }

  const socket = getDeviceSocket(req, deviceId);
  if (!socket) {
    return res.status(404).json({ error: "Device is offline" });
  }

  socket.emit("close_app", {
    pid: pid ? parseInt(pid, 10) : null,
    process_name: process_name || null,
  });

  const label = app_name || process_name || `PID ${pid}`;

  // Log in alerts
  db.prepare(
    `INSERT INTO alerts (device_id, alert_type, message, details)
     VALUES (?, 'remote_command', ?, ?)`
  ).run(
    deviceId,
    `Closed application remotely: ${label}`,
    JSON.stringify({ pid, process_name, initiated_by: req.user.username })
  );

  res.json({ ok: true, message: `Closing ${label}...` });
});

// ─── App Timers & Limits ────────────────────────────────────────────────────

/**
 * POST /api/apps/timer
 * Body: { deviceId, app_name, process_name, duration_seconds }
 * Start a scheduled countdown to close an app
 */
router.post("/timer", (req, res) => {
  const { deviceId, app_name, process_name, duration_seconds } = req.body;

  if (!deviceId || !duration_seconds) {
    return res.status(400).json({ error: "deviceId and duration_seconds required" });
  }

  const cleanName = (app_name || process_name || "App").trim();
  const cleanProc = (process_name || app_name || "").trim().toLowerCase();
  const duration = Math.max(10, parseInt(duration_seconds, 10));

  const socket = getDeviceSocket(req, deviceId);
  if (!socket) {
    return res.status(404).json({ error: "Device is offline" });
  }

  const timerId = `timer_${Date.now()}_${deviceId}`;
  const expiresAt = new Date(Date.now() + duration * 1000).toISOString();

  // Save in DB
  const result = db
    .prepare(
      `INSERT INTO app_timers (device_id, app_name, process_name, duration_seconds, expires_at, status)
       VALUES (?, ?, ?, ?, ?, 'active')`
    )
    .run(deviceId, cleanName, cleanProc, duration, expiresAt);

  // Send command to device agent
  socket.emit("set_app_timer", {
    timer_id: timerId,
    app_name: cleanName,
    process_name: cleanProc,
    duration_seconds: duration,
  });

  res.status(201).json({
    id: result.lastInsertRowid,
    timer_id: timerId,
    app_name: cleanName,
    process_name: cleanProc,
    duration_seconds: duration,
    expires_at: expiresAt,
    message: `Timer set: ${cleanName} will close in ${Math.round(duration / 60)} min`,
  });
});

/**
 * DELETE /api/apps/timer/:id
 * Cancel an active app timer
 */
router.delete("/timer/:id", (req, res) => {
  const param = req.params.id;
  const numId = parseInt(param, 10);
  let timer = null;

  if (!isNaN(numId)) {
    timer = db.prepare("SELECT * FROM app_timers WHERE id = ?").get(numId);
  }
  if (!timer) {
    timer = db.prepare("SELECT * FROM app_timers WHERE (process_name = ? OR app_name = ?) AND status = 'active' ORDER BY id DESC LIMIT 1").get(param, param);
  }
  if (!timer) {
    timer = db.prepare("SELECT * FROM app_timers WHERE process_name = ? OR app_name = ? ORDER BY id DESC LIMIT 1").get(param, param);
  }

  if (!timer) {
    return res.status(404).json({ error: "Timer not found" });
  }

  db.prepare("UPDATE app_timers SET status = 'cancelled' WHERE id = ?").run(timer.id);

  const socket = getDeviceSocket(req, timer.device_id);
  if (socket) {
    socket.emit("cancel_app_timer", {
      process_name: timer.process_name,
    });
  }

  res.json({ message: `Timer cancelled for ${timer.app_name}` });
});

// ─── Send Parent Message to Child ───────────────────────────────────────────

/**
 * POST /api/apps/message
 * Body: { deviceId, title, message, urgency?, duration? }
 * Display a floating message card on child's screen
 */
router.post("/message", (req, res) => {
  const { deviceId, title, message, urgency, duration } = req.body;

  if (!deviceId || !message) {
    return res.status(400).json({ error: "deviceId and message required" });
  }

  const cleanTitle = (title || "Parent Notice").trim();
  const cleanMsg = message.trim();
  const cleanUrg = ["info", "warning", "urgent"].includes(urgency) ? urgency : "info";
  const cleanDur = Math.max(3, Math.min(60, parseInt(duration || 10, 10)));

  const socket = getDeviceSocket(req, deviceId);
  if (!socket) {
    return res.status(404).json({ error: "Device is offline" });
  }

  // Insert in message log
  const result = db
    .prepare(
      `INSERT INTO child_messages (device_id, title, message, urgency, duration, sent_by)
       VALUES (?, ?, ?, ?, ?, ?)`
    )
    .run(deviceId, cleanTitle, cleanMsg, cleanUrg, cleanDur, req.user.username);

  // Send to device
  socket.emit("send_child_message", {
    title: cleanTitle,
    message: cleanMsg,
    urgency: cleanUrg,
    duration: cleanDur,
  });

  res.json({
    id: result.lastInsertRowid,
    ok: true,
    message: "Message sent to child's screen",
  });
});

/**
 * GET /api/apps/messages/:deviceId
 * Get history of messages sent to a device
 */
router.get("/messages/:deviceId", (req, res) => {
  const deviceId = parseInt(req.params.deviceId, 10);
  const rows = db
    .prepare(
      "SELECT * FROM child_messages WHERE device_id = ? ORDER BY created_at DESC LIMIT 20"
    )
    .all(deviceId);
  res.json(rows);
});

module.exports = {
  router,
  runningAppsCache,
  activeAppCache,
  activeTimersCache,
  broadcastBlockedApps,
};
