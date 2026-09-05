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
const {
  router: appsRouter,
  runningAppsCache,
  activeAppCache,
  activeTimersCache,
  broadcastBlockedApps,
} = require("./routes/apps");
const { ingestBatch } = require("./services/progressIngest");
const rollup = require("./services/rollup");
const telegram = require("./services/telegram");

// ─── Express Setup ──────────────────────────────────────────────────────────

const app = express();
const server = http.createServer(app);

const PORT = process.env.PORT || 3000;
const AGENT_SHARED_SECRET = (process.env.AGENT_SHARED_SECRET || "").trim();
const AGENT_AUTH_REQUIRED = String(process.env.AGENT_AUTH_REQUIRED || "").toLowerCase() === "true";

// BUG-12: which Socket.IO clients are allowed to receive device data. Screenshots
// are a child's live screen, so only sockets that prove they are a logged-in parent
// join the viewer room. Set SOCKET_AUTH_REQUIRED=false only for local debugging.
const SOCKET_AUTH_REQUIRED =
  String(process.env.SOCKET_AUTH_REQUIRED || "true").toLowerCase() !== "false";

// Room that receives dashboard-bound broadcasts. Previously these went out via
// io.emit(), which also sent every frame straight back to the agent that produced
// it — roughly 4 MB/s of pointless upload on a Pi at 20 fps.
const VIEWER_ROOM = "dashboard-viewers";
const SCREENSHOT_PERSIST_INTERVAL_MS = Math.max(
  0,
  parseInt(process.env.SCREENSHOT_PERSIST_INTERVAL_MS || "1000", 10) || 1000
);
// Default stream fps, per platform. The Pi captures through sudo+grim, which
// writes a PNG to disk and re-encodes it every frame, so it cannot sustain the
// Windows rate. Pushing a flat 20 fps at it queued frames until the socket stalled
// and the device looked offline. Both values stay overridable from the dashboard's
// stream settings (the `stream_config` event below).
function clampFps(value, fallback) {
  return Math.max(1, Math.min(30, parseInt(value, 10) || fallback));
}

const DEFAULT_STREAM_TARGET_FPS = clampFps(process.env.DEFAULT_STREAM_TARGET_FPS, 20);
const DEFAULT_STREAM_TARGET_FPS_LINUX = clampFps(
  process.env.DEFAULT_STREAM_TARGET_FPS_LINUX,
  5
);

/** Pick the connect-time fps for a device based on the platform it reported. */
function defaultFpsForPlatform(platform) {
  const p = String(platform || "").toLowerCase();
  if (p.includes("linux") || p.includes("arm") || p.includes("raspberry")) {
    return DEFAULT_STREAM_TARGET_FPS_LINUX;
  }
  return DEFAULT_STREAM_TARGET_FPS;
}
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

/** Send a dashboard-bound event to authorized viewers only, never back to agents. */
function emitToViewers(event, payload) {
  io.to(VIEWER_ROOM).emit(event, payload);
}

/**
 * Refresh a device's liveness from any inbound agent traffic.
 *
 * `status` used to be a one-way latch: it was set to 'online' only inside
 * register_device, so once the 20s sweeper below flipped a device to 'offline' it
 * stayed offline in the UI until the agent reconnected — even while frames kept
 * arriving. Any traffic now restores it, and a real transition is broadcast.
 */
function touchDeviceOnline(deviceId, deviceName, socketId) {
  if (!deviceId) return;
  try {
    const row = db.prepare("SELECT status, device_name FROM devices WHERE id = ?").get(deviceId);
    if (!row) return;

    if (row.status === "online") {
      db.prepare("UPDATE devices SET last_seen = CURRENT_TIMESTAMP WHERE id = ?").run(deviceId);
      return;
    }

    db.prepare(
      `UPDATE devices SET status = 'online', last_seen = CURRENT_TIMESTAMP, socket_id = ?
       WHERE id = ?`
    ).run(socketId || null, deviceId);

    console.log(`Device back online: ${deviceName || row.device_name || deviceId}`);
    emitToViewers("device_status_changed", {
      device_id: deviceId,
      device_name: deviceName || row.device_name,
      status: "online",
    });
  } catch (err) {
    console.error(`Device liveness update error: ${err.message}`);
  }
}

/**
 * Resolve the device row for an agent socket, tolerating a socket that has not
 * finished register_device yet.
 *
 * Copied from the running_apps_report idiom rather than shared with it, because
 * that handler silently returns on an unknown device while the progress handlers
 * have to answer the agent — an unanswered ack means the agent retries the same
 * batch every 30 seconds forever.
 */
function resolveAgentDeviceId(socket, data) {
  if (socket.deviceId) return socket.deviceId;
  const name = socket.deviceName || (data && data.device_name);
  if (!name) return null;
  try {
    const dev = db.prepare("SELECT id FROM devices WHERE device_name = ?").get(name);
    if (dev) {
      socket.deviceId = dev.id;
      return dev.id;
    }
  } catch (err) {
    console.error(`[progress] device lookup failed: ${err.message}`);
  }
  return null;
}

/**
 * Ingest one spooled progress batch, acknowledge it, then recompute the days it
 * touched.
 *
 * Order matters. The ack goes out first so the agent can free its spool without
 * waiting on ledger maths, and the rollup runs on the next tick where a failure
 * is logged instead of turning into an agent-side retry loop.
 */
function handleProgressBatch(socket, ledger, data, ack) {
  const reply = typeof ack === "function" ? ack : () => {};
  if (!data) return reply({ ok: false, error: "empty payload" });

  const devId = resolveAgentDeviceId(socket, data);
  if (!devId) {
    // Explicit refusal, not silence: the agent keeps the records and retries
    // once registration has actually happened.
    return reply({ ok: false, error: "unknown device" });
  }

  const result = ingestBatch(devId, ledger, data.records);
  reply(result);

  if (!result.ok || !result.days || !result.days.length) return;
  touchDeviceOnline(devId, socket.deviceName || data.device_name, socket.id);
  setImmediate(() => {
    try {
      rollup.rollupMany(result.days);
      emitToViewers("progress_updated", {
        device_id: devId,
        ledger,
        accepted: result.accepted,
        days: result.days.map((d) => String(d).split("|")[1]),
      });
    } catch (err) {
      console.error(`[progress] rollup after ${ledger} batch failed: ${err.message}`);
    }
  });
}

// Mark sockets as stale if they stop sending heartbeats/screenshots. The sweeper
// used to mutate the DB silently, so the dashboard kept showing a device as online
// (or offline) until the next full /api/devices poll — it now announces each row it
// actually changes.
setInterval(() => {
  try {
    const stale = db
      .prepare(
        `SELECT id, device_name FROM devices
         WHERE status = 'online'
           AND last_seen IS NOT NULL
           AND last_seen < datetime('now', '-20 seconds')`
      )
      .all();
    if (!stale.length) return;

    const markOffline = db.prepare(
      "UPDATE devices SET status = 'offline', socket_id = NULL WHERE id = ?"
    );
    for (const dev of stale) {
      markOffline.run(dev.id);
      console.log(`Device timed out (no traffic for 20s): ${dev.device_name}`);
      emitToViewers("device_status_changed", {
        device_id: dev.id,
        device_name: dev.device_name,
        status: "offline",
      });
    }
  } catch (err) {
    console.error(`Stale-device cleanup error: ${err.message}`);
  }
}, 10 * 1000);

// ─── Socket authorization (BUG-12) ─────────────────────────────────────────
//
// Every socket used to be anonymous and every dashboard broadcast went to all of
// them, so anything that could reach port 3000 could watch the child's screen and
// retune the stream. Sockets are now classified once at handshake time:
//
//   * viewer — presented a valid parent JWT; joins VIEWER_ROOM and may send
//     control events.
//   * agent  — presented the agent shared secret, or later calls register_device.
//     Never joins VIEWER_ROOM, so frames are not echoed back at it.
//   * anonymous — connects (the login page needs this) but receives no device data
//     and may not send control events.
//
// The connection itself is never refused: refusing it would leave the dashboard in
// a reconnect loop before the parent has had a chance to log in.
const { verifyToken } = require("./routes/auth");

function classifySocket(socket, authPayload) {
  const payload = authPayload || {};

  const agentToken = typeof payload.agent_token === "string" ? payload.agent_token.trim() : "";
  if (AGENT_SHARED_SECRET && agentToken && agentToken === AGENT_SHARED_SECRET) {
    socket.isAgent = true;
    return "agent";
  }

  const user = verifyToken(payload.token);
  if (user) {
    socket.isViewer = true;
    socket.user = { id: user.id, username: user.username, role: user.role };
    return "viewer";
  }

  return "anonymous";
}

io.use((socket, next) => {
  const kind = classifySocket(socket, socket.handshake.auth);
  socket.authKind = kind;
  if (kind === "anonymous" && SOCKET_AUTH_REQUIRED) {
    console.warn(
      `Socket ${socket.id} connected without credentials — no device data will be sent`
    );
  }
  next();
});

/** True when this socket is allowed to receive device data / send control events. */
function isAuthorizedViewer(socket) {
  if (socket.isAgent) return false;
  return socket.isViewer || !SOCKET_AUTH_REQUIRED;
}

io.on("connection", (socket) => {
  console.log(`Socket connected: ${socket.id} (${socket.authKind})`);

  if (isAuthorizedViewer(socket)) {
    socket.join(VIEWER_ROOM);
  }

  /**
   * Dashboard upgrades an already-open socket after login, so the parent does not
   * have to reload the page to start receiving frames.
   */
  socket.on("dashboard_auth", (data, ack) => {
    if (socket.isAgent) return;
    const kind = classifySocket(socket, data);
    socket.authKind = kind;
    if (isAuthorizedViewer(socket)) {
      socket.join(VIEWER_ROOM);
    } else {
      socket.leave(VIEWER_ROOM);
    }
    if (typeof ack === "function") {
      ack({ ok: kind === "viewer", authorized: isAuthorizedViewer(socket) });
    }
  });

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

    // This socket is an agent, not a dashboard. Take it out of the viewer room so
    // its own screenshots are not broadcast straight back to it.
    socket.isAgent = true;
    socket.isViewer = false;
    socket.authKind = "agent";
    socket.leave(VIEWER_ROOM);

    // Push stream settings FIRST. This one is cheap for the agent to apply, and
    // ordering it ahead of the blocklist means a slow blocklist sweep can no longer
    // delay the mirror from starting at the right frame rate.
    const targetFps = defaultFpsForPlatform(safePlatform);
    socket.emit("update_config", {
      target_fps: targetFps,
      screenshot_quality: DEFAULT_STREAM_QUALITY,
      max_width: DEFAULT_STREAM_MAX_WIDTH,
      low_latency_mode: true,
    });
    console.log(
      `[stream] ${safeDeviceName} (${safePlatform}) → target_fps=${targetFps}, ` +
        `quality=${DEFAULT_STREAM_QUALITY}, max_width=${DEFAULT_STREAM_MAX_WIDTH}`
    );

    // Send current custom blocklist to the agent
    const blocklist = db
      .prepare("SELECT domain FROM blocklist")
      .all()
      .map((r) => r.domain);
    socket.emit("update_blocklist", { domains: blocklist });

    // Request initial application inventory
    socket.emit("request_installed_apps");
    socket.emit("request_running_apps");

    // Send blocked applications last: applying it makes the agent walk the whole
    // process table, so it goes after the cheap events. Still unconditional — an
    // empty list is how a removed rule gets lifted on a reconnect.
    const blockedApps = db
      .prepare(
        "SELECT process_name FROM blocked_apps WHERE device_id IS NULL OR device_id = ?"
      )
      .all(deviceId)
      .map((r) => r.process_name);
    socket.emit("update_blocked_apps", { blocked_apps: blockedApps });

    // Notify dashboard
    emitToViewers("device_status_changed", {
      device_id: deviceId,
      device_name: safeDeviceName,
      status: "online",
    });
  });

  /**
   * Agent liveness ping (agent sends one every 5s).
   *
   * The server used to infer liveness purely from incoming screenshots, so a slow
   * capture — grim on a Pi can take seconds — or a capture backend failure looked
   * exactly like a dead device and tripped the 20s sweeper. That was the "goes
   * offline every few minutes" report. Heartbeats are independent of capture.
   */
  socket.on("heartbeat", (data) => {
    let devId = socket.deviceId;
    if (!devId) {
      const name = socket.deviceName || (data && data.device_name);
      if (!name) return;
      const dev = db.prepare("SELECT id FROM devices WHERE device_name = ?").get(name);
      if (!dev) return;
      devId = dev.id;
      socket.deviceId = dev.id;
      socket.deviceName = name;
    }
    touchDeviceOnline(devId, socket.deviceName, socket.id);
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

    // Update last_seen (and restore 'online' if the sweeper had flipped it).
    const lastSeen = lastSeenAtByDevice.get(socket.deviceId) || 0;
    if (nowMs - lastSeen >= 1000) {
      touchDeviceOnline(socket.deviceId, socket.deviceName || device_name, socket.id);
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
        emitToViewers("device_telemetry", telemetry);
      }
    }

    // Forward to dashboard viewers in real-time. Room-scoped, not io.emit: a plain
    // broadcast also shipped every frame back to the agent that sent it, which on a
    // Pi at 20 fps meant several MB/s of wasted upload and a socket that eventually
    // stalled long enough for the server to call the device offline.
    emitToViewers("live_screenshot", {
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
    const inserted = db.prepare(
      `INSERT INTO alerts (device_id, alert_type, message, details)
       VALUES (?, ?, ?, ?)`
    ).run(
      socket.deviceId || null,
      type,
      message,
      JSON.stringify(details || {})
    );

    // Forward to dashboard in real-time
    emitToViewers("new_alert", {
      device_id: socket.deviceId,
      device_name,
      type,
      message,
      details,
      timestamp,
    });

    // Push to the parent's phone. Fire and forget by design: the alert is already
    // persisted and on the dashboard, so this handler must never wait on — or be
    // broken by — an outbound HTTP call. The alerts row id is the dedupe key, so
    // one alert produces exactly one message even if the server restarts.
    telegram.notifyAlert(
      { device_name, type, message, details },
      inserted.lastInsertRowid
    );
  });

  /**
   * Agent reports live running applications and foreground app.
   */
  socket.on("running_apps_report", (data) => {
    if (!data) return;
    let devId = socket.deviceId;
    if (!devId && (socket.deviceName || data.device_name)) {
      const dev = db.prepare("SELECT id FROM devices WHERE device_name = ?").get(socket.deviceName || data.device_name);
      if (dev) {
        devId = dev.id;
        socket.deviceId = dev.id;
      }
    }
    if (!devId) return;

    const { active_app, running_apps, active_timers, timestamp } = data;

    if (active_app) {
      activeAppCache.set(devId, active_app);
    }
    if (Array.isArray(running_apps)) {
      runningAppsCache.set(devId, running_apps);
    }
    if (Array.isArray(active_timers)) {
      activeTimersCache.set(devId, active_timers);
    }

    // Inbound agent traffic also counts as liveness, so an app sync keeps the
    // device online even if capture is struggling.
    touchDeviceOnline(devId, socket.deviceName || data.device_name, socket.id);

    // Broadcast to web dashboard
    emitToViewers("running_apps_updated", {
      device_id: devId,
      device_name: socket.deviceName || data.device_name,
      active_app,
      running_apps: running_apps || [],
      active_timers: active_timers || [],
      timestamp: timestamp || Date.now(),
    });
  });

  /**
   * Agent reports installed applications.
   */
  socket.on("installed_apps_report", (data) => {
    if (!data || !Array.isArray(data.installed_apps)) return;
    let devId = socket.deviceId;
    if (!devId && (socket.deviceName || data.device_name)) {
      const dev = db.prepare("SELECT id FROM devices WHERE device_name = ?").get(socket.deviceName || data.device_name);
      if (dev) {
        devId = dev.id;
        socket.deviceId = dev.id;
      }
    }
    if (!devId) return;

    const { installed_apps } = data;

    try {
      const insert = db.prepare(
        `INSERT INTO device_apps (device_id, app_name, process_name, install_path, category, last_seen)
         VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
         ON CONFLICT(device_id, app_name) DO UPDATE SET
           process_name = excluded.process_name,
           install_path = excluded.install_path,
           category = excluded.category,
           last_seen = CURRENT_TIMESTAMP`
      );

      const tx = db.transaction((apps) => {
        for (const app of apps) {
          if (!app.app_name) continue;
          insert.run(
            devId,
            app.app_name,
            app.process_name || "",
            app.path || "",
            app.category || "general"
          );
        }
      });
      tx(installed_apps);
      console.log(`[apps] Synced ${installed_apps.length} installed apps for device ${socket.deviceName || devId}`);

      emitToViewers("installed_apps_updated", {
        device_id: devId,
        count: installed_apps.length,
      });
    } catch (err) {
      console.error(`[apps] Error persisting installed apps: ${err.message}`);
    }
  });

  /**
   * Academic-progress ingest — foreground app sessions.
   *
   * Acknowledged, unlike every other agent event here: the agent keeps these
   * records in an on-disk spool and only deletes a batch once the server says it
   * has them. An un-acked batch is retried on the next tick, so a dashboard
   * restart in the middle of an upload costs nothing.
   *
   * The rollup runs after the ack, not before it. Recomputing a day is fast, but
   * it is not the agent's problem — making it wait would put ledger maths on the
   * critical path of a 20-second socket timeout.
   */
  socket.on("usage_batch", (data, ack) => {
    handleProgressBatch(socket, "usage", data, ack);
  });

  /**
   * Academic-progress ingest — browser history (web + YouTube).
   * A separate event from usage_batch on purpose: the two ledgers never travel
   * in the same batch, so no server-side bug can merge their seconds.
   */
  socket.on("web_batch", (data, ack) => {
    handleProgressBatch(socket, "web", data, ack);
  });

  /**
   * Agent sends timer update/expiration.
   */
  socket.on("app_timer_update", (data) => {
    if (!socket.deviceId || !data || !data.timer) return;
    const timer = data.timer;
    const status = timer.status || "active";

    try {
      db.prepare(
        `UPDATE app_timers SET status = ? WHERE device_id = ? AND process_name = ?`
      ).run(status, socket.deviceId, timer.process_name);

      emitToViewers("app_timer_changed", {
        device_id: socket.deviceId,
        timer,
      });
    } catch (err) {
      console.debug(`Timer DB update note: ${err.message}`);
    }
  });

  /**
   * Agent confirms message delivered.
   */
  socket.on("child_message_delivered", (data) => {
    if (!socket.deviceId || !data) return;
    try {
      db.prepare(
        `UPDATE child_messages SET delivered = 1
         WHERE device_id = ? AND title = ? AND delivered = 0`
      ).run(socket.deviceId, data.title);

      emitToViewers("child_message_status", {
        device_id: socket.deviceId,
        delivered: true,
        title: data.title,
      });
    } catch (err) {
      console.debug(`Message status DB note: ${err.message}`);
    }
  });

  /**
   * Dashboard requests live stream tuning update (FPS/quality/resolution).
   * Forward to a specific device (or all devices if no target provided).
   */
  socket.on("stream_config", (data) => {
    // BUG-12: retuning the stream is a parent action. An unauthenticated socket
    // could previously drive every agent's frame rate and quality.
    if (!isAuthorizedViewer(socket)) {
      console.warn(`Rejected stream_config from unauthorized socket ${socket.id}`);
      socket.emit("stream_config_applied", {
        error: "Unauthorized",
        delivered: 0,
        device_id: null,
      });
      return;
    }

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

    if (!socket.deviceId) return;

    // Only the device's CURRENT socket may mark it offline.
    //
    // Reconnects and duplicate agents (two supervisors used to be able to start two
    // agents under one device_name) overlap: the new socket registers, then the old
    // one finally times out. Without this guard that stale disconnect flipped a
    // perfectly live device to 'offline' and wiped its caches — the flap the user saw.
    const current = db
      .prepare("SELECT socket_id, device_name FROM devices WHERE id = ?")
      .get(socket.deviceId);

    if (current && current.socket_id && current.socket_id !== socket.id) {
      console.log(
        `Ignoring stale disconnect for ${current.device_name}: ` +
          `device is live on socket ${current.socket_id}`
      );
      return;
    }

    db.prepare(
      "UPDATE devices SET status = 'offline', socket_id = NULL WHERE id = ? AND (socket_id = ? OR socket_id IS NULL)"
    ).run(socket.deviceId, socket.id);
    lastPersistAtByDevice.delete(socket.deviceId);
    lastSeenAtByDevice.delete(socket.deviceId);
    deviceTelemetry.delete(socket.deviceId);
    lastTelemetryEmitAtByDevice.delete(socket.deviceId);

    emitToViewers("device_status_changed", {
      device_id: socket.deviceId,
      device_name: socket.deviceName,
      status: "offline",
    });
  });
});

// ─── REST Routes ────────────────────────────────────────────────────────────

const { router: authRouter } = require("./routes/auth");
app.use("/api/auth", authRouter);
app.use("/api/devices", require("./routes/devices"));
app.use("/api/blocklist", require("./routes/blocklist"));
app.use("/api/alerts", require("./routes/alerts"));
app.use("/api/apps", appsRouter);
// Academic progress. Mounted with the other routers so it is registered before
// the app.get("*") SPA fallback further down — after it, every progress request
// would be answered with index.html and the page would silently show no data.
app.use("/api/progress", require("./routes/progress"));

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

// Health check — BUG-01: now also returns DB screenshot stats
app.get("/api/health", (req, res) => {
  const screenshotCount = db
    .prepare("SELECT COUNT(*) as count FROM screenshots")
    .get().count;
  const retentionDays = parseInt(process.env.SCREENSHOT_RETENTION_DAYS || "3", 10);
  res.json({
    status: "ok",
    uptime: process.uptime(),
    devices_online: db
      .prepare("SELECT COUNT(*) as count FROM devices WHERE status = 'online'")
      .get().count,
    screenshot_count: screenshotCount,
    screenshot_retention_days: retentionDays,
  });
});

// BUG-01 FIX: Manual cleanup endpoint — DELETE /api/admin/cleanup?days=N
// Lets the parent force a DB cleanup without restarting the server.
const { authMiddleware } = require("./routes/auth");
app.delete("/api/admin/cleanup", authMiddleware, (req, res) => {
  const days = Math.max(1, parseInt(req.query.days || "3", 10));
  try {
    const before = db.prepare("SELECT COUNT(*) as cnt FROM screenshots").get().cnt;
    const result = db
      .prepare(`DELETE FROM screenshots WHERE captured_at < datetime('now', ? || ' days')`)
      .run(`-${days}`);
    db.pragma("wal_checkpoint(TRUNCATE)");
    const after = db.prepare("SELECT COUNT(*) as cnt FROM screenshots").get().cnt;
    console.log(`[admin/cleanup] Deleted ${result.changes} screenshots older than ${days} days`);
    res.json({
      ok: true,
      deleted: result.changes,
      remaining: after,
      days_kept: days,
    });
  } catch (err) {
    console.error(`[admin/cleanup] Error: ${err.message}`);
    res.status(500).json({ error: err.message });
  }
});

// SPA fallback — serve dashboard for all non-API routes
app.get("*", (req, res, next) => {
  if (req.path.startsWith("/socket.io/")) {
    return next();
  }

  // BUG-14: an unknown /api/ path used to fall through to index.html, so the
  // dashboard received HTML where it expected JSON and reported a parse error
  // instead of a 404. That made a typo'd or missing endpoint look like the whole
  // page was broken.
  if (req.path.startsWith("/api/")) {
    return res.status(404).json({ error: "Not found", path: req.path });
  }

  const indexPath = path.join(__dirname, "..", "web-ts", "dist", "index.html");
  res.sendFile(indexPath, (err) => {
    if (!err) return;
    console.error(`Dashboard bundle missing at ${indexPath}: ${err.message}`);
    res
      .status(503)
      .type("text/plain")
      .send(
        "Guardian Shield dashboard is not built yet.\n\n" +
          "Run:  cd parent-dashboard/web-ts && npm install && npm run build\n"
      );
  });
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
