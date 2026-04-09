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

  CREATE INDEX IF NOT EXISTS idx_screenshots_device_time
    ON screenshots(device_id, captured_at DESC);
  CREATE INDEX IF NOT EXISTS idx_alerts_device_time
    ON alerts(device_id, created_at DESC);
  CREATE INDEX IF NOT EXISTS idx_devices_status
    ON devices(status);
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

function cleanupOldScreenshots() {
  const days = parseInt(process.env.SCREENSHOT_RETENTION_DAYS || "7", 10);
  const result = db
    .prepare(
      `DELETE FROM screenshots WHERE captured_at < datetime('now', ? || ' days')`
    )
    .run(`-${days}`);
  if (result.changes > 0) {
    console.log(`Cleaned up ${result.changes} old screenshots (>${days} days)`);
  }
}

// Run cleanup every hour
setInterval(cleanupOldScreenshots, 60 * 60 * 1000);
cleanupOldScreenshots();

module.exports = db;
