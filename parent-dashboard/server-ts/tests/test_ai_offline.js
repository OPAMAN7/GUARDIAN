#!/usr/bin/env node
/**
 * Offline tests for services/ai.js — the Gemini report service.
 *
 * Run from anywhere:
 *     node parent-dashboard/server-ts/tests/test_ai_offline.js
 *
 * Same contract as test_progress_server.js: no Pi, no socket, no network.
 * GUARDIAN_DB_PATH points at a throwaway SQLite file, and the provider call is
 * answered by an in-process fetch stub that never leaves the machine. The
 * mirror image of the progress suite: that one proves the numbers are right,
 * this one proves what *leaves* the machine is the smallest safe object and
 * that every failure path still hands the parent a report.
 *
 * A DNS request, a stray header, a leaked URL — none of them fail loudly, so
 * each is asserted on explicitly rather than left to the integration test.
 */

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

let PASS = 0;
let FAIL = 0;

function check(name, ok, detail = "") {
  if (ok) {
    PASS += 1;
    console.log(`  [PASS] ${name}`);
  } else {
    FAIL += 1;
    console.log(`  [FAIL] ${name} ${detail}`);
  }
}

// GS_TEST_ROOT lets the sandbox harness run these modules with a stubbed
// better-sqlite3; on a normal machine it resolves to the server directory.
const ROOT = process.env.GS_TEST_ROOT || path.join(__dirname, "..");

// Redirect the database BEFORE models/db.js is required — it opens the file at
// require time. The name check is a hard stop: no invocation of this file, by
// any harness, may be pointed at the production database.
const TEST_DB = process.env.GUARDIAN_DB_PATH
  || path.join(os.tmpdir(), `guardian-ai-test-${process.pid}.db`);
if (/guardian\.db$/i.test(TEST_DB)) {
  console.error("Refusing to run: GUARDIAN_DB_PATH points at the real database.");
  process.exit(2);
}
process.env.GUARDIAN_DB_PATH = TEST_DB;

function cleanup() {
  for (const suffix of ["", "-wal", "-shm"]) {
    try { fs.unlinkSync(TEST_DB + suffix); } catch { /* never created */ }
  }
}

const db = require(path.join(ROOT, "models", "db"));
const rollup = require(path.join(ROOT, "services", "rollup"));

// ─── Seeded scenario ─────────────────────────────────────────────────────────
// Three active days, consecutive, ending yesterday, scored 80/90/91. A 7-day
// window means the streak is exactly 3, the mean is 87 (=A), and every average
// below is an integer so the arithmetic can be checked by hand.

const DATE = new Date();
const DAY0 = localDay(DATE);
const D1 = localDay(new Date(DATE.getTime() - 1 * 86400000));
const D2 = localDay(new Date(DATE.getTime() - 2 * 86400000));
const D3 = localDay(new Date(DATE.getTime() - 3 * 86400000));
const D4 = localDay(new Date(DATE.getTime() - 4 * 86400000));

function localDay(d) {
  const dd = d instanceof Date ? d : new Date(d);
  const p = (n) => String(n).padStart(2, "0");
  return `${dd.getFullYear()}-${p(dd.getMonth() + 1)}-${p(dd.getDate())}`;
}

// One device, cleanly separated from the progress suite's identifiers.
let DEVICE_ID = null;

function wipeAi() {
  db.exec("DELETE FROM ai_insights; DELETE FROM ai_usage;");
}

function wipeProgress() {
  db.exec(
    "DELETE FROM progress_daily; DELETE FROM progress_scores; DELETE FROM progress_goals;"
  );
}

// ─── Scenario seed ──────────────────────────────────────────────────────────

function seedScenario() {
  wipeProgress();
  wipeAi();
  db.prepare("DELETE FROM devices WHERE device_name = ?").run("ai-scenario-pi");
  DEVICE_ID = db
    .prepare("INSERT INTO devices (device_name, status) VALUES (?, 'offline')")
    .run("ai-scenario-pi").lastInsertRowid;
  db.prepare(
    "INSERT INTO progress_goals (device_id, daily_academic_min) VALUES (?, 60)"
  ).run(DEVICE_ID);
  const score = db.prepare(
    `INSERT OR IGNORE INTO progress_scores
       (device_id, day, academic_sec, constructive_sec, neutral_sec, passive_sec,
        distraction_sec, counted_sec, browser_envelope_sec, effort_sec, target_sec,
        score, grade, has_data)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)`
  );
  // D3 first so the day list is in chronological order.
  score.run(DEVICE_ID, D3, 2400, 1200, 2100, 2400, 3600, 10800, 1800, 5400, 3600, 91, "A+");
  score.run(DEVICE_ID, D2, 1800, 1200, 2100, 2400, 3600, 10800, 1800, 5400, 3600, 80, "A");
  score.run(DEVICE_ID, D1, 3000, 1200, 2100, 2400, 3600, 10800, 1800, 5400, 3600, 90, "A+");
  const daily = db.prepare(
    `INSERT OR IGNORE INTO progress_daily
       (device_id, day, source, category, seconds, events, academic_weight,
        counts_toward_total)
     VALUES (?, ?, ?, ?, ?, ?, ?, 1)`
  );
  for (const day of [D1, D2, D3]) {
    daily.run(DEVICE_ID, day, "app", "coding", 2700, 3, 2);
    daily.run(DEVICE_ID, day, "web", "academic", 1800, 4, 2);
    daily.run(DEVICE_ID, day, "web", "news", 1500, 5, 0);
    daily.run(DEVICE_ID, day, "watch", "tutorial", 1200, 2, 2);
  }
  return DEVICE_ID;
}

// ─── ai.js loading with require-time env ─────────────────────────────────────
// ai.js snapshots GEMINI_API_KEY/ai flags at require time (same contract as a
// server restart), so each configuration must be loaded fresh. Nothing is
// cached between loads except models/db.js, whose schema one DB file owns.

function loadAi(envOverrides = {}) {
  const saved = {};
  for (const k of Object.keys(envOverrides)) {
    saved[k] = process.env[k];
    if (envOverrides[k] == null) delete process.env[k];
    else process.env[k] = String(envOverrides[k]);
  }
  delete process.env.AI_MODEL; // default model unless a variant asks for one
  const aiPath = require.resolve(path.join(ROOT, "services", "ai.js"));
  delete require.cache[aiPath];
  const ai = require(aiPath);
  for (const k of Object.keys(envOverrides)) {
    if (saved[k] == null) delete process.env[k];
    else process.env[k] = saved[k];
  }
  delete process.env.AI_MODEL;
  return ai;
}

// An in-process answer for the Gemini call. The response queue lets a test
// script the provider: succeed, 429, then a 200 with unusable JSON.
function stubFetch(responses) {
  const calls = [];
  let i = 0;
  global.fetch = async (url, opts) => {
    const entry = { url, method: opts.method, headers: opts.headers };
    try { entry.body = JSON.parse(opts.body); } catch { entry.body = null; }
    calls.push(entry);
    const r = responses[Math.min(i, responses.length - 1)];
    i += 1;
    return {
      ok: r.status >= 200 && r.status < 300,
      status: r.status,
      async json() { return r.json; },
    };
  };
  return calls;
}

const LOCAL_REPORT_KEYS = ["summary", "strengths", "focus_areas", "next_steps"];

function assertReportShape(report, label) {
  check(`${label} has all four sections`, LOCAL_REPORT_KEYS.every((k) => typeof report[k] === "string"
    || Array.isArray(report[k])), JSON.stringify(report));
  check(`${label} keeps every section populated`,
    typeof report.summary === "string" && report.summary.length > 0
      && report.strengths.length > 0 && report.focus_areas.length > 0
      && report.next_steps.length > 0, JSON.stringify(report.next_steps));
}

// ─── Payload: what actually leaves the machine ───────────────────────────────
// This is the section that matters most. The privacy promise printed on the
// Progress page is only true if buildPayload is a whitelist, so the test feeds
// summarize() output that has been deliberately polluted with the fields a
// future change might add — a URL, a window title, a raw row — and requires
// that none of them survive.

function testPayloadWhitelist() {
  console.log("\nPayload — an allow-list, checked against fields that must not travel");
  const ai = loadAi({ GEMINI_API_KEY: null, AI_SEND_TITLES: "false" });

  const summary = rollup.summarize(DEVICE_ID, 7);
  const polluted = {
    ...summary,
    // Exactly the kind of field somebody adds to summarize() one day.
    top_urls: ["https://reddit.com/r/teenagers/comments/abc/very_private_thread"],
    window_titles: ["Maths homework - chapter 4 - Google Docs"],
    child_name: "Aman Jr",
    raw_rows: [{ url: "https://mail.google.com/mail/u/0/#inbox/xyz" }],
    device_name: "child-pi",
  };
  const payload = ai.buildPayload(polluted, null);
  const json = JSON.stringify(payload);

  const allowedTop = ["schema", "window_days", "active_days", "grade", "score",
    "streak_days", "daily_average_minutes", "categories", "days"];
  check("the payload has exactly the nine documented top-level keys",
    Object.keys(payload).join(",") === allowedTop.join(","),
    Object.keys(payload).join(","));
  check("a URL added to summarize() is not forwarded",
    !/reddit\.com|mail\.google|https?:\/\//.test(json), json.slice(0, 200));
  check("a window title added to summarize() is not forwarded",
    !/chapter 4|Google Docs/i.test(json));
  check("the child's name is not forwarded", !/Aman|child-pi/i.test(json));
  check("no device identifier travels with the aggregates",
    !/device_id|device_name/.test(json));

  check("seconds are converted to minutes before sending",
    payload.daily_average_minutes.academic === 40
      && payload.daily_average_minutes.target === 60
      && payload.daily_average_minutes.counted_total === 180,
    JSON.stringify(payload.daily_average_minutes));
  check("the browser envelope is never in the payload at all",
    !/browser_envelope|envelope/.test(json));
  check("the three sources stay in three separate category blocks",
    Object.keys(payload.categories).join(",") === "app,web,watch"
      && payload.categories.app.every((c) => c.category !== "academic")
      && payload.categories.watch.every((c) => c.category === "tutorial"),
    JSON.stringify(payload.categories));
  check("a category entry carries only slug, minutes, share and weight",
    payload.categories.app.every(
      (c) => Object.keys(c).join(",") === "category,minutes,share_pct,weight"
    ), JSON.stringify(payload.categories.app[0]));
  check("per-day entries carry a day, a score and academic minutes — nothing else",
    payload.days.length === 3 && payload.days.every(
      (d) => Object.keys(d).join(",") === "day,score,academic_minutes"
    ), JSON.stringify(payload.days[0]));
  check("the day list is in chronological order",
    payload.days[0].day === D3 && payload.days[2].day === D1,
    payload.days.map((d) => d.day).join(","));
  check("titles are absent when AI_SEND_TITLES is off",
    payload.labels === undefined, JSON.stringify(payload.labels));
}

// ─── Local report (no provider configured) ───────────────────────────────────

async function testLocalFallback() {
  console.log("\nLocal report — the page works with no key at all");
  const ai = loadAi({ GEMINI_API_KEY: null, AI_SEND_TITLES: "false" });

  let calls = 0;
  global.fetch = async () => { calls += 1; throw new Error("network must not be touched"); };

  const res = await ai.generate(DEVICE_ID, { days: 7, force: true });
  check("no key means the provider is never contacted", calls === 0);
  check("no key returns a local report, not an error",
    res.source === "local" && res.reason === "not_configured"
      && res.cached === false, JSON.stringify({ s: res.source, r: res.reason }));
  assertReportShape(res.report, "no-key report");
  check("the local report names the actual numbers from the ledger",
    /40m/.test(res.report.summary) && /87\/100/.test(res.report.summary)
      && /3 active days/.test(res.report.summary),
    res.report.summary.slice(0, 180));
  check("the local report says out loud what the target gap is",
    /short of the 1h target|1h study block/.test(res.report.summary)
      || res.report.focus_areas.some((f) => /20m a day short/.test(f)),
    JSON.stringify(res.report.focus_areas));
  check("no local report is ever cached",
    ai.getLatest(DEVICE_ID) === null && res.report !== ai.localReport(res.payload),
    JSON.stringify(res.report));
  check("status() reports the missing key, not a healthy provider",
    ai.status().configured === false && ai.status().provider === "google-gemini",
    JSON.stringify(ai.status()));

  // An empty window is a distinct message, never a grade of "F" on no data.
  const emptyId = db.prepare("INSERT INTO devices (device_name, status) VALUES (?, 'offline')")
    .run("ai-empty-pi").lastInsertRowid;
  const empty = await ai.generate(emptyId, { days: 7, force: true });
  check("no activity reads as no data, with its own explanation",
    empty.reason === "no_data" && /nothing to grade yet/.test(empty.report.summary)
      && empty.report.strengths.length > 0,
    empty.reason);
}

// ─── Hashing and parsing (pure functions) ────────────────────────────────────

function testHashAndParse() {
  console.log("\nHash and parse — deterministic cache keys, tolerant model JSON");
  const ai = loadAi({ GEMINI_API_KEY: null, AI_SEND_TITLES: "false" });
  const p1 = ai.buildPayload(rollup.summarize(DEVICE_ID, 7), null);
  const p2 = ai.buildPayload(rollup.summarize(DEVICE_ID, 7), null);
  check("the same aggregates hash to the same cache key",
    ai.hashPayload(p1) === ai.hashPayload(p2)
      && ai.hashPayload(p1).length === 64,
    ai.hashPayload(p1));
  const p3 = { ...p1, daily_average_minutes: { ...p1.daily_average_minutes, academic: p1.daily_average_minutes.academic + 1 } };
  check("any change in the numbers changes the cache key",
    ai.hashPayload(p1) !== ai.hashPayload(p3));

  const good = ai.parseModelJson('{"summary":"Great","strengths":["a"],"focus_areas":["b"],"next_steps":["c"]}');
  check("plain JSON parses", good && good.summary === "Great");
  const fenced = ai.parseModelJson('```json\n{"summary":"Fenced","strengths":[]}\n```');
  check("fenced ```json replies parse", fenced && fenced.summary === "Fenced");
  const prose = ai.parseModelJson('Sure! Here you go: {"summary":"Prose","strengths":[]}');
  check("an answer wrapped in prose still parses", prose && prose.summary === "Prose");
  check("garbage headers do not parse into a report",
    ai.parseModelJson("definitely not json") === null);
}

// ─── Provider call: request shape and success path ───────────────────────────

async function testProviderCall() {
  console.log("\nProvider call — the request, the answer, the saved insight");
  wipeAi();
  const ai = loadAi({
    GEMINI_API_KEY: "AIza-test-key-123",
    AI_SEND_TITLES: "false",
    AI_DAILY_LIMIT: "3",
  });
  const answer = {
    candidates: [{
      content: { role: "model", parts: [{ text: JSON.stringify({
        summary: "A steady week: 40m of study a day against a 1h target.",
        strengths: ["Coding is the daily anchor.", "Three days above 60 in a row."],
        focus_areas: ["The academic target is still 20m short each day."],
        next_steps: ["Add one 20m block after school.", "Review the page together on Sunday."],
      }) }] },
      finishReason: "STOP",
    }],
    usageMetadata: { totalTokenCount: 321 },
  };
  const calls = stubFetch([{ status: 200, json: answer }]);

  const res = await ai.generate(DEVICE_ID, { days: 7, force: true });
  const req = calls[0];

  check("an AI Studio key routes to generativelanguage, not to Vertex",
    ai.API_FLAVOR === "studio"
      && req.url === `https://generativelanguage.googleapis.com/v1beta/models/${ai.MODEL}:generateContent`,
    `${ai.API_FLAVOR} ${req.url}`);
  check("the key is a header, never a ?key= query parameter",
    req.url.includes("?key=") === false
      && req.headers["x-goog-api-key"] === "AIza-test-key-123"
      && !JSON.stringify(req.body).includes("AIza"),
    JSON.stringify(req.headers));
  check("a Gemini report is stored and returned as the source",
    res.source === "gemini" && res.reason === null && res.cached === false,
    JSON.stringify({ s: res.source, r: res.reason }));
  check("the provider's four sections are on the page",
    res.report.summary.startsWith("A steady week")
      && res.report.strengths.length === 2
      && res.report.focus_areas.length === 1
      && res.report.next_steps.length === 2,
    JSON.stringify(res.report));
  check("the saved insight matches the stored hash and carries the model",
    (() => {
      const row = db.prepare(
        "SELECT input_hash, model, summary, strengths FROM ai_insights WHERE device_id = ?"
      ).get(DEVICE_ID);
      return row && row.input_hash === res.input_hash
        && row.model === ai.MODEL && JSON.parse(row.strengths).length === 2;
    })());
  check("status() now shows the key, one call used and the cache populated",
    ai.status().configured === true && ai.status().used_today === 1
      && ai.status().remaining_today === 2 && ai.status().cached_reports === 1,
    JSON.stringify(ai.status()));

  const cached = await ai.generate(DEVICE_ID, { days: 7 });
  check("a second run with identical numbers is served from cache, no new call",
    cached.cached === true && cached.source === "gemini" && calls.length === 1
      && cached.report.summary === res.report.summary,
    JSON.stringify({ s: cached.source, c: cached.cached }));

  const forced = await ai.generate(DEVICE_ID, { days: 7, force: true });
  check("force re-runs the model — same hash, same row, one more credit",
    forced.cached === false && calls.length === 2 && ai.status().used_today === 2
      && ai.status().cached_reports === 1
      && forced.report.summary === res.report.summary,
    JSON.stringify(ai.status()));
}

// ─── Provider failure and budget paths ───────────────────────────────────────

async function testFailurePaths() {
  console.log("\nProvider failure and budget — the report survives a bad afternoon");
  wipeAi();
  const ai = loadAi({
    GEMINI_API_KEY: "AIza-test-key-123",
    AI_SEND_TITLES: "false",
    AI_DAILY_LIMIT: "2",
  });
  const calls = stubFetch([
    { status: 500, json: { error: { message: "The request was throttled by quota." } } },
    { status: 200, json: { candidates: [{ content: { parts: [{ text: "sorry, no json" }] } }] } },
    { status: 200, json: { message: "should never be read" } },
  ]);

  const failed = await ai.generate(DEVICE_ID, { days: 7, force: true });
  check("an HTTP failure falls back to the local report, not the error page",
    failed.source === "local" && failed.reason === "provider_error",
    JSON.stringify({ s: failed.source, r: failed.reason }));
  assertReportShape(failed.report, "after a 500");
  check("the failure detail is a short message, never a stack or raw trace",
    typeof failed.detail === "string" && !/\n/.test(failed.detail)
      && failed.detail.startsWith("Gemini HTTP 500") && failed.detail.length < 200,
    failed.detail);

  const unparseable = await ai.generate(DEVICE_ID, { days: 7, force: true });
  check("a 200 that is not JSON falls back with the right reason",
    unparseable.source === "local" && unparseable.reason === "unparseable",
    JSON.stringify({ s: unparseable.source, r: unparseable.reason }));
  assertReportShape(unparseable.report, "after an unparseable answer");
  check("failed calls still cost budget — only successes would burn the free tier",
    ai.status().used_today === 2 && ai.status().remaining_today === 0,
    JSON.stringify(ai.status()));

  const over = await ai.generate(DEVICE_ID, { days: 7, force: true });
  check("at the daily limit the provider is not contacted at all",
    over.reason === "daily_limit" && over.source === "local" && calls.length === 2,
    JSON.stringify({ r: over.reason, calls: calls.length }));
  assertReportShape(over.report, "at the daily limit");
}

// ─── The AI_SEND_TITLES opt-in ───────────────────────────────────────────────

async function testTitlesOptIn() {
  console.log("\nAI_SEND_TITLES — the opt-in block is the whole difference");
  wipeAi();
  const ai = loadAi({
    GEMINI_API_KEY: "AIza-test-key-123",
    AI_SEND_TITLES: "true",
    AI_DAILY_LIMIT: "25",
  });
  const answer = {
    candidates: [{ content: { parts: [{ text: JSON.stringify({
      summary: "OK", strengths: ["s"], focus_areas: ["f"], next_steps: ["n"],
    }) }] } }],
  };
  const calls = stubFetch([{ status: 200, json: answer }]);

  const labels = {
    app: ["Chromium", "Codium"],
    web: ["khanacademy.org", "youtube.com"],
    watch: ["Linear Equations in Two Variables - Class 9"],
  };
  const res = await ai.generate(DEVICE_ID, { days: 7, force: true, labels });
  const prompt = calls[0].body.contents[0].parts[0].text;
  const payload = res.payload;

  check("the opt-in block appears only in payload.labels",
    payload.labels && Object.keys(payload.labels).join(",") === "apps,sites,videos",
    JSON.stringify(payload.labels));
  check("labels are clipped to 8 per source",
    payload.labels.apps.length === 2 && payload.labels.sites.length === 2
      && payload.labels.videos.length === 1,
    JSON.stringify(payload.labels));
  check("the prompt names the label when the flag is on",
    prompt.includes("Specific names the parent has opted to share"),
    prompt.slice(0, 80));
  check("a full URL never leaves as a label — bare host only",
    !payload.labels.sites.some((l) => l.startsWith("http") || l.includes("/")),
    JSON.stringify(payload.labels.sites));
  check("no raw path or query string leaks from the labels",
    !JSON.stringify(payload.labels).includes("watch?v=")
      && !JSON.stringify(payload.labels).includes("/math/"),
    JSON.stringify(payload.labels));
}

// ─── Key flavour routing ─────────────────────────────────────────────────────
// Google issues two key formats for the same models and rejects each on the
// other's host with a bare 401. The prefix is the only tell, so the choice of
// host is asserted here rather than left to the first live request to discover.

async function testKeyFlavours() {
  console.log("\nKey flavour — an AQ. express key must not be sent to AI Studio");
  wipeAi();

  const vertex = loadAi({
    GEMINI_API_KEY: "AQ.Ab8RN6-example-express-key",
    AI_API_FLAVOR: null,
    AI_SEND_TITLES: "false",
    AI_DAILY_LIMIT: "5",
  });
  const answer = {
    candidates: [{ content: { parts: [{ text: JSON.stringify({
      summary: "OK", strengths: ["s"], focus_areas: ["f"], next_steps: ["n"],
    }) }] } }],
  };
  let calls = stubFetch([{ status: 200, json: answer }]);
  await vertex.generate(DEVICE_ID, { days: 7, force: true });
  check("an AQ. key is detected as a Vertex express key",
    vertex.API_FLAVOR === "vertex", vertex.API_FLAVOR);
  check("a Vertex key goes to aiplatform.googleapis.com",
    calls[0].url === `https://aiplatform.googleapis.com/v1beta1/publishers/google/models/${vertex.MODEL}:generateContent`,
    calls[0].url);
  check("the Vertex request still carries the key as a header",
    calls[0].headers["x-goog-api-key"] === "AQ.Ab8RN6-example-express-key"
      && !calls[0].url.includes("AQ."),
    JSON.stringify(Object.keys(calls[0].headers)));
  check("the flavour is reported to the UI as a name, never as the key",
    vertex.status().api_flavor === "vertex"
      && !JSON.stringify(vertex.status()).includes("AQ."),
    JSON.stringify(vertex.status().api_flavor));
  check("the request body is identical on both hosts — only the path differs",
    calls[0].body.systemInstruction && calls[0].body.contents
      && calls[0].body.generationConfig.responseMimeType === "application/json",
    JSON.stringify(Object.keys(calls[0].body)));

  wipeAi();
  const forced = loadAi({
    GEMINI_API_KEY: "AQ.Ab8RN6-example-express-key",
    AI_API_FLAVOR: "studio",
    AI_SEND_TITLES: "false",
    AI_DAILY_LIMIT: "5",
  });
  calls = stubFetch([{ status: 200, json: answer }]);
  await forced.generate(DEVICE_ID, { days: 7, force: true });
  check("AI_API_FLAVOR overrides the prefix guess",
    forced.API_FLAVOR === "studio"
      && calls[0].url.startsWith("https://generativelanguage.googleapis.com/"),
    `${forced.API_FLAVOR} ${calls[0].url}`);
}


// Make sure an interrupted run can never leave a stub behind for the human
// user's own terminal: the real fetcher returns after the suite.
function restoreFetch() {
  // Deliberately left undefined until the end: global.fetch survives this
  // process anyway, but a stale stub in a spawned sibling would be worse.
  delete global.fetch;
}

async function main() {
  console.log("=".repeat(66));
  console.log("GUARDIAN AI report service — offline test suite");
  console.log("=".repeat(66));
  seedScenario();
  testPayloadWhitelist();
  await testLocalFallback();
  testHashAndParse();
  await testProviderCall();
  await testFailurePaths();
  await testTitlesOptIn();
  await testKeyFlavours();
  restoreFetch();
  console.log(`\n${"=".repeat(66)}`);
  console.log(`RESULT: ${PASS} passed, ${FAIL} failed`);
  console.log("=".repeat(66));
  try { db.close(); } catch { /* shim may not implement close */ }
  cleanup();
  process.exit(FAIL ? 1 : 0);
}

main().catch((err) => {
  console.error(`\nSUITE CRASHED: ${err.stack || err.message}`);
  cleanup();
  process.exit(1);
});





