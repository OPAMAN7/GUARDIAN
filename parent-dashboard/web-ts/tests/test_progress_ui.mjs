/**
 * Guardian Shield — Progress page UI checks
 * =========================================
 *
 * Runs the REAL render functions out of main.js against a minimal DOM and a
 * canned API payload. No browser, no server, no network:
 *
 *     node parent-dashboard/web-ts/tests/test_progress_ui.mjs
 *
 * The functions are extracted from main.js at run time rather than copied here.
 * A copy would drift, and a UI test that passes against last week's markup is
 * worse than no test at all.
 *
 * What this suite is really guarding: the page must never blend the three
 * sources. The server keeps them in three separate arrays, and this is the last
 * place that could add them together — so the cross-source total is asserted
 * *absent* from the rendered HTML.
 */

import fs from "node:fs";
import path from "node:path";
import url from "node:url";

const HERE = path.dirname(url.fileURLToPath(import.meta.url));
const MAIN = path.join(HERE, "..", "main.js");
const SRC = fs.readFileSync(MAIN, "utf8").split("\n");

let PASS = 0;
let FAIL = 0;
function check(label, cond, detail = "") {
  if (cond) {
    PASS += 1;
    console.log(`[ok]   ${label}`);
  } else {
    FAIL += 1;
    console.log(`[FAIL] ${label}${detail ? ` — ${detail}` : ""}`);
  }
}
const has = (hay, needle) => String(hay).includes(needle);

// ─── Extract the section under test ─────────────────────────────────────────

function grabFunction(name) {
  const start = SRC.findIndex((l) => l.startsWith(`function ${name}(`));
  if (start < 0) throw new Error(`function ${name}() not found in main.js`);
  const out = [];
  let depth = 0;
  for (const line of SRC.slice(start)) {
    out.push(line);
    depth += (line.match(/\{/g) || []).length - (line.match(/\}/g) || []).length;
    if (depth === 0 && out.length > 1 && line.startsWith("}")) break;
  }
  return out.join("\n");
}

/** The whole render section, lifted verbatim between its two landmark lines. */
function grabSection() {
  const start = SRC.findIndex((l) => l.includes("Academic Progress rendering"));
  const end = SRC.findIndex((l) => l.startsWith("function renderRulesPage("));
  if (start < 0 || end < 0) throw new Error("progress render section not found");
  return SRC.slice(start, end).join("\n");
}

const SECTION = grabSection();

const EXPORTS = [
  "renderProgressPage", "renderProgressToolbar", "renderProgressGrade",
  "renderProgressBands", "renderProgressTrend", "renderProgressSources",
  "renderProgressEnvelope", "renderProgressAi", "renderProgressGoals",
  "renderProgressRules", "progLedgerTable", "progTopTable",
  "fmtDur", "fmtPct", "progBand", "progCatLabel", "progGradeKey",
];

const CONTAINERS = [
  "progress-toolbar", "progress-banner", "progress-grade-card",
  "progress-bands-card", "progress-trend-card", "progress-sources",
  "progress-envelope-card", "progress-ai-card", "progress-goals-card",
  "progress-rules-card",
];

// ─── A DOM small enough to read in one sitting ───────────────────────────────
// Ten containers with an innerHTML each. That is all the render functions touch:
// they never query, never listen, never measure. Which is itself the reason this
// test can exist without a browser.

function mount(state) {
  const nodes = {};
  for (const id of CONTAINERS) nodes[id] = { id, innerHTML: "" };
  const build = new Function(
    "env",
    `"use strict";
     const { state, document, loadProgress } = env;
     ${SECTION}
     ${grabFunction("esc")}
     ${grabFunction("progressDayString")}
     ${grabFunction("progressDeviceId")}
     return { ${EXPORTS.join(", ")} };`
  );
  const api = build({
    state,
    document: { getElementById: (id) => nodes[id] || null },
    loadProgress: () => {},
  });
  return { api, html: (id) => nodes[id].innerHTML };
}

// ─── Fixture: the worked example the server tests already pin ────────────────
// Same numbers as test_rollup's canonical day, so a disagreement between the two
// suites means the UI and the scorer have genuinely drifted apart:
//
//   coding 3600 (+2) · browser 7200 (envelope) · steam 1800 (−2)
//   khanacademy 1800 (+2) · instagram 900 (−1)
//   YT lecture 1200 (+2) · YT short 600 (−2)
//
//   academic 6600 · passive 900 · distraction 2400 · counted 9900
//   envelope 7200 · effort 6600 · attainment 100% · focus 66.67%
//   penalty 3.64 → score 83 → A
//
// The three per-panel subtotals are 1h 30m / 45m / 30m. The cross-source totals
// (2h 45m counted, 3h 30m app-plus-envelope, 4h 45m everything) must never be
// rendered inside a source panel — that is the assertion this file exists for.

const pad = (n) => String(n).padStart(2, "0");
const dayStr = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const TODAY = dayStr(new Date());
const shift = (n) => {
  const d = new Date(`${TODAY}T12:00:00`);
  d.setDate(d.getDate() + n);
  return dayStr(d);
};

const SCORE = {
  academic_sec: 6600, constructive_sec: 0, neutral_sec: 0,
  passive_sec: 900, distraction_sec: 2400,
  counted_sec: 9900, browser_envelope_sec: 7200,
  effort_sec: 6600, target_sec: 3600,
  focus_pct: 66.67, attainment_pct: 100, penalty: 3.64,
  score: 83, grade: "A", has_data: 1,
};

// Rows exactly as rollup.ledgerForDay() shapes them, envelope first because it
// orders by seconds DESC — so the envelope really is the biggest row in the app
// panel, which is the situation the "never counted" styling has to survive.
const LEDGER = {
  app: [
    { category: "browser", seconds: 7200, events: 4, weight: 0, band: "neutral", counted: false },
    { category: "coding", seconds: 3600, events: 3, weight: 2, band: "academic", counted: true },
    { category: "gaming", seconds: 1800, events: 2, weight: -2, band: "distraction", counted: true },
  ],
  web: [
    { category: "education", seconds: 1800, events: 6, weight: 2, band: "academic", counted: true },
    { category: "social", seconds: 900, events: 5, weight: -1, band: "passive", counted: true },
  ],
  watch: [
    { category: "lecture", seconds: 1200, events: 2, weight: 2, band: "academic", counted: true },
    { category: "shorts", seconds: 600, events: 9, weight: -2, band: "distraction", counted: true },
  ],
};

// One label carries markup and a quote. A dashboard that shows a child's app
// names is a dashboard where the child chooses part of the HTML.
const HOSTILE = 'Thonny <b>"IDE"</b>';

const DETAIL = {
  app: [
    { label: HOSTILE, sub: "thonny", category: "coding", weight: 2, seconds: 3600, events: 3 },
    { label: "Steam", sub: "steam", category: "gaming", weight: -2, seconds: 1800, events: 2 },
  ],
  web: [
    { label: "khanacademy.org", sub: "chromium", category: "education", weight: 2, seconds: 1800, events: 6 },
    { label: "instagram.com", sub: "chromium", category: "social", weight: -1, seconds: 900, events: 5 },
  ],
  watch: [
    { label: "Algebra lecture 4", sub: "video", category: "lecture", weight: 2, seconds: 1200, events: 2 },
    { label: "#shorts prank", sub: "short", category: "shorts", weight: -2, seconds: 600, events: 9 },
  ],
  browser_envelope: [{ label: "Chromium", seconds: 7200, events: 4 }],
};

// Fourteen days ending today: today is the graded one, three days back has no
// data at all, and that day must read N/A rather than nought-out-of-a-hundred.
const TREND = Array.from({ length: 14 }, (_, i) => {
  const day = shift(i - 13);
  if (day === TODAY) return { day, score: 83, grade: "A", has_data: 1, academic_sec: 6600 };
  if (day === shift(-3)) return { day, score: 0, grade: "N/A", has_data: 0, academic_sec: 0 };
  return { day, score: 71, grade: "B", has_data: 1, academic_sec: 4800 };
});

const SUMMARY = {
  period: { from: shift(-6), to: TODAY, days: 7 },
  active_days: 5, grade: "B", score: 74, streak: 3,
  averages: {
    academic_sec: 5400, constructive_sec: 600, neutral_sec: 0, passive_sec: 900,
    distraction_sec: 1800, counted_sec: 8700, effort_sec: 5700, target_sec: 3600,
  },
  by_source: {
    // A window is wider than a day, so it legitimately carries categories the
    // selected day does not. The AI card only ever reports these as counts.
    app: [
      ...LEDGER.app.filter((r) => r.counted),
      { category: "study_tools", seconds: 2400, events: 4, weight: 2 },
    ],
    web: LEDGER.web,
    watch: LEDGER.watch,
  },
  daily: TREND.map((t) => ({ day: t.day, score: t.score, grade: t.grade, has_data: t.has_data })),
};

// Three taxonomies with nothing in common. The overlap being empty is what makes
// "the app dropdown must not offer a web category" a testable claim.
const RULES = {
  rules: [{
    id: 7, source: "app", match_type: "contains", match_value: "thonny",
    category: "coding", academic_weight: 2, origin: "parent",
    updated_at: "2026-09-04 10:00:00",
  }],
  match_types: ["exact", "domain", "contains"],
  categories: {
    app: { coding: 2, study_tools: 2, gaming: -2 },
    web: { education: 2, social: -1 },
    watch: { lecture: 2, shorts: -2 },
  },
};

function baseState(over = {}) {
  return {
    currentPage: "progress",
    selectedDeviceId: 1,
    devices: [{ id: 1, device_name: 'Aman "Pi" <lab>', status: "online" }],
    socket: null,
    progress: {
      deviceId: 1,
      date: TODAY,
      rubric: null,
      day: { device_id: 1, device_name: "pi", day: TODAY, score: SCORE, ledger: LEDGER },
      trend: TREND,
      summary: SUMMARY,
      detail: { device_id: 1, period: SUMMARY.period, ...DETAIL },
      goals: { daily_academic_min: 60, weekly_target_days: 5, target_sec: 3600 },
      rules: RULES,
      windowDays: 7,
      ruleSource: "app",
      aiStatus: null,
      insight: null,
      aiResult: null,
      aiBusy: false,
      loading: false,
      error: null,
      ...over,
    },
  };
}

// ─── 1. The full page renders, and the grade shows its working ───────────────

const full = mount(baseState());
full.api.renderProgressPage();

const grade = full.html("progress-grade-card");
check("grade card renders the letter with its own colour class",
  has(grade, 'class="prog-grade grade-a"'), grade.slice(0, 120));
check("grade card shows the score out of 100", has(grade, "83 / 100"));
check("grade card prints academic time", has(grade, "1h 50m"));
check("grade card prints counted time", has(grade, "2h 45m"));
check("grade card prints attainment", has(grade, "100%"));
check("grade card prints focus", has(grade, "67%"));
check("grade card prints the penalty as a deduction", has(grade, "−3.64 pts"));
check("grade card names the formula", has(grade, "0.6 × attainment"));
check("grade card refuses to grade the child",
  has(grade, "It is not a grade of the child"));
check("grade card shows the window grade from the summary", has(grade, "B · 74/100"));
check("grade card shows active days out of the window", has(grade, "5 of 7"));
check("grade card pluralises the streak", has(grade, "3 days"));
check("grade card labels the feature as an activity grade",
  has(grade, "Academic Activity Grade"));

// ─── 2. Bands: one bar, five rows, and the envelope disclaimed ────────────────

const bands = full.html("progress-bands-card");
check("stack segment for academic is 66.67% wide", has(bands, "width:66.67%"));
check("stack segment for passive is 9.09% wide", has(bands, "width:9.09%"));
check("stack segment for distraction is 24.24% wide", has(bands, "width:24.24%"));
check("bands with no time get no segment", !has(bands, "width:0.00%"));
check("band table lists all five weights",
  ["<td>+2</td>", "<td>+1</td>", "<td>0</td>", "<td>−1</td>", "<td>−2</td>"]
    .every((w) => has(bands, w)));
check("band table shows the constructive row even at zero",
  has(bands, "band-constructive"));
check("envelope is named in prose with its own duration", has(bands, "<strong>2h</strong>"));
check("envelope prose explains the double-count",
  has(bands, "charge the same minute twice"));

// ─── 3. Trend: fourteen bars, and an empty day that is not a failure ─────────

const trend = full.html("progress-trend-card");
const barCount = (trend.match(/data-progress-day="/g) || []).length;
check("trend draws one clickable column per day", barCount === 14, `got ${barCount}`);
check("today's bar is marked selected", has(trend, "prog-trend-col selected"));
check("today's bar height tracks the score", has(trend, "height:83%"));
check("a day without data gets the stub height", has(trend, "height:3%"));
check("a day without data is tinted N/A, not F",
  has(trend, "grade-na nodata") && !has(trend, "grade-f"));
check("trend explains the empty bars", has(trend, "rather than as a failing grade"));

// ─── 4. Sources: three panels that never add up to each other ───────────────
// The heart of the suite. Everything above is presentation; this is the invariant
// the feature was specified around.

const sources = full.html("progress-sources");
const panels = sources.split('class="card prog-panel"').slice(1);
check("exactly three source panels", panels.length === 3, `got ${panels.length}`);
check("each panel names the column it grades on",
  ["app_category", "web_category", "watch_category"].every((c) => has(sources, c)));
check("app panel subtotal counts 1h 30m", has(panels[0], "<strong>1h 30m</strong>"));
check("web panel subtotal counts 45m", has(panels[1], "<strong>45m</strong>"));
check("watch panel subtotal counts 30m", has(panels[2], "<strong>30m</strong>"));

// 9900s counted, 12600s app-plus-envelope, 17100s everything. None of these is a
// number any panel is entitled to know.
check("no panel prints the cross-source counted total", !has(sources, "2h 45m"));
check("no panel adds the envelope to its own subtotal", !has(sources, "3h 30m"));
check("no panel prints a grand total", !has(sources, "4h 45m"));
check("subtotals are labelled per-panel, not as totals",
  (sources.match(/Counted in this panel:/g) || []).length === 3);

check("envelope row is demoted in the app panel",
  has(panels[0], 'class="prog-row-envelope"'));
check("envelope row says so in words", has(panels[0], '<span class="prog-tag">not counted</span>'));
check("envelope row has no share of anything", has(panels[0], "<td>—</td>"));
check("only the app panel carries an envelope row",
  !has(panels[1], "prog-row-envelope") && !has(panels[2], "prog-row-envelope"));

// Each vocabulary stays inside its own panel. This is the visible half of the
// server's UNIQUE(source, ...) key: if a web category ever surfaced in the app
// panel, the ledger would have been written against the wrong source.
check("app panel shows only app categories",
  has(panels[0], "Coding") && !has(panels[0], "Education") && !has(panels[0], "Lecture"));
check("web panel shows only web categories",
  has(panels[1], "Education") && !has(panels[1], "Coding") && !has(panels[1], "Shorts"));
check("watch panel shows only watch categories",
  has(panels[2], "Lecture") && !has(panels[2], "Coding") && !has(panels[2], "Education"));
check("category keys are humanised, never shown raw",
  full.api.progCatLabel("study_tools") === "Study tools" && !has(sources, "study_tools"));

check("hostile app names are escaped", has(sources, "Thonny &lt;b&gt;&quot;IDE&quot;&lt;/b&gt;"));
check("no raw markup survives from a label", !has(sources, "<b>"));

// ─── 5. Envelope card ───────────────────────────────────────────────────────

const env = full.html("progress-envelope-card");
check("envelope card is titled as not counted", has(env, "shown, never counted"));
check("envelope card totals the window", has(env, "2h over 7 days"));
check("envelope card names the browser", has(env, "Chromium"));
check("envelope card tags its rows", has(env, "prog-row-envelope"));
check("envelope card offers itself as a cross-check",
  has(env, "the browser-history collector is missing pages"));

// ─── 6. AI card: the payload is aggregates, and it says so ──────────────────

const ai = full.html("progress-ai-card");
check("ai card states the privacy boundary", has(ai, "aggregates only"));
check("ai card enumerates what is never sent",
  has(ai, "No screenshot, no full URL and no search term is ever sent"));
check("ai card counts categories per source", has(ai, "3 app · 2 web · 2 watch"));
check("ai payload contains no site or video labels",
  !has(ai, "khanacademy.org") && !has(ai, "instagram.com") && !has(ai, "Algebra lecture"));

// The button is deliberately NOT gated on a provider key: with no key the server
// writes the same four sections from the same aggregates, so a disabled button
// would be hiding a working feature.
check("generate button is always available", has(ai, 'id="progress-ai-btn"') && !has(ai, "disabled"));
check("with no report yet the card says so", has(ai, "No report yet"));

// With a key configured the header states the model and the remaining budget —
// never the key, which must not exist anywhere in the browser bundle's data.
const keyed = mount(baseState({
  aiStatus: {
    provider: "google-gemini", model: "gemini-2.5-flash", configured: true, network: true,
    send_titles: false, daily_limit: 25, used_today: 3, remaining_today: 22, cached_reports: 1,
  },
  insight: {
    id: 7, period: { from: shift(-6), to: TODAY }, model: "gemini-2.5-flash",
    summary: "Academic time is meeting the 60-minute target on four of seven days.",
    strengths: ["Coding is the strongest app category."],
    focus_areas: ["Short-form video is 24% of counted time."],
    next_steps: ["Agree a daily cap for gaming rather than banning it."],
    created_at: "2026-09-04 09:15:00",
  },
}));
keyed.api.renderProgressAi();
const keyedAi = keyed.html("progress-ai-card");
check("configured card names the model and the budget",
  has(keyedAi, "gemini-2.5-flash") && has(keyedAi, "22/25 requests left today"));
check("stored report renders all four sections",
  has(keyedAi, "meeting the 60-minute target")
  && has(keyedAi, "Working well") && has(keyedAi, "Worth a look") && has(keyedAi, "What to do next"));
check("stored report says when it was written", has(keyedAi, "Saved "));
check("no API key material is ever rendered",
  !/AIza[0-9A-Za-z_-]/.test(keyedAi) && !has(keyedAi, "GEMINI_API_KEY") && !has(keyedAi, "x-goog-api-key"));

// A locally written report must say why it is plainer, or the parent will assume
// the AI wrote it and judge the feature on prose it never produced.
const localAi = mount(baseState({
  aiStatus: {
    provider: "google-gemini", model: "gemini-2.5-flash", configured: false, network: true,
    send_titles: false, daily_limit: 25, used_today: 0, remaining_today: 25, cached_reports: 0,
  },
  aiResult: {
    source: "local", cached: false, reason: "not_configured",
    report: { summary: "Over the last 7 days this device averaged 1h 50m of academic activity a day.",
      strengths: ["Coding is the strongest app category at 1h."], focus_areas: [], next_steps: [] },
  },
}));
localAi.api.renderProgressAi();
const localHtml = localAi.html("progress-ai-card");
check("unconfigured card admits there is no key", has(localHtml, "no provider key"));
check("local report explains why it is local",
  has(localHtml, "no AI provider key is configured on the server"));
check("local report still renders its summary", has(localHtml, "averaged 1h 50m"));
check("empty local sections are dropped, not rendered blank",
  has(localHtml, "Working well") && !has(localHtml, "Worth a look"));

// The opt-in label switch must be visible in the UI when it is on. A privacy
// setting the parent cannot see the state of is not a privacy setting.
const titled = mount(baseState({
  aiStatus: {
    provider: "google-gemini", model: "gemini-2.5-flash", configured: true, network: true,
    send_titles: true, daily_limit: 25, used_today: 0, remaining_today: 25, cached_reports: 0,
  },
}));
titled.api.renderProgressAi();
const titledHtml = titled.html("progress-ai-card");
check("titles-on is announced in the card", has(titledHtml, "AI_SEND_TITLES is on"));
check("titles-off promises no labels at all",
  has(ai, "Not even an app name or a domain leaves this machine")
  && !has(titledHtml, "Not even an app name"));

// Busy state: the button must not be clickable twice, or one impatient parent
// spends two requests on identical numbers.
const aiBusy = mount(baseState({ aiBusy: true }));
aiBusy.api.renderProgressAi();
check("the button disables itself while a report is being written",
  has(aiBusy.html("progress-ai-card"), "disabled") && has(aiBusy.html("progress-ai-card"), "Writing report…"));

// Structural: the card must not reach for the detail lists, which DO hold real
// domains and video titles. Reading them here is how a label would leak into a
// payload the card claims contains none.
check("the ai card never reads the labelled detail lists",
  !/renderProgressAi[\s\S]{0,2600}state\.progress\.detail/.test(SECTION));

// ─── 7. Goals and the erase escape hatch ────────────────────────────────────

const goals = full.html("progress-goals-card");
check("goal form carries the stored target", has(goals, 'id="progress-goal-min"') && has(goals, 'value="60"'));
check("goal form carries the target days", has(goals, 'value="5"'));
check("goal form warns that saving re-scores history", has(goals, "re-scores every stored day"));
check("history can be erased from the page", has(goals, 'id="progress-erase-btn"'));
check("erase warns it cannot be undone", has(goals, "cannot be undone"));

// ─── 8. Override editor: the dropdown cannot offer the wrong taxonomy ───────
// The server rejects a web category on an app rule with a 400. The UI is not
// allowed to let the parent get that far.

const rulesApp = full.html("progress-rules-card");
check("rule editor labels the active taxonomy", has(rulesApp, "Category (app)"));
check("app rule offers app categories",
  has(rulesApp, 'value="coding"') && has(rulesApp, 'value="study_tools"'));
check("app rule shows each category's weight", has(rulesApp, "Coding (+2)") && has(rulesApp, "Gaming (-2)"));
check("app rule cannot offer a web category", !has(rulesApp, 'value="education"'));
check("app rule cannot offer a watch category", !has(rulesApp, 'value="lecture"'));
check("rule editor hints a pattern for the source", has(rulesApp, 'placeholder="thonny"'));
check("existing override is listed with its origin", has(rulesApp, ">parent<"));
check("existing override can be deleted", has(rulesApp, 'data-progress-rule-del="7"'));
check("rule count is reported", has(rulesApp, "1 rule<"));
check("rule editor explains where the weight comes from",
  has(rulesApp, "The weight comes from the category"));

const webState = baseState({ ruleSource: "web" });
const webMount = mount(webState);
webMount.api.renderProgressRules();
const rulesWeb = webMount.html("progress-rules-card");
check("web rule offers web categories",
  has(rulesWeb, 'value="education"') && has(rulesWeb, 'value="social"'));
check("web rule cannot offer an app category", !has(rulesWeb, 'value="coding"'));
check("web rule cannot offer a watch category", !has(rulesWeb, 'value="shorts"'));
check("web rule hints a domain", has(rulesWeb, 'placeholder="example.com"'));

const badSource = mount(baseState({ ruleSource: "nonsense" }));
badSource.api.renderProgressRules();
check("an unknown source falls back to app rather than an empty dropdown",
  has(badSource.html("progress-rules-card"), 'value="coding"'));

// ─── 9. Toolbar ─────────────────────────────────────────────────────────────

const bar = full.html("progress-toolbar");
check("device name is escaped in the picker", has(bar, "Aman &quot;Pi&quot; &lt;lab&gt;"));
check("date input is capped at today", has(bar, `max="${TODAY}"`));
check("next-day is disabled on today",
  has(bar, 'data-progress-shift="1" title="Next day" disabled'));
check("window picker keeps the selected span", has(bar, '<option value="7" selected>'));

// ─── 10. A thin day is N/A, never F ─────────────────────────────────────────
// The payload here is deliberately inconsistent: has_data 0 but grade "F". The
// page must trust has_data, because "F" for a day the child barely used the
// machine is the single most damaging thing this feature could print.

const thin = mount(baseState({
  day: {
    day: TODAY,
    score: { ...SCORE, academic_sec: 120, counted_sec: 120, distraction_sec: 0,
      passive_sec: 0, effort_sec: 120, score: 4, grade: "F", has_data: 0 },
    ledger: { app: [], web: [], watch: [] },
  },
}));
thin.api.renderProgressGrade();
const thinHtml = thin.html("progress-grade-card");
check("a thin day renders N/A", has(thinHtml, "grade-na") && has(thinHtml, ">N/A<"));
check("a thin day never renders F", !has(thinHtml, "grade-f") && !has(thinHtml, ">F<"));
check("a thin day says why", has(thinHtml, "not enough data"));
check("a thin day explains the threshold", has(thinHtml, "under 5 minutes"));

check("A+ gets its own class, not A's", full.api.progGradeKey("A+") === "aplus");
check("N/A survives the slash", full.api.progGradeKey("N/A") === "na");
check("a missing grade defaults to N/A", full.api.progGradeKey(null) === "na");

// ─── 11. Nothing to show is a normal state, not a crash ─────────────────────

const bare = mount({
  currentPage: "progress",
  selectedDeviceId: null,
  devices: [],
  progress: {
    deviceId: null, date: null, rubric: null, day: null, trend: [], summary: null,
    detail: null, goals: null, rules: null, windowDays: 7, ruleSource: "app",
    loading: false, error: null,
  },
});
bare.api.renderProgressPage();
check("no device reads as a prompt, not an error",
  has(bare.html("progress-toolbar"), "No devices registered yet"));
check("no data offers an explanation", has(bare.html("progress-grade-card"), "No progress data yet"));
check("no counted time is its own empty state",
  has(bare.html("progress-bands-card"), "Nothing counted for this day"));
check("no history is its own empty state", has(bare.html("progress-trend-card"), "No history yet"));
const bareSources = bare.html("progress-sources");
check("all three panels still render when empty",
  (bareSources.match(/prog-panel/g) || []).length === 3);
check("each empty panel says so once",
  (bareSources.match(/No activity recorded for this day\./g) || []).length === 3);
check("each empty window says so once",
  (bareSources.match(/Nothing recorded in this window\./g) || []).length === 3);
check("no envelope is its own empty state",
  has(bare.html("progress-envelope-card"), "No browser foreground time recorded"));
check("no aggregates is its own empty state",
  has(bare.html("progress-ai-card"), "No aggregates yet"));
check("goals fall back to the documented defaults",
  has(bare.html("progress-goals-card"), 'value="60"') && has(bare.html("progress-goals-card"), 'value="5"'));
check("rule editor survives having no taxonomy at all",
  has(bare.html("progress-rules-card"), "No overrides yet"));

// ─── 12. Banners ────────────────────────────────────────────────────────────

const busy = mount(baseState({ loading: true }));
busy.api.renderProgressPage();
check("loading says loading", has(busy.html("progress-banner"), "Loading progress data"));

const broken = mount(baseState({ error: 'Device <script>alert(1)</script> is gone' }));
broken.api.renderProgressPage();
const bannerHtml = broken.html("progress-banner");
check("an error is shown to the parent", has(bannerHtml, "prog-banner error"));
check("an error message is escaped",
  has(bannerHtml, "&lt;script&gt;") && !has(bannerHtml, "<script>"));

// ─── 13. Structural: the section has no way to blend the sources ────────────
// Assertions about the code rather than its output. A future edit that adds a
// combined table would have to add a second call site to trip this.

check("progLedgerTable has exactly one call site",
  (SECTION.match(/progLedgerTable\(/g) || []).length === 2,
  `${(SECTION.match(/progLedgerTable\(/g) || []).length} occurrences incl. declaration`);
check("the section never flattens the ledger", !/\.flat\(/.test(SECTION));
check("the section never iterates the ledger's values", !/Object\.values\(\s*ledger/.test(SECTION));
check("the section never concatenates anything", !/\.concat\(/.test(SECTION));
check("the section reads the ledger only by source key",
  (SECTION.match(/ledger\[src\]/g) || []).length === 1);

console.log(`\n${PASS} passed, ${FAIL} failed`);
process.exit(FAIL ? 1 : 0);
