/**
 * Guardian Shield — AI progress report (Google Gemini)
 * ====================================================
 *
 * Turns the numbers on the Progress page into a short written report for the
 * parent: what is going well, what to look at next, what to actually do.
 *
 * Three rules shape every line of this file.
 *
 * 1. Aggregates only. The single input is `rollup.summarize()` — per-band daily
 *    averages and per-category totals. No screenshot, no URL, no search term and
 *    no page title reaches the provider. `buildPayload()` copies a fixed
 *    whitelist field by field and never spreads the summary object, so a column
 *    added to summarize() later cannot leak by accident.
 *
 * 2. It works without a key. Every failure path — no key, no global fetch, quota
 *    spent, provider error, unparseable answer — falls through to
 *    `localReport()`, which writes the same four sections from the same
 *    aggregates with no network at all. The parent always gets an answer; only
 *    the prose quality changes.
 *
 * 3. The same numbers are never paid for twice. The payload is hashed and the
 *    answer cached in `ai_insights` under UNIQUE(device_id, input_hash), so
 *    re-opening the page is free and a day of clicking costs one call.
 *
 * The key lives in server-ts/.env as GEMINI_API_KEY and is read here only. It
 * travels as a request header, never as a query parameter, so it cannot surface
 * in a log line or an error string, and `status()` returns booleans — nothing
 * this module exports ever carries the key toward the browser bundle.
 */

"use strict";

const crypto = require("crypto");
const db = require("../models/db");
const rollup = require("./rollup");

// ─── Configuration ──────────────────────────────────────────────────────────
// Read once at require time. Changing a key means restarting the server, which
// is the same contract as every other secret in .env.

const PROVIDER = "google-gemini";
const MODEL = (process.env.AI_MODEL || "gemini-2.5-flash").trim() || "gemini-2.5-flash";
const API_KEY = (process.env.GEMINI_API_KEY || "").trim();

/**
 * Two different Google products issue keys for the same models, and they are not
 * interchangeable — an AI Studio key is rejected by Vertex and an express-mode
 * key is rejected by AI Studio, both with a bare 401 that says nothing useful.
 * The key prefix is the only reliable tell:
 *
 *   AIza…  AI Studio (aistudio.google.com/apikey)  → generativelanguage.googleapis.com
 *   AQ.…   Vertex AI express mode                  → aiplatform.googleapis.com
 *
 * Detected rather than configured so the parent pastes a key and it works.
 * AI_API_FLAVOR=studio|vertex forces it if a future key format fools the guess.
 */
function detectFlavor(key) {
  const forced = String(process.env.AI_API_FLAVOR || "").trim().toLowerCase();
  if (forced === "studio" || forced === "vertex") return forced;
  if (/^AQ\./.test(key)) return "vertex";
  return "studio";
}

const API_FLAVOR = detectFlavor(API_KEY);

/** Same request body and same response shape on both hosts — only the path differs. */
const ENDPOINT = API_FLAVOR === "vertex"
  ? `https://aiplatform.googleapis.com/v1beta1/publishers/google/models/${MODEL}:generateContent`
  : `https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent`;


/** Free-tier guard rail. A stuck front end must not be able to spend the quota. */
const DAILY_LIMIT = Math.max(1, parseInt(process.env.AI_DAILY_LIMIT || "25", 10) || 25);
const TIMEOUT_MS = Math.max(5000, parseInt(process.env.AI_TIMEOUT_MS || "25000", 10) || 25000);

/**
 * Off by default, and deliberately awkward to turn on.
 *
 * With AI_SEND_TITLES=true the payload gains one extra block: the top few app
 * names, the top few bare domains (host only — never a path, never a query
 * string) and the top few YouTube video titles for the window. That is the
 * complete difference. It buys a report that can say "the maths channel" instead
 * of "education video time", and it costs the child's specific viewing history
 * leaving the house. Left false, no label of any kind is transmitted.
 */
const SEND_TITLES = String(process.env.AI_SEND_TITLES || "false").toLowerCase() === "true";

/** Bump when the payload shape or the prompt changes, so old caches miss. */
const PAYLOAD_VERSION = 1;

const hasFetch = () => typeof fetch === "function";

// ─── Budget ledger ──────────────────────────────────────────────────────────

const utcDay = () => new Date().toISOString().slice(0, 10);

function usedToday() {
  const row = db.prepare("SELECT calls FROM ai_usage WHERE day = ?").get(utcDay());
  return row ? Number(row.calls) || 0 : 0;
}

/**
 * Count the call before making it, not after.
 *
 * A request that times out or 429s has still been seen by the provider, and
 * counting only successes is how a retry loop quietly burns a day's free tier.
 */
function chargeCall() {
  db.prepare(
    `INSERT INTO ai_usage (day, calls, updated_at) VALUES (?, 1, CURRENT_TIMESTAMP)
       ON CONFLICT(day) DO UPDATE SET calls = calls + 1, updated_at = CURRENT_TIMESTAMP`
  ).run(utcDay());
}

// ─── Payload ────────────────────────────────────────────────────────────────

const mins = (sec) => Math.round((Number(sec) || 0) / 60);
const pct = (part, whole) => (whole > 0 ? Math.round((part / whole) * 1000) / 10 : 0);

/** A short, safe label: category slugs only, hard length cap, control chars out. */
function tidy(value, max = 60) {
  return String(value == null ? "" : value)
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .trim()
    .slice(0, max);
}

/**
 * The exact bytes that leave this machine.
 *
 * Written as an explicit field-by-field copy on purpose. The tempting version is
 * `{...summary}` minus a deny-list, but a deny-list is wrong by default: the day
 * someone adds a `top_urls` field to summarize(), the deny-list version starts
 * shipping browsing history and nothing fails. This version starts ignoring it.
 */
function buildPayload(summary, labels) {
  const a = (summary && summary.averages) || {};
  const src = (summary && summary.by_source) || {};
  const counted = Number(a.counted_sec) || 0;

  const catBlock = (key) => (Array.isArray(src[key]) ? src[key] : [])
    .slice(0, 12)
    .map((c) => ({
      category: tidy(c.category, 40),
      minutes: mins(c.seconds),
      share_pct: pct(Number(c.seconds) || 0, counted * (Number(summary.active_days) || 1)),
      weight: Number(c.weight) || 0,
    }));

  const payload = {
    schema: PAYLOAD_VERSION,
    window_days: Number((summary.period || {}).days) || 7,
    active_days: Number(summary.active_days) || 0,
    grade: tidy(summary.grade, 4),
    score: Number(summary.score) || 0,
    streak_days: Number(summary.streak) || 0,
    daily_average_minutes: {
      academic: mins(a.academic_sec),
      constructive: mins(a.constructive_sec),
      neutral: mins(a.neutral_sec),
      passive: mins(a.passive_sec),
      distraction: mins(a.distraction_sec),
      counted_total: mins(a.counted_sec),
      effort: mins(a.effort_sec),
      target: mins(a.target_sec),
    },
    categories: { app: catBlock("app"), web: catBlock("web"), watch: catBlock("watch") },
    days: (Array.isArray(summary.daily) ? summary.daily : []).map((d) => ({
      day: tidy(d.day, 10),
      score: d.has_data ? Number(d.score) || 0 : null,
      academic_minutes: mins(d.academic_sec),
    })),
  };

  // Opt-in and visibly separate, so "what did we send" has a one-word answer.
  if (SEND_TITLES && labels) {
    payload.labels = {
      apps: (labels.app || []).slice(0, 8).map((l) => tidy(l, 40)),
      sites: (labels.web || []).slice(0, 8).map((l) => tidy(l, 40)),
      videos: (labels.watch || []).slice(0, 8).map((l) => tidy(l, 80)),
    };
  }
  return payload;
}

/** Deterministic because buildPayload emits keys in a fixed order. */
function hashPayload(payload) {
  return crypto
    .createHash("sha256")
    .update(`${MODEL}|v${PAYLOAD_VERSION}|${JSON.stringify(payload)}`)
    .digest("hex");
}

// ─── Local report (no network) ───────────────────────────────────────────────

const humanCat = (slug) => tidy(slug, 40).replace(/[_-]+/g, " ").replace(/^./, (c) => c.toUpperCase());
const hm = (m) => (m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`);
const SOURCE_WORD = { app: "app", web: "site", watch: "video" };

/** Every category across the three sources, tagged with where it came from. */
function rankCategories(payload, predicate) {
  const out = [];
  for (const src of ["app", "web", "watch"]) {
    for (const c of payload.categories[src] || []) {
      if (predicate(c)) out.push({ ...c, source: src });
    }
  }
  return out.sort((x, y) => y.minutes - x.minutes);
}

/**
 * The report the parent gets when there is no key, no quota or no network.
 *
 * Not a placeholder and not an apology: it states the same four things from the
 * same aggregates, just in plainer sentences. A parent who never configures a
 * provider still gets a usable page, which is the only reason it is acceptable
 * for the AI part of this feature to be optional at all.
 */
function localReport(payload) {
  const d = payload.daily_average_minutes;
  const short = Math.max(0, d.target - d.academic);
  const good = rankCategories(payload, (c) => c.weight > 0).slice(0, 3);
  const bad = rankCategories(payload, (c) => c.weight < 0).slice(0, 3);
  const missed = payload.window_days - payload.active_days;

  const summary = payload.active_days === 0
    ? `No activity has been recorded for this device in the last ${payload.window_days} days, `
      + "so there is nothing to grade yet. Once the agent has been running for a "
      + "day the grade, the bands and this report all fill in automatically."
    : `Over the last ${payload.window_days} days this device averaged ${hm(d.academic)} of `
      + `academic activity a day against a ${hm(d.target)} target, plus ${hm(d.constructive)} `
      + `of constructive time, out of ${hm(d.counted_total)} of counted screen time. `
      + `That is a ${payload.grade} (${payload.score}/100) across ${payload.active_days} `
      + `active day${payload.active_days === 1 ? "" : "s"}`
      + (payload.streak_days > 0 ? `, with a ${payload.streak_days}-day run above 60.` : ".");

  const strengths = [];
  if (d.academic >= d.target) strengths.push(`The daily academic target of ${hm(d.target)} is being met on average.`);
  if (payload.streak_days >= 2) strengths.push(`${payload.streak_days} days in a row scoring 60 or better.`);
  for (const c of good) {
    strengths.push(`${humanCat(c.category)} is the strongest ${SOURCE_WORD[c.source]} category at ${hm(c.minutes)} in the window.`);
  }
  if (!strengths.length) strengths.push("Not enough positive activity recorded yet to call anything a strength.");

  const focus = [];
  if (short > 0 && payload.active_days > 0) focus.push(`Academic time is ${hm(short)} a day short of the ${hm(d.target)} target.`);
  if (d.distraction > 0 && d.counted_total > 0) {
    focus.push(`Distraction is ${pct(d.distraction, d.counted_total)}% of counted time (${hm(d.distraction)} a day).`);
  }
  for (const c of bad) {
    focus.push(`${humanCat(c.category)} (${SOURCE_WORD[c.source]}) took ${hm(c.minutes)} across the window.`);
  }
  if (missed > 0) focus.push(`${missed} of the last ${payload.window_days} days have no usable data — either the device was off or the agent was not running.`);
  if (!focus.length) focus.push("Nothing stands out as a problem in this window.");

  const steps = [];
  if (short > 0) steps.push(`Aim for one more ${hm(Math.min(short, 30))} study block a day to close the gap on the target.`);
  if (bad.length) steps.push(`Agree a daily cap for ${humanCat(bad[0].category)} rather than banning it outright — the grade responds to the ratio, not to zero.`);
  if (good.length) steps.push(`Build on ${humanCat(good[0].category)}: it is already the habit that is working.`);
  steps.push("Review this page together once a week so the number is something you both look at, not something done to them.");

  return {
    summary,
    strengths,
    focus_areas: focus,
    next_steps: steps.slice(0, 4),
  };
}

// ─── Prompt ─────────────────────────────────────────────────────────────────

/**
 * The wording rules matter as much as the numbers.
 *
 * This grade measures activity against a target — it is not a measure of the
 * child. A report that says "your child is lazy" would be both unsupported by
 * the data and the fastest possible way to make a parent stop trusting the
 * feature, so the instruction below forbids diagnosing, comparing to other
 * children, and anything punitive.
 */
const SYSTEM_PROMPT = [
  "You write short progress reports for a parent using a home monitoring tool.",
  "You are given only aggregate numbers: minutes per day per activity band, per-category totals, and a 0-100 score.",
  "The score grades measured ACTIVITY against a parent-set academic-minutes target. It is not a measure of the child's ability, intelligence or character.",
  "Rules: never diagnose anything medical or psychological; never compare the child to other children or to any norm; never suggest punishment, shaming or removing the device; never invent a number that is not in the data; never claim to know what the child was thinking.",
  "Bands: academic (weight +2) is study, coursework, coding and reference; constructive (+1) is creative and productive tools; neutral (0) is uncategorised; passive (-1) is casual browsing and social feeds; distraction (-2) is games and short-form video.",
  "Write for a non-technical parent. Be specific about the numbers you were given, and concrete about what to do next. Keep every item to one or two sentences.",
  "Respond with JSON only, matching the requested schema. No markdown.",
].join("\n");

const RESPONSE_SCHEMA = {
  type: "object",
  properties: {
    summary: { type: "string" },
    strengths: { type: "array", items: { type: "string" } },
    focus_areas: { type: "array", items: { type: "string" } },
    next_steps: { type: "array", items: { type: "string" } },
  },
  required: ["summary", "strengths", "focus_areas", "next_steps"],
};

function userPrompt(payload) {
  return [
    `Window: last ${payload.window_days} days, ${payload.active_days} of them with usable data.`,
    `Grade ${payload.grade} (${payload.score}/100). Current streak of days scoring 60+: ${payload.streak_days}.`,
    "Daily averages in minutes (counted_total excludes browser window time, which is tracked separately to avoid double counting):",
    JSON.stringify(payload.daily_average_minutes),
    "Category totals for the whole window, kept in three separate groups because apps, websites and YouTube viewing are never mixed:",
    JSON.stringify(payload.categories),
    "Per-day scores (score null means too little data that day):",
    JSON.stringify(payload.days),
    payload.labels ? `Specific names the parent has opted to share: ${JSON.stringify(payload.labels)}` : "No app, site or video names were shared — reason about categories only.",
    "",
    "Write: summary (2-4 sentences on where things stand), strengths (2-3 items), focus_areas (2-3 items), next_steps (2-4 concrete actions a parent can take this week).",
  ].join("\n");
}

// ─── Provider call ──────────────────────────────────────────────────────────

const asList = (v, max = 6) => (Array.isArray(v) ? v : typeof v === "string" && v.trim() ? [v] : [])
  .map((s) => tidy(s, 400))
  .filter(Boolean)
  .slice(0, max);

/** Model answers sometimes arrive fenced in ```json despite the mime type. */
function parseModelJson(text) {
  const raw = String(text || "").trim().replace(/^```(?:json)?/i, "").replace(/```$/, "").trim();
  try {
    return JSON.parse(raw);
  } catch {
    const first = raw.indexOf("{");
    const last = raw.lastIndexOf("}");
    if (first >= 0 && last > first) {
      try { return JSON.parse(raw.slice(first, last + 1)); } catch { /* fall through */ }
    }
    return null;
  }
}

/**
 * One request, one answer, no retries.
 *
 * Retrying is what turns a bad afternoon into an exhausted free tier, and the
 * caller already has a working fallback — so a failure here just means the
 * parent reads the locally written version of the same report.
 */
async function callGemini(payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(ENDPOINT, {
      method: "POST",
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        // Header, not ?key= — a URL ends up in stack traces and proxy logs.
        "x-goog-api-key": API_KEY,
      },
      body: JSON.stringify({
        systemInstruction: { parts: [{ text: SYSTEM_PROMPT }] },
        contents: [{ role: "user", parts: [{ text: userPrompt(payload) }] }],
        generationConfig: {
          temperature: 0.4,
          maxOutputTokens: 1200,
          responseMimeType: "application/json",
          responseSchema: RESPONSE_SCHEMA,
        },
      }),
    });
    if (!res.ok) {
      let detail = "";
      try {
        const body = await res.json();
        detail = ((body || {}).error || {}).message || "";
      } catch { /* non-JSON error body */ }
      const err = new Error(`Gemini HTTP ${res.status}${detail ? `: ${tidy(detail, 200)}` : ""}`);
      err.status = res.status;
      throw err;
    }
    const body = await res.json();
    const cand = ((body.candidates || [])[0] || {});
    const text = ((cand.content || {}).parts || []).map((p) => p.text || "").join("");
    const parsed = parseModelJson(text);
    if (!parsed || !parsed.summary) {
      const err = new Error(`Gemini returned no usable report (finish: ${tidy(cand.finishReason || "unknown", 40)})`);
      err.unparseable = true;
      throw err;
    }
    return {
      summary: tidy(parsed.summary, 1500),
      strengths: asList(parsed.strengths),
      focus_areas: asList(parsed.focus_areas),
      next_steps: asList(parsed.next_steps),
    };
  } finally {
    clearTimeout(timer);
  }
}

// ─── Cache ──────────────────────────────────────────────────────────────────

const SELECT_CACHED = `SELECT id, period_start, period_end, input_hash, model, summary,
                              strengths, focus_areas, next_steps, created_at
                         FROM ai_insights WHERE device_id = ?`;

function shape(row) {
  if (!row) return null;
  const list = (s) => {
    try { const v = JSON.parse(s || "[]"); return Array.isArray(v) ? v : []; } catch { return []; }
  };
  return {
    id: row.id,
    period: { from: row.period_start, to: row.period_end },
    model: row.model,
    input_hash: row.input_hash,
    summary: row.summary || "",
    strengths: list(row.strengths),
    focus_areas: list(row.focus_areas),
    next_steps: list(row.next_steps),
    created_at: row.created_at,
  };
}

function getCached(deviceId, hash) {
  return shape(db.prepare(`${SELECT_CACHED} AND input_hash = ?`).get(deviceId, hash));
}

/** Latest report for a device, whatever window it was written for. */
function getLatest(deviceId) {
  return shape(db.prepare(`${SELECT_CACHED} ORDER BY created_at DESC, id DESC LIMIT 1`).get(deviceId));
}

/**
 * Locally written reports are never stored.
 *
 * They cost nothing to regenerate, and caching them would mean a parent who
 * clicks once before configuring a key keeps seeing the plain version from cache
 * afterwards — the confusing failure where adding the key appears to do nothing.
 */
function saveInsight(deviceId, period, hash, report, model) {
  db.prepare(
    `INSERT INTO ai_insights
       (device_id, period_start, period_end, input_hash, model, summary,
        strengths, focus_areas, next_steps, raw_json)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(device_id, input_hash) DO UPDATE SET
         summary = excluded.summary, strengths = excluded.strengths,
         focus_areas = excluded.focus_areas, next_steps = excluded.next_steps,
         model = excluded.model, raw_json = excluded.raw_json,
         created_at = CURRENT_TIMESTAMP`
  ).run(
    deviceId, period.from, period.to, hash, model, report.summary,
    JSON.stringify(report.strengths), JSON.stringify(report.focus_areas),
    JSON.stringify(report.next_steps), JSON.stringify(report)
  );
  return getCached(deviceId, hash);
}

// ─── Public surface ─────────────────────────────────────────────────────────

/** Booleans and counts only. This object is safe to serve to the browser. */
function status() {
  const used = usedToday();
  const cached = db.prepare("SELECT COUNT(*) AS n FROM ai_insights").get();
  return {
    provider: PROVIDER,
    model: MODEL,
    // Which Google host the key is for — a name, never the key or the full URL.
    api_flavor: API_FLAVOR,
    configured: Boolean(API_KEY),
    network: hasFetch(),
    send_titles: SEND_TITLES,
    daily_limit: DAILY_LIMIT,
    used_today: used,
    remaining_today: Math.max(0, DAILY_LIMIT - used),
    cached_reports: Number((cached || {}).n) || 0,
    fallback: "Reports are written from local aggregates when no provider is configured.",
  };
}

/**
 * Produce a report for a device, from cache when the numbers have not moved.
 *
 * Returns `{ report, cached, source, reason, payload, usage }`. `source` is
 * "gemini" or "local"; `reason` names the reason for a local answer so the UI can
 * say "add a key" rather than "something went wrong". `payload` is echoed back
 * deliberately: the privacy claim on the Progress page is only credible if the
 * parent can see the exact object that was sent.
 */
async function generate(deviceId, options = {}) {
  const days = Math.min(90, Math.max(1, parseInt(options.days, 10) || 7));
  const summary = rollup.summarize(deviceId, days);
  const payload = buildPayload(summary, options.labels);
  const hash = hashPayload(payload);
  const period = { from: (summary.period || {}).from, to: (summary.period || {}).to };
  const out = (extra) => ({ payload, input_hash: hash, usage: status(), ...extra });

  if (payload.active_days === 0) {
    return out({ report: localReport(payload), cached: false, source: "local", reason: "no_data" });
  }
  if (!options.force) {
    const hit = getCached(deviceId, hash);
    if (hit) return out({ report: hit, cached: true, source: "gemini", reason: null });
  }
  if (!API_KEY) {
    return out({ report: localReport(payload), cached: false, source: "local", reason: "not_configured" });
  }
  if (!hasFetch()) {
    return out({ report: localReport(payload), cached: false, source: "local", reason: "no_fetch" });
  }
  if (usedToday() >= DAILY_LIMIT) {
    return out({ report: localReport(payload), cached: false, source: "local", reason: "daily_limit" });
  }

  chargeCall();
  try {
    const report = await callGemini(payload);
    const saved = saveInsight(deviceId, period, hash, report, MODEL) || { ...report, model: MODEL };
    return out({ report: saved, cached: false, source: "gemini", reason: null, usage: status() });
  } catch (err) {
    // Never let a provider outage take the page down: same four sections, local.
    const detail = tidy(err && err.message ? err.message : String(err), 200);
    console.warn(`[AI] ${PROVIDER} call failed — serving local report (${detail})`);
    return out({
      report: localReport(payload),
      cached: false,
      source: "local",
      reason: err && err.unparseable ? "unparseable" : "provider_error",
      detail,
      usage: status(),
    });
  }
}

module.exports = {
  PROVIDER,
  MODEL,
  API_FLAVOR,
  ENDPOINT,
  DAILY_LIMIT,
  SEND_TITLES,
  PAYLOAD_VERSION,
  isConfigured: () => Boolean(API_KEY),
  status,
  buildPayload,
  hashPayload,
  localReport,
  parseModelJson,
  getCached,
  getLatest,
  usedToday,
  generate,
};

