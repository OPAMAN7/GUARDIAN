/**
 * Guardian Shield — Academic-Progress Ingest
 * ==========================================
 *
 * The one place raw collector records enter the database. Kept out of index.js
 * so the socket handlers stay four lines each and this logic is unit-testable
 * without a socket.
 *
 * Three properties this module has to guarantee:
 *
 * IDEMPOTENT. The agent's spool gives at-least-once delivery: a batch that was
 * written but whose ack was lost *will* arrive again. Every row carries an
 * agent-generated stable key and goes in with INSERT OR IGNORE against a UNIQUE
 * index, so a replay changes nothing. The ack reports `accepted` from
 * `result.changes`, and a number lower than the batch size is still success —
 * that is how the agent learns the duplicates were absorbed rather than
 * retrying them forever.
 *
 * LEDGER-SAFE. A record whose `kind` does not match the event it arrived on is
 * dropped, not coerced. The agent already ships the two ledgers on separate
 * events; if a bug ever mixed them, silently accepting the row would move time
 * between categories, which is the one failure this feature cannot have.
 *
 * BOUNDED. Everything is length- and range-clamped before it reaches SQLite. A
 * 4 MB window title or a 900-day "session" from a clock jump would otherwise be
 * stored verbatim and then rendered into the parent's page.
 */

const db = require("../models/db");
const { classifyApp, classifyWeb } = require("./categorize");

/** Records per batch. The agent sends 250; this is headroom, not a target. */
const MAX_BATCH = 600;
/** No single session or visit can credit more than a day of time. */
const MAX_DURATION_SEC = 86400;

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;

const str = (v, max) => {
  if (v === null || v === undefined) return null;
  const s = String(v).trim();
  if (!s) return null;
  return s.length > max ? s.slice(0, max) : s;
};

const int = (v, lo, hi) => {
  const n = Math.round(Number(v));
  if (!Number.isFinite(n)) return lo;
  return Math.min(hi, Math.max(lo, n));
};

/**
 * ISO timestamp for storage. The agent sends UTC ISO strings; anything
 * unparseable falls back to "now" rather than writing NULL into a NOT NULL
 * column and losing the whole row.
 */
function isoOr(value, fallbackMs) {
  const t = Date.parse(value);
  const ms = Number.isFinite(t) ? t : fallbackMs;
  return new Date(ms).toISOString().slice(0, 19).replace("T", " ");
}

const INSERT_SESSION = db.prepare(`
  INSERT OR IGNORE INTO usage_sessions
    (device_id, session_key, app_name, process_name, window_title, started_at,
     ended_at, duration_sec, day, is_browser, app_category, academic_weight,
     platform, created_at)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
`);

const INSERT_VISIT = db.prepare(`
  INSERT OR IGNORE INTO web_visits
    (device_id, visit_key, browser, domain, url_path, title, visited_at, day,
     dwell_sec, visit_count, platform, media_id, media_kind, web_category,
     watch_category, academic_weight, created_at)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
`);

/**
 * Normalise one focus-session record, or null if it is unusable.
 *
 * A browser's window title is dropped here as well as on the agent. It is the
 * page the child was on, it belongs to the web ledger, and storing it twice
 * would mean a "delete this child's browsing history" action left copies behind
 * in the app ledger.
 */
function normalizeSession(rec) {
  if (!rec || rec.kind !== "usage") return null;
  const key = str(rec.session_key, 180);
  const appName = str(rec.app_name, 200);
  const day = str(rec.day, 10);
  if (!key || !appName || !day || !DAY_RE.test(day)) return null;
  const duration = int(rec.duration_sec, 0, MAX_DURATION_SEC);
  if (duration <= 0) return null;
  const isBrowser = Number(rec.is_browser) === 1 ? 1 : 0;
  const now = Date.now();
  return {
    session_key: key,
    app_name: appName,
    process_name: str(rec.process_name, 120),
    window_title: isBrowser ? null : str(rec.window_title, 300),
    started_at: isoOr(rec.started_at, now - duration * 1000),
    ended_at: isoOr(rec.ended_at, now),
    duration_sec: duration,
    day,
    is_browser: isBrowser,
    platform: str(rec.platform, 40),
  };
}

/** Normalise one browser-history record, or null if it is unusable. */
function normalizeVisit(rec) {
  if (!rec || rec.kind !== "web") return null;
  const key = str(rec.visit_key, 180);
  const domain = str(rec.domain, 190);
  const day = str(rec.day, 10);
  if (!key || !domain || !day || !DAY_RE.test(day)) return null;
  // A path is stored, a query string is not — the agent strips it, and this is
  // the second gate so a future collector change cannot leak search terms.
  const path = str(rec.url_path, 400);
  return {
    visit_key: key,
    browser: str(rec.browser, 60) || "unknown",
    domain: domain.toLowerCase().replace(/^www\./, ""),
    url_path: path ? path.split("?")[0].split("#")[0] : null,
    title: str(rec.title, 300),
    visited_at: isoOr(rec.visited_at, Date.now()),
    day,
    dwell_sec: int(rec.dwell_sec, 0, MAX_DURATION_SEC),
    platform: (str(rec.platform, 20) || "web").toLowerCase(),
    media_id: str(rec.media_id, 40),
    media_kind: str(rec.media_kind, 20),
  };
}

/**
 * Insert a batch of focus sessions.
 *
 * One transaction for the whole batch: 250 individual inserts on a Pi-fed
 * SQLite file is 250 fsyncs, which is where an ack timeout would come from.
 */
const insertSessions = db.transaction((deviceId, rows) => {
  let accepted = 0;
  const days = new Set();
  for (const row of rows) {
    const c = classifyApp(row);
    const res = INSERT_SESSION.run(deviceId, row.session_key, row.app_name,
      row.process_name, row.window_title, row.started_at, row.ended_at,
      row.duration_sec, row.day, row.is_browser, c.category, c.weight,
      row.platform);
    if (res.changes > 0) {
      accepted += 1;
      days.add(`${deviceId}|${row.day}`);
    }
  }
  return { accepted, days };
});

const insertVisits = db.transaction((deviceId, rows) => {
  let accepted = 0;
  const days = new Set();
  for (const row of rows) {
    const c = classifyWeb(row);
    const res = INSERT_VISIT.run(deviceId, row.visit_key, row.browser,
      row.domain, row.url_path, row.title, row.visited_at, row.day,
      row.dwell_sec, 1, row.platform, row.media_id, row.media_kind,
      c.web_category, c.watch_category, c.weight);
    if (res.changes > 0) {
      accepted += 1;
      days.add(`${deviceId}|${row.day}`);
    }
  }
  return { accepted, days };
});

/**
 * Ingest one batch and roll up the days it touched.
 *
 * `ledger` is 'usage' or 'web' and selects both the normaliser and the table —
 * the caller cannot accidentally route web records into the app ledger.
 *
 * The return shape is the socket ack: `{ ok, accepted, received, rejected }`.
 * `accepted` counts rows that were genuinely new. The agent treats any `ok`
 * response as permission to drop the batch, so duplicates do not loop.
 */
function ingestBatch(deviceId, ledger, records) {
  if (!deviceId) return { ok: false, error: "unknown device" };
  if (!Array.isArray(records)) return { ok: false, error: "records must be an array" };
  if (records.length > MAX_BATCH) {
    return { ok: false, error: `batch too large (${records.length} > ${MAX_BATCH})` };
  }

  const normalize = ledger === "usage" ? normalizeSession : normalizeVisit;
  const rows = [];
  for (const rec of records) {
    const row = normalize(rec);
    if (row) rows.push(row);
  }
  const rejected = records.length - rows.length;

  // An empty-but-well-formed batch is still a success: the records were all
  // duplicates or all malformed, and asking the agent to keep retrying a batch
  // the server will never accept would wedge the spool forever.
  if (!rows.length) {
    return { ok: true, accepted: 0, received: records.length, rejected };
  }

  let result;
  try {
    result = ledger === "usage"
      ? insertSessions(deviceId, rows)
      : insertVisits(deviceId, rows);
  } catch (err) {
    console.error(`[progress] ${ledger} ingest failed: ${err.message}`);
    return { ok: false, error: "database error" };
  }

  return {
    ok: true,
    accepted: result.accepted,
    received: records.length,
    rejected,
    days: [...result.days],
  };
}

module.exports = {
  MAX_BATCH,
  normalizeSession,
  normalizeVisit,
  ingestBatch,
};
