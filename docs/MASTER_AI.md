# 🛡️ GUARDIAN SHIELD — MASTER AI SYSTEM GUIDE
> **CRITICAL INSTRUCTION FOR ALL AI AGENTS**:
> 1. **YOU MUST READ THIS FILE FIRST** at the start of every session. This is the single source of truth for the entire Guardian Shield repository.
> 2. **BEFORE YOU FINISH YOUR SESSION**, you **MUST UPDATE** the "LAST AI CHANGES & STATUS" section at the bottom of this file. Record exactly what you executed, whether any errors were encountered or left behind, and what the next AI must do.

---

## 1. 📖 What is Guardian Shield?

**Guardian Shield** is an advanced, multi-platform parental control, employee oversight, and cybersecurity monitoring suite. It consists of:
1. **Target Agent (`pi-agent/`)**: Lightweight Python daemon running on target devices (Raspberry Pi OS / Windows / Linux) that captures screen feeds, monitors active apps and processes, blocks unauthorized websites and applications, tracks screen time limits, inspects file downloads for malware, and displays parental notices directly on the desktop.
2. **Parent Dashboard (`parent-dashboard/`)**: High-performance Node.js Express server + Socket.IO + SQLite database with a glassmorphism dark-theme Single Page Application (SPA). Parents can watch live feeds, control active apps, set timers, send direct messages to the child screen, and configure domain/app blocklists.
3. **Supervisor / Watchdog (`windows-app/` & `guardian_watchdog.py`)**: Self-healing watchdog processes that guarantee 99.9% uptime by automatically respawning the agent if terminated.

---

## 2. 🌐 System Architecture & Connection Details

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│                              PARENT DASHBOARD (Node.js)                              │
│                Express REST API + Socket.IO Server (Port 3000)                       │
│                        Database: SQLite (guardian.db)                                │
│                   Web SPA: /web-ts (Vite / Vanilla JS + CSS)                         │
└────────────────────────────────────────▲─────────────────────────────────────────────┘
                                         │ WebSocket / HTTP
                                         ▼
┌──────────────────────────────────────────────────────────────────────────────────────┐
│                               TARGET DEVICE AGENTS                                   │
│  ┌────────────────────────────────────────┐ ┌──────────────────────────────────────┐ │
│  │     Raspberry Pi Agent (Debian 12)     │ │        Windows Desktop Agent         │ │
│  │   IP: auto-detected (never hardcode)   │ │   Path: c:\GUARDIAN\pi-agent\agent   │ │
│  │   Runs from: /opt/guardian-agent       │ │   Venv: \windows-app\.live-agent-venv│ │
│  │   Service: guardian-agent.service      │ │                                      │ │
│  └────────────────────────────────────────┘ └──────────────────────────────────────┘ │
│  Features: Live screen stream (grim/scrot/mss), AppTracker, Blocker, Timers, Popups  │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### 🔑 Network Info — one source of truth

Everything about the target lives in **`windows-app/config/guard_runtime.json`**, which
`GUARD.py` creates, reads and rewrites. Read that file for current values instead of
trusting any IP written in documentation, and change it through `GUARD.bat ip <addr>`
or menu **I** rather than by hand.

| Key | Meaning |
|-----|---------|
| `pi_hostname` | mDNS/hostname used to find the Pi when the cached IP is stale |
| `pi_username` / `pi_password` | SSH credentials (also used for `sudo` on the Pi) |
| `last_pi_ip` | Most recent Pi address GUARD successfully reached — a cache, not config |
| `last_host_ip` | Windows address the Pi was last told to connect back to |
| `server_ip` | `auto` = pick the Windows NIC on the Pi's subnet; or a fixed address |
| `server_port` | Dashboard port (`3000`) |
| `pi_install_dir` | `/opt/guardian-agent` — the directory systemd actually runs |
| `pi_service` | `guardian-agent` |

* **Pi agent directory**: `/opt/guardian-agent` (systemd `WorkingDirectory`). Copies
  under `/home/pi/...` are historical and are *not* what runs.
* **Parent dashboard**: `http://localhost:3000` on Windows; the Pi is given the LAN
  address of that host, never `localhost`.
* **Default dashboard login**: `admin` / `admin123`
* **Database**: `c:\GUARDIAN\parent-dashboard\server-ts\data\guardian.db`

---

## 3. 📂 Repository File Map

| Path | Purpose |
|------|---------|
| [`pi-agent/agent/app_tracker.py`](file:///c:/GUARDIAN/pi-agent/agent/app_tracker.py) | **Core App Engine**: Active foreground app tracking, running processes, 200+ installed apps scanner, app blocklist enforcement (auto-kill), countdown timers, and Tkinter screen popup overlays. |
| [`pi-agent/agent/main.py`](file:///c:/GUARDIAN/pi-agent/agent/main.py) | **Agent Orchestrator**: Socket.IO client, screenshot capture loop, telemetry collector, remote command dispatcher (`close_app`, `set_app_timer`, `send_child_message`, `update_blocked_apps`). The heartbeat runs in its own thread, independent of capture. |
| [`pi-agent/agent/blocker.py`](file:///c:/GUARDIAN/pi-agent/agent/blocker.py) | **Hosts File Blocker**: Modifies `/etc/hosts` or Windows `hosts` to redirect blocked domains to 127.0.0.1. |
| [`pi-agent/agent/block_page_server.py`](file:///c:/GUARDIAN/pi-agent/agent/block_page_server.py) | **Local Warning Server**: Serves warning pages (Level 1-3 & NUH UH) when blocked domains are accessed. |
| [`pi-agent/agent/screenshot.py`](file:///c:/GUARDIAN/pi-agent/agent/screenshot.py) | **Screen Capture**: JPEG-compressed frames with dynamic FPS/resolution scaling. Backend is chosen at runtime, in order: window capture (Windows), **`grim`** (Wayland — the Raspberry Pi OS Bookworm desktop default), `scrot` (X11/Xvfb), then `mss`. The Wayland target is re-probed while no backend works, because systemd can start the agent before the compositor exists. |
| [`pi-agent/agent/tests/test_wayland_redetect.py`](file:///c:/GUARDIAN/pi-agent/agent/tests/test_wayland_redetect.py) | **BUG-28 regression test.** Fakes `grim` and a compositor socket that appears late; proves `capture()` recovers instead of caching "no Wayland" forever. Runs anywhere — no Pi, no network. |
| [`pi-agent/agent/guardian_watchdog.py`](file:///c:/GUARDIAN/pi-agent/agent/guardian_watchdog.py) | **Agent Supervisor**: Monitors agent PID and auto-restarts if killed. |
| [`parent-dashboard/server-ts/index.js`](file:///c:/GUARDIAN/parent-dashboard/server-ts/index.js) | **Backend Entry Point**: Express + Socket.IO server, real-time broadcasts, screenshot ingestion. Sockets are classified at handshake (viewer / agent / anonymous); only viewers join the `dashboard-viewers` room, so frames are never echoed back at the agent. |
| [`parent-dashboard/server-ts/models/db.js`](file:///c:/GUARDIAN/parent-dashboard/server-ts/models/db.js) | **SQLite Database**: Table definitions, WAL checkpoint management, automated screenshot cleanup. |
| [`parent-dashboard/server-ts/routes/apps.js`](file:///c:/GUARDIAN/parent-dashboard/server-ts/routes/apps.js) | **Apps API**: Endpoints for running apps, installed apps catalog, app blocking, timers, and child messages. |
| [`parent-dashboard/web-ts/main.js`](file:///c:/GUARDIAN/parent-dashboard/web-ts/main.js) | **Frontend App**: SPA logic, Live View, Devices, Apps page, Blocklist, Rules, Alerts, and Modals. |
| [`parent-dashboard/web-ts/style.css`](file:///c:/GUARDIAN/parent-dashboard/web-ts/style.css) | **Design System**: Dark-theme glassmorphism tokens, active app hero card, timer strips, responsive grids. |
| [`windows-app/launcher/GuardianOneClick.cs`](file:///c:/GUARDIAN/windows-app/launcher/GuardianOneClick.cs) | **Windows One-Click Launcher**: C# tray app that launches watchdog, proxy, and dashboard. |
| [`windows-app/watchdog/watchdog_server.py`](file:///c:/GUARDIAN/windows-app/watchdog/watchdog_server.py) | **Windows Watchdog**: Supervises parent-dashboard process on Windows. |
| [`GUARD.bat`](file:///c:/GUARDIAN/GUARD.bat) | **Entry point for everything.** Thin wrapper: checks Python is on PATH, forwards args to `GUARD.py`. Subcommands: `deploy`, `verify`, `diag`, `ip <addr>`. |
| [`GUARD.py`](file:///c:/GUARDIAN/GUARD.py) | **Master CLI**: menu + Pi lifecycle. Detects the Pi, primes the SSH host key, seeds the sudo credential cache, uploads the agent, runs the installer, patches `server_url`, restarts and verifies the service, and starts/stops local dashboard + Windows agent. Every remote step is a named phase with its own error message. |
| [`windows-app/config/guard_runtime.json`](file:///c:/GUARDIAN/windows-app/config/guard_runtime.json) | **Single source of truth** for Pi credentials, install dir, service name, dashboard IP/port. Written by `GUARD.py`; never hardcode these values elsewhere. |
| [`docs/SESSION_MEMORY.md`](file:///c:/GUARDIAN/docs/SESSION_MEMORY.md) | **Running bug log** (BUG-01…). Root cause, fix and verification state per bug. Append here; do not re-diagnose an entry that is already closed. |

---

## 4. 🚀 Feature & Bug Tracker

### 🐛 Bug Fix Status

`BUG-10` and up live in [`docs/SESSION_MEMORY.md`](file:///c:/GUARDIAN/docs/SESSION_MEMORY.md)
with root cause and verification state — read that before touching deploy, capture or
the app tracker. Recent closures worth knowing: **BUG-10** AppTracker lock stall,
**BUG-23** remote `pkill` killing its own SSH session, **BUG-28** blank live view from a
one-shot Wayland probe, **BUG-29** blank live view from a stale `web-ts/dist` bundle that
predated the socket auth handshake. Note that BUG-28 and BUG-29 present *identically*
("device online, mirror blank") from opposite ends of the wire — check the cheap one
(bundle freshness) before SSH-ing into the Pi.

* [x] **BUG-01**: DB 1.28 GB bloat fixed (Cleaned to 3.3 MB, added WAL checkpointing & 3-day retention auto-cleanup).
* [x] **BUG-02**: Duplicate `import queue` in `pi-agent/agent/main.py`.
* [ ] **BUG-03**: Security: `.env` committed with default secrets.
* [ ] **BUG-04**: `persistence.py` references missing `watchdog.py`.
* [ ] **BUG-05**: `os.chdir()` in Windows watchdog is thread-unsafe.
* [ ] **BUG-06**: No rate limiting on `/change-password`.
* [ ] **BUG-07**: Screenshot history endpoint missing auth middleware.
* [ ] **BUG-08**: Custom `shutil_which` duplicates Python stdlib.
* [ ] **BUG-09**: Hardcoded UID in systemd unit installation.

### ✨ Implemented Features
* [x] **F-01 / F-11**: **Live Application Tracking & Management**
  * Real-time active foreground window & app detection with title & duration.
  * Live running processes table with RAM (MB), CPU%, and PID.
  * 200+ installed applications discovery on Windows & Linux with category filters.
  * Anonymous / instant application closing ("Close App Now").
  * Application blocklist with background auto-kill enforcement.
  * App usage countdown timers with 5m/1m warnings and automatic termination.
  * Direct parent message delivery to child screen with Tkinter floating banner.
  * Dedicated **"Apps"** sidebar page in web dashboard + Live View active app badge.
* [x] **Pi Deployment Automation**: full agent upload, installer bootstrap, `server_url`
  patch, service restart and verification — all through `GUARD.bat`.

---

## 5. 🛠️ How to Deploy Code to the Raspberry Pi

**Use `GUARD.bat`. Do not hand-roll SSH/SFTP.** It owns the IP, the credentials, the
host-key cache, the upload, the installer and the service restart, and it reports which
phase failed. Earlier revisions of this document contained a `paramiko` one-liner and a
hardcoded IP; both are gone because they bypassed the config file and silently deployed
to the wrong directory.

```bat
cd C:\GUARDIAN
GUARD.bat            :: interactive menu
GUARD.bat deploy     :: upload pi-agent\agent\* -> /opt/guardian-agent, install, restart, verify
GUARD.bat verify     :: is the service active, and can the Pi reach the dashboard
GUARD.bat diag       :: install/service state, server_url, capture backend, journal tail
GUARD.bat build      :: rebuild the dashboard UI (web-ts/dist) — add "force" to always build
GUARD.bat ip <addr>  :: set the address agents dial home to ("auto" = match the Pi's subnet)
GUARD.bat status     :: local NICs, detected Pi, dashboard port, UI bundle freshness
```

### Menu map

| Key | Action | Notes |
|-----|--------|-------|
| `1` | Launch Dashboard Core | Node server on `:3000`. **Rebuilds `web-ts/dist` first if it is stale** — see BUG-29. |
| `2` | Launch Windows Agent | Local agent + watchdog |
| `3` | Launch Pi Agent Sync | **Full sync**: upload every file in `pi-agent/agent/`, run `install-agent.sh` if `start.sh`/`venv` are missing, patch `server_url`, restart, verify. Falls back to config-only recalibration if the full sync fails. |
| `4` | Recalibrate Network IP Sync | Light path: re-point `server_url` and restart. Use when only the IP moved. |
| `5` | Run Full Init Sequence | 1 → 2 → 3 |
| `D` | Deploy Pi Agent | Same as `GUARD.bat deploy` |
| `I` | Set Server IP | Writes `server_ip` in `guard_runtime.json` |
| `T` | Troubleshoot Pi | Same as `GUARD.bat diag` — **start here when something is broken** |
| `6` / `7` | Open dashboard / network report | Local NICs, detected Pi, current `server_url` |
| `8` | Update Pi Hostname/User | Rewrites credentials in `guard_runtime.json` |
| `9` / `0` | Close all systems / exit | |

`config.json` is deliberately **excluded** from the upload so the Pi keeps its own
device identity; only `server_url` is patched afterwards.

### Reading `GUARD.bat diag`

| Marker | Healthy | Meaning when wrong |
|--------|---------|--------------------|
| `GUARDIAN_SVC` | `active` | Unit dead — the journal tail is printed automatically |
| `GRIM` | `yes` | Wayland desktop cannot be captured at all without it |
| `WAYLAND_SOCKETS` | a path | Empty = no desktop session logged in; nothing to capture |
| `CAPTURE_FAIL` | `0` | Non-zero = frames are failing; device shows online, mirror blank |
| `SERVER_URL` | LAN IP of the Windows host | `localhost` or an old subnet = re-run menu `I` |
| `Dashboard UI` | `current` | `STALE`/`MISSING` = the browser is running an old bundle; socket features fail while the page looks fine. Run `GUARD.bat build`. |

### After changing frontend code

`GUARD.bat` now builds the UI for you (menu `1`, or `GUARD.bat build`). The manual
equivalent is:

```bat
cd C:\GUARDIAN\parent-dashboard\web-ts && npm run build
```
`dist/` is what the server serves; skipping the build means your change is invisible —
and worse, an old bundle can silently break the socket contract while REST-driven parts
of the page keep working (that was BUG-29: a blank live view with the device online).

---

## 6. ⚠️ Critical Rules (Never Break These)

1. **NEVER push `.env` to GitHub** — it contains JWT secrets and admin credentials.
2. **NEVER hardcode the Pi's IP, user or password.** Read
   `windows-app/config/guard_runtime.json`; change it via `GUARD.bat ip <addr>` or menu
   `I`/`8`. Any address written into code or docs will be stale within a DHCP lease.
3. **NEVER set Pi `server_url` to `localhost`** — on the Pi, that is the Pi.
4. **NEVER modify a `hosts` file without root/admin** — it fails silently, and the
   blocker then falls back to active-tab enforcement only.
5. **NEVER put a bare `pkill -f <pattern>` in a command sent over SSH.** `pkill -f`
   matches full command lines including the carrier shell's own argv, so the step kills
   the session running it and every later step in the chain is skipped. Use a bracketed
   regex: `pkill -f '[g]uardian_watchdog\.py'`. (This was BUG-23; it presented as a
   password error.)
6. **"Device online" does NOT mean mirroring works.** The Pi's heartbeat is deliberately
   independent of screen capture, so a totally dead capture backend still shows a green
   device. Judge the live view by whether frames arrive, never by the status dot.
7. **Run `GUARD.bat diag` before believing any error message.** Remote failures used to
   be reported as "check SSH+sudo password" regardless of cause. `diag` names the phase
   and prints install state, service state, `server_url`, capture backend and the
   journal tail.
8. **The browser runs `web-ts/dist`, NOT `main.js`.** `server-ts/index.js` serves
   `web-ts/dist` as static files, so editing `main.js` changes nothing the user sees until
   Vite rebuilds. Run `GUARD.bat build` (menu `1` now does it automatically when the
   bundle is stale). **Never debug a socket-driven feature before checking bundle
   freshness** — the first line of `GUARD.bat diag` is that check. A bundle older than
   `server-ts/index.js` can predate a socket contract change and be silently rejected,
   while REST-driven parts of the page keep looking perfectly healthy. That was BUG-29.
9. **A Wayland Pi needs `grim` plus a logged-in desktop session.** No compositor socket
   under `/run/user/<uid>/` means there is literally nothing to capture; `scrot` and
   `mss` cannot substitute on Wayland.
10. **Prefer editing over rewriting these docs.** When replacing a whole section
    programmatically, anchor on a unique heading — the phrase "LAST AI CHANGES & STATUS"
    also appears in the instruction at the top of this file, and matching the first
    occurrence truncates everything in between.
11. **ALWAYS update the "LAST AI CHANGES & STATUS" section below before ending your
    turn**, and append new bugs to `docs/SESSION_MEMORY.md`.

---

## 📌 LAST AI CHANGES & STATUS
> *This section was updated on 2026-09-02 by Claude (Opus 5).*

### 🛠️ What was executed in this session

1. **Deploy chain (BUG-23 → BUG-27)** — "Failed to update Pi config.json / restart agent
   (check SSH+sudo password)" and "Remote configure/restart failed" were one defect and
   had nothing to do with the password. `_remote_config_patch()` sent one long remote
   chain containing a literal `sudo pkill -f guardian_watchdog.py`; `pkill -f` matched
   the carrier shell's own argv, so the step killed its own SSH session and
   `systemctl restart` — the next step — never ran. Fixed with a bracketed regex, and
   the chain was split into named phases (`probe_ssh_and_sudo`, `_cfg_patch_cmd`,
   `_housekeeping_cmd`, `_pip_refresh_cmd`, `_restart_cmd`) so each failure reports
   itself. Also fixed: bare `sudo` with no TTY under `plink -batch`, uncached host keys
   aborting `plink`/`pscp`, an unbounded `pip install -r` outliving the SSH timeout, and
   `verify_pi_online` failing a good deploy when port 3000 was not yet listening.
2. **Menu option 3 is now a full sync** — upload every file in `pi-agent/agent/`, run
   `install-agent.sh` when `start.sh`/`venv` are missing, patch `server_url`, restart,
   verify; it falls back to config-only recalibration only if that fails. Option 4
   remains the light config-only path. Added `GUARD.bat diag` (menu **T**).
3. **Blank live view (BUG-28)** — device online, mirror never painted, waiting did not
   help. `ScreenshotCapture.__init__` probed for a Wayland compositor **once**. Started
   by systemd at boot, the agent ran before `/run/user/<uid>/wayland-0` existed, cached
   `None`, and `capture()` skipped the Wayland branch for the whole process lifetime —
   every frame fell through to `scrot`, which cannot capture Wayland. Because the
   heartbeat is independent of capture, the device still looked online. Detection is now
   re-run (throttled, 5 s) whenever no backend is working, and a vanished session is
   dropped instead of retried forever.
4. **`GUARD.bat diag` now reports the capture backend** — `GRIM`, `SCROT`,
   `WAYLAND_SOCKETS` and a `CAPTURE_FAIL` count from the journal, plus the journal tail
   and a plain-language cause when the service is up but capture is dead.
5. **This document** — removed the hardcoded `192.168.1.3` and the `paramiko` deploy
   snippet (both bypassed `guard_runtime.json` and pointed at the wrong directory),
   documented the real menu/subcommands and the `diag` markers, and added the rules that
   would have prevented BUG-23 and BUG-28.
6. **The other Markdown docs, so the next AI starts oriented** —
   `docs/SESSION_MEMORY.md`: CURRENT STATUS rewritten (no hardcoded IP; it now names
   `guard_runtime.json` as the authority and gives the exact next command), KEY FACTS now
   lists `GUARD.bat`'s full command set and menu map, three new rules (11 `pkill -f`,
   12 never cache a capture probe, 13 "online" ≠ mirroring), a BUG-28 queue entry, a
   BUG-28 fix note and a session-log row. `docs/README.md`: a START HERE table pointing at
   this file and SESSION_MEMORY, the `GUARD.bat` command list, the "one source of truth"
   rule for IPs, and the folder tree corrected. `docs/plan to success.md`: banner-marked
   **historical** — it is full of stale `192.168.137.x` hotspot addresses that no longer
   apply and would mislead anyone following it as a runbook.
7. **A regression test was added to the repo** —
   `pi-agent/agent/tests/test_wayland_redetect.py`, so BUG-28 cannot come back silently.
   `scp_push()` already passes `-r`, so the new subfolder does not disturb the deploy.
8. **Blank live view, part two (BUG-29) — the reason the BUG-28 fix appeared to change
   nothing.** The user re-ran everything through `GUARD.bat` and the mirror was still
   blank, with a toast reading "No target device received stream settings". The Pi was not
   at fault: `web-ts/dist/assets/index-B-c3DfGv.js` was built **2026-08-26 21:48**, while
   `main.js` and `server-ts/index.js` are from **2026-09-02**. The server since gained a
   Socket.IO auth handshake (`classifySocket`, `SOCKET_AUTH_REQUIRED` defaults to *true*),
   and the August bundle never sends a token, so the browser socket was classified
   `anonymous`, never joined the `dashboard-viewers` room, and therefore never received a
   single `live_screenshot` — while the device list and the active-app pill kept working
   because those arrive over REST. `stream_config` was rejected for the same reason, which
   is what produced the toast. Nothing rebuilt the bundle: `start-server.bat` installs only
   *server* dependencies. **`GUARD.py` now owns bundle freshness** — `web_bundle_state()`
   returns `nosrc`/`missing`/`stale`/`fresh` by comparing the newest source mtime against
   the newest `dist/` mtime (1 s jitter grace), `build_web_dashboard()` runs
   `npm install` (only when `node_modules` is absent) then `npm run build`, menu `1`
   builds before launching, `GUARD.bat build [force]` is exposed as a subcommand, and both
   `status` and `diag` print a `Dashboard UI:` line. The misleading toast was also fixed:
   an `Unauthorized` reply now says the *dashboard* is not authorized and to rebuild the
   UI, instead of blaming the device.

### 🟢 Verification

* `screenshot.py` and `GUARD.py` compile; the generated `diag` probe was executed in a
  Linux shell and its markers parse correctly.
* BUG-28 was reproduced and fixed under test: a socket appearing *after* construction
  yields no frame with the old one-shot probe (5 consecutive `None`) and a valid JPEG
  with the fix; a disappearing session clears the target; `capture()` never raises.
  The test now lives in the repo — `python3 pi-agent/agent/tests/test_wayland_redetect.py`
  (fakes `grim`, needs no Pi and no network; 7/7 pass). Re-run it before touching
  `screenshot.py`.
* **Not yet exercised against the real Pi** — this environment has no LAN access. The
  hardware check is the next step.
* BUG-29's staleness detector and build path were exercised against a temp tree with a
  stubbed `npm` (fresh / stale / missing / no-source, successful build, no-op when already
  fresh, failing build, missing `npm`) — 10/10 checks pass, and the check that runs against
  the *real* tree confirms it reports `stale: main.js is newer than the last build
  (2026-09-02 vs 2026-08-26)`. **The actual Vite build could not run here** — the sandbox
  cannot reach the npm registry and only the Windows esbuild binary is installed, so the
  first real build happens on the user's machine.

### 🎯 Next steps

* **Run `GUARD.bat build`, then hard-refresh the dashboard (Ctrl+Shift+R).** Until the
  bundle is rebuilt the live view cannot work no matter what the Pi does — see BUG-29
  above. Menu `1` now does this build automatically.
* Run `GUARD.bat deploy` (or menu 3) to ship the `screenshot.py` fix, then
  `GUARD.bat diag`. Its first line is the `Dashboard UI` freshness check; after that,
  `CAPTURE_FAIL=0` with a non-empty `WAYLAND_SOCKETS` means the mirror should be live, and
  a blank `WAYLAND_SOCKETS` means no desktop session is logged in.
* The user-review checklist below is **historical**. Those app/message failures were
  traced to a single AppTracker lock stall (BUG-10 in `docs/SESSION_MEMORY.md`) and
  fixed; re-test them on hardware rather than re-diagnosing from scratch.
* Remaining open items: **BUG-03** (`.env` secrets) is the highest-value one.
* Update this section and `docs/SESSION_MEMORY.md` before you end your turn.

### THIS IS THE USER REVIEW OF PRIVIOUS AI (DO NOT CHANGE)
## THIS IS A CONSIDERATION NEXT AI NEEDS TO DO BEFORE ENDING SESSION ATLEAST AND TEST THESE 
# THE MESSSAGE SEND FEATURE IS NOT RUNNING ONTO THE PI IN THE FIRST TEST 
# THE APP CONTROL SYSTEM ARE NOT WORKING ON THE PI 
# WHAT APP IS RUNNING IS NOT DISPLAYED ONTO THE PI 
# APP BLOCK FEATURE IS NOT WORKING 
# WHICH APP ARE INSTALLED IS NOT DISPLAYED ONTO THE DASHBOARD
# RESCAN BUTTON DOES NOTHING
# THE HISTORY TAB WORKS FINE WHEN REFRESHED BUT THE MESSAGES DONOT GO TO THE PI AND DISPLAY






