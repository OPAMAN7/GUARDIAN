/**
 * GUARDIAN — Telegram alert channel, offline suite
 *
 * The AI suite proves what leaves the machine for a report. This one proves the
 * three things that fail silently in an alert channel:
 *
 *   1. The bot token never appears in anything the browser or a log can see.
 *   2. One alert produces exactly one message — including across restarts, which
 *      is the case a live test can never reproduce on purpose.
 *   3. Telegram being down, slow or hostile costs the alert nothing: the row is
 *      already in the database and the handler must not throw.
 *
 * No network: global.fetch is replaced with an in-process Telegram that records
 * every request, so the exact bytes are assertable.
 *
 * Run:  node tests/test_telegram_offline.js
 */

const path = require("path");
const os = require("os");
const fs = require("fs");

const ROOT = process.env.GS_TEST_ROOT || path.join(__dirname, "..");
const TEST_DB =
  process.env.GUARDIAN_DB_PATH ||
  path.join(os.tmpdir(), `guardian-tg-test-${process.pid}.db`);

// The real database is 190-odd MB of a child's history. Never here.
if (/guardian\.db$/i.test(TEST_DB)) {
  console.error("Refusing to run: GUARDIAN_DB_PATH points at the real database.");
  process.exit(2);
}
process.env.GUARDIAN_DB_PATH = TEST_DB;
process.env.NODE_ENV = process.env.NODE_ENV || "test";

let PASS = 0;
let FAIL = 0;

function check(name, ok, detail = "") {
  if (ok) {
    PASS += 1;
    console.log(`  [PASS] ${name}`);
  } else {
    FAIL += 1;
    console.log(`  [FAIL] ${name}${detail ? ` — ${detail}` : ""}`);
  }
}

const db = require(path.join(ROOT, "models", "db"));

function cleanup() {
  for (const suffix of ["", "-wal", "-shm"]) {
    try { fs.unlinkSync(TEST_DB + suffix); } catch { /* fine */ }
  }
}

// A token shaped like a real one, so a leak test has something to look for.
const FAKE_TOKEN = "7712345678:AAF-offline-suite-token-do-not-use";
const FAKE_CHAT = "-1001234567890";

function wipeLog() {
  db.exec("DELETE FROM notify_log;");
}

/**
 * telegram.js snapshots its env at require time — the same contract as a server
 * restart — so every configuration needs a fresh load.
 */
function loadTg(env = {}) {
  const keys = [
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALERTS",
    "TELEGRAM_ALERT_TYPES", "TELEGRAM_MAX_PER_HOUR", "TELEGRAM_TIMEOUT_MS",
  ];
  const saved = {};
  for (const k of keys) {
    saved[k] = process.env[k];
    delete process.env[k];
  }
  for (const [k, v] of Object.entries(env)) {
    if (v != null) process.env[k] = String(v);
  }
  const p = require.resolve(path.join(ROOT, "services", "telegram.js"));
  delete require.cache[p];
  const tg = require(p);
  for (const k of keys) {
    if (saved[k] == null) delete process.env[k];
    else process.env[k] = saved[k];
  }
  return tg;
}

/**
 * An in-process Telegram. `responses` is a queue; the last entry repeats, so a
 * test can script "first call fine, everything after that throttled".
 * `mode` is "json" (a Bot API envelope), "http" (a bare status) or "throw".
 */
function stubFetch(responses) {
  const calls = [];
  let i = 0;
  global.fetch = async (url, opts) => {
    const entry = { url, method: opts.method, headers: opts.headers };
    try { entry.body = JSON.parse(opts.body); } catch { entry.body = null; }
    calls.push(entry);
    const r = responses[Math.min(i, responses.length - 1)];
    i += 1;
    if (r.throw) {
      const err = new Error(r.throw === "timeout" ? "The operation was aborted" : r.throw);
      if (r.throw === "timeout") err.name = "TimeoutError";
      throw err;
    }
    return {
      ok: r.status >= 200 && r.status < 300,
      status: r.status,
      async json() {
        if (r.badJson) throw new Error("Unexpected token < in JSON");
        return r.json;
      },
    };
  };
  return calls;
}

function restoreFetch() {
  delete global.fetch;
}

const SAMPLE_ALERT = {
  device_name: "child-pi",
  type: "blocked_site",
  message: "Blocked navigation attempt to reddit.com/r/teenagers",
  details: {
    domain: "reddit.com",
    url: "https://reddit.com/r/teenagers/comments/abc/private_thread",
    profile_path: "/home/pi/.config/chromium/Default/History",
  },
};

// ─── 1. Unconfigured is a quiet no-op, not an error ─────────────────────────

async function testNotConfigured() {
  console.log("\nUnconfigured — silence, never a crash");
  wipeLog();
  const tg = loadTg({});
  const calls = stubFetch([{ status: 200, json: { ok: true, result: {} } }]);

  const st = tg.status();
  check("status reports not configured", st.configured === false, JSON.stringify(st.configured));
  check("status says which button to press", typeof st.hint === "string" && st.hint.includes("GUARD"), String(st.hint));
  check("enabled is false without credentials", st.enabled === false, String(st.enabled));

  const r = await tg.send("hello");
  check("send refuses with a reason instead of throwing", r.sent === false && r.reason === "not_configured", JSON.stringify(r));
  check("nothing was sent to the network", calls.length === 0, String(calls.length));

  let threw = false;
  try { tg.notifyAlert(SAMPLE_ALERT, 1); } catch { threw = true; }
  await new Promise((r2) => setTimeout(r2, 20));
  check("notifyAlert never throws into the socket handler", threw === false);
  const rows = db.prepare("SELECT COUNT(*) AS n FROM notify_log").get().n;
  check("an unconfigured channel writes no ledger rows", rows === 0, String(rows));

  const missing = await tg.selfTest();
  check("selfTest names the missing half", missing.ok === false && /TOKEN/.test(missing.detail), JSON.stringify(missing));

  const withTokenOnly = loadTg({ TELEGRAM_BOT_TOKEN: FAKE_TOKEN });
  const half = await withTokenOnly.selfTest();
  check("with a token but no chat id it says chat id", half.ok === false && /CHAT_ID/.test(half.detail), JSON.stringify(half));
}

// ─── 2. The token is not a thing the outside world can learn ────────────────

async function testSecrecy() {
  console.log("\nSecrecy — the token stays on the server");
  wipeLog();
  const tg = loadTg({ TELEGRAM_BOT_TOKEN: FAKE_TOKEN, TELEGRAM_CHAT_ID: FAKE_CHAT });

  const st = JSON.stringify(tg.status());
  check("status() never contains the token", !st.includes(FAKE_TOKEN) && !st.includes("AAF-offline"), st.slice(0, 120));
  check("status() never contains the chat id", !st.includes(FAKE_CHAT), st.slice(0, 120));
  check("status() still proves both halves are present",
    tg.status().has_token === true && tg.status().has_chat_id === true);

  // Telegram's API puts the token in the path; there is no header form. So the
  // rule that matters is that the URL never escapes into an error, a log line or
  // the ledger — those are the places a token actually gets copied out of.
  let calls = stubFetch([{ status: 401, json: { ok: false, description: "Unauthorized" } }]);
  const failed = await tg.send("nope", { refType: "test", refId: "secrecy-1" });
  check("a rejected send reports a reason", failed.sent === false && failed.reason === "failed", JSON.stringify(failed));
  check("the failure detail never carries the token",
    !failed.detail.includes(FAKE_TOKEN) && !failed.detail.includes("api.telegram.org"), failed.detail);
  const row = db.prepare("SELECT status, detail FROM notify_log WHERE ref_id = 'secrecy-1'").get();
  check("the ledger records the failure", row && row.status === "failed", JSON.stringify(row));
  check("the ledger detail never carries the token",
    row && !String(row.detail).includes(FAKE_TOKEN), row && row.detail);
  check("the ledger detail is one short line",
    row && row.detail.length <= 200 && !row.detail.includes("\n"), row && String(row.detail.length));

  calls = stubFetch([{ throw: "timeout" }]);
  const timedOut = await tg.send("nope", { refType: "test", refId: "secrecy-2" });
  check("a timeout is reported in seconds, not as a stack",
    timedOut.sent === false && /no answer in \d+s/.test(timedOut.detail), timedOut.detail);
  check("the timeout detail never carries the token",
    !timedOut.detail.includes(FAKE_TOKEN), timedOut.detail);
  check("the request did reach the stub before timing out", calls.length === 1, String(calls.length));
}

// ─── 3. What the message actually says ──────────────────────────────────────

function testFormatting() {
  console.log("\nFormatting — readable on a lock screen, and no leaked URLs");
  const tg = loadTg({ TELEGRAM_BOT_TOKEN: FAKE_TOKEN, TELEGRAM_CHAT_ID: FAKE_CHAT });

  const text = tg.formatAlert(SAMPLE_ALERT);
  check("the alert type is in the headline", text.includes("blocked_site"), text.slice(0, 60));
  check("the device name is named", text.includes("child-pi"));
  check("the message body survives", text.includes("Blocked navigation attempt"));
  check("a full URL from details never reaches the phone",
    !text.includes("/r/teenagers/comments/"), text);
  check("a local filesystem path never reaches the phone",
    !text.includes("/home/pi/"), text);
  check("one useful detail is picked out of the blob", text.includes("domain: reddit.com"), text);

  const nasty = tg.formatAlert({
    device_name: "kid <pi> & co",
    type: "malware",
    message: "found <script>alert(1)</script> in a download",
    details: null,
  });
  check("angle brackets in a device name are escaped", nasty.includes("kid &lt;pi&gt; &amp; co"), nasty);
  check("a script tag from a filename cannot become markup",
    nasty.includes("&lt;script&gt;") && !nasty.includes("<script>"), nasty);

  const long = tg.formatAlert({
    device_name: "d",
    type: "dangerous_file",
    message: "x".repeat(5000),
    details: { file_name: "y".repeat(900) },
  });
  check("a runaway message is clipped well under Telegram's 4096 limit",
    long.length < 1200, String(long.length));
  check("clipping is visible rather than silent", long.includes("…"), long.slice(-40));
  check("an unknown type still gets an icon", tg.formatAlert({ type: "who_knows" }).startsWith("🔔"));
}

// ─── 4. One alert, one message — even across a restart ──────────────────────

async function testDedupe() {
  console.log("\nDedupe — the ledger is claimed before the network call");
  wipeLog();
  const tg = loadTg({ TELEGRAM_BOT_TOKEN: FAKE_TOKEN, TELEGRAM_CHAT_ID: FAKE_CHAT });
  const ok = { status: 200, json: { ok: true, result: { message_id: 5 } } };
  let calls = stubFetch([ok]);

  const first = await tg.send("one", { refType: "alert", refId: "4242" });
  const second = await tg.send("one", { refType: "alert", refId: "4242" });
  check("the first send goes out", first.sent === true, JSON.stringify(first));
  check("the second is refused as already sent",
    second.sent === false && second.reason === "already_sent", JSON.stringify(second));
  check("only one request reached Telegram", calls.length === 1, String(calls.length));
  check("exactly one ledger row exists",
    db.prepare("SELECT COUNT(*) AS n FROM notify_log WHERE ref_id='4242'").get().n === 1);

  // A fresh require is what a server restart looks like from here: same database,
  // new process state. The ledger, not memory, has to be what stops the resend.
  const rebooted = loadTg({ TELEGRAM_BOT_TOKEN: FAKE_TOKEN, TELEGRAM_CHAT_ID: FAKE_CHAT });
  calls = stubFetch([ok]);
  const afterBoot = await rebooted.send("one", { refType: "alert", refId: "4242" });
  check("a restarted server does not re-push an old alert",
    afterBoot.sent === false && afterBoot.reason === "already_sent", JSON.stringify(afterBoot));
  check("and made no request at all", calls.length === 0, String(calls.length));

  // Different alert ids are different events and must both arrive.
  calls = stubFetch([ok]);
  await tg.send("two", { refType: "alert", refId: "4243" });
  await tg.send("three", { refType: "alert", refId: "4244" });
  check("distinct alert ids each get their own message", calls.length === 2, String(calls.length));
  check("the chat id is on every request",
    calls.every((c) => c.body.chat_id === FAKE_CHAT));
  check("HTML parse mode and no link preview on every request",
    calls.every((c) => c.body.parse_mode === "HTML" && c.body.disable_web_page_preview === true));
  check("the request is a POST to sendMessage",
    calls.every((c) => c.method === "POST" && c.url.endsWith("/sendMessage")), calls[0].url.slice(-30));
}

// ─── 5. A bad afternoon at Telegram costs the alert nothing ─────────────────

async function testFailurePaths() {
  console.log("\nFailure paths — the alert is already saved, so nothing here is fatal");
  wipeLog();
  const tg = loadTg({ TELEGRAM_BOT_TOKEN: FAKE_TOKEN, TELEGRAM_CHAT_ID: FAKE_CHAT });

  stubFetch([{ status: 200, badJson: true }]);
  const garbled = await tg.send("hi", { refType: "test", refId: "f1" });
  check("a 200 with unparseable body is a failure, not a success",
    garbled.sent === false && garbled.reason === "failed", JSON.stringify(garbled));
  check("an unreadable answer is named as such, not reported as HTTP 200",
    garbled.detail.includes("unreadable answer"), garbled.detail);

  stubFetch([{ status: 200, json: { ok: false, description: "chat not found" } }]);
  const notFound = await tg.send("hi", { refType: "test", refId: "f2" });
  check("Telegram's own ok:false is honoured even on HTTP 200",
    notFound.sent === false && notFound.detail.includes("chat not found"), JSON.stringify(notFound));

  stubFetch([{ throw: "getaddrinfo ENOTFOUND api.telegram.org" }]);
  const offline = await tg.send("hi", { refType: "test", refId: "f3" });
  check("a DNS failure degrades to a reason",
    offline.sent === false && offline.detail.includes("unreachable"), JSON.stringify(offline));

  // The socket handler's real risk is an unhandled rejection taking the process
  // down, so drive the fire-and-forget path with a throwing provider.
  stubFetch([{ throw: "boom" }]);
  let unhandled = null;
  const onUnhandled = (err) => { unhandled = err; };
  process.on("unhandledRejection", onUnhandled);
  tg.notifyAlert(SAMPLE_ALERT, 9001);
  await new Promise((r) => setTimeout(r, 60));
  process.off("unhandledRejection", onUnhandled);
  check("a throwing provider produces no unhandled rejection", unhandled === null, String(unhandled));
  const row = db.prepare("SELECT status FROM notify_log WHERE ref_id='9001'").get();
  check("the failed push is still recorded against the alert id",
    row && row.status === "failed", JSON.stringify(row));

  // A failed row must not be retried into a duplicate later.
  stubFetch([{ status: 200, json: { ok: true, result: {} } }]);
  const retry = await tg.send("hi", { refType: "alert", refId: "9001" });
  check("a failed push is not silently retried into a duplicate",
    retry.sent === false && retry.reason === "already_sent", JSON.stringify(retry));

  const muted = loadTg({
    TELEGRAM_BOT_TOKEN: FAKE_TOKEN, TELEGRAM_CHAT_ID: FAKE_CHAT, TELEGRAM_ALERTS: "false",
  });
  const calls = stubFetch([{ status: 200, json: { ok: true, result: {} } }]);
  const off = await muted.send("hi", { refType: "test", refId: "f4" });
  check("TELEGRAM_ALERTS=false mutes the channel without losing the credentials",
    off.sent === false && off.reason === "disabled" && muted.status().configured === true, JSON.stringify(off));
  check("a muted channel makes no request", calls.length === 0, String(calls.length));
}

// ─── 6. A stuck agent must not become a siren ───────────────────────────────

async function testRateCap() {
  console.log("\nRate cap — the phone is protected from a looping agent");
  wipeLog();
  const tg = loadTg({
    TELEGRAM_BOT_TOKEN: FAKE_TOKEN, TELEGRAM_CHAT_ID: FAKE_CHAT, TELEGRAM_MAX_PER_HOUR: "3",
  });
  const calls = stubFetch([{ status: 200, json: { ok: true, result: {} } }]);

  const results = [];
  for (let i = 1; i <= 6; i += 1) {
    results.push(await tg.send(`burst ${i}`, { refType: "alert", refId: `cap-${i}` }));
  }
  check("the cap is read from the environment", tg.MAX_PER_HOUR === 3, String(tg.MAX_PER_HOUR));
  check("exactly three messages went out", calls.length === 3, String(calls.length));
  check("the overflow reports rate_capped",
    results.slice(3).every((r) => r.reason === "rate_capped"), JSON.stringify(results.map((r) => r.reason)));
  const skipped = db.prepare(
    "SELECT COUNT(*) AS n FROM notify_log WHERE status='skipped_rate'"
  ).get().n;
  check("the dropped alerts are recorded as skipped, not lost without trace",
    skipped === 3, String(skipped));
  check("a dropped alert is dropped, not queued for later",
    (await tg.send("burst 1 again", { refType: "alert", refId: "cap-4" })).reason === "already_sent");
}

// ─── 7. Type filter and self test ───────────────────────────────────────────

async function testFilterAndSelfTest() {
  console.log("\nType filter and self test");
  wipeLog();
  const tg = loadTg({
    TELEGRAM_BOT_TOKEN: FAKE_TOKEN,
    TELEGRAM_CHAT_ID: FAKE_CHAT,
    TELEGRAM_ALERT_TYPES: "malware, dangerous_file",
  });
  let calls = stubFetch([{ status: 200, json: { ok: true, result: {} } }]);

  check("the filter is parsed and trimmed",
    tg.TYPE_FILTER.join("|") === "malware|dangerous_file", tg.TYPE_FILTER.join("|"));
  check("an allowed type passes", tg.typeAllowed("MALWARE") === true);
  check("an unlisted type is filtered", tg.typeAllowed("blocked_site") === false);
  tg.notifyAlert(SAMPLE_ALERT, 5001);
  await new Promise((r) => setTimeout(r, 40));
  check("a filtered alert makes no request and no ledger row",
    calls.length === 0 && db.prepare("SELECT COUNT(*) AS n FROM notify_log").get().n === 0,
    String(calls.length));

  tg.notifyAlert({ ...SAMPLE_ALERT, type: "malware" }, 5002);
  await new Promise((r) => setTimeout(r, 40));
  check("an allowed alert does go out", calls.length === 1, String(calls.length));
  check("an empty filter means everything is forwarded",
    loadTg({ TELEGRAM_BOT_TOKEN: FAKE_TOKEN, TELEGRAM_CHAT_ID: FAKE_CHAT }).typeAllowed("anything") === true);

  // The self test is the one message the parent explicitly asked for, so it is
  // exempt from both the ledger and the cap — and must stay exempt, or a second
  // click in the settings screen would silently do nothing.
  wipeLog();
  const plain = loadTg({
    TELEGRAM_BOT_TOKEN: FAKE_TOKEN, TELEGRAM_CHAT_ID: FAKE_CHAT, TELEGRAM_MAX_PER_HOUR: "1",
  });
  calls = stubFetch([{ status: 200, json: { ok: true, result: {} } }]);
  const t1 = await plain.selfTest();
  const t2 = await plain.selfTest();
  check("the self test reports success", t1.ok === true && t2.ok === true, JSON.stringify([t1, t2]));
  check("it can be run twice — no dedupe, no cap", calls.length === 2, String(calls.length));
  check("it writes no ledger rows",
    db.prepare("SELECT COUNT(*) AS n FROM notify_log").get().n === 0);
  check("the confirmation names GUARDIAN so the parent knows what connected",
    calls[0].body.text.includes("GUARDIAN connected"), calls[0].body.text.slice(0, 60));

  stubFetch([{ status: 404, json: { ok: false, description: "Not Found" } }]);
  const bad = await plain.selfTest();
  check("a wrong token fails the self test with a readable reason",
    bad.ok === false && bad.detail.includes("Not Found") && !bad.detail.includes(FAKE_TOKEN),
    JSON.stringify(bad));
}

async function main() {
  console.log("=".repeat(66));
  console.log("GUARDIAN Telegram alert channel — offline test suite");
  console.log("=".repeat(66));
  await testNotConfigured();
  await testSecrecy();
  testFormatting();
  await testDedupe();
  await testFailurePaths();
  await testRateCap();
  await testFilterAndSelfTest();
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





