/**
 * Guardian Shield — Main Server
 * Express + Socket.IO server for real-time agent communication and REST API.
 */

require("dotenv").config();

const express = require("express");
const http = require("http");
const { Server } = require("socket.io");
const cors = require("cors");
const helmet = require("helmet");
const morgan = require("morgan");
const path = require("path");
const db = require("./models/db");

// ─── Express Setup ──────────────────────────────────────────────────────────

const app = express();
const server = http.createServer(app);

const PORT = process.env.PORT || 3000;
const AGENT_SHARED_SECRET = (process.env.AGENT_SHARED_SECRET || "").trim();
const AGENT_AUTH_REQUIRED = String(process.env.AGENT_AUTH_REQUIRED || "").toLowerCase() === "true";
const SCREENSHOT_PERSIST_INTERVAL_MS = Math.max(
  0,
  parseInt(process.env.SCREENSHOT_PERSIST_INTERVAL_MS || "1000", 10) || 1000
);
const DEFAULT_STREAM_TARGET_FPS = Math.max(
  1,
  Math.min(30, parseInt(process.env.DEFAULT_STREAM_TARGET_FPS || "20", 10) || 20)
);
const DEFAULT_STREAM_QUALITY = Math.max(
  20,
  Math.min(90, parseInt(process.env.DEFAULT_STREAM_QUALITY || "50", 10) || 50)
);
const DEFAULT_STREAM_MAX_WIDTH = Math.max(
  640,
  Math.min(1920, parseInt(process.env.DEFAULT_STREAM_MAX_WIDTH || "1280", 10) || 1280)
);

const allowedOrigins = (process.env.ALLOWED_ORIGINS || "")
  .split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);

const corsOptions = allowedOrigins.length
  ? { origin: allowedOrigins, methods: ["GET", "POST", "PUT", "DELETE"] }
  : { origin: "*", methods: ["GET", "POST", "PUT", "DELETE"] };

// Middleware
app.use(cors(corsOptions));
app.use(helmet({ contentSecurityPolicy: false }));
app.use(morgan("short"));
app.use(express.json({ limit: "10mb" }));
app.use(express.urlencoded({ extended: true }));

// Serve the dashboard static files
app.use(express.static(path.join(__dirname, "..", "web-ts", "dist")));

// ─── Socket.IO Setup ───────────────────────────────────────────────────────

const io = new Server(server, {
  cors: corsOptions,
  maxHttpBufferSize: 10 * 1024 * 1024, // 10MB for screenshots
});

// Make io accessible to routes
app.set("io", io);

// Track latest screenshot per device for quick dashboard access
const latestScreenshots = new Map();
const deviceTelemetry = new Map();
const lastTelemetryEmitAtByDevice = new Map();
const lastPersistAtByDevice = new Map();
const lastSeenAtByDevice = new Map();
app.set("deviceTelemetry", deviceTelemetry);

// Clear stale online flags from previous runs so UI/device targeting starts clean.
db.prepare("UPDATE devices SET status = 'offline', socket_id = NULL WHERE status = 'online'").run();

// Mark sockets as stale if they stop sending heartbeats/screenshots.
setInterval(() => {
  try {
    db.prepare(
      `UPDATE devices
       SET status = 'offline', socket_id = NULL
       WHERE status = 'online'
         AND last_seen IS NOT NULL
         AND last_seen < datetime('now', '-20 seconds')`
    ).run();
  } catch (err) {
    console.error(`Stale-device cleanup error: ${err.message}`);
  }
}, 10 * 1000);

io.on("connection", (socket) => {
  console.log(`Socket connected: ${socket.id}`);

  // ── Agent Events ────────────────────────────────────────────────────────

  /**
   * Agent registers itself when it connects.
   * Creates or updates the device record.
   */
  socket.on("register_device", (data) => {
    if (AGENT_AUTH_REQUIRED && AGENT_SHARED_SECRET) {
      const suppliedToken = typeof data?.auth_token === "string" ? data.auth_token.trim() : "";
      if (!suppliedToken || suppliedToken !== AGENT_SHARED_SECRET) {
        console.warn(`Rejected unauthorized device registration from socket ${socket.id}`);
        socket.emit("registration_error", { error: "Unauthorized device" });
        socket.disconnect(true);
        return;
      }
    }

    const { device_name, platform, hostname } = data;
    const safeDeviceName = typeof device_name === "string" ? device_name.trim() : "";
    if (!safeDeviceName) {
      socket.emit("registration_error", { error: "Invalid device name" });
      socket.disconnect(true);
      return;
    }

    const safePlatform = typeof platform === "string" ? platform.slice(0, 64) : "unknown";
    const safeHostname = typeof hostname === "string" ? hostname.slice(0, 128) : "unknown";
    console.log(`Device registered: ${safeDeviceName} (${safePlatform})`);

    // Upsert device
    const existing = db
      .prepare("SELECT id FROM devices WHERE device_name = ?")
      .get(safeDeviceName);

    let deviceId;
    if (existing) {
      db.prepare(
        `UPDATE devices SET platform = ?, hostname = ?, status = 'online',
         last_seen = CURRENT_TIMESTAMP, socket_id = ? WHERE id = ?`
      ).run(safePlatform, safeHostname, socket.id, existing.id);
      deviceId = existing.id;
    } else {
      const result = db
        .prepare(
          `INSERT INTO devices (device_name, platform, hostname, status, last_seen, socket_id)
           VALUES (?, ?, ?, 'online', CURRENT_TIMESTAMP, ?)`
        )
        .run(safeDeviceName, safePlatform, safeHostname, socket.id);
      deviceId = result.lastInsertRowid;
    }

    socket.deviceId = deviceId;
    socket.deviceName = safeDeviceName;

    // Send current custom blocklist to the agent
    const blocklist = db
      .prepare("SELECT domain FROM blocklist")
      .all()
      .map((r) => r.domain);
    socket.emit("update_blocklist", { domains: blocklist });

    // Push default smooth-stream settings on connect so preview does not stay
    // at low fallback intervals until manual tuning is applied.
    socket.emit("update_config", {
      target_fps: DEFAULT_STREAM_TARGET_FPS,
      screenshot_quality: DEFAULT_STREAM_QUALITY,
      max_width: DEFAULT_STREAM_MAX_WIDTH,
      low_latency_mode: true,
    });

    // Notify dashboard
    io.emit("device_status_changed", {
      device_id: deviceId,
      device_name: safeDeviceName,
      status: "online",
    });
  });

  /**
   * Agent sends a screenshot.
   * Store in DB and forward to dashboard viewers.
   */
  socket.on("screenshot", (data, ack) => {
    if (typeof ack === "function") {
      ack({ ok: true });
    }

    const { device_name, image, timestamp, size, meta } = data;

    if (!socket.deviceId) return;

    const nowMs = Date.now();
    const lastPersist = lastPersistAtByDevice.get(socket.deviceId) || 0;
    const shouldPersist =
      SCREENSHOT_PERSIST_INTERVAL_MS === 0 ||
      nowMs - lastPersist >= SCREENSHOT_PERSIST_INTERVAL_MS;

    // Store in database (keep for retention period)
    if (shouldPersist) {
      try {
        db.prepare(
          `INSERT INTO screenshots (device_id, image_data, file_size, captured_at)
           VALUES (?, ?, ?, datetime(?, 'unixepoch'))`
        ).run(socket.deviceId, image, size, Math.floor(timestamp));
        lastPersistAtByDevice.set(socket.deviceId, nowMs);
      } catch (err) {
        // Non-critical, log and continue
        console.error(`Screenshot DB error: ${err.message}`);
      }
    }

    // Update last_seen
    const lastSeen = lastSeenAtByDevice.get(socket.deviceId) || 0;
    if (nowMs - lastSeen >= 1000) {
      db.prepare(
        "UPDATE devices SET last_seen = CURRENT_TIMESTAMP WHERE id = ?"
      ).run(socket.deviceId);
      lastSeenAtByDevice.set(socket.deviceId, nowMs);
    }

    // Cache latest screenshot
    latestScreenshots.set(socket.deviceId, {
      image,
      timestamp,
      size,
      meta: meta || null,
    });

    if (meta && typeof meta === "object") {
      const telemetry = {
        ...meta,
        device_id: socket.deviceId,
        device_name: socket.deviceName || device_name,
        timestamp,
      };
      deviceTelemetry.set(socket.deviceId, telemetry);
      const lastEmitAt = lastTelemetryEmitAtByDevice.get(socket.deviceId) || 0;
      if (nowMs - lastEmitAt >= 2000) {
        lastTelemetryEmitAtByDevice.set(socket.deviceId, nowMs);
        io.emit("device_telemetry", telemetry);
      }
    }

    // Forward to dashboard in real-time
    io.emit("live_screenshot", {
      device_id: socket.deviceId,
      device_name,
      image,
      timestamp,
      meta: meta || null,
    });
  });

  /**
   * Agent sends an alert (malware, dangerous file, blocked site attempt).
   */
  socket.on("alert", (data) => {
    const { device_name, type, message, details, timestamp } = data;
    console.log(`⚠️ Alert from ${device_name}: [${type}] ${message}`);

    // Store in database
    db.prepare(
      `INSERT INTO alerts (device_id, alert_type, message, details)
       VALUES (?, ?, ?, ?)`
    ).run(
      socket.deviceId || null,
      type,
      message,
      JSON.stringify(details || {})
    );

    // Forward to dashboard in real-time
    io.emit("new_alert", {
      device_id: socket.deviceId,
      device_name,
      type,
      message,
      details,
      timestamp,
    });
  });

  /**
   * Dashboard requests live stream tuning update (FPS/quality/resolution).
   * Forward to a specific device (or all devices if no target provided).
   */
  socket.on("stream_config", (data) => {
    const parsedTarget = Number.parseInt(data?.device_id, 10);
    const targetDeviceId = Number.isNaN(parsedTarget) ? null : parsedTarget;
    const cfg = data?.config || {};

    const sanitized = {
      target_fps: Math.max(1, Math.min(30, parseInt(cfg.target_fps || 20, 10))),
      screenshot_quality: Math.max(20, Math.min(90, parseInt(cfg.screenshot_quality || 50, 10))),
      max_width: Math.max(640, Math.min(1920, parseInt(cfg.max_width || 1280, 10))),
      low_latency_mode: cfg.low_latency_mode !== false,
    };

    let delivered = 0;
    for (const [, clientSocket] of io.sockets.sockets) {
      if (!clientSocket.deviceId) continue;
      const clientDeviceId = Number.parseInt(clientSocket.deviceId, 10);
      if (targetDeviceId !== null && clientDeviceId !== targetDeviceId) continue;
      clientSocket.emit("update_config", sanitized);
      delivered += 1;
    }

    socket.emit("stream_config_applied", {
      device_id: targetDeviceId,
      delivered,
      config: sanitized,
    });
  });

  // ── Disconnect ────────────────────────────────────────────────────────────

  socket.on("disconnect", () => {
    console.log(`Socket disconnected: ${socket.id}`);

    if (socket.deviceId) {
      db.prepare(
        "UPDATE devices SET status = 'offline', socket_id = NULL WHERE id = ?"
      ).run(socket.deviceId);
      lastPersistAtByDevice.delete(socket.deviceId);
      lastSeenAtByDevice.delete(socket.deviceId);
      deviceTelemetry.delete(socket.deviceId);
      lastTelemetryEmitAtByDevice.delete(socket.deviceId);

      io.emit("device_status_changed", {
        device_id: socket.deviceId,
        device_name: socket.deviceName,
        status: "offline",
      });
    }
  });
});

// ─── REST Routes ────────────────────────────────────────────────────────────

const { router: authRouter } = require("./routes/auth");
app.use("/api/auth", authRouter);
app.use("/api/devices", require("./routes/devices"));
app.use("/api/blocklist", require("./routes/blocklist"));
app.use("/api/alerts", require("./routes/alerts"));

// Get latest screenshot for a device
app.get("/api/screenshots/latest/:deviceId", (req, res) => {
  const cached = latestScreenshots.get(parseInt(req.params.deviceId));
  if (cached) {
    return res.json(cached);
  }

  const screenshot = db
    .prepare(
      `SELECT image_data, captured_at FROM screenshots
       WHERE device_id = ? ORDER BY captured_at DESC LIMIT 1`
    )
    .get(req.params.deviceId);

  if (screenshot) {
    res.json({ image: screenshot.image_data, timestamp: screenshot.captured_at });
  } else {
    res.status(404).json({ error: "No screenshots available" });
  }
});

// Get screenshot history for a device
app.get("/api/screenshots/history/:deviceId", (req, res) => {
  const limit = Math.min(parseInt(req.query.limit || "20", 10), 100);
  const screenshots = db
    .prepare(
      `SELECT id, file_size, captured_at FROM screenshots
       WHERE device_id = ? ORDER BY captured_at DESC LIMIT ?`
    )
    .all(req.params.deviceId, limit);
  res.json(screenshots);
});

// Health check
app.get("/api/health", (req, res) => {
  res.json({
    status: "ok",
    uptime: process.uptime(),
    devices_online: db
      .prepare("SELECT COUNT(*) as count FROM devices WHERE status = 'online'")
      .get().count,
  });
});

// SPA fallback — serve dashboard for all non-API routes
app.get("*", (req, res, next) => {
  if (req.path.startsWith("/socket.io/")) {
    return next();
  }
  res.sendFile(path.join(__dirname, "..", "web-ts", "dist", "index.html"));
});

// ─── Start Server ───────────────────────────────────────────────────────────

server.listen(PORT, () => {
  if (AGENT_AUTH_REQUIRED && !AGENT_SHARED_SECRET) {
    console.warn("[security] AGENT_AUTH_REQUIRED=true but AGENT_SHARED_SECRET is empty.");
  }
  console.log("");
  console.log("═══════════════════════════════════════════════");
  console.log("  🛡️  Guardian Shield Server");
  console.log(`  📡 Running on http://localhost:${PORT}`);
  console.log(`  📊 Dashboard: http://localhost:${PORT}`);
  console.log("═══════════════════════════════════════════════");
  console.log("");
});
