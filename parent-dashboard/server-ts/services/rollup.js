/**
 * Guardian Shield — Progress Rollup & Grading
 * ===========================================
 *
 * Folds raw rows (usage_sessions, web_visits) into the per-day ledger
 * (progress_daily) and then into a single grade (progress_scores).
 *
 * Two invariants drive every decision here:
 *
 * 1. THE THREE SOURCES NEVER BLEND. A YouTube visit produces exactly one
 *    'watch' ledger row. Its web_category is stored on the raw row for context
 *    but is never summed into the 'web' totals — otherwise the same seconds
 *    would appear twice and the grade would be wrong in the child's favour.
 *
 * 2. THE GRADE IS COMPUTED FROM THE DISPLAYED LEDGER, not from the raw rows.
 *    The Progress page shows the arithmetic and a parent must be able to
 *    reproduce the number by hand from what is on screen. Deriving the score
 *    from a different query than the one the UI renders would eventually drift.
 *
 * Browser app-sessions are the third trap: their seconds are the *same*
 * wall-clock time as the web ledger. They are stored (a parent wants to see
 * "4 h in Chromium") with counts_toward_total = 0 and are excluded from every
 * total. Only progress_scores.browser_envelope_sec reports them.
 *
 * Everything is recompute-from-scratch, never incremental: a rollup for a day
 * DELETEs that day's ledger and rebuilds it. Late-arriving offline spool data
 * is therefore self-healing, and a rules change plus a reclassify pass fixes
 * history instead of leaving a mix of old and new labels.
 */

const db = require("../models/db");
const {
  band,
  classifyApp,
  classifyWeb,
  APP_CATEGORIES,
  WEB_CATEGORIES,
  WATCH_CATEGORIES,
} = require("./categorize");

// ─── Grading constants ──────────────────────────────────────────────────────
// Documented here because these five numbers *are* the rubric. Changing one
// changes every grade in the UI, so they are named rather than inlined.

/** Below this much counted activity a day is "no data", not a bad day. */
const MIN_DATA_SEC = 300;
/** Constructive time counts half towards effort; academic counts fully. */
const EFFORT_CONSTRUCTIVE_FACTOR = 0.5;
/** How much of the score is "did enough" vs "spent it well". */
const ATTAINMENT_SHARE = 0.6;
const FOCUS_SHARE = 0.4;
/** Distraction can cost at most this many points, so one bad hour is not an F. */
const MAX_PENALTY = 15;
const DEFAULT_TARGET_SEC = 3600;

// Highest floor first — the first match wins.
const GRADE_BANDS = [
  [90, "A+"], [80, "A"], [70, "B"], [60, "C"], [50, "D"], [35, "E"], [0, "F"],
];

/**
 * Letter for a score.
 * A day with no data is "N/A", never "F": a child who did not use the computer
 * has not failed anything, and showing an F for a school holiday destroys the
 * parent's trust in every other number on the page.
 */
function gradeFor(score, hasData) {
  if (!hasData) return "N/A";
  for (const [floor, letter] of GRADE_BANDS) {
    if (score >= floor) return letter;
  }
  return "F";
}

const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));

/** The child's daily academic target, in seconds. */
function targetSecFor(deviceId) {
  try {
    const row = db
      .prepare("SELECT daily_academic_min FROM progress_goals WHERE device_id = ?")
      .get(deviceId);
    const min = Number(row?.daily_academic_min);
    if (Number.isFinite(min) && min > 0) return Math.round(min * 60);
  } catch (err) {
    console.warn(`[rollup] goal lookup failed: ${err.message}`);
  }
  return DEFAULT_TARGET_SEC;
}

/** Canonical weight for a category, falling back to a custom override's own. */
function weightForCategory(source, category, fallback) {
  const table =
    source === "app" ? APP_CATEGORIES
      : source === "web" ? WEB_CATEGORIES
        : WATCH_CATEGORIES;
  const known = table[category];
  return known === undefined ? Number(fallback) || 0 : known;
}

// ─── Ledger build ───────────────────────────────────────────────────────────

const SELECT_APP_GROUPS = db.prepare(`
  SELECT COALESCE(app_category, 'other') AS category,
         COALESCE(academic_weight, 0)    AS weight,
         SUM(COALESCE(duration_sec, 0))  AS seconds,
         COUNT(*)                        AS events,
         MAX(COALESCE(is_browser, 0))    AS is_browser
    FROM usage_sessions
   WHERE device_id = ? AND day = ?
   GROUP BY category, weight
`);

// One CASE decides the source, so a YouTube row can only ever land in 'watch'.
// This single expression is what keeps the two ledgers from double-counting.
const SELECT_WEB_GROUPS = db.prepare(`
  SELECT CASE WHEN LOWER(COALESCE(platform, 'web')) = 'youtube'
              THEN 'watch' ELSE 'web' END AS source,
         CASE WHEN LOWER(COALESCE(platform, 'web')) = 'youtube'
              THEN COALESCE(watch_category, 'other')
              ELSE COALESCE(web_category, 'other') END AS category,
         COALESCE(academic_weight, 0)   AS weight,
         SUM(COALESCE(dwell_sec, 0))    AS seconds,
         COUNT(*)                       AS events
    FROM web_visits
   WHERE device_id = ? AND day = ?
   GROUP BY source, category, weight
`);

/**
 * Collapse (category, weight) groups into one row per category, keeping the
 * weight that the most seconds voted for.
 *
 * Weight is normally a pure function of the category, so there is usually one
 * group per category. It can differ when a parent override changed a weight
 * after some rows were already ingested; picking the dominant weight keeps the
 * ledger honest until the next reclassify pass evens it out.
 */
function mergeGroups(rows, source) {
  const byCategory = new Map();
  for (const r of rows) {
    const key = r.category || "other";
    const seconds = Math.max(0, Math.round(Number(r.seconds) || 0));
    const cur = byCategory.get(key) || {
      category: key, seconds: 0, events: 0, votes: new Map(), isBrowser: 0,
    };
    cur.seconds += seconds;
    cur.events += Number(r.events) || 0;
    cur.isBrowser = Math.max(cur.isBrowser, Number(r.is_browser) || 0);
    const w = Number(r.weight) || 0;
    cur.votes.set(w, (cur.votes.get(w) || 0) + seconds + 1);
    byCategory.set(key, cur);
  }
  const out = [];
  for (const entry of byCategory.values()) {
    let bestW = 0;
    let bestVote = -1;
    for (const [w, vote] of entry.votes) {
      if (vote > bestVote) { bestVote = vote; bestW = w; }
    }
    out.push({
      source,
      category: entry.category,
      seconds: entry.seconds,
      events: entry.events,
      weight: weightForCategory(source, entry.category, bestW),
      // The envelope flag follows the classifier's contract (browsers are
      // forced to 'browser'), not a per-row guess, so it cannot drift.
      countsTowardTotal: source === "app" && entry.category === "browser" ? 0 : 1,
      isBrowser: entry.isBrowser,
    });
  }
  return out;
}

const DELETE_DAY_LEDGER = db.prepare(
  "DELETE FROM progress_daily WHERE device_id = ? AND day = ?"
);

const INSERT_LEDGER = db.prepare(`
  INSERT INTO progress_daily
    (device_id, day, source, category, seconds, events, academic_weight,
     counts_toward_total, updated_at)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
`);

const UPSERT_SCORE = db.prepare(`
  INSERT INTO progress_scores
    (device_id, day, academic_sec, constructive_sec, neutral_sec, passive_sec,
     distraction_sec, counted_sec, browser_envelope_sec, effort_sec, target_sec,
     focus_pct, attainment_pct, penalty, score, grade, has_data, updated_at)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
  ON CONFLICT(device_id, day) DO UPDATE SET
    academic_sec = excluded.academic_sec,
    constructive_sec = excluded.constructive_sec,
    neutral_sec = excluded.neutral_sec,
    passive_sec = excluded.passive_sec,
    distraction_sec = excluded.distraction_sec,
    counted_sec = excluded.counted_sec,
    browser_envelope_sec = excluded.browser_envelope_sec,
    effort_sec = excluded.effort_sec,
    target_sec = excluded.target_sec,
    focus_pct = excluded.focus_pct,
    attainment_pct = excluded.attainment_pct,
    penalty = excluded.penalty,
    score = excluded.score,
    grade = excluded.grade,
    has_data = excluded.has_data,
    updated_at = CURRENT_TIMESTAMP
`);

/**
 * Score one day from its own ledger rows.
 *
 * Exported (and pure) so the unit tests can drive the rubric with synthetic
 * numbers, and so the UI can be shown the exact same formula it renders.
 */
function scoreLedger(ledger, targetSec) {
  const bands = {
    academic: 0, constructive: 0, neutral: 0, passive: 0, distraction: 0,
  };
  let envelope = 0;
  for (const row of ledger) {
    const secs = Math.max(0, Math.round(Number(row.seconds) || 0));
    if (!row.countsTowardTotal) { envelope += secs; continue; }
    bands[band(row.weight)] += secs;
  }

  const counted = bands.academic + bands.constructive + bands.neutral
    + bands.passive + bands.distraction;
  const effort = bands.academic + EFFORT_CONSTRUCTIVE_FACTOR * bands.constructive;
  const target = targetSec > 0 ? targetSec : DEFAULT_TARGET_SEC;
  const attainment = clamp(effort / target, 0, 1);
  const focus = counted > 0 ? clamp(effort / counted, 0, 1) : 0;
  const raw = 100 * (ATTAINMENT_SHARE * attainment + FOCUS_SHARE * focus);
  const penalty = counted > 0
    ? Math.min(MAX_PENALTY, MAX_PENALTY * (bands.distraction / counted))
    : 0;
  const hasData = counted >= MIN_DATA_SEC;
  const score = clamp(Math.round(raw - penalty), 0, 100);

  return {
    academic_sec: bands.academic,
    constructive_sec: bands.constructive,
    neutral_sec: bands.neutral,
    passive_sec: bands.passive,
    distraction_sec: bands.distraction,
    counted_sec: counted,
    browser_envelope_sec: envelope,
    effort_sec: Math.round(effort),
    target_sec: target,
    focus_pct: Number((focus * 100).toFixed(2)),
    attainment_pct: Number((attainment * 100).toFixed(2)),
    penalty: Number(penalty.toFixed(2)),
    score,
    grade: gradeFor(score, hasData),
    has_data: hasData ? 1 : 0,
  };
}

/** Build the ledger rows for one device-day without writing anything. */
function buildLedger(deviceId, day) {
  const appRows = mergeGroups(SELECT_APP_GROUPS.all(deviceId, day), "app");
  const webRaw = SELECT_WEB_GROUPS.all(deviceId, day);
  const webRows = mergeGroups(webRaw.filter((r) => r.source === "web"), "web");
  const watchRows = mergeGroups(webRaw.filter((r) => r.source === "watch"), "watch");
  return [...appRows, ...webRows, ...watchRows].filter(
    (r) => r.seconds > 0 || r.events > 0
  );
}

/**
 * Recompute one device-day end to end.
 *
 * Wrapped in a transaction so the page can never read a half-deleted ledger:
 * without it, a dashboard poll landing between the DELETE and the INSERTs would
 * show a grade of zero for a day that has data.
 */
const rollupDay = db.transaction((deviceId, day) => {
  const ledger = buildLedger(deviceId, day);
  DELETE_DAY_LEDGER.run(deviceId, day);
  for (const row of ledger) {
    INSERT_LEDGER.run(deviceId, day, row.source, row.category, row.seconds,
      row.events, row.weight, row.countsTowardTotal);
  }
  const s = scoreLedger(ledger, targetSecFor(deviceId));
  UPSERT_SCORE.run(deviceId, day, s.academic_sec, s.constructive_sec,
    s.neutral_sec, s.passive_sec, s.distraction_sec, s.counted_sec,
    s.browser_envelope_sec, s.effort_sec, s.target_sec, s.focus_pct,
    s.attainment_pct, s.penalty, s.score, s.grade, s.has_data);
  return s;
});

/**
 * Recompute several device-days.
 *
 * `pairs` is an iterable of "deviceId|day" strings (that is what the ingest
 * handlers accumulate) or {deviceId, day} objects. Each day is its own
 * transaction on purpose: one malformed day must not roll back a batch that
 * covered a week of catch-up spool data.
 */
function rollupMany(pairs) {
  let done = 0;
  for (const pair of pairs) {
    let deviceId;
    let day;
    if (typeof pair === "string") {
      const i = pair.indexOf("|");
      deviceId = Number(pair.slice(0, i));
      day = pair.slice(i + 1);
    } else {
      deviceId = Number(pair.deviceId);
      day = pair.day;
    }
    if (!deviceId || !day) continue;
    try {
      rollupDay(deviceId, day);
      done += 1;
    } catch (err) {
      console.error(`[rollup] ${deviceId}/${day} failed: ${err.message}`);
    }
  }
  return done;
}

// ─── Reclassify (what makes a parent override retroactive) ──────────────────

const UPDATE_SESSION_CATEGORY = db.prepare(
  "UPDATE usage_sessions SET app_category = ?, academic_weight = ? WHERE id = ?"
);
const UPDATE_VISIT_CATEGORY = db.prepare(
  `UPDATE web_visits
      SET web_category = ?, watch_category = ?, academic_weight = ?
    WHERE id = ?`
);

/**
 * Re-run the classifier over stored rows in [fromDay, toDay] and roll the
 * affected days up again.
 *
 * Without this, "this app is schoolwork, not a game" would only affect
 * tomorrow's data and the parent would think the override did nothing.
 * Row-level UPDATEs (not a bulk UPDATE ... WHERE) because the new label comes
 * from JavaScript rule tables that SQL cannot express.
 */
const reclassifyRange = db.transaction((deviceId, fromDay, toDay) => {
  const days = new Set();
  const sessions = db
    .prepare(
      `SELECT id, day, app_name, process_name, window_title, is_browser
         FROM usage_sessions
        WHERE device_id = ? AND day BETWEEN ? AND ?`
    )
    .all(deviceId, fromDay, toDay);
  for (const s of sessions) {
    const c = classifyApp(s);
    UPDATE_SESSION_CATEGORY.run(c.category, c.weight, s.id);
    days.add(s.day);
  }
  const visits = db
    .prepare(
      `SELECT id, day, domain, title, platform, media_id, media_kind
         FROM web_visits
        WHERE device_id = ? AND day BETWEEN ? AND ?`
    )
    .all(deviceId, fromDay, toDay);
  for (const v of visits) {
    const c = classifyWeb(v);
    UPDATE_VISIT_CATEGORY.run(c.web_category, c.watch_category, c.weight, v.id);
    days.add(v.day);
  }
  return { rows: sessions.length + visits.length, days: [...days] };
});

/**
 * Reclassify then re-roll. Split from the transaction above because rollupDay
 * is itself a transaction and better-sqlite3 does not nest them.
 */
function reclassifyAndRoll(deviceId, fromDay, toDay) {
  const res = reclassifyRange(deviceId, fromDay, toDay);
  const rolled = rollupMany(res.days.map((day) => ({ deviceId, day })));
  return { ...res, rolled };
}

// ─── Read helpers (shared by routes/progress.js and services/ai.js) ─────────

const EMPTY_SCORE = {
  academic_sec: 0, constructive_sec: 0, neutral_sec: 0, passive_sec: 0,
  distraction_sec: 0, counted_sec: 0, browser_envelope_sec: 0, effort_sec: 0,
  target_sec: DEFAULT_TARGET_SEC, focus_pct: 0, attainment_pct: 0, penalty: 0,
  score: 0, grade: "N/A", has_data: 0,
};

/** Local-date string for a Date, in the server's timezone (matches the agent). */
function dayString(date) {
  const d = date instanceof Date ? date : new Date(date);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** Today, and the day N days back, as YYYY-MM-DD. */
function dayRange(days) {
  const end = new Date();
  const start = new Date(end.getTime() - (Math.max(1, days) - 1) * 86400000);
  return { from: dayString(start), to: dayString(end) };
}

/** The stored score row for a day, or a zeroed one so the UI never sees null. */
function scoreForDay(deviceId, day) {
  const row = db
    .prepare("SELECT * FROM progress_scores WHERE device_id = ? AND day = ?")
    .get(deviceId, day);
  return row || { ...EMPTY_SCORE, device_id: deviceId, day };
}

/**
 * The three ledgers for a day, kept in three separate arrays.
 *
 * The API returns them separated rather than one flat list with a `source`
 * field so the front end cannot accidentally render a combined total — the
 * shape itself enforces the rule.
 */
function ledgerForDay(deviceId, day) {
  const rows = db
    .prepare(
      `SELECT source, category, seconds, events, academic_weight,
              counts_toward_total
         FROM progress_daily
        WHERE device_id = ? AND day = ?
        ORDER BY seconds DESC`
    )
    .all(deviceId, day);
  const shape = (r) => ({
    category: r.category,
    seconds: r.seconds,
    events: r.events,
    weight: r.academic_weight,
    band: band(r.academic_weight),
    counted: r.counts_toward_total === 1,
  });
  return {
    app: rows.filter((r) => r.source === "app").map(shape),
    web: rows.filter((r) => r.source === "web").map(shape),
    watch: rows.filter((r) => r.source === "watch").map(shape),
  };
}

/** Score rows for the last N days, oldest first, with gaps filled in. */
function trend(deviceId, days = 14) {
  const { from, to } = dayRange(days);
  const rows = db
    .prepare(
      `SELECT day, score, grade, has_data, academic_sec, constructive_sec,
              neutral_sec, passive_sec, distraction_sec, counted_sec, effort_sec,
              target_sec
         FROM progress_scores
        WHERE device_id = ? AND day BETWEEN ? AND ?
        ORDER BY day ASC`
    )
    .all(deviceId, from, to);
  const byDay = new Map(rows.map((r) => [r.day, r]));
  const out = [];
  const cursor = new Date(`${from}T12:00:00`);
  for (let i = 0; i < Math.max(1, days); i += 1) {
    const day = dayString(cursor);
    out.push(byDay.get(day) || { day, score: 0, grade: "N/A", has_data: 0,
      academic_sec: 0, constructive_sec: 0, neutral_sec: 0, passive_sec: 0,
      distraction_sec: 0, counted_sec: 0, effort_sec: 0,
      target_sec: DEFAULT_TARGET_SEC });
    cursor.setDate(cursor.getDate() + 1);
    if (day === to) break;
  }
  return out;
}

/**
 * Period summary: the weekly grade, the streak, and the top categories.
 *
 * This is also the AI privacy boundary. Everything in the returned object is an
 * aggregate — category names, seconds and counts. No URL, no window title, no
 * screenshot and no search term can reach this shape, which is why
 * services/ai.js is only ever handed the output of this function.
 */
function summarize(deviceId, days = 7) {
  const { from, to } = dayRange(days);
  const scores = db
    .prepare(
      `SELECT * FROM progress_scores
        WHERE device_id = ? AND day BETWEEN ? AND ? ORDER BY day ASC`
    )
    .all(deviceId, from, to);
  const active = scores.filter((s) => s.has_data === 1);
  const avg = (key) => (active.length
    ? Math.round(active.reduce((a, s) => a + (Number(s[key]) || 0), 0) / active.length)
    : 0);
  const meanScore = active.length
    ? Math.round(active.reduce((a, s) => a + s.score, 0) / active.length)
    : 0;

  const cats = db
    .prepare(
      `SELECT source, category, SUM(seconds) AS seconds, SUM(events) AS events,
              MAX(academic_weight) AS weight
         FROM progress_daily
        WHERE device_id = ? AND day BETWEEN ? AND ? AND counts_toward_total = 1
        GROUP BY source, category
        ORDER BY seconds DESC`
    )
    .all(deviceId, from, to);

  return {
    period: { from, to, days: Math.max(1, days) },
    active_days: active.length,
    grade: gradeFor(meanScore, active.length > 0),
    score: meanScore,
    streak: streak(deviceId),
    averages: {
      academic_sec: avg("academic_sec"),
      constructive_sec: avg("constructive_sec"),
      neutral_sec: avg("neutral_sec"),
      passive_sec: avg("passive_sec"),
      distraction_sec: avg("distraction_sec"),
      counted_sec: avg("counted_sec"),
      effort_sec: avg("effort_sec"),
      target_sec: targetSecFor(deviceId),
    },
    by_source: {
      app: cats.filter((c) => c.source === "app"),
      web: cats.filter((c) => c.source === "web"),
      watch: cats.filter((c) => c.source === "watch"),
    },
    daily: scores.map((s) => ({ day: s.day, score: s.score, grade: s.grade,
      academic_sec: s.academic_sec, distraction_sec: s.distraction_sec,
      has_data: s.has_data })),
  };
}

/**
 * Consecutive days, counting back from today, scoring 60 or better.
 *
 * A day with no data ends the streak rather than being skipped: a streak is a
 * habit claim, and silently bridging a gap would overstate it. Today is allowed
 * to be empty without breaking yesterday's streak, because the day is not over.
 */
function streak(deviceId) {
  const rows = db
    .prepare(
      `SELECT day, score, has_data FROM progress_scores
        WHERE device_id = ? ORDER BY day DESC LIMIT 400`
    )
    .all(deviceId);
  const byDay = new Map(rows.map((r) => [r.day, r]));
  const cursor = new Date();
  let count = 0;
  const today = dayString(cursor);
  for (let i = 0; i < 400; i += 1) {
    const day = dayString(cursor);
    const row = byDay.get(day);
    const good = row && row.has_data === 1 && row.score >= 60;
    if (good) count += 1;
    else if (!(day === today)) break;
    cursor.setDate(cursor.getDate() - 1);
  }
  return count;
}

/** Every day that has raw rows for a device — used by the "rebuild all" route. */
function knownDays(deviceId) {
  return db
    .prepare(
      `SELECT day FROM (
         SELECT DISTINCT day FROM usage_sessions WHERE device_id = ?
         UNION SELECT DISTINCT day FROM web_visits WHERE device_id = ?
       ) ORDER BY day ASC`
    )
    .all(deviceId, deviceId)
    .map((r) => r.day);
}

module.exports = {
  MIN_DATA_SEC,
  DEFAULT_TARGET_SEC,
  EFFORT_CONSTRUCTIVE_FACTOR,
  ATTAINMENT_SHARE,
  FOCUS_SHARE,
  MAX_PENALTY,
  GRADE_BANDS,
  gradeFor,
  scoreLedger,
  buildLedger,
  rollupDay,
  rollupMany,
  reclassifyAndRoll,
  targetSecFor,
  dayString,
  dayRange,
  scoreForDay,
  ledgerForDay,
  trend,
  summarize,
  streak,
  knownDays,
};
