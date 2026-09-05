#!/usr/bin/env node
/**
 * Offline tests for the academic-progress server side.
 *
 * Run from anywhere:
 *     node parent-dashboard/server-ts/tests/test_progress_server.js
 *
 * No Pi, no socket, no network. The suite points models/db.js at a throwaway
 * SQLite file in the temp directory (GUARDIAN_DB_PATH) and deletes it at the
 * end, so it never touches the real guardian.db — a test run that seeded fake
 * devices into production would corrupt every number on the Progress page. The
 * real classifier, ingest, rollup and route modules are then driven with
 * synthetic records shaped exactly like the ones the agent's collectors emit.
 *
 * The four things worth testing here are the four ways this feature can produce
 * a *wrong number* rather than an error:
 *   1. the same seconds counted twice (browser envelope vs web ledger),
 *   2. a YouTube row scored on its web category instead of its watch category,
 *   3. a replayed offline batch inflating totals,
 *   4. the grade not matching the ledger the parent is shown.
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
  || path.join(os.tmpdir(), `guardian-progress-test-${process.pid}.db`);
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
const categorize = require(path.join(ROOT, "services", "categorize"));
const ingest = require(path.join(ROOT, "services", "progressIngest"));
const rollup = require(path.join(ROOT, "services", "rollup"));

const DAY = "2026-09-04";
const PREV = "2026-09-03";

function iso(day, hh, mm) {
  return `${day}T${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}:00Z`;
}

// Three devices, deliberately.
//   DEVICE_ID  — the ingest-validation device. It ends up holding deliberately
//                malformed and clamped rows, so it must never be the device the
//                grading assertions read from.
//   PROG_ID    — the clean scenario device: exactly the rows seedScenario writes.
//   SCRATCH_ID — sacrificial, for the "erase this child's history" test.
let DEVICE_ID = null;
let PROG_ID = null;
let SCRATCH_ID = null;

function setupDevice() {
  const add = (name) => {
    db.prepare(
      "INSERT OR IGNORE INTO devices (device_name, status) VALUES (?, 'offline')"
    ).run(name);
    return db.prepare("SELECT id FROM devices WHERE device_name = ?").get(name).id;
  };
  DEVICE_ID = add("test-pi");
  PROG_ID = add("scenario-pi");
  SCRATCH_ID = add("scratch-pi");
}

// ─── Classifier ─────────────────────────────────────────────────────────────

function testClassifier() {
  console.log("\nClassifier — three disjoint taxonomies");
  const appCats = Object.keys(categorize.APP_CATEGORIES);
  const webCats = Object.keys(categorize.WEB_CATEGORIES);
  const watchCats = Object.keys(categorize.WATCH_CATEGORIES);
  const overlap = (a, b) => a.filter((c) => b.includes(c) && c !== "other");
  check("app and web categories share no name", overlap(appCats, webCats).length === 0,
    `shared: ${overlap(appCats, webCats)}`);
  check("web and watch categories share no name", overlap(webCats, watchCats).length === 0,
    `shared: ${overlap(webCats, watchCats)}`);
  check("app and watch categories share no name",
    overlap(appCats, watchCats).filter((c) => c !== "entertainment").length === 0,
    `shared: ${overlap(appCats, watchCats)}`);

  const thonny = categorize.classifyApp({ process_name: "thonny", app_name: "Thonny", is_browser: 0 });
  check("code editor is academic", thonny.category === "coding" && thonny.weight === 2,
    JSON.stringify(thonny));
  const steam = categorize.classifyApp({ process_name: "steam", app_name: "Steam", is_browser: 0 });
  check("game launcher is a distraction", steam.category === "gaming" && steam.weight === -2,
    JSON.stringify(steam));
  const chrome = categorize.classifyApp({
    process_name: "chromium", app_name: "Chromium", is_browser: 1, window_title: "Instagram",
  });
  check("browser is forced to the envelope category",
    chrome.category === "browser" && chrome.weight === 0, JSON.stringify(chrome));

  const khan = categorize.classifyWeb({ domain: "www.khanacademy.org", platform: "web" });
  check("study site is academic",
    khan.source === "web" && khan.category === "academic" && khan.watch_category === null,
    JSON.stringify(khan));
  const insta = categorize.classifyWeb({ domain: "instagram.com", platform: "web" });
  check("social site is passive", insta.category === "social" && insta.weight === -1,
    JSON.stringify(insta));
  const sub = categorize.classifyWeb({ domain: "en.wikipedia.org", platform: "web" });
  check("subdomain matches the parent rule", sub.category === "reference", JSON.stringify(sub));

  const lecture = categorize.classifyWeb({
    domain: "youtube.com", platform: "youtube", media_kind: "video",
    title: "Integration by parts | Calculus lecture 4",
  });
  check("youtube lecture is scored on the watch taxonomy",
    lecture.source === "watch" && lecture.category === "lecture" && lecture.weight === 2,
    JSON.stringify(lecture));
  check("youtube row still records its web category for context",
    lecture.web_category === "video_other", JSON.stringify(lecture));

  const short = categorize.classifyWeb({
    domain: "youtube.com", platform: "youtube", media_kind: "short",
    title: "physics revision one shot ncert",
  });
  check("a Short stays a Short whatever the title claims",
    short.category === "shorts" && short.weight === -2, JSON.stringify(short));
}

// ─── Ingest ─────────────────────────────────────────────────────────────────

function usageRec(over = {}) {
  return {
    kind: "usage",
    session_key: over.session_key || `s-${Math.random().toString(36).slice(2, 12)}`,
    app_name: "Thonny",
    process_name: "thonny",
    window_title: "essay.py",
    started_at: iso(DAY, 10, 0),
    ended_at: iso(DAY, 10, 30),
    duration_sec: 1800,
    day: DAY,
    is_browser: 0,
    platform: "linux",
    ...over,
  };
}

function webRec(over = {}) {
  return {
    kind: "web",
    visit_key: over.visit_key || `v-${Math.random().toString(36).slice(2, 12)}`,
    browser: "chromium",
    domain: "khanacademy.org",
    url_path: "/math/algebra",
    title: "Algebra | Khan Academy",
    visited_at: iso(DAY, 11, 0),
    day: DAY,
    dwell_sec: 600,
    platform: "web",
    media_id: null,
    media_kind: null,
    ...over,
  };
}

function testIngest() {
  console.log("\nIngest — idempotency and validation");
  const first = ingest.ingestBatch(DEVICE_ID, "usage", [
    usageRec({ session_key: "k1" }),
    usageRec({ session_key: "k2", process_name: "steam", app_name: "Steam", duration_sec: 900 }),
  ]);
  check("new records are accepted", first.ok && first.accepted === 2, JSON.stringify(first));

  const replay = ingest.ingestBatch(DEVICE_ID, "usage", [
    usageRec({ session_key: "k1" }),
    usageRec({ session_key: "k2", process_name: "steam", app_name: "Steam" }),
  ]);
  check("a replayed batch is accepted but inserts nothing",
    replay.ok && replay.accepted === 0, JSON.stringify(replay));
  const total = db
    .prepare("SELECT COUNT(*) AS n FROM usage_sessions WHERE device_id = ?")
    .get(DEVICE_ID).n;
  check("replay did not duplicate rows", total === 2, `rows=${total}`);

  const wrongLedger = ingest.ingestBatch(DEVICE_ID, "usage", [webRec()]);
  check("a web record sent on the usage event is rejected, not coerced",
    wrongLedger.ok && wrongLedger.accepted === 0 && wrongLedger.rejected === 1,
    JSON.stringify(wrongLedger));

  const junk = ingest.ingestBatch(DEVICE_ID, "usage", [
    usageRec({ day: "not-a-day" }),
    usageRec({ duration_sec: 0 }),
    usageRec({ app_name: "" }),
    usageRec({ session_key: "" }),
  ]);
  check("malformed records are dropped without failing the batch",
    junk.ok && junk.accepted === 0 && junk.rejected === 4, JSON.stringify(junk));

  const huge = ingest.ingestBatch(DEVICE_ID, "usage", [
    usageRec({ session_key: "clock-jump", duration_sec: 99999999 }),
  ]);
  const clamped = db
    .prepare("SELECT duration_sec FROM usage_sessions WHERE session_key = 'clock-jump'")
    .get();
  check("a clock jump is clamped to one day",
    huge.accepted === 1 && clamped.duration_sec === 86400, JSON.stringify(clamped));

  const over = ingest.ingestBatch(DEVICE_ID, "usage",
    new Array(ingest.MAX_BATCH + 1).fill(0).map(() => usageRec()));
  check("an oversized batch is refused so the agent keeps it", over.ok === false,
    JSON.stringify(over));

  const noDevice = ingest.ingestBatch(null, "usage", [usageRec()]);
  check("an unknown device is refused explicitly", noDevice.ok === false,
    JSON.stringify(noDevice));

  const browserTitle = ingest.ingestBatch(DEVICE_ID, "usage", [
    usageRec({ session_key: "br1", app_name: "Chromium", process_name: "chromium",
      is_browser: 1, window_title: "Instagram — private stuff" }),
  ]);
  const stored = db
    .prepare("SELECT window_title, app_category FROM usage_sessions WHERE session_key = 'br1'")
    .get();
  check("a browser window title is never stored in the app ledger",
    browserTitle.accepted === 1 && stored.window_title === null
      && stored.app_category === "browser", JSON.stringify(stored));

  const q = ingest.ingestBatch(DEVICE_ID, "web", [
    webRec({ visit_key: "q1", url_path: "/search?q=secret+term#frag" }),
  ]);
  const path0 = db.prepare("SELECT url_path FROM web_visits WHERE visit_key = 'q1'").get();
  check("a query string is stripped at the second gate",
    q.accepted === 1 && path0.url_path === "/search", JSON.stringify(path0));
}

// ─── The scenario the whole feature rests on ────────────────────────────────
// One day, all three sources active, including the trap: 2 hours of browser app
// time that describes the SAME seconds as the web and YouTube rows.

function seedScenario(deviceId, day) {
  ingest.ingestBatch(deviceId, "usage", [
    { ...usageRec(), session_key: `${day}-code`, day, duration_sec: 3600,
      started_at: iso(day, 9, 0), ended_at: iso(day, 10, 0) },
    { ...usageRec(), session_key: `${day}-browser`, day, app_name: "Chromium",
      process_name: "chromium", is_browser: 1, window_title: "whatever",
      duration_sec: 7200, started_at: iso(day, 10, 0), ended_at: iso(day, 12, 0) },
    { ...usageRec(), session_key: `${day}-game`, day, app_name: "Steam",
      process_name: "steam", duration_sec: 1800,
      started_at: iso(day, 14, 0), ended_at: iso(day, 14, 30) },
  ]);
  ingest.ingestBatch(deviceId, "web", [
    { ...webRec(), visit_key: `${day}-khan`, day, dwell_sec: 1800 },
    { ...webRec(), visit_key: `${day}-insta`, day, domain: "instagram.com",
      url_path: "/", title: "Instagram", dwell_sec: 900 },
    { ...webRec(), visit_key: `${day}-lec`, day, domain: "youtube.com",
      url_path: "/watch", title: "Integration by parts | Calculus lecture 4",
      platform: "youtube", media_id: "LEC0000001", media_kind: "video",
      dwell_sec: 1200 },
    { ...webRec(), visit_key: `${day}-short`, day, domain: "youtube.com",
      url_path: "/shorts/x", title: "physics revision one shot",
      platform: "youtube", media_id: "SHORT00001", media_kind: "short",
      dwell_sec: 600 },
  ]);
  rollup.rollupDay(deviceId, day);
}

function testLedgerSeparation() {
  console.log("\nLedger separation — the same seconds are never counted twice");
  seedScenario(PROG_ID, PREV);
  const ledger = rollup.ledgerForDay(PROG_ID, PREV);
  const score = rollup.scoreForDay(PROG_ID, PREV);

  const cat = (rows, name) => rows.find((r) => r.category === name);
  check("app ledger holds the coding time", cat(ledger.app, "coding")?.seconds === 3600,
    JSON.stringify(ledger.app));
  check("browser time is present but flagged uncounted",
    cat(ledger.app, "browser")?.seconds === 7200 && cat(ledger.app, "browser")?.counted === false,
    JSON.stringify(cat(ledger.app, "browser")));
  check("browser time is reported separately as the envelope",
    score.browser_envelope_sec === 7200, `got ${score.browser_envelope_sec}`);
  check("browser time is NOT in the counted total",
    score.counted_sec === 9900, `got ${score.counted_sec}`);

  check("web ledger holds only real web rows",
    cat(ledger.web, "academic")?.seconds === 1800 && cat(ledger.web, "social")?.seconds === 900
      && ledger.web.length === 2, JSON.stringify(ledger.web));
  check("no YouTube row leaked into the web ledger",
    !ledger.web.some((r) => r.category === "video_other"), JSON.stringify(ledger.web));
  check("watch ledger is scored on its own taxonomy",
    cat(ledger.watch, "lecture")?.seconds === 1200 && cat(ledger.watch, "shorts")?.seconds === 600,
    JSON.stringify(ledger.watch));
  check("a Short is graded as a distraction",
    cat(ledger.watch, "shorts")?.weight === -2, JSON.stringify(cat(ledger.watch, "shorts")));

  const summed = [...ledger.app, ...ledger.web, ...ledger.watch]
    .filter((r) => r.counted)
    .reduce((a, r) => a + r.seconds, 0);
  check("counted_sec equals the sum of the ledger rows the UI shows as counted",
    summed === score.counted_sec, `ledger=${summed} score=${score.counted_sec}`);

  rollup.rollupDay(PROG_ID, PREV);
  const again = rollup.scoreForDay(PROG_ID, PREV);
  const rows = db
    .prepare("SELECT COUNT(*) AS n FROM progress_daily WHERE device_id = ? AND day = ?")
    .get(PROG_ID, PREV).n;
  check("a second rollup changes nothing and adds no rows",
    again.score === score.score && rows === 7, `score=${again.score} rows=${rows}`);
}

// ─── Grading ────────────────────────────────────────────────────────────────

function testGrading() {
  console.log("\nGrading — the arithmetic a parent can redo by hand");
  const s = rollup.scoreForDay(PROG_ID, PREV);
  // academic 6600, passive 900, distraction 2400, counted 9900, target 3600
  check("academic seconds", s.academic_sec === 6600, `got ${s.academic_sec}`);
  check("passive seconds", s.passive_sec === 900, `got ${s.passive_sec}`);
  check("distraction seconds", s.distraction_sec === 2400, `got ${s.distraction_sec}`);
  check("effort = academic + half of constructive", s.effort_sec === 6600, `got ${s.effort_sec}`);
  check("attainment caps at 100%", s.attainment_pct === 100, `got ${s.attainment_pct}`);
  check("focus = effort / counted", Math.abs(s.focus_pct - 66.67) < 0.02, `got ${s.focus_pct}`);
  check("penalty is proportional and capped", Math.abs(s.penalty - 3.64) < 0.02, `got ${s.penalty}`);
  check("score", s.score === 83, `got ${s.score}`);
  check("grade", s.grade === "A", `got ${s.grade}`);

  // Pure rubric checks, no database involved.
  const L = (source, category, seconds, weight, counted = 1) =>
    ({ source, category, seconds, weight, countsTowardTotal: counted });

  const perfectShort = rollup.scoreLedger([L("app", "coding", 1200, 2)], 3600);
  check("20 minutes of pure study cannot score 100",
    perfectShort.score < 100 && perfectShort.score > 50, `got ${perfectShort.score}`);
  const onTarget = rollup.scoreLedger([L("app", "coding", 3600, 2)], 3600);
  check("hitting the target with nothing else is 100", onTarget.score === 100,
    `got ${onTarget.score}`);
  const idle = rollup.scoreLedger([L("app", "utility", 120, 0)], 3600);
  check("a nearly-unused day is N/A, not F", idle.grade === "N/A" && idle.has_data === 0,
    `got ${idle.grade}`);
  const allGames = rollup.scoreLedger([L("app", "gaming", 7200, -2)], 3600);
  check("a day of nothing but games fails", allGames.grade === "F" && allGames.score === 0,
    `got ${allGames.grade}/${allGames.score}`);
  const envelopeOnly = rollup.scoreLedger([L("app", "browser", 7200, 0, 0)], 3600);
  check("browser time alone is still no data",
    envelopeOnly.counted_sec === 0 && envelopeOnly.browser_envelope_sec === 7200
      && envelopeOnly.grade === "N/A", JSON.stringify(envelopeOnly));
  const penaltyCap = rollup.scoreLedger(
    [L("app", "coding", 3600, 2), L("watch", "shorts", 36000, -2)], 3600);
  check("the distraction penalty never exceeds 15 points", penaltyCap.penalty <= 15,
    `got ${penaltyCap.penalty}`);
  const constructive = rollup.scoreLedger([L("app", "writing_docs", 3600, 1)], 3600);
  check("constructive time counts half towards effort", constructive.effort_sec === 1800,
    `got ${constructive.effort_sec}`);

  console.log("\nTrend and summary");
  seedScenario(PROG_ID, DAY);
  const t = rollup.trend(PROG_ID, 14);
  check("trend returns one point per day with no gaps", t.length === 14, `got ${t.length}`);
  check("trend is oldest first", t[0].day < t[t.length - 1].day, `${t[0].day}..${t[13].day}`);
  const withData = t.filter((p) => p.has_data === 1);
  check("both seeded days appear in the trend", withData.length === 2,
    `got ${withData.length}`);
  const sum = rollup.summarize(PROG_ID, 7);
  check("weekly grade averages only the days with data",
    sum.active_days === 2 && sum.grade === "A", JSON.stringify({ d: sum.active_days, g: sum.grade }));
  check("summary keeps the three sources apart",
    sum.by_source.app.every((c) => c.source === "app")
      && sum.by_source.watch.every((c) => c.source === "watch")
      && !sum.by_source.web.some((c) => c.category === "video_other"),
    JSON.stringify(sum.by_source));
  const leaked = JSON.stringify(sum);
  check("nothing in the AI-facing summary contains a URL, path or page title",
    !leaked.includes("khanacademy") && !leaked.includes("Integration")
      && !leaked.includes("/math/algebra"), "summary carried raw browsing detail");
}

// ─── Parent overrides ───────────────────────────────────────────────────────
// An override that only affects tomorrow's data looks broken to a parent, so the
// test that matters is the retroactive one — and its mirror image, that removing
// the override puts the history back exactly as it was.

function addRule(source, matchValue, category, weight) {
  db.prepare(
    `INSERT INTO activity_categories
       (source, match_type, match_value, category, academic_weight, origin, updated_at)
     VALUES (?, 'contains', ?, ?, ?, 'parent', CURRENT_TIMESTAMP)
     ON CONFLICT(source, match_type, match_value) DO UPDATE SET
       category = excluded.category, academic_weight = excluded.academic_weight`
  ).run(source, matchValue, category, weight);
  categorize.invalidateRuleCache();
}

function dropRule(matchValue) {
  db.prepare("DELETE FROM activity_categories WHERE match_value = ?").run(matchValue);
  categorize.invalidateRuleCache();
}

function testOverrides() {
  console.log("\nOverrides — retroactive, reversible, and still ledger-safe");
  const before = rollup.scoreForDay(PROG_ID, PREV);

  addRule("app", "steam", "study_tools", 2);
  const relabelled = categorize.classifyApp({
    process_name: "steam", app_name: "Steam", is_browser: 0,
  });
  check("an override wins over the built-in rule",
    relabelled.category === "study_tools" && relabelled.weight === 2,
    JSON.stringify(relabelled));
  const webSide = categorize.classifyWeb({ domain: "steampowered.com", platform: "web" });
  check("an app override does not reach into the web taxonomy",
    webSide.category !== "study_tools", JSON.stringify(webSide));

  const res = rollup.reclassifyAndRoll(PROG_ID, PREV, PREV);
  const led = rollup.ledgerForDay(PROG_ID, PREV);
  const after = rollup.scoreForDay(PROG_ID, PREV);
  check("reclassify rewrote stored rows and re-rolled the day",
    res.rows === 7 && res.rolled === 1, JSON.stringify(res));
  check("the re-labelled app left the gaming row entirely",
    !led.app.some((r) => r.category === "gaming")
      && led.app.some((r) => r.category === "study_tools" && r.seconds === 1800),
    JSON.stringify(led.app));
  check("history is re-graded, not just re-labelled",
    after.distraction_sec === 600 && after.academic_sec === 8400 && after.score === 93,
    JSON.stringify({ d: after.distraction_sec, a: after.academic_sec, s: after.score }));
  check("re-labelling moved time between bands without inventing any",
    after.counted_sec === before.counted_sec, `${after.counted_sec} vs ${before.counted_sec}`);

  // A parent may well try to label their browser as schoolwork. If that worked,
  // the browser envelope would start counting and every web second would be
  // counted twice — the exact bug this feature is built to avoid.
  addRule("app", "chromium", "study_tools", 2);
  const stillEnvelope = categorize.classifyApp({
    process_name: "chromium", app_name: "Chromium", is_browser: 1,
  });
  check("no override can turn a browser into counted app time",
    stillEnvelope.category === "browser" && stillEnvelope.weight === 0,
    JSON.stringify(stillEnvelope));
  rollup.reclassifyAndRoll(PROG_ID, PREV, PREV);
  const envScore = rollup.scoreForDay(PROG_ID, PREV);
  check("the browser envelope stays out of the total after a reclassify",
    envScore.browser_envelope_sec === 7200 && envScore.counted_sec === 9900,
    JSON.stringify({ e: envScore.browser_envelope_sec, c: envScore.counted_sec }));
  dropRule("chromium");

  dropRule("steam");
  rollup.reclassifyAndRoll(PROG_ID, PREV, PREV);
  const restored = rollup.scoreForDay(PROG_ID, PREV);
  check("deleting the override puts the graded history back exactly as it was",
    restored.score === before.score
      && restored.academic_sec === before.academic_sec
      && restored.distraction_sec === before.distraction_sec,
    JSON.stringify({ s: restored.score, a: restored.academic_sec, d: restored.distraction_sec }));
}

// ─── HTTP surface ───────────────────────────────────────────────────────────
// The router is mounted on a throwaway express app on an ephemeral port. This is
// the only way to test the parts that live in the route layer rather than in a
// service: the source/category cross-check, the day clamps, and the fact that
// the response *shape* keeps the three sources apart.

async function testRoutes() {
  console.log("\nHTTP API — shape, clamps and the cross-source guard");

  const src = fs.readFileSync(path.join(ROOT, "routes", "progress.js"), "utf8");
  const authAt = src.indexOf("router.use(authMiddleware)");
  const firstRoute = src.search(/router\.(get|post|put|delete)\(/);
  check("no progress route is registered above the auth middleware",
    authAt > 0 && firstRoute > authAt, `auth@${authAt} route@${firstRoute}`);

  // The progress router enforces authMiddleware at the router level, so a
  // request without a Bearer token gets a 401 — including from this suite.
  // Sign a real token with the same effective secret routes/auth.js resolves
  // (JWT_SECRET, or the dev fallback when not in production) and carry it on
  // every call, the way the actual dashboard does after login. No bypass:
  // tests must still travel the same auth path a browser does.
  const jwt = require("jsonwebtoken");
  const effectiveSecret = process.env.JWT_SECRET
    || (process.env.NODE_ENV === "production" ? null : "guardian-shield-dev-only-secret");
  const token = jwt.sign(
    { id: 1, username: "suite", role: "admin" },
    effectiveSecret,
    { expiresIn: "24h" }
  );

  const express = require("express");
  const app = express();
  app.use(express.json());
  app.use("/api/progress", require(path.join(ROOT, "routes", "progress")));
  const server = app.listen(0);
  await new Promise((resolve) => server.once("listening", resolve));
  const base = `http://127.0.0.1:${server.address().port}/api/progress`;

  const call = async (method, url, body) => {
    const r = await fetch(base + url, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return { status: r.status, body: await r.json().catch(() => null) };
  };
  const get = (url) => call("GET", url);

  try {
    const rubric = await get("/rubric");
    check("the rubric is served so the UI cannot hold a second copy of it",
      rubric.status === 200 && rubric.body.weights.academic === 2
        && rubric.body.grades[0].letter === "A+"
        && Object.keys(rubric.body.categories).join(",") === "app,web,watch",
      JSON.stringify(rubric.body?.weights));
    check("the rubric says out loud that it grades activity, not the child",
      rubric.body.notes.some((n) => /not a grade of the child/i.test(n)),
      JSON.stringify(rubric.body?.notes));

    const day = await get(`/${PROG_ID}/day?date=${PREV}`);
    check("the day route returns the score a parent can check by hand",
      day.status === 200 && day.body.score.score === 83 && day.body.score.grade === "A",
      JSON.stringify(day.body?.score));
    check("the three ledgers arrive as three separate objects",
      Object.keys(day.body.ledger).join(",") === "app,web,watch",
      Object.keys(day.body?.ledger || {}).join(","));
    const allRows = [...day.body.ledger.app, ...day.body.ledger.web, ...day.body.ledger.watch];
    check("no ledger row carries a source field the front end could group on",
      allRows.length === 7 && allRows.every((r) => !("source" in r)),
      `rows=${allRows.length}`);
    check("the browser row is delivered pre-flagged as uncounted",
      day.body.ledger.app.find((r) => r.category === "browser")?.counted === false,
      JSON.stringify(day.body.ledger.app));

    const today = await get(`/${PROG_ID}/day`);
    check("no date means today, not an error", today.status === 200
      && today.body.day === rollup.dayString(new Date()), JSON.stringify(today.body?.day));
    const junkDate = await get(`/${PROG_ID}/day?date=yesterday-ish`);
    check("an unparseable date falls back to today instead of 400",
      junkDate.status === 200, `got ${junkDate.status}`);
    check("a non-numeric device id is a 400", (await get("/abc/day")).status === 400);
    check("an unknown device id is a 404", (await get("/999999/day")).status === 404);

    const trend = await get(`/${PROG_ID}/trend?days=999`);
    check("the trend window is clamped, not honoured verbatim",
      trend.body.days === 120 && trend.body.points.length === 120,
      `days=${trend.body?.days} points=${trend.body?.points?.length}`);
    const summary = await get(`/${PROG_ID}/summary?days=7`);
    check("the weekly summary grades only the days with data",
      summary.body.active_days === 2 && summary.body.grade === "A",
      JSON.stringify({ d: summary.body?.active_days, g: summary.body?.grade }));

    const detail = await get(`/${PROG_ID}/detail?days=7`);
    check("the app list is real apps only — the browser is not one of them",
      detail.body.app.some((a) => /thonny/i.test(a.label))
        && !detail.body.app.some((a) => /chrom/i.test(a.label)),
      JSON.stringify(detail.body?.app?.map((a) => a.label)));
    check("the site list holds no YouTube row",
      !detail.body.web.some((w) => /youtube/i.test(w.label))
        && detail.body.web.some((w) => w.label === "khanacademy.org"),
      JSON.stringify(detail.body?.web?.map((w) => w.label)));
    check("the watch list groups by video and keeps the watch category",
      detail.body.watch.some((v) => v.category === "lecture" && v.seconds === 2400),
      JSON.stringify(detail.body?.watch));
    check("the browser envelope is reported under its own key",
      detail.body.browser_envelope.length === 1
        && detail.body.browser_envelope[0].seconds === 14400,
      JSON.stringify(detail.body?.browser_envelope));

    const badCat = await call("POST", "/rules", { source: "app",
      match_type: "contains", match_value: "khanacademy", category: "academic" });
    check("a web category filed against an app rule is refused",
      badCat.status === 400 && /not a app category/.test(badCat.body.error),
      JSON.stringify(badCat.body));
    const tooShort = await call("POST", "/rules", { source: "app",
      match_type: "contains", match_value: "io", category: "coding" });
    check("a pattern too short to match safely is refused", tooShort.status === 400,
      JSON.stringify(tooShort.body));
    const badSource = await call("POST", "/rules", { source: "youtube",
      match_type: "contains", match_value: "olympiad", category: "exam_prep" });
    check("an invented source is refused", badSource.status === 400,
      JSON.stringify(badSource.body));
    const badMatch = await call("POST", "/rules", { source: "watch",
      match_type: "regex", match_value: "olympiad", category: "exam_prep" });
    check("an unsupported match type is refused", badMatch.status === 400,
      JSON.stringify(badMatch.body));

    const made = await call("POST", "/rules", { source: "watch",
      match_type: "contains", match_value: "olympiad", category: "exam_prep",
      academic_weight: 99 });
    check("the weight comes from the category, never from the request body",
      made.status === 200 && made.body.academic_weight === 2,
      JSON.stringify(made.body));
    const listed = await get("/rules");
    const mine = listed.body.rules.find((r) => r.match_value === "olympiad");
    check("the rule is listed back with its origin recorded",
      mine && mine.origin === "parent", JSON.stringify(mine));
    const gone = await call("DELETE", `/rules/${mine.id}`);
    check("deleting a rule reports what it removed", gone.body.deleted === 1,
      JSON.stringify(gone.body));

    check("an impossible target is refused",
      (await call("PUT", `/${PROG_ID}/goals`, { daily_academic_min: 2 })).status === 400);
    const harder = await call("PUT", `/${PROG_ID}/goals`, { daily_academic_min: 120 });
    check("changing the target re-scores every stored day at once",
      harder.body.rescored === 2, JSON.stringify(harder.body));
    const regraded = await get(`/${PROG_ID}/day?date=${PREV}`);
    check("the same activity against a harder target grades lower",
      regraded.body.score.target_sec === 7200 && regraded.body.score.score === 78
        && regraded.body.score.grade === "B", JSON.stringify(regraded.body?.score));
    await call("PUT", `/${PROG_ID}/goals`, { daily_academic_min: 60 });
    const backAgain = await get(`/${PROG_ID}/day?date=${PREV}`);
    check("restoring the target restores the grade",
      backAgain.body.score.score === 83, JSON.stringify(backAgain.body?.score));

    const rebuilt = await call("POST", `/${PROG_ID}/rebuild`, { days: 7 });
    check("rebuild reclassifies every raw row in the window and re-rolls it",
      rebuilt.body.success === true && rebuilt.body.rows === 14 && rebuilt.body.rolled === 2,
      JSON.stringify(rebuilt.body));

    // Erasure gets its own device: a parent's "delete this" has to actually
    // delete, and proving that on the scenario device would destroy the rest of
    // the suite.
    seedScenario(SCRATCH_ID, PREV);
    seedScenario(SCRATCH_ID, DAY);
    const unconfirmed = await call("DELETE", `/${SCRATCH_ID}/history`, {});
    check("a stray fetch cannot erase a child's history",
      unconfirmed.status === 400, JSON.stringify(unconfirmed.body));
    const cutoff = await call("DELETE", `/${SCRATCH_ID}/history`,
      { confirm: "DELETE", before: DAY });
    check("a retention cutoff erases only the days before it",
      cutoff.body.deleted.usage_sessions === 3 && cutoff.body.deleted.web_visits === 4
        && cutoff.body.deleted.progress_daily === 7,
      JSON.stringify(cutoff.body?.deleted));
    check("the day after the cutoff survived",
      (await get(`/${SCRATCH_ID}/day?date=${DAY}`)).body.score.score === 83,
      "cutoff took the wrong side of the boundary");
    const wiped = await call("DELETE", `/${SCRATCH_ID}/history`, { confirm: "DELETE" });
    check("a full erase clears every progress table for that child",
      wiped.body.deleted.usage_sessions === 3 && wiped.body.deleted.progress_scores === 1
        && "ai_insights" in wiped.body.deleted, JSON.stringify(wiped.body?.deleted));
    const emptied = await get(`/${SCRATCH_ID}/day?date=${DAY}`);
    check("an erased child reads as no data, not as a zero grade",
      emptied.body.score.grade === "N/A" && emptied.body.ledger.app.length === 0,
      JSON.stringify(emptied.body?.score));
    check("erasing one child left the other untouched",
      (await get(`/${PROG_ID}/day?date=${DAY}`)).body.score.score === 83,
      "a device-scoped delete crossed devices");
  } finally {
    server.close();
  }
}


async function main() {
  console.log("=".repeat(66));
  console.log("GUARDIAN academic-progress server — offline test suite");
  console.log("=".repeat(66));
  setupDevice();
  testClassifier();
  testIngest();
  testLedgerSeparation();
  testGrading();
  testOverrides();
  await testRoutes();
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
