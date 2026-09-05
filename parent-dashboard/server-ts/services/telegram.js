/**
 * Guardian Shield — Telegram alert channel
 *
 * Pushes device alerts to the parent's phone. Design constraints, in order of
 * importance:
 *
 *  1. The bot token is server-side only. It lives in server-ts/.env (gitignored),
 *     never reaches the browser bundle, and status() reports booleans and names
 *     only — never the token, never the full API URL (a URL carrying the token
 *     ends up in stack traces and proxy logs).
 *  2. Nothing here may ever throw into the alert path. An alert is already
 *     written to the database before Telegram is asked to do anything, so a
 *     network failure must degrade to a log line, not lose the alert or take
 *     down the socket handler. Every entry point resolves.
 *  3. One alert, one message — even across restarts. notify_log's
 *     UNIQUE(channel, ref_type, ref_id) is claimed BEFORE the HTTP call, so a
 *     crash halfway through a send cannot produce a duplicate on the next boot.
 *  4. An hourly ceiling. A stuck agent that re-emits the same class of alert
 *     must not turn the parent's phone into a siren, so sends are capped and
 *     the overflow is recorded as skipped rather than queued forever.
 *
 * Configure with, in server-ts/.env:
 *   TELEGRAM_BOT_TOKEN=123456:AA...      from @BotFather
 *   TELEGRAM_CHAT_ID=987654321          from @userinfobot, or a group id (-100...)
 *   TELEGRAM_ALERTS=true                 default true when both above are set
 *   TELEGRAM_ALERT_TYPES=               optional allowlist, comma separated
 *   TELEGRAM_MAX_PER_HOUR=20            flood ceiling
 */

const db = require("../models/db");

const CHANNEL = "telegram";

// Snapshotted at require time, the same contract as every other service here:
// changing .env takes effect on the next server start, not mid-flight.
const TOKEN = (process.env.TELEGRAM_BOT_TOKEN || "").trim();
const CHAT_ID = (process.env.TELEGRAM_CHAT_ID || "").trim();
const MAX_PER_HOUR = Math.max(
  1,
  parseInt(process.env.TELEGRAM_MAX_PER_HOUR || "20", 10) || 20
);
const TIMEOUT_MS = Math.max(
  2000,
  parseInt(process.env.TELEGRAM_TIMEOUT_MS || "8000", 10) || 8000
);

// Explicit off switch, so a parent can keep the credentials and mute the channel.
const ENABLED =
  String(process.env.TELEGRAM_ALERTS || "true").toLowerCase() !== "false";

// An empty list means every alert type is forwarded.
const TYPE_FILTER = String(process.env.TELEGRAM_ALERT_TYPES || "")
  .split(",")
  .map((s) => s.trim().toLowerCase())
  .filter(Boolean);

const CONFIGURED = Boolean(TOKEN && CHAT_ID);

/** Telegram's own base. Built per call so the token never sits in a module const
 *  that some future log statement might print wholesale. */
function apiUrl(method) {
  return `https://api.telegram.org/bot${TOKEN}/${method}`;
}

/** Only these three characters matter for parse_mode HTML, which is why HTML is
 *  used here instead of MarkdownV2 — a child's window title is full of the
 *  fifteen characters MarkdownV2 would demand be escaped. */
function escapeHtml(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/** Collapse to one line and clip — alert details can carry a whole file path
 *  list, and a 4096-character Telegram limit is a rejection, not a truncation. */
function tidy(text, max = 300) {
  const one = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
  return one.length > max ? `${one.slice(0, max - 1)}…` : one;
}

const ICONS = {
  malware: "🦠",
  dangerous_file: "📄",
  blocked_site: "🚫",
  app_blocked: "⛔",
  app_timer_expired: "⏰",
  blocking_error: "⚠️",
};

/** What the UI is allowed to know. No token, no chat id, no URL. */
function status() {
  let sent24 = 0;
  let lastAt = null;
  try {
    const row = db
      .prepare(
        `SELECT COUNT(*) AS n, MAX(created_at) AS last_at
           FROM notify_log
          WHERE channel = ? AND status = 'sent'
            AND created_at >= datetime('now','-1 day')`
      )
      .get(CHANNEL);
    sent24 = row ? row.n : 0;
    lastAt = row ? row.last_at : null;
  } catch {
    // A missing table must not break the status endpoint.
  }
  return {
    channel: CHANNEL,
    configured: CONFIGURED,
    enabled: ENABLED && CONFIGURED,
    // Presence, not value: enough to diagnose "which half did I forget".
    has_token: Boolean(TOKEN),
    has_chat_id: Boolean(CHAT_ID),
    type_filter: TYPE_FILTER.length ? TYPE_FILTER : null,
    max_per_hour: MAX_PER_HOUR,
    sent_last_24h: sent24,
    last_sent_at: lastAt,
    hint: CONFIGURED
      ? null
      : "Run GUARD.bat, choose M (Connect Telegram Alerts), then restart the dashboard.",
  };
}

/** How many messages this channel has already pushed in the last hour. */
function sentLastHour() {
  try {
    const row = db
      .prepare(
        `SELECT COUNT(*) AS n FROM notify_log
          WHERE channel = ? AND status = 'sent'
            AND created_at >= datetime('now','-1 hour')`
      )
      .get(CHANNEL);
    return row ? row.n : 0;
  } catch {
    return 0;
  }
}

/**
 * Take ownership of one (ref_type, ref_id) before the network call.
 *
 * The INSERT is the lock. If it collides, some earlier run — possibly in a
 * previous process — already handled this reference and we stay quiet. Claiming
 * first means the worst case is a lost message, never a duplicate: a parent who
 * misses one push still sees the alert in the dashboard, whereas a phone that
 * buzzes twice for the same event teaches them to ignore the channel.
 */
function claim(refType, refId) {
  try {
    const r = db
      .prepare(
        `INSERT OR IGNORE INTO notify_log (channel, ref_type, ref_id, status, detail)
         VALUES (?, ?, ?, 'pending', NULL)`
      )
      .run(CHANNEL, String(refType), String(refId));
    return r.changes > 0;
  } catch (err) {
    console.warn(`[telegram] could not claim ${refType}/${refId}: ${err.message}`);
    return false;
  }
}

/** Record the outcome on the row already claimed above. */
function settle(refType, refId, state, detail) {
  try {
    db.prepare(
      `UPDATE notify_log SET status = ?, detail = ?
        WHERE channel = ? AND ref_type = ? AND ref_id = ?`
    ).run(state, detail ? tidy(detail, 200) : null, CHANNEL, String(refType), String(refId));
  } catch {
    // Logging the outcome is a convenience; failing to log it is not fatal.
  }
}

/**
 * One HTTP attempt against Telegram. No retries: the caller already owns a
 * notify_log row, and a retry loop against a rate-limited bot is how a token
 * gets throttled for everyone. Resolves with an outcome, never rejects.
 */
async function post(method, body) {
  if (typeof fetch !== "function") {
    return { ok: false, detail: "no fetch available in this runtime" };
  }
  let res;
  try {
    res = await fetch(apiUrl(method), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (err) {
    const why = err && err.name === "TimeoutError"
      ? `no answer in ${Math.round(TIMEOUT_MS / 1000)}s`
      : (err && err.message) || "network error";
    // Never echo the URL — it carries the token.
    return { ok: false, detail: `Telegram unreachable (${why})` };
  }

  let payload = null;
  let readable = true;
  try {
    payload = await res.json();
  } catch {
    payload = null;
    readable = false;
  }

  if (!res.ok || !payload || payload.ok !== true) {
    // Distinguish the three ways this goes wrong, because they need different
    // fixes: a bad token, a bad chat id, or something between here and Telegram
    // rewriting the response (a captive portal or a filtering proxy).
    let why;
    if (!readable) why = `unreadable answer (HTTP ${res.status})`;
    else if (payload && payload.description) why = payload.description;
    else why = `HTTP ${res.status}`;
    return { ok: false, detail: `Telegram rejected the message (${tidy(why, 160)})` };
  }
  return { ok: true, detail: null, result: payload.result || null };
}

/**
 * Send one message, at most once per (refType, refId).
 *
 * Returns { sent, reason } and never throws — callers are socket handlers that
 * must keep serving the dashboard whatever Telegram does. When the hourly cap is
 * hit the message is DROPPED, not queued: an alert an hour late is noise, and the
 * event is still in the dashboard and in notify_log with status 'skipped_rate'.
 */
async function send(text, { refType = "manual", refId = null, force = false } = {}) {
  if (!CONFIGURED) return { sent: false, reason: "not_configured" };
  if (!ENABLED) return { sent: false, reason: "disabled" };

  const ref = refId == null ? `${Date.now()}-${Math.random().toString(36).slice(2, 8)}` : refId;

  if (!force && !claim(refType, ref)) {
    return { sent: false, reason: "already_sent" };
  }

  if (!force && sentLastHour() >= MAX_PER_HOUR) {
    settle(refType, ref, "skipped_rate", `hourly cap of ${MAX_PER_HOUR} reached`);
    console.warn(`[telegram] hourly cap ${MAX_PER_HOUR} reached — dropped ${refType}/${ref}`);
    return { sent: false, reason: "rate_capped" };
  }

  const outcome = await post("sendMessage", {
    chat_id: CHAT_ID,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
  });

  if (!force) settle(refType, ref, outcome.ok ? "sent" : "failed", outcome.detail);
  if (!outcome.ok) {
    console.warn(`[telegram] ${refType}/${ref} not delivered — ${outcome.detail}`);
    return { sent: false, reason: "failed", detail: outcome.detail };
  }
  return { sent: true, reason: "sent" };
}

/**
 * Format an alert the way it reads best on a phone lock screen: what happened,
 * on which device, and one line of detail. Device NAME, never the raw details
 * blob — that can contain full file paths and URLs.
 */
function formatAlert({ type, device_name: deviceName, message, details }) {
  const icon = ICONS[String(type || "").toLowerCase()] || "🔔";
  const lines = [
    `${icon} <b>GUARDIAN — ${escapeHtml(tidy(type || "alert", 40))}</b>`,
    `Device: <b>${escapeHtml(tidy(deviceName || "unknown device", 60))}</b>`,
    escapeHtml(tidy(message || "(no message)", 400)),
  ];
  const hint = pickDetail(details);
  if (hint) lines.push(`<i>${escapeHtml(tidy(hint, 200))}</i>`);
  lines.push(`<i>${escapeHtml(new Date().toLocaleString())}</i>`);
  return lines.join("\n");
}

/**
 * One useful field out of the details blob rather than the whole thing. The blob
 * is written by the agent and can hold paths, URLs and process lists; a phone
 * notification wants the app or site name and nothing else.
 */
function pickDetail(details) {
  if (!details || typeof details !== "object") return null;
  for (const key of ["app_name", "process", "domain", "site", "url_host", "file_name", "reason"]) {
    if (details[key]) return `${key.replace(/_/g, " ")}: ${details[key]}`;
  }
  return null;
}

/** Should this alert type reach the phone at all? */
function typeAllowed(type) {
  if (!TYPE_FILTER.length) return true;
  return TYPE_FILTER.includes(String(type || "").toLowerCase());
}

/**
 * The one function the socket handler calls. Synchronous from the caller's point
 * of view: it starts the send and returns immediately, because the alert is
 * already safely in the database and the handler must not wait on Telegram.
 * `alertId` is the alerts table row id, which is what makes the dedupe exact.
 */
function notifyAlert(alert, alertId) {
  if (!CONFIGURED || !ENABLED) return;
  if (!typeAllowed(alert && alert.type)) return;
  const ref = alertId == null ? null : String(alertId);
  // Fire and forget, with the rejection handler that keeps an async failure from
  // becoming an unhandled rejection and killing the process on newer Node.
  Promise.resolve()
    .then(() => send(formatAlert(alert || {}), { refType: "alert", refId: ref }))
    .catch((err) => console.warn(`[telegram] alert push failed: ${err.message}`));
}

/**
 * Confirm a freshly pasted token and chat id actually work. Bypasses dedupe and
 * the hourly cap on purpose — the parent asked for this message — and awaits the
 * result so GUARD can print a real verdict instead of a hopeful one.
 */
async function selfTest() {
  if (!CONFIGURED) {
    return {
      ok: false,
      detail: TOKEN
        ? "TELEGRAM_CHAT_ID is missing"
        : "TELEGRAM_BOT_TOKEN is missing",
    };
  }
  const text = [
    "✅ <b>GUARDIAN connected</b>",
    "This chat will receive device alerts from your Guardian dashboard.",
    `<i>${escapeHtml(new Date().toLocaleString())}</i>`,
  ].join("\n");
  const outcome = await post("sendMessage", {
    chat_id: CHAT_ID,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
  });
  return { ok: outcome.ok, detail: outcome.detail };
}

module.exports = {
  status,
  send,
  selfTest,
  notifyAlert,
  formatAlert,
  escapeHtml,
  typeAllowed,
  pickDetail,
  // Exported for the offline suite and for diagnostics — names and limits only.
  CHANNEL,
  CONFIGURED,
  ENABLED,
  MAX_PER_HOUR,
  TYPE_FILTER,
};
