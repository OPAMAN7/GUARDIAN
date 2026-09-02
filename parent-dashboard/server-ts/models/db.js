/**
 * Guardian Shield — Database Setup (SQLite)
 * Creates and manages all database tables.
 */

const Database = require("better-sqlite3");
const path = require("path");
const bcrypt = require("bcryptjs");

const DB_PATH = path.join(__dirname, "..", "data", "guardian.db");

// Ensure data directory exists
const fs = require("fs");
const dataDir = path.dirname(DB_PATH);
if (!fs.existsSync(dataDir)) {
  fs.mkdirSync(dataDir, { recursive: true });
}

const db = new Database(DB_PATH);

// Enable WAL mode for better concurrent access
db.pragma("journal_mode = WAL");
db.pragma("foreign_keys = ON");
// BUG-01 FIX: Force WAL checkpoint on startup so the WAL file
// doesn't silently accumulate data from previous runs.
db.pragma("wal_checkpoint(TRUNCATE)");

// ─── Create Tables ──────────────────────────────────────────────────────────

db.exec(`
  CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'admin',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  );

  CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_name TEXT UNIQUE NOT NULL,
    platform TEXT,
    hostname TEXT,
    status TEXT DEFAULT 'offline',
    last_seen DATETIME,
    socket_id TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  );

  CREATE TABLE IF NOT EXISTS screenshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    image_data TEXT NOT NULL,
    file_size INTEGER,
    captured_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
  );

  CREATE TABLE IF NOT EXISTS blocklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT UNIQUE NOT NULL,
    category TEXT DEFAULT 'custom',
    added_by TEXT DEFAULT 'admin',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  );

  CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER,
    alert_type TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT,
    is_read INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE SET NULL
  );

  -- ─── Application Tracking & Management Tables ─────────────────────────────

  CREATE TABLE IF NOT EXISTS blocked_apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER,
    app_name TEXT NOT NULL,
    process_name TEXT NOT NULL,
    category TEXT DEFAULT 'general',
    added_by TEXT DEFAULT 'admin',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
  );

  CREATE TABLE IF NOT EXISTS app_timers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    app_name TEXT NOT NULL,
    process_name TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    expires_at DATETIME NOT NULL,
    status TEXT DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
  );

  CREATE TABLE IF NOT EXISTS device_apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    app_name TEXT NOT NULL,
    process_name TEXT,
    install_path TEXT,
    category TEXT DEFAULT 'general',
    last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
    UNIQUE(device_id, app_name)
  );

  CREATE TABLE IF NOT EXISTS child_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    urgency TEXT DEFAULT 'info',
    duration INTEGER DEFAULT 10,
    delivered INTEGER DEFAULT 0,
    sent_by TEXT DEFAULT 'admin',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
  );

  CREATE INDEX IF NOT EXISTS idx_screenshots_device_time
    ON screenshots(device_id, captured_at DESC);
  CREATE INDEX IF NOT EXISTS idx_alerts_device_time
    ON alerts(device_id, created_at DESC);
  CREATE INDEX IF NOT EXISTS idx_devices_status
    ON devices(status);
  CREATE INDEX IF NOT EXISTS idx_blocked_apps_dev_proc
    ON blocked_apps(device_id, process_name);
  CREATE INDEX IF NOT EXISTS idx_app_timers_status
    ON app_timers(device_id, status);
`);

// ─── Seed Default Admin ──────────────────────────────────────────────────────

const adminUsername = process.env.ADMIN_USERNAME || "admin";
const adminPassword = process.env.ADMIN_PASSWORD || "admin123";

const existingAdmin = db
  .prepare("SELECT id FROM users WHERE username = ?")
  .get(adminUsername);

if (!existingAdmin) {
  const hash = bcrypt.hashSync(adminPassword, 10);
  db.prepare("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)").run(
    adminUsername,
    hash,
    "admin"
  );
  console.log(`Default admin user created: ${adminUsername}`);
}

// ─── Screenshot Cleanup ──────────────────────────────────────────────────────
// BUG-01 FIX: Default retention lowered from 7 days to 3 days.
// Cleanup now runs every 30 minutes instead of 60.
// WAL is checkpointed after each cleanup to reclaim disk space.

function cleanupOldScreenshots() {
  const days = parseInt(process.env.SCREENSHOT_RETENTION_DAYS || "3", 10);
  const result = db
    .prepare(
      `DELETE FROM screenshots WHERE captured_at < datetime('now', ? || ' days')`
    )
    .run(`-${days}`);
  if (result.changes > 0) {
    console.log(`[DB] Cleaned up ${result.changes} old screenshots (>${days} days)`);
    // Checkpoint WAL after bulk delete so disk space is actually freed.
    try {
      db.pragma("wal_checkpoint(TRUNCATE)");
    } catch (err) {
      console.warn(`[DB] WAL checkpoint warning: ${err.message}`);
    }
  }
}

// Run cleanup every 30 minutes
setInterval(cleanupOldScreenshots, 30 * 60 * 1000);
cleanupOldScreenshots();

module.exports = db;
