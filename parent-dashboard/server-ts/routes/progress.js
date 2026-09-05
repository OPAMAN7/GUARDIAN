/**
 * Guardian Shield — Academic Progress API
 * =======================================
 *
 * Read/write surface for the child-progress feature.
 *
 * Every route is behind authMiddleware at the router level. This is the most
 * sensitive data the product holds — a browsing history plus a judgement about a
 * child — so there is no "public" progress endpoint, not even a health check.
 *
 * The response shapes keep the three sources in three separate objects
 * (`app`, `web`, `watch`) rather than one list with a source field. A flat list
 * would let the front end sum across sources by accident; this shape makes the
 * mistake impossible to write.
 */

const express = require("express");
const db = require("../models/db");
const { authMiddleware } = require("./auth");
const rollup = require("../services/rollup");
const ai = require("../services/ai");
const {
  APP_CATEGORIES,
  WEB_CATEGORIES,
  WATCH_CATEGORIES,
  invalidateRuleCache,
} = require("../services/categorize");

const router = express.Router();
router.use(authMiddleware);

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;

/** Resolve :deviceId, or answer 400/404 and return null. */
function device(req, res) {
  const id = parseInt(req.params.deviceId, 10);
  if (!Number.isInteger(id) || id <= 0) {
    res.status(400).json({ error: "Invalid device id" });
    return null;
  }
  const row = db.prepare("SELECT id, device_name FROM devices WHERE id = ?").get(id);
  if (!row) {
    res.status(404).json({ error: "Device not found" });
    return null;
  }
  return row;
}

const clampDays = (v, def, max) => {
  const n = parseInt(v, 10);
  if (!Number.isFinite(n) || n <= 0) return def;
  return Math.min(max, n);
};

/**
 * The rubric itself, so the UI can render the formula instead of hardcoding a
 * second copy of it. A grade a parent cannot check by hand is a grade they will
 * not trust, and two copies of the constants would drift apart on the first
 * tuning pass.
 */
router.get("/rubric", (req, res) => {
  res.json({
    weights: {
      academic: 2, constructive: 1, neutral: 0, passive: -1, distraction: -2,
    },
    categories: {
      app: APP_CATEGORIES,
      web: WEB_CATEGORIES,
      watch: WATCH_CATEGORIES,
    },
    formula: {
      counted_sec: "academic + constructive + neutral + passive + distraction",
      effort_sec: `academic + ${rollup.EFFORT_CONSTRUCTIVE_FACTOR} * constructive`,
      attainment: "min(1, effort / target)",
      focus: "counted > 0 ? effort / counted : 0",
      raw: `100 * (${rollup.ATTAINMENT_SHARE} * attainment + ${rollup.FOCUS_SHARE} * focus)`,
      penalty: `min(${rollup.MAX_PENALTY}, ${rollup.MAX_PENALTY} * distraction / counted)`,
      score: "clamp(round(raw - penalty), 0, 100)",
      has_data: `counted >= ${rollup.MIN_DATA_SEC}`,
    },
    grades: rollup.GRADE_BANDS.map(([floor, letter]) => ({ floor, letter })),
    notes: [
      "Browser app time is shown but never counted — it is the same wall-clock time as the web rows.",
      "YouTube time is graded on its own watch categories, never on the web ones.",
      "A day below the minimum is 'N/A', not a failing grade.",
      "This grades the activity against a target. It is not a grade of the child.",
    ],
  });
});

/**
 * One day: the score with every component, plus the three ledgers.
 * `?date=` defaults to today in the server's local timezone, which is the same
 * basis the agent uses to stamp `day`.
 */
router.get("/:deviceId/day", (req, res) => {
  const dev = device(req, res);
  if (!dev) return;
  const day = DAY_RE.test(String(req.query.date || ""))
    ? String(req.query.date)
    : rollup.dayString(new Date());
  res.json({
    device_id: dev.id,
    device_name: dev.device_name,
    day,
    score: rollup.scoreForDay(dev.id, day),
    ledger: rollup.ledgerForDay(dev.id, day),
  });
});

/** Daily scores for the last N days, gaps filled, for the trend bars. */
router.get("/:deviceId/trend", (req, res) => {
  const dev = device(req, res);
  if (!dev) return;
  const days = clampDays(req.query.days, 14, 120);
  res.json({ device_id: dev.id, days, points: rollup.trend(dev.id, days) });
});

/** Period summary: weekly grade, streak, averages, category totals per source. */
router.get("/:deviceId/summary", (req, res) => {
  const dev = device(req, res);
  if (!dev) return;
  const days = clampDays(req.query.days, 7, 90);
  res.json({
    device_id: dev.id,
    device_name: dev.device_name,
    ...rollup.summarize(dev.id, days),
  });
});

// ─── Per-source detail ──────────────────────────────────────────────────────
// Three separate queries rather than one UNION, because each source has its own
// natural identity: an app is a process, a site is a domain, a video is a
// media_id. Forcing them into one shape is exactly the blending the feature
// exists to avoid.

const TOP_APPS = db.prepare(`
  SELECT app_name AS label, process_name AS sub, app_category AS category,
         MAX(academic_weight) AS weight, SUM(duration_sec) AS seconds,
         COUNT(*) AS events
    FROM usage_sessions
   WHERE device_id = ? AND day BETWEEN ? AND ? AND is_browser = 0
   GROUP BY LOWER(COALESCE(process_name, app_name))
   ORDER BY seconds DESC LIMIT ?
`);

const TOP_SITES = db.prepare(`
  SELECT domain AS label, browser AS sub, web_category AS category,
         MAX(academic_weight) AS weight, SUM(dwell_sec) AS seconds,
         COUNT(*) AS events
    FROM web_visits
   WHERE device_id = ? AND day BETWEEN ? AND ?
     AND LOWER(COALESCE(platform, 'web')) != 'youtube'
   GROUP BY domain
   ORDER BY seconds DESC LIMIT ?
`);

const TOP_WATCH = db.prepare(`
  SELECT COALESCE(title, media_id, 'YouTube') AS label,
         COALESCE(media_kind, 'video') AS sub, watch_category AS category,
         MAX(academic_weight) AS weight, SUM(dwell_sec) AS seconds,
         COUNT(*) AS events
    FROM web_visits
   WHERE device_id = ? AND day BETWEEN ? AND ?
     AND LOWER(COALESCE(platform, 'web')) = 'youtube'
   GROUP BY COALESCE(media_id, title)
   ORDER BY seconds DESC LIMIT ?
`);

/** The browser envelope, reported on its own so it is never mistaken for a total. */
const BROWSER_ENVELOPE = db.prepare(`
  SELECT app_name AS label, SUM(duration_sec) AS seconds, COUNT(*) AS events
    FROM usage_sessions
   WHERE device_id = ? AND day BETWEEN ? AND ? AND is_browser = 1
   GROUP BY LOWER(app_name) ORDER BY seconds DESC LIMIT 10
`);

router.get("/:deviceId/detail", (req, res) => {
  const dev = device(req, res);
  if (!dev) return;
  const days = clampDays(req.query.days, 7, 90);
  const { from, to } = rollup.dayRange(days);
  const limit = clampDays(req.query.limit, 15, 100);
  res.json({
    device_id: dev.id,
    period: { from, to, days },
    app: TOP_APPS.all(dev.id, from, to, limit),
    web: TOP_SITES.all(dev.id, from, to, limit),
    watch: TOP_WATCH.all(dev.id, from, to, limit),
    browser_envelope: BROWSER_ENVELOPE.all(dev.id, from, to),
  });
});

// ─── AI report ──────────────────────────────────────────────────────────────
// Three routes: what the provider situation is, the last report written, and
// write a new one. Only the third can touch the network.

/** Provider state for the UI. Booleans and counts — never the key itself. */
router.get("/ai/status", (req, res) => {
  res.json(ai.status());
});

router.get("/:deviceId/insight", (req, res) => {
  const dev = device(req, res);
  if (!dev) return;
  res.json({ device_id: dev.id, insight: ai.getLatest(dev.id), usage: ai.status() });
});

/**
 * Collect the opt-in label block.
 *
 * Called only when AI_SEND_TITLES is on, and it reads the label columns straight
 * from the detail queries so there is exactly one definition of "top app" in the
 * codebase. Domains are already bare hosts in web_visits — no path, no query
 * string is stored in that column, so none can be forwarded from here.
 */
function labelsFor(deviceId, from, to) {
  const names = (rows) => rows.map((r) => r.label).filter(Boolean);
  return {
    app: names(TOP_APPS.all(deviceId, from, to, 8)),
    web: names(TOP_SITES.all(deviceId, from, to, 8)),
    watch: names(TOP_WATCH.all(deviceId, from, to, 8)),
  };
}

/**
 * POST because it can spend money and writes a row; `force` skips the cache.
 *
 * Never 502s on a provider failure: ai.generate() answers with a locally written
 * report and a `reason`, so the parent gets the four sections either way and the
 * UI can explain why the prose is plainer than usual.
 */
router.post("/:deviceId/analyze", async (req, res) => {
  const dev = device(req, res);
  if (!dev) return;
  const days = clampDays(req.body?.days ?? req.query.days, 7, 90);
  const { from, to } = rollup.dayRange(days);
  try {
    const result = await ai.generate(dev.id, {
      days,
      force: String(req.body?.force ?? req.query.force ?? "") === "true",
      labels: ai.SEND_TITLES ? labelsFor(dev.id, from, to) : null,
    });
    res.json({ device_id: dev.id, device_name: dev.device_name, ...result });
  } catch (err) {
    console.error(`[Progress] analyze failed for device ${dev.id}: ${err.message}`);
    res.status(500).json({ error: "Could not generate a report" });
  }
});

// ─── Goals ──────────────────────────────────────────────────────────────────

router.get("/:deviceId/goals", (req, res) => {
  const dev = device(req, res);
  if (!dev) return;
  const row = db
    .prepare("SELECT daily_academic_min, weekly_target_days FROM progress_goals WHERE device_id = ?")
    .get(dev.id);
  res.json({
    device_id: dev.id,
    daily_academic_min: row?.daily_academic_min ?? 60,
    weekly_target_days: row?.weekly_target_days ?? 5,
    target_sec: rollup.targetSecFor(dev.id),
  });
});

/**
 * Change the target. Every stored day is re-scored immediately — a target that
 * only applied to future days would leave the trend chart comparing yesterday's
 * 60-minute grade against today's 90-minute one on the same axis.
 */
router.put("/:deviceId/goals", (req, res) => {
  const dev = device(req, res);
  if (!dev) return;
  const min = parseInt(req.body?.daily_academic_min, 10);
  const daysPerWeek = parseInt(req.body?.weekly_target_days, 10);
  if (!Number.isFinite(min) || min < 5 || min > 720) {
    return res.status(400).json({ error: "daily_academic_min must be 5-720 minutes" });
  }
  const wk = Number.isFinite(daysPerWeek) ? Math.min(7, Math.max(1, daysPerWeek)) : 5;
  try {
    db.prepare(
      `INSERT INTO progress_goals (device_id, daily_academic_min, weekly_target_days, updated_at)
       VALUES (?, ?, ?, CURRENT_TIMESTAMP)
       ON CONFLICT(device_id) DO UPDATE SET
         daily_academic_min = excluded.daily_academic_min,
         weekly_target_days = excluded.weekly_target_days,
         updated_at = CURRENT_TIMESTAMP`
    ).run(dev.id, min, wk);
    const days = rollup.knownDays(dev.id);
    const rolled = rollup.rollupMany(days.map((day) => ({ deviceId: dev.id, day })));
    res.json({ success: true, daily_academic_min: min, weekly_target_days: wk, rescored: rolled });
  } catch (err) {
    console.error(`[progress] goal update failed: ${err.message}`);
    res.status(500).json({ error: "Failed to save goal" });
  }
});

// ─── Category overrides ─────────────────────────────────────────────────────

const CATEGORY_TABLES = {
  app: APP_CATEGORIES,
  web: WEB_CATEGORIES,
  watch: WATCH_CATEGORIES,
};
const MATCH_TYPES = ["exact", "domain", "contains"];

router.get("/rules", (req, res) => {
  const rows = db
    .prepare(
      `SELECT id, source, match_type, match_value, category, academic_weight, origin,
              updated_at
         FROM activity_categories
        ORDER BY source ASC, updated_at DESC`
    )
    .all();
  res.json({
    rules: rows,
    match_types: MATCH_TYPES,
    categories: CATEGORY_TABLES,
  });
});

/**
 * Add or replace one override.
 *
 * The category must belong to the taxonomy of the stated source. This is the
 * server-side half of "do not mix things up": without the check, a parent could
 * file a web category against an app rule, and that app's time would land in the
 * web ledger on the next reclassify pass.
 *
 * The weight is derived from the category, never taken from the request, so the
 * rubric stays internally consistent no matter what the client sends.
 */
router.post("/rules", (req, res) => {
  const source = String(req.body?.source || "").toLowerCase();
  const matchType = String(req.body?.match_type || "contains").toLowerCase();
  const matchValue = String(req.body?.match_value || "").trim().toLowerCase();
  const category = String(req.body?.category || "").trim();

  if (!CATEGORY_TABLES[source]) {
    return res.status(400).json({ error: "source must be app, web or watch" });
  }
  if (!MATCH_TYPES.includes(matchType)) {
    return res.status(400).json({ error: `match_type must be one of ${MATCH_TYPES.join(", ")}` });
  }
  if (matchValue.length < 3 || matchValue.length > 190) {
    // Short patterns match everything: "io" would re-label every .io domain and
    // half the process names on the system (the BUG-13 lesson, applied here).
    return res.status(400).json({ error: "match_value must be 3-190 characters" });
  }
  if (!(category in CATEGORY_TABLES[source])) {
    return res.status(400).json({
      error: `category '${category}' is not a ${source} category`,
      allowed: Object.keys(CATEGORY_TABLES[source]),
    });
  }

  const weight = CATEGORY_TABLES[source][category];
  try {
    db.prepare(
      `INSERT INTO activity_categories
         (source, match_type, match_value, category, academic_weight, origin, updated_at)
       VALUES (?, ?, ?, ?, ?, 'parent', CURRENT_TIMESTAMP)
       ON CONFLICT(source, match_type, match_value) DO UPDATE SET
         category = excluded.category,
         academic_weight = excluded.academic_weight,
         origin = 'parent',
         updated_at = CURRENT_TIMESTAMP`
    ).run(source, matchType, matchValue, category, weight);
    invalidateRuleCache();
    res.json({ success: true, source, match_type: matchType, match_value: matchValue, category, academic_weight: weight });
  } catch (err) {
    console.error(`[progress] rule save failed: ${err.message}`);
    res.status(500).json({ error: "Failed to save rule" });
  }
});

router.delete("/rules/:id", (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!Number.isInteger(id)) return res.status(400).json({ error: "Invalid rule id" });
  const info = db.prepare("DELETE FROM activity_categories WHERE id = ?").run(id);
  invalidateRuleCache();
  res.json({ success: true, deleted: info.changes });
});

// ─── Maintenance ────────────────────────────────────────────────────────────

/**
 * Re-apply the current rules to stored history and rebuild the ledger.
 *
 * This is what makes an override feel real: without it a parent marks an app as
 * schoolwork, sees yesterday unchanged, and concludes the feature is broken.
 * Bounded by `days` (default 60) because a full reclassify walks every raw row.
 */
router.post("/:deviceId/rebuild", (req, res) => {
  const dev = device(req, res);
  if (!dev) return;
  const days = clampDays(req.body?.days ?? req.query.days, 60, 400);
  const { from, to } = rollup.dayRange(days);
  try {
    const result = rollup.reclassifyAndRoll(dev.id, from, to);
    res.json({ success: true, period: { from, to, days }, ...result });
  } catch (err) {
    console.error(`[progress] rebuild failed: ${err.message}`);
    res.status(500).json({ error: "Rebuild failed" });
  }
});

/**
 * Delete this child's progress history.
 *
 * Deliberately available in the API, not just as a database chore: the product
 * stores a browsing history and a judgement, and a parent has to be able to
 * erase both. Requires `confirm: "DELETE"` in the body so a stray fetch cannot
 * do it, and reports what it removed.
 */
router.delete("/:deviceId/history", (req, res) => {
  const dev = device(req, res);
  if (!dev) return;
  if (String(req.body?.confirm || "") !== "DELETE") {
    return res.status(400).json({ error: 'Send {"confirm":"DELETE"} to erase progress history' });
  }
  const before = String(req.body?.before || "").trim();
  const cutoff = DAY_RE.test(before) ? before : null;

  try {
    const wipe = db.transaction(() => {
      const counts = {};
      const tables = [
        ["usage_sessions", "day"],
        ["web_visits", "day"],
        ["progress_daily", "day"],
        ["progress_scores", "day"],
      ];
      for (const [table, col] of tables) {
        const sql = cutoff
          ? `DELETE FROM ${table} WHERE device_id = ? AND ${col} < ?`
          : `DELETE FROM ${table} WHERE device_id = ?`;
        const stmt = db.prepare(sql);
        counts[table] = (cutoff ? stmt.run(dev.id, cutoff) : stmt.run(dev.id)).changes;
      }
      if (!cutoff) {
        counts.ai_insights = db
          .prepare("DELETE FROM ai_insights WHERE device_id = ?")
          .run(dev.id).changes;
      }
      return counts;
    });
    const deleted = wipe();
    console.log(`[progress] history erased for device ${dev.device_name}${cutoff ? ` before ${cutoff}` : ""}`);
    res.json({ success: true, before: cutoff, deleted });
  } catch (err) {
    console.error(`[progress] history delete failed: ${err.message}`);
    res.status(500).json({ error: "Delete failed" });
  }
});

module.exports = router;
