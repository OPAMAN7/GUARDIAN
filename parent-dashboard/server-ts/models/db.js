/**
 * Guardian Shield — Database Setup (SQLite)
 * Creates and manages all database tables.
 */

const Database = require("better-sqlite3");
const path = require("path");
const bcrypt = require("bcryptjs");

// The default is the production database. GUARDIAN_DB_PATH is an escape hatch
// for the offline test suites: they need to boot *this* schema (not a copy that
// would drift) against a throwaway file. Without it, running a test on the
// parent's machine would write fake devices and fake usage into the real
// database, and the numbers on the Progress page would be wrong forever after.
const DB_PATH = process.env.GUARDIAN_DB_PATH
  ? path.resolve(process.env.GUARDIAN_DB_PATH)
  : path.join(__dirname, "..", "data", "guardian.db");

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

// ─── Academic Progress Tables (F-18..F-25) ───────────────────────────────────
// Kept in a separate db.exec() block on purpose: the whole feature can be
// reverted by deleting this one contiguous section.
//
// HARD RULE: never add `ALTER TABLE ... ADD COLUMN` to this file. db.exec()
// re-runs the entire schema on EVERY server start and SQLite has no
// `ADD COLUMN IF NOT EXISTS`, so an ALTER works on first boot and then crashes
// the server with "duplicate column name" forever after. Every column a future
// step needs must be declared here, in the CREATE TABLE, up front.
//
// HARD RULE: the three data sources ('app', 'web', 'watch') are never blended.
// `source` is part of the primary key of progress_daily for exactly that reason.

db.exec(`
  -- Foreground application sessions, collapsed on the agent before upload.
  -- session_key is agent-generated and stable so a replayed offline spool
  -- re-inserts nothing (INSERT OR IGNORE + UNIQUE).
  CREATE TABLE IF NOT EXISTS usage_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    session_key TEXT NOT NULL,
    app_name TEXT NOT NULL,
    process_name TEXT,
    window_title TEXT,
    started_at DATETIME NOT NULL,
    ended_at DATETIME NOT NULL,
    duration_sec INTEGER NOT NULL DEFAULT 0,
    day TEXT NOT NULL,
    is_browser INTEGER NOT NULL DEFAULT 0,
    app_category TEXT DEFAULT 'other',
    academic_weight INTEGER DEFAULT 0,
    platform TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
    UNIQUE(device_id, session_key)
  );

  -- Browser history rows. platform/media_id/media_kind/watch_category are
  -- declared HERE (not ALTERed in later) so the YouTube step needs no migration.
  -- url_path deliberately excludes the query string: no search terms stored.
  CREATE TABLE IF NOT EXISTS web_visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    visit_key TEXT NOT NULL,
    browser TEXT NOT NULL,
    domain TEXT NOT NULL,
    url_path TEXT,
    title TEXT,
    visited_at DATETIME NOT NULL,
    day TEXT NOT NULL,
    dwell_sec INTEGER NOT NULL DEFAULT 0,
    visit_count INTEGER DEFAULT 1,
    platform TEXT DEFAULT 'web',
    media_id TEXT,
    media_kind TEXT,
    web_category TEXT DEFAULT 'other',
    watch_category TEXT,
    academic_weight INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
    UNIQUE(device_id, visit_key)
  );

  -- Classification rules. Scoped by source so an 'education' rule for web can
  -- never silently re-label an app, and parent overrides (origin='parent')
  -- always win over the seeded defaults.
  CREATE TABLE IF NOT EXISTS activity_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    match_type TEXT NOT NULL,
    match_value TEXT NOT NULL,
    category TEXT NOT NULL,
    academic_weight INTEGER NOT NULL DEFAULT 0,
    origin TEXT DEFAULT 'seed',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, match_type, match_value)
  );

  -- Nightly fold. UNIQUE includes the source column — without it the three
  -- sources collapse into one row and the "do not mix them up" rule breaks.
  -- counts_toward_total=0 marks the browser-app envelope, which is displayed
  -- but never summed (it is the same wall-clock time as the web rows).
  CREATE TABLE IF NOT EXISTS progress_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    category TEXT NOT NULL,
    seconds INTEGER NOT NULL DEFAULT 0,
    events INTEGER NOT NULL DEFAULT 0,
    academic_weight INTEGER NOT NULL DEFAULT 0,
    counts_toward_total INTEGER NOT NULL DEFAULT 1,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
    UNIQUE(device_id, day, source, category)
  );

  -- Every component of the grade is stored, not just the result, so the UI can
  -- show the arithmetic and a parent can check it by hand.
  CREATE TABLE IF NOT EXISTS progress_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    academic_sec INTEGER DEFAULT 0,
    constructive_sec INTEGER DEFAULT 0,
    neutral_sec INTEGER DEFAULT 0,
    passive_sec INTEGER DEFAULT 0,
    distraction_sec INTEGER DEFAULT 0,
    counted_sec INTEGER DEFAULT 0,
    browser_envelope_sec INTEGER DEFAULT 0,
    effort_sec INTEGER DEFAULT 0,
    target_sec INTEGER DEFAULT 3600,
    focus_pct REAL DEFAULT 0,
    attainment_pct REAL DEFAULT 0,
    penalty REAL DEFAULT 0,
    score INTEGER DEFAULT 0,
    grade TEXT DEFAULT 'N/A',
    has_data INTEGER DEFAULT 0,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
    UNIQUE(device_id, day)
  );

  CREATE TABLE IF NOT EXISTS progress_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    daily_academic_min INTEGER NOT NULL DEFAULT 60,
    weekly_target_days INTEGER NOT NULL DEFAULT 5,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
    UNIQUE(device_id)
  );

  -- AI reports are cached by input_hash: identical aggregates never burn a
  -- second free-tier call.
  CREATE TABLE IF NOT EXISTS ai_insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    model TEXT,
    summary TEXT,
    strengths TEXT,
    focus_areas TEXT,
    next_steps TEXT,
    raw_json TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
    UNIQUE(device_id, input_hash)
  );

  CREATE TABLE IF NOT EXISTS ai_usage (
    day TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
  );

  -- Outbound notification ledger (Telegram today). UNIQUE(channel, ref_type,
  -- ref_id) is the dedupe: an alert is pushed once, even across restarts.
  CREATE TABLE IF NOT EXISTS notify_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    ref_type TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    status TEXT,
    detail TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(channel, ref_type, ref_id)
  );

  CREATE INDEX IF NOT EXISTS idx_usage_sessions_dev_day
    ON usage_sessions(device_id, day);
  CREATE INDEX IF NOT EXISTS idx_usage_sessions_started
    ON usage_sessions(started_at);
  CREATE INDEX IF NOT EXISTS idx_web_visits_dev_day
    ON web_visits(device_id, day);
  CREATE INDEX IF NOT EXISTS idx_web_visits_visited
    ON web_visits(visited_at);
  CREATE INDEX IF NOT EXISTS idx_web_visits_platform
    ON web_visits(device_id, platform, day);
  CREATE INDEX IF NOT EXISTS idx_progress_daily_lookup
    ON progress_daily(device_id, day, source);
  CREATE INDEX IF NOT EXISTS idx_progress_scores_day
    ON progress_scores(device_id, day DESC);
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

// ─── Progress Data Retention ─────────────────────────────────────────────────
// usage_sessions and web_visits are the two new high-write tables. Raw rows are
// only needed until the nightly rollup has folded them, so they get a hard
// retention window; the folded aggregates are tiny and kept far longer.
// Progress data is the most sensitive data in the product — it does not sit
// on disk indefinitely by default.

function cleanupProgressData() {
  const rawDays = parseInt(process.env.PROGRESS_RETENTION_DAYS || "90", 10);
  const foldDays = parseInt(process.env.PROGRESS_ROLLUP_RETENTION_DAYS || "400", 10);
  let removed = 0;
  try {
    removed += db
      .prepare(`DELETE FROM usage_sessions WHERE started_at < datetime('now', ? || ' days')`)
      .run(`-${rawDays}`).changes;
    removed += db
      .prepare(`DELETE FROM web_visits WHERE visited_at < datetime('now', ? || ' days')`)
      .run(`-${rawDays}`).changes;
    removed += db
      .prepare(`DELETE FROM progress_daily WHERE day < date('now', ? || ' days')`)
      .run(`-${foldDays}`).changes;
    removed += db
      .prepare(`DELETE FROM progress_scores WHERE day < date('now', ? || ' days')`)
      .run(`-${foldDays}`).changes;
    removed += db
      .prepare(`DELETE FROM notify_log WHERE created_at < datetime('now','-30 days')`)
      .run().changes;
  } catch (err) {
    console.warn(`[DB] Progress cleanup warning: ${err.message}`);
    return;
  }
  if (removed > 0) {
    console.log(`[DB] Progress retention removed ${removed} rows (raw >${rawDays}d, folded >${foldDays}d)`);
    try {
      db.pragma("wal_checkpoint(TRUNCATE)");
    } catch (err) {
      console.warn(`[DB] WAL checkpoint warning: ${err.message}`);
    }
  }
}

setInterval(cleanupProgressData, 6 * 60 * 60 * 1000);
cleanupProgressData();

module.exports = db;
