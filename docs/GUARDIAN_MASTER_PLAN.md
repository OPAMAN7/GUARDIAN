# 🛡️ GUARDIAN — FULL PROJECT ANALYSIS & MASTER PLAN

> Generated: 2026-08-20 | Based on complete codebase scan of every file

---

## 📐 PROJECT OVERVIEW

**Guardian Shield** is a parent-child monitoring system with 3 main components:

| Component | Language | Role |
|-----------|----------|------|
| **Pi Agent** | Python | Runs on child's Raspberry Pi — captures screen, blocks sites, guards downloads |
| **Parent Dashboard** | Node.js + Vanilla JS | Runs on parent's Windows PC — live feed, alerts, blocklist control |
| **GUARD Console** | Python CLI | Unified launcher on parent's PC — starts all 3 services and syncs network |

### Architecture Diagram

```
PARENT'S WINDOWS PC
┌──────────────────────────────────────────────────────────┐
│  GUARD.py ──▶  Express Server (Node.js :3000)            │
│  (launcher)    + Socket.IO                               │
│                + SQLite DB (currently 1.2GB!)            │
│                     │                                    │
│  Windows Watchdog   ▼                                    │
│  (keeps Node up)   Web Dashboard (Vanilla JS + Vite)     │
└────────────────────────┬─────────────────────────────────┘
                         │ Socket.IO WebSocket
CHILD'S RASPBERRY PI     │
┌────────────────────────▼─────────────────────────────────┐
│  Guardian Agent (Python)                                 │
│  ├── ScreenshotCapture ──▶ Sends frames to parent        │
│  ├── WebsiteBlocker    ──▶ Modifies /etc/hosts           │
│  ├── DownloadGuard     ──▶ Scans files + quarantine      │
│  ├── BlockPageServer   ──▶ localhost:80 warning pages    │
│  └── Watchdog          ──▶ Auto-restart if agent killed  │
└───────────────────────────────────────────────────────────┘
```

---

## 🐛 BUGS FOUND — FIX THESE

> **Status note (2026-09-02):** the bug numbering below is from the original
> 2026-08-20 scan. The **live ledger is `docs/SESSION_MEMORY.md`** — use its numbers.
> As of 2026-09-02 the agent-liveness cluster (AppTracker deadlock, unauthenticated
> sockets, unguarded process kills, missing heartbeat, frozen mirror) is fixed and
> verified offline; `.env` secrets and the two auth-middleware items are still open.

### 🔴 CRITICAL

#### BUG-01 — DB is 1.28 GB and WAL not checkpointed
**File:** `parent-dashboard/server-ts/data/guardian.db`
The WAL file is 4.2 MB, main DB is 1.28 GB. Screenshots pile up faster than the cleanup job runs. Will crash eventually due to disk space.

**Fix in `db.js` after the WAL pragma:**
```js
db.pragma('wal_checkpoint(TRUNCATE)');
```
Also lower `SCREENSHOT_RETENTION_DAYS` default from `"7"` to `"3"` and add a manual `/api/admin/cleanup` endpoint.

---

#### BUG-02 — `import queue` duplicated in main.py
**File:** `pi-agent/agent/main.py` — Lines 11 AND 22
`import queue` is imported twice. Remove line 22.

---

#### BUG-03 — `.env` has hardcoded weak secrets committed to git
**File:** `parent-dashboard/server-ts/.env`
```
JWT_SECRET=guardian-shield-secret-change-me-in-production
ADMIN_PASSWORD=admin123
```
If pushed to GitHub, the entire auth system is permanently compromised.

**Fix:**
1. Add `.env` to `.gitignore`
2. Create `.env.example` with placeholder values
3. Regenerate JWT secret: `node -e "console.log(require('crypto').randomBytes(64).toString('hex'))"`
4. Change `admin123` to a strong password

---

#### BUG-04 — `persistence.py` references non-existent `watchdog.py`
**File:** `pi-agent/agent/persistence.py` — Line 204
```python
watchdog_path = agent_dir / "watchdog.py"   # WRONG — file doesn't exist
```
Actual file is `guardian_watchdog.py`. When `install_linux_service()` is called, the systemd watchdog unit points to a missing file.

**Fix:** Change to `agent_dir / "guardian_watchdog.py"`

---

#### BUG-05 — Windows watchdog `os.chdir()` is thread-unsafe
**File:** `windows-app/watchdog/watchdog_server.py` — Line 69
```python
os.chdir(str(directory))  # changes CWD for the entire process, breaks other threads
```
`os.chdir()` changes the process-wide working directory. This can break the parallel `subprocess.Popen` call that restarts Node.js.

**Fix:** Use Python 3.7+ `http.server.SimpleHTTPRequestHandler`'s built-in `directory` parameter instead:
```python
import functools

def _serve_file(directory: Path, port: int, duration: int):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    srv = http.server.HTTPServer(("localhost", port), handler)
    srv.timeout = 1
    deadline = time.time() + duration
    while time.time() < deadline:
        srv.handle_request()
    srv.server_close()
```

---

#### BUG-06 — No rate limiting on `/change-password`
**File:** `parent-dashboard/server-ts/routes/auth.js`
`/login` has brute-force protection (10 attempts / 10 min). `/change-password` has none. An attacker with a stolen token can brute-force the old password.

**Fix:** Apply the same `loginAttempts` map to change-password attempts.

---

### 🟡 MEDIUM

#### BUG-07 — `shared-types/` folder is completely empty
**File:** `parent-dashboard/shared-types/`
Promised TypeScript shared types. Folder is empty. Both server and web are still JS, not TS.
**Fix:** Populate with JSDoc type definitions or delete entirely.

---

#### BUG-08 — Screenshot endpoints have NO auth middleware
**File:** `parent-dashboard/server-ts/index.js` — Lines 346–376
`/api/screenshots/latest/:deviceId` and `/api/screenshots/history/:deviceId` are fully public. Anyone can request live screenshots without logging in.

**Fix:** Add `authMiddleware` to both screenshot route handlers.

---

#### BUG-09 — `warning_counts.json` committed to git (runtime state)
**File:** `pi-agent/agent/warning_counts.json`
This file tracks blocked-site visit counts. It's runtime state. Committing it causes stale counts on fresh deploys.
**Fix:** Add `warning_counts.json` to `.gitignore`

---

#### BUG-10 — TLS private key committed to git
**File:** `pi-agent/agent/block_page_cert.pem` and `block_page_key.pem`
TLS private keys should never be in git. Even self-signed certs shouldn't be committed.
**Fix:** Add `*.pem` to `.gitignore`. Generate cert on install via the `cryptography` library (it already does this).

---

#### BUG-11 — Custom `shutil_which()` duplicates stdlib
**File:** `GUARD.py` — Lines 363–373
Python's `shutil.which()` already exists and handles edge cases better.
**Fix:** Replace custom function with `import shutil` and `shutil.which("ssh") is not None`

---

#### BUG-12 — Systemd unit hardcodes `XAUTHORITY=/run/user/1000/.Xauthority`
**File:** `pi-agent/agent/persistence.py` — Line 36
Only works if desktop user is UID 1000. Will fail silently on non-standard setups.
**Fix:** Detect UID of target user dynamically at install time.

---

### 🟢 LOW

#### BUG-13 — `kill_attempts.txt` committed to git
**File:** `windows-app/watchdog/kill_attempts.txt` — Runtime state, add to `.gitignore`

#### BUG-14 — Test screenshots in repo root (1.4MB bloat)
**Files:** `guardian_wayland_test.png`, `latest_aman_test.jpg` — Delete these.

#### BUG-15 — `stitch/` folder is orphaned prototype code
**File:** `stitch/` — `code.html` (30KB) and `screen.png` are never integrated. Delete or integrate.

---

## 🗑️ DELETE THESE FILES NOW

| File/Folder | Reason |
|-------------|--------|
| `guardian_wayland_test.png` | Dev test screenshot, 1.4 MB bloat |
| `latest_aman_test.jpg` | Dev test photo, 44 KB bloat |
| `stitch/` | Orphaned UI prototype, never integrated |
| `parent-dashboard/shared-types/` | Empty placeholder, no content |
| `pi-agent/agent/warning_counts.json` | Runtime state, shouldn't be in git |
| `pi-agent/agent/__pycache__/` | Python bytecache, add to .gitignore |
| `windows-app/watchdog/kill_attempts.txt` | Runtime state, add to .gitignore |
| `windows-app/config/local-agent-config.runtime.json` | Duplicate/temp config copy |
| `windows-app/config/local-agent-config.verify.json` | Temp verification copy |

---

## 🚀 FUTURE FEATURES — PRIORITY ORDER

### 🔥 PHASE 1 — Must Have (These complete the core product)

| # | Feature | Why |
|---|---------|-----|
| 1 | **Multi-device support UI** | Dashboard tracks N devices, UI barely shows 1 at a time |
| 2 | **Screenshot timeline/history viewer** | DB stores history but there's zero UI to browse it |
| 3 | **Alert sounds on dashboard** | Buzzer exists on Pi, but browser dashboard is silent |
| 4 | **Blocklist categories** (Social / Gaming / Adult) | Currently only "custom" — parents need presets |
| 5 | **Scheduled screen-time limits** | Parents can't set "no internet 10pm–7am" — critical feature |

### 🧩 PHASE 2 — High Value (Differentiate the product)

| # | Feature | Why |
|---|---------|-----|
| 6 | **Dashboard GUI overhaul** | Your own docs say this needs doing (Stitch design) |
| 7 | **Mobile-responsive dashboard** | Parents check on kids from phones |
| 8 | **Email/SMS alert integration** | Parents need alerts even when dashboard is closed |
| 9 | **Screenshot retention UI** | Currently hardcoded in .env — make it a dashboard setting |
| 10 | **Remote lock screen command** | `block_screen.py` exists but has no remote trigger yet! |

### 🌟 PHASE 3 — Future Vision (Make it a real product)

| # | Feature | Why |
|---|---------|-----|
| 11 | **App usage tracking** | Know which apps child runs, not just which websites |
| 12 | **AI content moderation** | Gemini Vision API flags screen content in real-time |
| 13 | **Multi-parent support** | Both parents log in with different roles |
| 14 | **Weekly PDF activity reports** | Screen time, top blocked sites, alerts summary |
| 15 | **Android/iOS monitoring** | Expand beyond Raspberry Pi |

### 📈 PHASE 4 — Child Progress Tracking (detailed roadmap: Weeks 6–8 below)

Approved 2026-09-02. Not started — planning only until the three live failures are
confirmed fixed on the Pi.

| # | Feature | Why |
|---|---------|-----|
| F-18 | **Foreground usage sessions** | Active app is known every second but never stored — screen time is uncomputable today |
| F-19 | **Browser history collection** | Blocklist only logs *blocked* attempts; progress needs what was actually read |
| F-20 | **YouTube / platform watch history** | A maths lecture and a prank video are both `youtube.com` — only the title separates them |
| F-21 | **Categorisation + progress score** | "3h on chrome.exe" means nothing; "2h education / 1h video" does |
| F-22 | **Progress dashboard page** | One score, one trend, two lists — parent understands it in five seconds |
| F-23 | **AI insight section** | Cloud API turns the aggregates into a 30-second narrative |
| F-24 | **Goals & nudges** | A score with no target is trivia; reuses the child-message overlay |
| F-25 | **Progress in the weekly PDF/email** | Makes the Week 5 report worth opening |

---

## 📋 MASTER EXECUTION PLANNER

> Do these in order. Each week builds on the previous.

---

### ✅ WEEK 1 — Bug Fixes & Repo Cleanup

**Goal:** Clean, stable, safe codebase. No bugs, no dead files, no secrets in git.

#### Day 1 — Security & Git Hygiene (2–3 hours)

**Why first:** If you push to GitHub with current secrets, the system is permanently compromised.

- [ ] Add `parent-dashboard/server-ts/.env` to `.gitignore`
- [ ] Create `.env.example` with placeholder values
- [ ] Add to `.gitignore`: `*.pem`, `*.key`, `warning_counts.json`, `kill_attempts.txt`, `__pycache__/`, `*.db`, `*.db-shm`, `*.db-wal`
- [ ] Generate new JWT secret: `node -e "console.log(require('crypto').randomBytes(64).toString('hex'))"`
- [ ] Change `ADMIN_PASSWORD` from `admin123` to a strong password

#### Day 2 — Delete Dead Files (30 min)

**Why:** Dead files confuse navigation and bloat the repo.

- [ ] Delete `guardian_wayland_test.png`
- [ ] Delete `latest_aman_test.jpg`
- [ ] Delete `stitch/` folder
- [ ] Delete `parent-dashboard/shared-types/`
- [ ] Delete `windows-app/config/local-agent-config.runtime.json`
- [ ] Delete `windows-app/config/local-agent-config.verify.json`

#### Day 3 — Fix Code Bugs (3–4 hours)

- [ ] **BUG-02:** Remove duplicate `import queue` at line 22 in `main.py`
- [ ] **BUG-04:** Fix `persistence.py` line 204: `"watchdog.py"` → `"guardian_watchdog.py"`
- [ ] **BUG-05:** Fix `watchdog_server.py` `_serve_file` — use `functools.partial` with `directory=` parameter
- [ ] **BUG-06:** Add rate limiting to `/change-password` in `auth.js`
- [ ] **BUG-08:** Add `authMiddleware` to screenshot routes in `index.js`
- [ ] **BUG-11:** Replace custom `shutil_which()` in `GUARD.py` with `shutil.which()`

#### Day 4 — Database Emergency Fix (1–2 hours)

**Why:** 1.28 GB DB will hit 10+ GB within weeks at 20fps capture.

- [ ] Add to `db.js` after WAL pragma: `db.pragma('wal_checkpoint(TRUNCATE)')`
- [ ] Run manual DB cleanup now:
  ```sql
  DELETE FROM screenshots WHERE captured_at < datetime('now', '-3 days');
  VACUUM;
  ```
- [ ] Change `SCREENSHOT_RETENTION_DAYS` default to `3` in `.env.example`
- [ ] Add `DELETE /api/admin/cleanup?days=N` endpoint

#### Day 5 — Validate Everything (2 hours)

- [ ] Start `node index.js` in `server-ts/` — verify no crash
- [ ] Login at `http://localhost:3000` with new credentials
- [ ] Add test domain to blocklist — verify it saves
- [ ] Check `/api/health` returns 200
- [ ] Run `start-live-control.bat` — verify screenshot stream works

---

### 🎨 WEEK 2 — Dashboard GUI Overhaul

**Goal:** Make the dashboard look premium. Current UI is functional but basic.

#### Day 6–7 — Design System (4–6 hours)

**Why:** Every great product needs a coherent visual language first.

- [ ] Add Google Font `Outfit` or `Inter` to `index.html`
- [ ] Define CSS design tokens in `style.css`:
  ```css
  :root {
    --bg: #0f172a;
    --surface: #1e293b;
    --accent: #0ea5e9;
    --danger: #ef4444;
    --success: #22c55e;
    --text: #f1f5f9;
    --muted: #64748b;
    --radius: 12px;
    --font: 'Outfit', sans-serif;
  }
  ```
- [ ] Rebuild sidebar nav with new design tokens
- [ ] Redesign login page (currently a plain form)

#### Day 8–9 — Core Pages Redesign (6–8 hours)

- [ ] **Live View:** Device selector dropdown, CPU/memory/temp telemetry bar, fullscreen toggle button
- [ ] **Devices Page:** Card grid layout, online/offline badges, last-seen timestamp, action buttons
- [ ] **Blocklist Page:** Searchable sortable table, category filter chips, bulk-add textarea
- [ ] **Alerts Page:** Severity-colored row highlights, expandable detail panel, mark-all-read button

#### Day 10 — Polish & Mobile (2–3 hours)

- [ ] Test every page at 375px mobile width
- [ ] Add `@media (max-width: 768px)` sidebar collapse
- [ ] Add `transition: all 0.2s ease` to all interactive elements
- [ ] `npm run build` — verify production bundle works

---

### 📅 WEEK 3 — Screenshot Timeline & Screen Time

**Goal:** Add the two most important missing features.

#### Day 11–12 — Screenshot History Viewer (4–5 hours)

**Why:** Screenshots are stored in the DB but completely inaccessible from the UI — wasted feature.

- [ ] Add "Timeline" page to nav
- [ ] Fetch from `/api/screenshots/history/:deviceId?limit=50`
- [ ] Display thumbnails in horizontal scrollable strip
- [ ] Click thumbnail → modal with full-size image + timestamp
- [ ] Date range filter (Today / Last 7 Days / Custom range)

#### Day 13–14 — Screen Time Scheduling (5–6 hours)

**Why:** A parental monitoring tool without time limits is incomplete.

New DB table:
```sql
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id INTEGER,
  day_of_week TEXT,    -- 'mon', 'tue', etc.
  start_time TEXT,     -- '22:00'
  end_time TEXT,       -- '07:00'
  action TEXT DEFAULT 'block_all',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

- [ ] Add `GET/POST/DELETE /api/schedules/:deviceId` routes
- [ ] Agent polls schedule every 60s and enforces via hosts file
- [ ] Dashboard: weekly grid UI showing allowed (green) / blocked (red) hours per day

#### Day 15 — End-to-End Testing (2 hours)

- [ ] Verify schedule blocks internet on Pi at specified time
- [ ] Verify schedule auto-unblocks after end time
- [ ] Verify timeline shows correct screenshots with correct timestamps

---

### 🔔 WEEK 4 — Alerts & Notifications

**Goal:** Parents get notified even when the dashboard isn't open.

#### Day 16–17 — Email Alerts (4–5 hours)

**Why:** Parents can't watch the dashboard 24/7.

- [ ] `npm install nodemailer` in server-ts
- [ ] Add SMTP config to `.env`:
  ```
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=your@gmail.com
  SMTP_PASS=your_app_password
  ALERT_EMAIL_TO=parent@email.com
  ```
- [ ] Create `emailer.js` — sends formatted HTML email
- [ ] In `index.js` `socket.on('alert')` — trigger email for `malware` and level 3 `blocked_site` events

#### Day 18 — Browser Push Notifications (2 hours)

**Why:** Instant heads-up even when tab is in background.

- [ ] Request `Notification` permission on dashboard load
- [ ] On `new_alert` socket event: if `document.hidden`, fire `new Notification(...)`
- [ ] Show device name + alert type in notification title

#### Day 19–20 — Blocklist Presets & Categories (3–4 hours)

**Why:** Parents shouldn't have to type every domain manually.

Presets to add:
- Social Media: `instagram.com`, `tiktok.com`, `snapchat.com`, `twitter.com`, `facebook.com`
- Gaming: `roblox.com`, `minecraft.net`, `steam.com`, `epicgames.com`
- Video: `youtube.com`, `twitch.tv`, `netflix.com`, `disneyplus.com`

- [ ] Add "Quick Add Preset" section to Blocklist page
- [ ] Category filter on blocklist table
- [ ] Use existing `/api/blocklist/bulk` endpoint for batch adds

---

### 🔒 WEEK 5 — Remote Lock & Activity Reports

**Goal:** Give parents direct control and an audit trail.

#### Day 21–22 — Remote Lock Screen (3–4 hours)

**Why:** `block_screen.py` already exists but has NO remote trigger. This is a 2-hour feature sitting unused.

- [ ] Add `lock_screen` Socket.IO event to server
- [ ] In Pi agent: `server.sio.on('lock_screen') → block_screen.show_nuh_uh()`
- [ ] Add "🔒 Lock Screen" button to device card in dashboard
- [ ] Add "🔓 Unlock" command (kills any open NUH UH processes)

#### Day 23–24 — PDF Activity Report (4–5 hours)

**Why:** Parents want a weekly summary without monitoring in real-time.

- [ ] `npm install puppeteer` in server-ts
- [ ] Create `GET /api/reports/:deviceId/weekly` — generates PDF with:
  - Total estimated screen time
  - Top 10 blocked site attempts
  - Alert count by type
  - Last 10 screenshot thumbnails of the week
- [ ] Add "📄 Export Report" button to device detail page

#### Day 25 — Final QA Pass (2–3 hours)

- [ ] Full end-to-end test with real Pi connected
- [ ] Verify all 5 weeks of features work together
- [ ] Check server logs for any unhandled errors
- [ ] Verify DB stays under 200 MB with 3-day retention

---

### 📈 WEEK 6 — Child Progress Tracking: Data Collection

**Goal:** Record *how time is actually spent* — per app and per site — so there is
history to analyse later. Collection only: no new dashboard, no AI this week.

**Prerequisite:** the 2026-09-02 liveness fixes (BUG-10, BUG-12, BUG-13, agent
heartbeat) must be on the Pi first. Data from an agent that keeps dropping offline is
worse than no data — the gaps read as "no screen time".

> Bug/feature numbers in this roadmap follow `docs/SESSION_MEMORY.md`, which is the
> running ledger. This file holds the plan; that file holds the state.

#### Day 26–27 — Persist foreground app sessions (4–5 hours)

**Why:** `app_tracker.py` already resolves the active app every second, but nothing is
stored, so "screen time" cannot be computed at all today.

```sql
CREATE TABLE IF NOT EXISTS usage_sessions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id    INTEGER NOT NULL,
  app_name     TEXT    NOT NULL,
  window_title TEXT,
  started_at   DATETIME NOT NULL,
  ended_at     DATETIME NOT NULL,
  seconds      INTEGER  NOT NULL,
  UNIQUE(device_id, app_name, started_at)
);
CREATE INDEX IF NOT EXISTS idx_usage_device_time
  ON usage_sessions(device_id, started_at);
```

- [ ] New `pi-agent/agent/usage_tracker.py`: collapse per-second active-app polls into
      sessions (new session when the app changes, or after a 60s idle gap)
- [ ] Drop sessions under 5s so alt-tabbing does not write thousands of rows
- [ ] Buffer in memory, flush every 60s via a new `usage_batch` socket event
- [ ] Spool to `/opt/guardian-agent/usage_queue.jsonl` while offline and replay on
      reconnect — the UNIQUE constraint makes replay idempotent
- [ ] Server: `socket.on("usage_batch")` → `INSERT OR IGNORE`, ack with the accepted
      count so the agent only drops what actually landed
- [ ] Read the batch handler off the socket read loop (`_run_off_read_loop`) — same
      rule that fixed the BUG-10 stalls

#### Day 28–29 — Browser + YouTube watch history (6–7 hours)

**Why:** the blocklist only records *blocked* attempts. Progress needs the other 95% —
what the child actually read and watched. Decision (locked): read the browser's own
history database directly on the device rather than installing a browser extension.

```sql
CREATE TABLE IF NOT EXISTS web_visits (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id  INTEGER NOT NULL,
  url        TEXT NOT NULL,
  domain     TEXT NOT NULL,
  title      TEXT,
  visited_at DATETIME NOT NULL,
  dwell_secs INTEGER DEFAULT 0,
  browser    TEXT,
  UNIQUE(device_id, url, visited_at)
);
CREATE INDEX IF NOT EXISTS idx_visits_device_time
  ON web_visits(device_id, visited_at);
```

- [ ] New `pi-agent/agent/browser_history.py`, polled every 2 min
- [ ] Chromium/Chrome: `~/.config/chromium/Default/History` — join `urls` + `visits`.
      Timestamps are microseconds since **1601-01-01**:
      `unix = chrome_time / 1_000_000 - 11_644_473_600`
- [ ] Firefox: `~/.mozilla/firefox/*.default*/places.sqlite` — join `moz_places` +
      `moz_historyvisits`. `visit_date` is microseconds since **1970-01-01**
- [ ] **Copy the DB to /tmp before reading it.** A running browser holds a lock; open
      the copy read-only (`file:...?mode=ro&immutable=1`) so the browser is untouched
- [ ] Keep a watermark per browser (`last_visit_id`, `last_visit_time`) in
      `/opt/guardian-agent/history_state.json` — never re-upload the whole history
- [ ] Derive `dwell_secs` from the gap to the next visit in the same browser, capped
      at 30 min so an idle open tab does not count as 6 hours of reading
- [ ] Upload with the same batch/spool/ack path as `usage_sessions`
- [ ] Handle the private-browsing gap honestly: incognito never hits the history DB.
      Note it in the UI later rather than pretending coverage is total

**YouTube (and the other platforms) need their own pass — same table, extra fields:**

```sql
ALTER TABLE web_visits ADD COLUMN platform   TEXT;   -- youtube | netflix | spotify | ...
ALTER TABLE web_visits ADD COLUMN media_id   TEXT;   -- the ?v= id, for dedupe
ALTER TABLE web_visits ADD COLUMN media_title TEXT;  -- the video title
```

- [ ] Detect `youtube.com/watch?v=<id>` and `youtu.be/<id>`; store `media_id` so the
      same video watched twice is two visits but one identifiable title
- [ ] The page title in the history DB *is* the video title ("... - YouTube") — strip
      the suffix and keep it as `media_title`. This is the whole reason YouTube can be
      categorised at all
- [ ] Shorts (`/shorts/<id>`) counted separately from long-form — 40 shorts and one
      lecture are not the same 30 minutes
- [ ] Watch time ≈ dwell on the watch URL, capped at 3h; a paused tab is not viewing
- [ ] Same treatment for netflix/primevideo/spotify/twitch where the title is in the
      page title
- [ ] Known limit, write it in the UI: this is *browser* watch history. The YouTube
      app on a phone/TV and account-level history are not visible. Covering those
      needs Google Takeout or the YouTube Data API with the child's account consent —
      decide later, do not silently half-do it

#### Day 30 — Nightly rollup + retention (2–3 hours)

**Why:** the dashboard must not aggregate months of raw rows on every page load, and
raw visit rows are the most sensitive data in the system.

```sql
CREATE TABLE IF NOT EXISTS progress_daily (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id       INTEGER NOT NULL,
  day             TEXT NOT NULL,          -- '2026-09-02'
  category        TEXT NOT NULL,          -- education | creative | social | gaming | video | other
  seconds         INTEGER NOT NULL DEFAULT 0,
  visits          INTEGER NOT NULL DEFAULT 0,
  blocked_hits    INTEGER NOT NULL DEFAULT 0,
  UNIQUE(device_id, day, category)
);
```

- [ ] Rollup job at 00:15 local: fold yesterday's `usage_sessions` + `web_visits` +
      blocked alerts into `progress_daily`
- [ ] Retention: raw `web_visits` 30 days, `usage_sessions` 90 days, `progress_daily`
      forever (it is tiny). Reuse the `db.js` cleanup timer that fixed BUG-01
- [ ] `wal_checkpoint(TRUNCATE)` after each rollup — same discipline as BUG-01

---

### 🧮 WEEK 7 — Progress Scoring & the Progress Page

**Goal:** turn the collected rows into something a parent understands in five seconds.
Deterministic maths only — still no AI. If the numbers are not trustworthy on their own,
an AI summary on top of them is just confident nonsense.

#### Day 31–32 — Categorisation engine (4–5 hours)

**Why:** "3h 20m on chrome.exe" tells a parent nothing. "2h education, 1h video" does.

```sql
CREATE TABLE IF NOT EXISTS activity_categories (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  match_type TEXT NOT NULL,     -- 'domain' | 'app'
  pattern    TEXT NOT NULL,     -- 'khanacademy.org' | 'code.exe'
  category   TEXT NOT NULL,     -- education | creative | social | gaming | video | other
  weight     INTEGER DEFAULT 0, -- -2 distraction .. +2 productive
  source     TEXT DEFAULT 'builtin', -- builtin | parent | ai
  UNIQUE(match_type, pattern)
);
```

- [ ] Ship a builtin seed list (~150 rows): khanacademy/wikipedia/docs/github →
      education; scratch/blender/gimp/vscode → creative; youtube/netflix → video;
      roblox/steam/minecraft → gaming; instagram/tiktok/snapchat → social
- [ ] Match order: exact app → exact domain → registrable-domain suffix → `other`.
      Reuse the `MIN_SUBSTRING_RULE_LEN` lesson from BUG-13: never substring-match
      short patterns
- [ ] Cache the resolved category per domain/app in memory — the same 40 domains
      repeat all day
- [ ] Parent override: `PUT /api/progress/categories` (`source='parent'` always wins
      over `builtin` and `ai`)
- [ ] YouTube is the hard case: classify by `media_title`, not by domain, and default
      to `video` rather than guessing `education`
- [ ] Keyword rules first (tutorial, lecture, class 10, physics, revision → education;
      gameplay, funny, prank, reaction → video). Only titles no rule matches go to the
      AI, in one batched call per night, and the answer is cached per `media_id` so a
      title is never classified twice

#### Day 33–34 — Progress score + API (4–5 hours)

**Why:** parents want one number that moves, plus the detail behind it.

Score (documented, boring, reproducible — no black box):
```
productive = education + creative seconds
passive    = video + social + gaming seconds
score      = round(100 * productive / (productive + passive))   # 0 when no data
```

- [ ] `GET /api/progress/:deviceId/summary?range=7d` → score, per-category seconds,
      top 10 apps, top 10 domains, day-by-day series, blocked-attempt count
- [ ] `GET /api/progress/:deviceId/day/:day` → the same shape for one day
- [ ] Compare against the previous equal-length window and return the delta
- [ ] `authMiddleware` on every progress route (this is the most sensitive data in the
      product — treat it like the screenshot routes should be treated, per BUG-07)

#### Day 35 — Progress page in the dashboard (4–5 hours)

**Why:** the data is useless until it is one click from the Live View.

- [ ] Add "📈 Progress" to the sidebar nav in `web-ts/main.js`
- [ ] Header: big score ring + delta vs last week ("+8 vs last week")
- [ ] Stacked bar per day for the last 14 days, coloured by category
- [ ] Two side-by-side lists: "Time well spent" and "Time sinks", each with hours
- [ ] Streak row: consecutive days with score ≥ target
- [ ] Empty state must say *why* it is empty ("no usage recorded yet — is the agent
      online?") and link to the device page, not just render a blank chart
- [ ] Escape every app name and page title with the fixed `esc()` (BUG-11) — page
      titles are attacker-controlled text arriving from the child's device

---

### 🤖 WEEK 8 — AI Insights, Goals & Reports

**Goal:** a short weekly narrative a parent can read in 30 seconds, plus goals so the
number means something. Decision (locked): **cloud AI API for all analysis** — the Pi
does no inference.

#### Day 36–37 — AI insight generation (5–6 hours)

**Why:** parents do not want to interpret a chart; they want "maths practice is up,
late-night video is up too".

```sql
CREATE TABLE IF NOT EXISTS ai_insights (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id   INTEGER NOT NULL,
  period      TEXT NOT NULL,      -- 'week:2026-W36'
  headline    TEXT,
  body        TEXT,
  model       TEXT,
  input_hash  TEXT,               -- skip the call when nothing changed
  created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(device_id, period)
);
```

- [ ] `parent-dashboard/server-ts/services/ai.js` — one module, one entry point,
      provider chosen by `.env`. API key **server-side only**; it must never reach the
      web bundle
- [ ] Send **aggregates, not raw logs**: category totals, top domains, top apps, hour
      histogram. No screenshots, no full URLs, no page titles by default
- [ ] `AI_SEND_TITLES=false` by default; document exactly what leaves the network when
      it is switched on
- [ ] Hash the aggregate input; if the hash matches the stored `input_hash`, reuse the
      cached insight instead of paying for an identical call
- [ ] Budget guard: `AI_MAX_CALLS_PER_DAY` (default 10) and a hard token cap per call
- [ ] Degrade gracefully — on any AI error the Progress page still renders the
      deterministic numbers with the AI card marked unavailable

#### Day 38 — Goals & nudges (3–4 hours)

**Why:** a score with no target is trivia. A target turns it into progress.

```sql
CREATE TABLE IF NOT EXISTS progress_goals (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id   INTEGER NOT NULL,
  kind        TEXT NOT NULL,   -- 'min_education_minutes' | 'max_category_minutes' | 'min_score'
  category    TEXT,
  target      INTEGER NOT NULL,
  window      TEXT DEFAULT 'day',   -- day | week
  active      INTEGER DEFAULT 1
);
```

- [ ] Goals editor on the Progress page (start with three goal kinds, no more)
- [ ] Evaluate goals during the nightly rollup; write a `progress_goal` alert row on a
      miss so it lands in the existing alerts UI
- [ ] Optional child-side nudge: reuse the existing `send_child_message` overlay for
      "30 minutes of reading left today" — encouragement, not scolding
- [ ] Keep nudges rate-limited to 1/hour so the overlay never becomes noise

#### Day 39 — Weekly progress report (3–4 hours)

**Why:** Week 5 already builds a PDF pipeline; progress is the section that makes it
worth opening.

- [ ] Extend `GET /api/reports/:deviceId/weekly` with a Progress section: score trend,
      category split, top education + top distraction, the AI narrative, goal results
- [ ] Add a "📈 Progress" block to the report layout above the screenshot thumbnails
- [ ] Optional weekly email using the Week 4 nodemailer setup (subject line = headline)

#### Day 40 — QA, privacy pass & sign-off (3–4 hours)

- [ ] Verify a full day of real Pi data: agent restarts mid-day, laptop sleeps, Wi-Fi
      drops — no double-counted sessions, no lost spool file
- [ ] Verify the offline spool replays after a 30-minute outage and totals stay correct
- [ ] Verify score maths by hand against one day of raw rows (spreadsheet check)
- [ ] Confirm DB growth: `usage_sessions` + `web_visits` under 50 MB after 30 days
- [ ] Privacy review, written down in this file:
      - what is collected, retention per table, who can read it
      - exactly what is sent to the AI provider (and the toggle that changes it)
      - a documented "delete this child's history" action
- [ ] Age-appropriate framing: the Progress page describes activity, it does not grade
      the child. Wording matters more than the chart here

---

**Roadmap dependency chain (do not reorder):**
`liveness fixes deployed` → `Week 6 collection` → `Week 7 scoring + page` →
`Week 8 AI + goals + report`. Week 7 needs at least 3 days of Week 6 data before the
page shows anything meaningful; Week 8's AI narrative needs a full week.





## 📌 QUICK REFERENCE — WHEN TO DO WHAT

| Urgency | Action |
|---------|--------|
| **RIGHT NOW** | Deploy the 2026-09-02 liveness fixes to the Pi (`GUARD.bat deploy`) and rebuild web-ts, then confirm: Apps page responds, mirror is live, device stays online |
| **TODAY** | Fix BUG-03 (.env secrets + admin123), add .gitignore entries |
| **THIS WEEK** | Remaining Week 1 items — delete dead files, BUG-06/07 auth hardening, BUG-09 |
| **THIS MONTH** | Week 2–3: Dashboard redesign, screenshot timeline, screen-time scheduler |
| **NEXT MONTH** | Week 4–5: Email alerts, remote lock, PDF reports, blocklist presets |
| **AFTER THAT** | Week 6–8: child progress tracking (collect → score → AI insight) |
| **FUTURE** | Phase 3: AI moderation, multi-parent, mobile apps |

---

## ⚠️ NEVER DO THESE

1. **Never push `.env` to GitHub** — regenerate all secrets if it ever happens
2. **Never set `server_url` to `localhost` in Pi's `config.json`** — `localhost` on Pi means the Pi itself, not Windows
3. **Never let the DB grow beyond 500 MB** — you're already at 1.28 GB
4. **Never run `start-live-control.bat` in production** — it's for local demo only
5. **Never modify the system hosts file without admin/root** — the blocker silently fails without it
6. **Never delete `guardian_watchdog.py`** — it's the only thing that keeps the agent alive on Pi
7. **Never do slow work inside a python-socketio handler on the agent** — handlers run
   inline on the read loop, so a slow one stops PONG replies and the device goes
   "offline". Dispatch with `self._run_off_read_loop(...)`
8. **Never hold `AppTracker._lock` across a psutil walk or a kill** — snapshot the rules,
   release the lock, then act. That was the deadlock behind all three 2026-09-02 failures
9. **Never use `io.emit(...)` for parent data in server-ts** — it reaches agents and
   anonymous sockets too. Use `emitToViewers(...)`
10. **Never run two watchdogs for one agent** — the systemd unit must pass
    `--no-watchdog`, or two agents share one device_name and flap each other offline

---

*This is the single source of truth. Append all future decisions here.*
