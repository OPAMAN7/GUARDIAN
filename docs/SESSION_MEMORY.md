# GUARDIAN — SESSION MEMORY FILE
# Show this file at the start of every new session.
# It tells the AI exactly where we left off and what to do next.

---

## CURRENT STATUS
- Full codebase scan: DONE (2026-08-20), re-audited 2026-09-02
- Bug list created: DONE — BUG-01..09 plus BUG-10..15 from the 2026-09-02 audit
- Fixes applied so far: BUG-01, BUG-02, BUG-04, BUG-05, BUG-08, BUG-10, BUG-11,
  BUG-12, BUG-13, BUG-14 DONE (BUG-15 partial), BUG-16..22, BUG-23..27 (deploy chain),
  BUG-28 (blank live view), BUG-29 (stale dashboard bundle)
- The three reported live failures (Apps page dead, mirror frozen, device flapping
  offline) all traced to BUG-10 + agent read-loop blocking. Code fixed and verified
  offline: 67/67 automated checks pass. NOT yet confirmed on real hardware.
- 2026-09-02, user follow-up: everything works EXCEPT screen mirroring — device online,
  live view blank. That is BUG-28 (one-shot Wayland probe under systemd), now fixed in
  pi-agent/agent/screenshot.py. Verified offline only.
- 2026-09-02, second follow-up ("I ran everything from the GUARDIAN bat file, still no
  result"): the remaining cause was **BUG-29 — web-ts/dist was built 2026-08-26 and
  predates the server's socket-auth handshake**, so the browser socket was rejected as
  anonymous and never received frames. Nothing rebuilt the bundle. GUARD.py now builds it.
- Next up: **`GUARD.bat build`, then hard-refresh the dashboard (Ctrl+Shift+R)** — without
  this the live view cannot work whatever the Pi does. Then `GUARD.bat deploy` (or menu 3)
  to ship screenshot.py, then `GUARD.bat diag` — its first line is the `Dashboard UI`
  freshness check; after that `CAPTURE_FAIL=0` + a non-empty `WAYLAND_SOCKETS` means the
  mirror should be live, and an empty `WAYLAND_SOCKETS` means no desktop session is logged
  in on the Pi. After that BUG-03 (.env secrets) → BUG-06/07 (auth hardening) → BUG-09.
- Child progress tracking: APPROVED, planning only. Roadmap = Weeks 6–8 in
  docs/GUARDIAN_MASTER_PLAN.md. Nothing built yet, by design.
- Pi available: YES — user pi, hostname aman-test. The IP/credentials live ONLY in
  windows-app/config/guard_runtime.json (`pi_ip`, `server_ip`); read them from there,
  never hardcode. Values as of 2026-09-02: Pi 192.168.1.15, dashboard host
  192.168.1.10:3000 (same subnet, on purpose).
- Deploy: `GUARD.bat deploy` uploads pi-agent/agent/* to the Pi, runs and verifies it.
  `GUARD.bat diag` (menu T) prints the Pi's real state, capture backend and journal tail
  — run it before believing any error message.
- The browser serves `parent-dashboard/web-ts/dist`, never `main.js`. `GUARD.bat build`
  rebuilds it and menu `1` does so automatically when it is stale — but if you edit
  frontend code and skip the build, the user sees none of it (BUG-29).

---

## BUG FIX QUEUE — Do these in order, one at a time

Mark [x] when done. Never skip ahead.

- [x] BUG-01 — DB is 1.28 GB, screenshots piling up — FIXED 2026-08-25
  Result: DB shrank from 1,285 MB to 3.3 MB (99.8% reduction)
  Changes made:
    - db.js: WAL checkpoint on startup + after every cleanup
    - db.js: retention lowered from 7 days to 3 days
    - db.js: cleanup runs every 30 min instead of 60
    - index.js: added DELETE /api/admin/cleanup?days=N endpoint
    - index.js: health check now shows screenshot_count and retention_days
    - Ran VACUUM manually: DB went from 1.28 GB to 3.3 MB

- [x] BUG-02 — import queue duplicated — FIXED 2026-08-26
  File: pi-agent/agent/main.py
  Fix: Deleted duplicate import line

- [ ] BUG-03 — .env has admin123 and weak JWT secret (SECURITY - DO FIRST)
  File: parent-dashboard/server-ts/.env
  Fix: Add .env to .gitignore, create .env.example, regenerate JWT secret, change admin password

- [x] BUG-04 — persistence.py references watchdog.py which does not exist — FIXED 2026-09-02
  File: pi-agent/agent/persistence.py line 204
  Fix applied: now resolves guardian_watchdog.py and bails out with a clear error
  if the file is missing, so a bad unit file is never written

- [x] BUG-05 — os.chdir() in Windows watchdog is thread-unsafe — FIXED 2026-09-02
  File: windows-app/watchdog/watchdog_server.py line 69
  Fix applied: functools.partial(_FileHandler, directory=...) + try/finally on
  server_close; removed the now-unused `import os`. Verified: file still serves
  and process cwd is no longer mutated.

- [ ] BUG-06 — No rate limiting on /change-password (SECURITY)
  File: parent-dashboard/server-ts/routes/auth.js line 138
  Fix: Apply same loginAttempts map used for login route
  Status 2026-09-02: CONFIRMED still open (awaiting approval — auth change)

- [ ] BUG-07 — Screenshot endpoints have NO auth middleware (SECURITY)
  File: parent-dashboard/server-ts/index.js lines 500 + 521
  Fix: Add authMiddleware to /api/screenshots/latest/:deviceId and /api/screenshots/history/:deviceId
  Status 2026-09-02: CONFIRMED still open (awaiting approval — auth change)

- [x] BUG-08 — Custom shutil_which() duplicates Python stdlib — FIXED 2026-09-02
  File: GUARD.py lines 363-373
  Fix applied: function deleted, both call sites now use shutil.which(); the SSH
  presence check also accepts PuTTY's plink

- [ ] BUG-09 — persistence.py hardcodes XAUTHORITY=/run/user/1000/.Xauthority
  File: pi-agent/agent/persistence.py line 36
  Fix: Detect UID of target user dynamically at install time
  Status 2026-09-02: CONFIRMED still open (behaviour-changing — not auto-fixed)

### New findings — fresh full audit 2026-09-02

- [x] BUG-10 — app_tracker deadlocks the agent the first time you block an app
  (CRITICAL) — FIXED 2026-09-02
  File: pi-agent/agent/app_tracker.py
  What happened: `self._lock` was a plain `threading.Lock` (non-reentrant).
  `set_blocked_apps()` took the lock and then called `_kill_blocked_processes()`
  while still holding it; that called `_is_blocked()`, which tried to take the
  same lock again. The thread hung forever — silently, since a deadlock raises
  nothing. This one defect explained ALL THREE reported failures: the hung thread
  was the socket read loop, so the agent stopped answering the server's pings
  (device went offline every few minutes) and stopped sending frames (mirror froze
  on the last image).
  Fix applied:
    - `self._lock = threading.RLock()` (reentrant)
    - the process sweep now runs OUTSIDE the lock — snapshot rules under the lock,
      release, then sweep. Holding a lock across a psutil walk was the real
      design error
    - every slow socket handler is now dispatched off the read loop (see main.py)
  Verified: 15/15 offline checks, including a reproduction of the original
  deadlock and 6 threads x 40 lock round-trips with no hang.

- [x] BUG-11 — esc() does not escape quotes, so app names are stored XSS (HIGH)
  — FIXED 2026-09-02
  File: parent-dashboard/web-ts/main.js
  Fix applied: `esc()` rewritten as an explicit five-way replace
  (`& < > " '`) instead of the textContent/innerHTML trick, so values are now safe
  inside double-quoted attributes. Also fixes `esc(0)` silently rendering empty.
  Verified: attribute break-out payload `x" onerror="alert(1)` no longer produces
  a stray quote (8/8 escape checks).

- [x] BUG-12 — Socket.IO connections are never authenticated (HIGH)
  — FIXED 2026-09-02
  File: parent-dashboard/server-ts/index.js (+ routes/auth.js exports verifyToken)
  Fix applied: `io.use()` classifies every socket as agent (shared secret), viewer
  (valid JWT) or anonymous. Authorized viewers join a `dashboard-viewers` room and
  ALL parent-facing broadcasts (`live_screenshot`, `device_telemetry`,
  `running_apps_updated`, `installed_apps_updated`, `new_alert`,
  `app_timer_changed`, `child_message_status`, `device_status_changed`) now emit to
  that room instead of `io.emit`. `stream_config` is rejected for unauthorized
  sockets. Connections are still ACCEPTED when anonymous — the login page needs a
  socket — they just receive nothing. A `dashboard_auth` event upgrades a socket
  after login without a reconnect. Kill switch: `SOCKET_AUTH_REQUIRED=false`.
  Verified: anonymous socket receives zero frames while an authorized viewer
  receives them; anonymous `stream_config` returns `{error:"Unauthorized"}`.

- [x] BUG-13 — Blocklist kills by substring with no protected-process list (HIGH)
  — FIXED 2026-09-02
  File: pi-agent/agent/app_tracker.py
  What happened: `_is_blocked` matched a rule anywhere inside the process name or
  window title, then killed the whole process tree — as root on the Pi. A rule
  like `python` or `system` would terminate the guardian agent itself or an OS
  process.
  Fix applied:
    - `NEVER_KILL_PROCESS_NAMES` allowlist (systemd, sshd, Xorg, labwc,
      wpa_supplicant, python/python3, lsass.exe, explorer.exe, ...)
    - `_is_protected_process()` also skips PID 1, our own PID and our own
      ancestors, so the agent can never kill itself
    - `MIN_SUBSTRING_RULE_LEN = 5`: rules shorter than 5 chars must match the
      executable base name exactly; only longer rules substring-match
    - both `_kill_blocked_processes()` and `close_app` go through the guard
  Verified: with `python` blocked, a full sweep leaves this interpreter alive;
  rule `ro` no longer matches `chrome`; `roblox` still matches
  `robloxplayerbeta.exe` and the window title "Roblox — play".
  NOTE: never fix BUG-10 without BUG-13. Fixing the deadlock is what makes the
  kill sweep actually run.

- [x] BUG-14 — SPA fallback swallows unknown /api routes (MEDIUM)
  — FIXED 2026-09-02
  File: parent-dashboard/server-ts/index.js
  Fix applied: unknown `/api/*` paths now return `404 {"error":"Not found"}` as
  JSON instead of index.html with status 200. Non-API routes still serve the SPA.
  Verified: `/api/does-not-exist` → 404 application/json; `/some/spa/route` → 200.

- [ ] BUG-15 — web-ts/dist is gitignored, so a fresh clone serves nothing (MEDIUM)
  File: .gitignore line 20 (`parent-dashboard/web-ts/dist/`)
  What happens: the server sends `web-ts/dist/index.html` for every page, but
  dist is not committed. On a fresh clone the dashboard is a blank page until
  someone runs `npm run build` in web-ts.
  Status 2026-09-02: PARTIAL — the server now returns a 503 with the exact build
  command instead of a blank page / opaque error. Still to do: run the build
  automatically from start-server.bat when dist/index.html is missing.

### Liveness defects found while fixing the three reported failures (2026-09-02)

These are why the device "went offline every few minutes" even between deadlocks.
All found by tracing the reported symptoms, all fixed the same day.

- [x] BUG-16 — No heartbeat existed at all — FIXED 2026-09-02
  Files: pi-agent/agent/main.py + parent-dashboard/server-ts/index.js
  What happened: `last_seen` was refreshed ONLY by the `screenshot` handler. A
  device that was alive but not streaming (or whose frames were being dropped)
  looked dead to the 20s stale sweeper.
  Fix: agent sends `heartbeat` every 5s (`HEARTBEAT_INTERVAL_SEC = 5.0`) from its
  own thread; server has a `heartbeat` handler that refreshes `last_seen`.
  Verified: heartbeat restores a device the sweeper had marked offline.

- [x] BUG-17 — `status='online'` was a one-way latch — FIXED 2026-09-02
  File: parent-dashboard/server-ts/index.js
  What happened: `online` was written only in `register_device`. Once the sweeper
  flipped a device offline, incoming frames could not restore it — only a full
  reconnect could. A 20-second hiccup became a long outage.
  Fix: `touchDeviceOnline()` helper, called from `heartbeat`, `screenshot` and
  `running_apps_report`; it restores `online` and broadcasts the change.

- [x] BUG-18 — Frames were broadcast to every socket including the agent
  — FIXED 2026-09-02 (same change as BUG-12)
  What happened: `io.emit("live_screenshot")` pushed ~4 MB/s at 20fps back to the
  Pi itself, starving the very read loop that had to answer pings.
  Fix: room-based delivery (`dashboard-viewers`); agents never receive frames.
  Verified: the agent does not receive its own frame.

- [x] BUG-19 — Stale sweeper changed the DB but told nobody — FIXED 2026-09-02
  File: parent-dashboard/server-ts/index.js
  What happened: the 20s sweeper set `status='offline'` without emitting
  `device_status_changed`, so the dashboard kept showing a LIVE badge over a dead
  frame. The UI and the DB disagreed until a page reload.
  Fix: the sweeper now selects the rows it is about to change and emits
  `device_status_changed` for each one.

- [x] BUG-20 — Two watchdogs, two agents, one device_name — FIXED 2026-09-02
  Files: pi-agent/agent/persistence.py, pi-agent/scripts/install-agent.sh, GUARD.py
  What happened: the generated systemd ExecStart had no `--no-watchdog`, so the
  service spawned its own watchdog alongside `guardianshield-watchdog.service`.
  Two supervisors could start two agents under one device_name; when the first
  socket finally timed out, its disconnect marked the LIVE device offline.
  Fix: `--no-watchdog` in the generated unit and in start.sh; GUARD.py sed-patches
  existing start.sh files and pkills stray watchdogs on deploy; the server's
  disconnect handler now ignores a stale socket (`socket_id` guard).
  Verified: a stale disconnect no longer flips a live device offline.

- [x] BUG-21 — Screenshot ack latch could freeze the stream forever
  — FIXED 2026-09-02
  File: pi-agent/agent/main.py
  What happened: `_send_inflight` was set before emitting a frame and cleared only
  by the server's ack callback. One lost ack stopped frames permanently, and the
  function returned True so nothing was even cached. It was dormant only because
  the deadlock stopped `update_config` from enabling low-latency mode — fixing the
  deadlock ARMED this bug.
  Fix: record `_send_inflight_at`; if no ack arrives within
  `SEND_ACK_TIMEOUT_SEC = 2.5`, log a warning, break the latch and send anyway.
  Fresh in-flight frames are still dropped, so backpressure is preserved.
  Verified: a stale latch is cleared and the frame sends; a fresh one still drops.

- [x] BUG-22 — Dashboard threw away decoded frames (the "static image")
  — FIXED 2026-09-02
  File: parent-dashboard/web-ts/main.js
  What happened: `frameRenderToken` discarded a decode that had FINISHED if a newer
  frame had merely STARTED decoding. At 20fps on a busy tab, every single frame
  qualified, so the canvas kept showing the first frame it ever painted.
  Fix: compare against `lastAppliedFrameToken` — paint any frame newer than what is
  already on screen, and discard only genuinely out-of-order late decodes.
  Verified by simulation: with decode slower than the frame interval the old logic
  painted 1/40 frames, the new logic paints 40/40, and out-of-order decodes are
  still rejected.

- [x] BUG-23 — `pkill -f guardian_watchdog.py` killed the SSH session running it
  — FIXED 2026-09-02
  File: GUARD.py (`_remote_config_patch`, now `_housekeeping_cmd`)
  What happened: the remote chain sent to the Pi contained a literal
  `pkill -f guardian_watchdog.py`. `pkill -f` matches against full command lines,
  and the remote shell's OWN command line contained that text — so the step
  SIGTERM'd the session executing it. Everything after it (`systemctl restart`)
  never ran, ssh returned non-zero, and GUARD printed
  "Failed to update Pi config.json / restart agent (check SSH+sudo password)"
  or "Remote configure/restart failed". The Pi never came back online because
  the restart was the step that got skipped.
  Fix: `pkill -f '[g]uardian_watchdog\.py'` — the bracketed regex cannot match the
  literal `[g]uardian_watchdog.py` sitting in our own argv, so only real strays die.
  Verified: the same housekeeping block previously exited -15 (SIGTERM) and left
  `__pycache__` in place; it now exits 0 and completes every step, across five
  different start.sh layouts.

- [x] BUG-24 — `systemctl is-active` exit code impersonated a config failure
  — FIXED 2026-09-02
  File: GUARD.py
  What happened: the remote chain ended with `... && sleep 2 && systemctl is-active`.
  A `;`-list returns its LAST command's status, and `is-active` exits 3 for a dead
  unit — so "the agent crashed on boot" and "the config patch failed" produced the
  identical message, pointing the user at the SSH/sudo password. `sleep 2` was also
  too short for a Pi that imports Pillow + socketio before it reports active.
  Fix: split into named phases (`probe_ssh_and_sudo` → `_cfg_patch_cmd` →
  `_housekeeping_cmd` → `_restart_cmd`), each with its own message. The restart
  phase always exits 0 and reports the truth through `GUARDIAN_SVC=` markers, polling
  up to 12s. A dead unit now dumps `journalctl -u guardian-agent -n 40`.
  Verified: simulated active/failed/activating — all exit 0, all report true state.

- [x] BUG-25 — sudo could never get a password; plink aborted on uncached host keys
  — FIXED 2026-09-02
  File: GUARD.py
  What happened: remote steps ran bare `sudo`. With `plink -batch`/`sshpass` there is
  no TTY, so on any Pi without NOPASSWD sudo it read EOF and failed. Separately,
  `plink`/`pscp -batch` abort outright when the host key is not cached in the registry.
  Fix: `sudo_preamble()` seeds the sudo credential cache once per session
  (`printf '%s\n' <pw> | sudo -S -p '' -v`), later calls use `sudo -n` so they fail
  fast instead of hanging; `_prime_host_key()` caches the host key once per IP.
  Verified: password with quotes, `$`, backticks and `;` survives shell quoting
  byte-for-byte into sudo's stdin.

- [x] BUG-26 — unbounded `pip install -r` inside the config step
  — FIXED 2026-09-02
  File: GUARD.py
  What happened: the dependency refresh shared one SSH call with the config patch.
  On a Pi joined to the Windows hotspot with no internet route, pip's retries
  outlived the 180s SSH timeout, so a *network* condition surfaced as an SSH/sudo error.
  Fix: own phase, `--timeout 8 --retries 1`, reports ok/skipped/novenv, never fatal.

- [x] BUG-27 — deploy reported failure when only the local dashboard was down
  — FIXED 2026-09-02
  File: GUARD.py (`verify_pi_online`)
  What happened: verification returned `svc_ok and dash_ok`, so a perfectly deployed
  Pi failed the deploy whenever port 3000 was not already listening on Windows.
  Fix: the Pi service decides pass/fail; a silent dashboard is now a warning.

- [x] BUG-28 — live view stayed blank while the device reported online
  — FIXED 2026-09-02
  File: pi-agent/agent/screenshot.py (+ GUARD.py diagnostics)
  What happened: `ScreenshotCapture.__init__` probed for a Wayland compositor once and
  cached the answer. Under systemd the agent starts at boot, before the desktop session
  creates `/run/user/<uid>/wayland-0`, so it cached `None` and `capture()` skipped the
  Wayland branch for the entire process lifetime; every frame fell through to `scrot`,
  which cannot capture Wayland. The heartbeat loop is independent of capture, so the
  dashboard kept showing the device online with no frames — the symptom was "online,
  mirror never paints, waiting does not help". Exposed by the BUG-23 fix, which made
  `systemctl restart` actually run and moved the agent out of a live desktop session.
  Fix: `_refresh_wayland_target()` re-probes from `capture()` while no backend is
  working, throttled by `WAYLAND_REDETECT_SEC = 5.0`, and drops a target that stops
  producing frames so a new session is adopted instead of a dead one retried forever.
  `GUARD.bat diag` now prints GRIM / SCROT / WAYLAND_SOCKETS / CAPTURE_FAIL.
  Regression test: `python3 pi-agent/agent/tests/test_wayland_redetect.py` (fakes grim,
  needs no Pi and no network; 7 checks).

- [x] BUG-29 — the dashboard served a week-old bundle, so the live view could never work
  — FIXED 2026-09-02
  File: GUARD.py (bundle freshness + build), parent-dashboard/web-ts/main.js (toast)
  What happened: after the BUG-28 fix the user re-ran everything from GUARD.bat and the
  mirror was still blank, with the toast "No target device received stream settings".
  `web-ts/dist/assets/index-B-c3DfGv.js` was built 2026-08-26 21:48; `main.js` and
  `server-ts/index.js` are 2026-09-02. `server-ts/index.js` serves `web-ts/dist`
  statically, so the browser was running August code. In between, the server gained a
  Socket.IO auth handshake (`classifySocket`, `SOCKET_AUTH_REQUIRED` defaults to true) —
  the old bundle sends no token, so its socket was classified `anonymous`, never joined
  the `dashboard-viewers` room, and never received one `live_screenshot`. Proof from the
  artifacts themselves: `grep -c dashboard_auth dist/assets/index-B-c3DfGv.js` → **0**,
  same grep on `main.js` → 1; likewise `auth:` (the handshake option) 0 vs 1. The device list
  and active-app pill still looked healthy because they come over REST, which made it look
  like a Pi-side capture problem. `stream_config` was rejected the same way, producing
  `delivered: 0` and that toast. Nothing ever rebuilt the bundle: `start-server.bat`
  installs *server* dependencies only, and GUARD.py had no build step.
  Fix: `web_bundle_state()` compares the newest source mtime with the newest `dist/` mtime
  (1 s jitter grace) and returns `nosrc`/`missing`/`stale`/`fresh`; `build_web_dashboard()`
  runs `npm install` (only when `node_modules` is missing) then `npm run build`; menu `1`
  builds before launching; `GUARD.bat build [force]` is a subcommand; `status` and `diag`
  both print a `Dashboard UI:` line. The toast for an `Unauthorized` reply now blames the
  dashboard and says to rebuild, instead of blaming the device.
  Lesson: a stale bundle is invisible — the page renders, REST works, and only the socket
  contract silently breaks. Check bundle freshness *first* when a socket feature is dead.

---

## SESSION FIX NOTE — 2026-09-02 (GUARD option 3)

Option 3 "Launch Pi Agent Sync" used to call `recalibrate_pi_sync()`, which only
patched `server_url` and restarted the unit. It never uploaded code and never ran
the installer, so on a fresh or half-installed Pi it restarted a unit that did not
exist. Option 3 now runs the full `deploy_pi_agent()` — upload every file in
`pi-agent/agent/`, bootstrap the installer if `start.sh`/`venv` are missing, patch
`server_url`, restart, verify — and falls back to config-only recalibration only if
the full sync fails. Option 4 remains the light config-only path.
New diagnostics: `GUARD.bat diag` (also menu **T**) prints install state, service
state, enabled state, running `main.py` count, `server_url`, and the journal tail.

---

## SESSION FIX NOTE — 2026-09-02 (blank live view, BUG-28)

Reported symptom: the dashboard shows the device **online**, but the live view never
paints, and leaving it running does not help.

Cause: `ScreenshotCapture.__init__` probed once for a Wayland compositor and cached the
result. Started by systemd at boot, the agent runs *before* the desktop session creates
`/run/user/<uid>/wayland-0`, so the probe cached `None` and `capture()` skipped the
Wayland branch for the whole process lifetime — every frame fell through to `scrot`,
which cannot capture Wayland and returns nothing. The heartbeat loop is independent of
capture, so the device kept reporting online with no frames. This only surfaced now
because the BUG-23 fix made `systemctl restart` actually run, moving the agent out of a
live desktop session and into systemd.

Fix (`pi-agent/agent/screenshot.py`): detection moved into `_refresh_wayland_target()`,
re-run from `capture()` whenever no backend is working, throttled by
`WAYLAND_REDETECT_SEC = 5.0`; a target that stops producing frames is dropped so a new
session is picked up instead of a dead one being retried forever.

Diagnosis aid: `GUARD.bat diag` now prints `GRIM`, `SCROT`, `WAYLAND_SOCKETS` and a
`CAPTURE_FAIL` count from the journal, and says in plain language when the service is up
but capture is dead. Empty `WAYLAND_SOCKETS` = no desktop session is logged in on the Pi,
so there is nothing to capture — that is a Pi-side login problem, not an agent bug.

---

## FILE DELETE QUEUE — Do after BUG-03 gitignore is set up

- [ ] Delete guardian_wayland_test.png (root) — test screenshot 1.4MB
- [ ] Delete latest_aman_test.jpg (root) — test photo 44KB
- [ ] Delete stitch/ folder — orphaned UI prototype never integrated
- [ ] Delete parent-dashboard/shared-types/ — empty placeholder folder
- [ ] Delete windows-app/config/local-agent-config.runtime.json — temp duplicate
- [ ] Delete windows-app/config/local-agent-config.verify.json — temp duplicate
- [ ] Add to .gitignore: *.pem, *.key, warning_counts.json, kill_attempts.txt, __pycache__/, *.db, *.db-shm, *.db-wal

---

## FEATURE BUILD QUEUE — Do these after all bugs are fixed

### Phase 1 — Complete the Core (no Pi needed for most)
- [ ] F-01 Screenshot history/timeline viewer in dashboard UI
- [ ] F-02 Multi-device selector UI (backend supports it, UI does not)
- [ ] F-03 Alert sounds on parent dashboard browser
- [ ] F-04 Blocklist category presets (Social / Gaming / Video one-click adds)
- [ ] F-05 Scheduled screen-time limits (new DB table + schedule enforcer on agent)

### Phase 2 — High Value (some need Pi)
- [ ] F-06 Dashboard GUI full overhaul — dark theme, CSS tokens, premium look
- [ ] F-07 Mobile-responsive dashboard
- [ ] F-08 Email alerts via nodemailer (malware, 3x block attempt triggers)
- [ ] F-09 Screenshot retention setting in dashboard UI
- [ ] F-10 Remote lock screen command (block_screen.py exists — just needs a socket trigger)

### Phase 3 — Future Vision
- [x] F-11 App usage tracking & management — COMPLETED (2026-08-26)
  Features delivered:
  - Live foreground/active app tracking with window title & duration
  - Real-time running user processes table with CPU%, RAM, uptime
  - Installed applications library scanner (200+ apps on Windows & Linux)
  - Application blocklist with instant auto-kill enforcement
  - Anonymous instant process termination ("Close App Now")
  - App time limits & countdown timers with auto-close & warnings
  - Direct message delivery to child's screen with styled Tkinter overlay & notifications
  - Dedicated "Apps" sidebar page in dashboard + Live View active app badge
- [ ] F-12 AI content moderation via Gemini Vision API
- [ ] F-13 Multi-parent login with roles
- [ ] F-14 Weekly PDF activity report export
- [ ] F-15 Android/iOS monitoring expansion

### Phase 4 — Child Progress Tracking (APPROVED 2026-09-02 — planning only, do NOT start)

Full day-by-day roadmap: **Weeks 6–8 in docs/GUARDIAN_MASTER_PLAN.md**.
Locked decisions: browser history is read from the browser's own SQLite DBs on the
device (no extension); ALL analysis runs through a cloud AI API (the Pi does no
inference). Categories: education, creative, social, gaming, video, other.

- [ ] F-18 Foreground app usage sessions — persist active-app time (`usage_sessions`)
- [ ] F-19 Web history collection — Chromium `History` + Firefox `places.sqlite`,
      copy-then-read, per-browser watermark, offline spool (`web_visits`)
- [ ] F-20 YouTube / platform watch history — video URL + title + dwell time, own
      category rules (a maths lecture and a prank video are both youtube.com)
- [ ] F-21 Categorisation engine + progress score (`activity_categories`,
      `progress_daily`, nightly rollup, parent overrides)
- [ ] F-22 Progress dashboard page — score ring, 14-day stacked trend, top lists
- [ ] F-23 AI insight section — weekly narrative from aggregates only, cached by
      input hash, daily call budget, degrades to plain numbers on failure
- [ ] F-24 Goals & nudges — targets per day/week, misses raise a normal alert, optional
      encouraging overlay on the child's screen (rate-limited)
- [ ] F-25 Progress section in the weekly PDF/email report

**Dependency:** F-21 needs ~3 days of F-18/F-19/F-20 data before it shows anything
real; F-23 needs a full week. Do not build the AI section first — it will hallucinate
over an empty table.

### Tooling / launcher

- [x] F-16 One-command Pi deploy from the launcher — COMPLETED (2026-09-02)
      `GUARD.bat deploy` (or menu option D) uploads every file in
      pi-agent/agent/ to /opt/guardian-agent, runs install-agent.sh first if the
      Pi has no venv/start.sh yet, patches server_url, restarts the service and
      verifies it. config.json is deliberately NOT uploaded — the Pi keeps its
      own device_name/auth_token and only server_url is rewritten.
- [x] F-17 Single-source IP config — COMPLETED (2026-09-02)
      windows-app/config/guard_runtime.json holds server_ip/server_port.
      "auto" picks the local IPv4 on the Pi's /24; a fixed value is used
      verbatim. Every deploy/recalibrate pushes it to pi-agent/agent/config.json
      and to the live Pi, and keeps the Windows agent on localhost.
      CLI: `GUARD.bat ip <addr|auto> [port]`, `GUARD.bat verify`, `GUARD.bat status`.

---

## KEY FACTS TO REMEMBER

Project root:         C:\GUARDIAN
Parent server:        parent-dashboard/server-ts/index.js  (Node.js, port 3000)
Parent dashboard:     parent-dashboard/web-ts/main.js      (Vanilla JS SPA)
Pi agent entry:       pi-agent/agent/main.py               (Python)
App tracker module:   pi-agent/agent/app_tracker.py        (Python)
Master launcher:      GUARD.bat  →  GUARD.py                (menu + subcommands)
Launcher commands:    GUARD.bat [start|stop|status|deploy|verify|diag|ip|help]
                      Menu: 1 dashboard, 2 windows agent, 3 FULL Pi sync,
                      4 config-only recalibrate, D deploy, I set server IP, T diag
Windows watchdog:     windows-app/watchdog/watchdog_server.py
DB location:          parent-dashboard/server-ts/data/guardian.db (3.3 MB)
Pi hostname:          aman-test (User: pi) — IP comes from guard_runtime.json, do not
                      hardcode it; it was 192.168.1.15 on 2026-09-02
Windows host IP:      from guard_runtime.json `server_ip`:`server_port` (the address
                      agents connect to) — 192.168.1.10:3000 on 2026-09-02
IP source of truth:   windows-app/config/guard_runtime.json (managed by GUARD.py)
Pi install dir:       /opt/guardian-agent, systemd unit guardian-agent.service
Screen capture:       grim (Wayland) → scrot (X11) → mss. "Device online" says nothing
                      about capture — check `GUARD.bat diag`.
Default admin login:  admin / admin123  CHANGE THIS (BUG-03)

---

## RULES — Never break these

1. Never push .env to GitHub — secrets are in there
2. Never set Pi config.json server_url to localhost — localhost on Pi means Pi itself not Windows
3. Never run start-live-control.bat in production — demo mode only
4. Never modify hosts file without admin/root — blocker silently fails
5. Never delete guardian_watchdog.py — it keeps the Pi agent alive
6. Never hand-edit pi-agent/agent/config.json server_url — GUARD.py owns it
   (menu D = Deploy, I = Set Server IP). Hand edits are overwritten.
7. Never do slow work inside a python-socketio handler on the agent — python-socketio
   runs handlers inline on its read loop, so anything slow stops PONG replies and the
   server marks the device offline. Use `self._run_off_read_loop("name", fn)`.
8. Never hold AppTracker._lock across a psutil walk or a process kill. Snapshot the
   rules under the lock, release, then act. This was BUG-10.
9. Never use `io.emit(...)` for parent-facing data in server-ts — it reaches agents and
   unauthenticated sockets too. Use `emitToViewers(...)` (the dashboard-viewers room).
10. Never add a blocklist rule shorter than 5 characters expecting substring matching —
    short rules match the executable base name exactly, on purpose (BUG-13).
11. Never put a bare `pkill -f <pattern>` in a command chain sent over SSH — `-f` matches
    the carrier shell's own argv, so the step kills its own session and every later step
    in the chain silently never runs. Bracket the first character:
    `pkill -f '[g]uardian_watchdog\.py'`. This was BUG-23.
12. Never cache a capture-backend probe for the lifetime of the process. systemd starts
    the agent at boot, before the desktop compositor has created
    `/run/user/<uid>/wayland-0`, so a one-shot probe caches "no Wayland" forever and every
    frame falls through to `scrot`, which cannot capture Wayland. Re-probe (throttled)
    whenever no backend is working. This was BUG-28.
13. Never treat "device online" as proof that mirroring works — the heartbeat is
    deliberately independent of the capture path, so a device with a totally dead
    screenshot backend still reports online. Check `GUARD.bat diag` (`CAPTURE_FAIL`,
    `GRIM`, `WAYLAND_SOCKETS`), not the dashboard badge.
14. Never debug a socket feature before checking that `parent-dashboard/web-ts/dist` is
    newer than the code you changed. The browser loads `dist`, never `main.js`, so an
    un-rebuilt bundle can predate a server-side socket contract (the auth handshake) and be
    rejected silently while every REST-driven part of the page keeps working. Run
    `GUARD.bat build` — the `Dashboard UI:` line in `GUARD.bat status` / `diag` says whether
    it is stale. This was BUG-29, and it hid the BUG-28 fix for a whole round of testing.

---

## SESSION LOG

| Date       | What was done                                                                    |
|------------|----------------------------------------------------------------------------------|
| 2026-08-20 | Full codebase scan. All bugs and features identified.                            |
| 2026-08-25 | BUG-01 FIXED: Cleaned up bloated DB from 1.28 GB to 3.3 MB (99.8% reduction).     |
| 2026-08-26 | FEATURE F-11 COMPLETE: Live App Tracking, App Blocking, Timers & Child Messages. |
| 2026-09-02 | Fresh full audit. BUG-03..09 re-confirmed; BUG-10..15 newly found (BUG-10 app_tracker deadlock reproduced). |
| 2026-09-02 | BUG-04, BUG-05, BUG-08 FIXED + verified (py_compile + functional test of the watchdog file server). |
| 2026-09-02 | F-16 DONE: GUARD launcher gained Pi deploy (menu D) — uploads all of pi-agent/agent/, first-time-installs if needed, patches server_url, restarts + verifies the service. |
| 2026-09-02 | F-17 DONE: single-source IP config. guard_runtime.json server_ip/server_port drive the Pi config and the local agent config; GUARD.bat gained deploy/verify/ip/status commands. Aligned host=192.168.1.10, pi=192.168.1.15. |
| 2026-09-02 | User reported 3 live failures (Apps page dead except messages, mirror = static image, device offline every few minutes). Root-caused ALL THREE to one defect: BUG-10, a self-deadlock on AppTracker._lock, triggered ~1 RTT after every agent start because the server always emits update_blocked_apps on register. Reproduced the lock pattern standalone. |
| 2026-09-02 | BUG-10, 11, 12, 13, 14 + BUG-16..22 FIXED. Agent: RLock, sweep outside the lock, never-kill allowlist, 5s heartbeat, ack-latch timeout, all slow socket handlers dispatched off the read loop. Server: socket auth + viewer room, per-platform fps (Pi 5 / Windows 20), online-status restore, sweeper broadcasts, stale-disconnect guard, /api 404. Web: esc() quote escaping, frame-token fix. Verified offline: 67/67 checks (15 app_tracker, 19 main.py, 14 web, 15 server, 4 fps). NOT yet verified on hardware. |
| 2026-09-02 | PLANNER UPDATED for child progress tracking: GUARDIAN_MASTER_PLAN.md gained Phase 4 + Weeks 6–8 (collect → score → AI insight → report), SESSION_MEMORY gained F-18..F-24. Deliberately NOT started — user asked to plan first and verify the fixes. |
| 2026-09-02 | User reported option 3 and `GUARD.bat deploy` both failing ("check SSH+sudo password" / "Remote configure/restart failed") with the Pi never coming online. Root cause BUG-23: the remote chain's own `pkill -f guardian_watchdog.py` matched the SSH session's command line and SIGTERM'd it before `systemctl restart` ran. BUG-24..27 fixed alongside (is-active exit 3 masquerading as a config failure, sudo with no TTY, uncached plink host key, unbounded pip, deploy failing on a quiet local dashboard). Option 3 is now a FULL sync (upload + install-if-needed + restart + verify) and `GUARD.bat diag` / menu T dump the Pi's real state plus the journal tail. Verified offline: remote scripts executed locally against a stubbed sudo/systemctl/journalctl Pi — config patch preserves auth_token, survives a corrupt config.json, sed is idempotent across 5 start.sh layouts, restart reports active/failed/activating truthfully, all four failure branches print their own message. NOT yet run against the real Pi. |
| 2026-09-02 | User reported everything working EXCEPT screen mirroring — device shows online, live view never paints, waiting does not help. Root cause BUG-28: `ScreenshotCapture.__init__` probed for a Wayland compositor exactly once; under systemd the agent starts before `/run/user/<uid>/wayland-0` exists, so it cached `None` and every frame for the rest of the process fell through to `scrot`, which cannot capture Wayland. The independent heartbeat kept the device "online", hiding it. Only surfaced now because the BUG-23 fix made `systemctl restart` actually run, moving the agent from a desktop session into systemd. FIXED in `pi-agent/agent/screenshot.py`: `_refresh_wayland_target()` re-probes from `capture()` while no backend works (5s throttle) and drops a session that stops producing frames. `GUARD.bat diag` now reports `GRIM`, `SCROT`, `WAYLAND_SOCKETS`, `CAPTURE_FAIL` and explains a live service with dead capture. `docs/MASTER_AI.md` rewritten: no hardcoded IP anywhere, new "Network Info — one source of truth" section for guard_runtime.json, full GUARD.bat command + menu map, diag marker table, rules 1-11. Verified offline: 7/7 checks (frame appears once the socket does; pre-fix semantics return None forever), py_compile clean. NOT yet run against the real Pi. |
| 2026-09-02 | User re-ran everything from GUARD.bat and the mirror was STILL blank, toast: "No target device received stream settings". Root cause BUG-29 — nothing to do with the Pi: `web-ts/dist` was built 2026-08-26 21:48 while `main.js` / `server-ts/index.js` are 2026-09-02, and the server serves `dist` statically. The August bundle predates the socket auth handshake added in the BUG-16..22 round, so with `SOCKET_AUTH_REQUIRED` defaulting to true the browser socket was classified `anonymous`, never joined `dashboard-viewers`, and received zero `live_screenshot` frames; `stream_config` was rejected the same way (`delivered: 0` → that toast). REST-driven UI (device list, active-app pill) kept working, which made it look like a capture bug. Nothing rebuilt the bundle — `start-server.bat` installs server deps only. FIXED in GUARD.py: `web_bundle_state()` (nosrc/missing/stale/fresh, 1s jitter grace), `build_web_dashboard()` (npm install only if node_modules is absent, then `npm run build`), menu `1` builds before launching, new `GUARD.bat build [force]`, and a `Dashboard UI:` line at the top of `diag` and the end of `status`. `main.js`: the `Unauthorized` toast now blames the dashboard and says to rebuild instead of blaming the device. Verified offline: 10/10 checks against a temp tree with a stubbed npm (all four states, successful build, no-op when fresh, failing build, missing npm), and the check against the real tree correctly reports it stale. The real Vite build could NOT run here (npm registry blocked, only the Windows esbuild binary present) — it happens on the user's machine. |

---

Show this file at the start of every new session.
After completing a task update the checkboxes and session log before closing.
