# Guardian Shield

Parent/child monitoring stack with clean folder separation.

## START HERE (humans and AIs)

Read these two first — they are the live state of the project, this file is only a
folder map:

| Read this | Why |
|-----------|-----|
| `docs/MASTER_AI.md` | Architecture, file map, network config, the GUARD.bat command/menu reference, the hard rules, and what the last AI changed. |
| `docs/SESSION_MEMORY.md` | Open bug queue, fix history, session log. Update it before you finish. |

Everything is driven by one launcher — `GUARD.bat` in the project root:

```bat
GUARD.bat            :: interactive menu (1 = start dashboard, 3 = Pi sync, T = diag)
GUARD.bat deploy     :: push pi-agent/agent/* to the Pi, install if needed, restart, verify
GUARD.bat verify     :: check the Pi service and the dashboard are answering
GUARD.bat diag       :: what the Pi is ACTUALLY doing (service, capture backend, journal)
GUARD.bat build      :: rebuild the dashboard UI (web-ts/dist); add "force" to always build
GUARD.bat ip <addr>  :: set the dashboard IP the agents connect to
GUARD.bat status     :: local state + whether the dashboard bundle is current
```

There is no `start` subcommand — starting the dashboard is menu option `1`, which also
rebuilds `web-ts/dist` first if it is stale. That matters: the browser loads `dist`, not
`main.js`, so frontend edits are invisible until a build runs (BUG-29).

The Pi IP, user, password, install dir and service name live in exactly one file:
`windows-app/config/guard_runtime.json`. Read it from there — never hardcode an IP in
code or docs, and never hand-edit `pi-agent/agent/config.json` (GUARD.py owns it).

## Current Structure

```text
GUARDIAN/
  GUARD.bat           # single entry point -> GUARD.py
  GUARD.py            # launcher: menu + deploy/verify/diag/build/ip/status subcommands

  parent-dashboard/
    server-ts/        # Parent backend (currently JS runtime, TS target)
    web-ts/           # Parent web dashboard (currently JS runtime, TS target)
    shared-types/

  windows-app/
    scripts/          # Windows launch/install scripts
    watchdog/         # watchdog_server.py + nuh_uh assets
    launcher/         # one-click launcher source/exe
    config/           # guard_runtime.json = single source of truth for IPs/creds

  pi-agent/
    agent/            # Raspberry Pi monitored agent
    scripts/          # install-agent.sh, deploy-rpi.sh
    docs/

  docs/
    MASTER_AI.md      # read first
    SESSION_MEMORY.md # bug queue + session log
    GUARDIAN_MASTER_PLAN.md
    plan to success.md  # historical, hotspot-era

  deploy-rpi.sh       # top-level wrapper to pi-agent/scripts/deploy-rpi.sh
```

## Manual route (legacy — prefer GUARD.bat above)

## 1) Parent server setup (Windows)

From windows-app/scripts:

```bat
install-server.bat
```

Then start protected server:

```bat
start-server.bat
```

## 2) Parent dashboard/web assets

Handled by scripts automatically, or manual run:

```powershell
cd parent-dashboard/web-ts
npm install
npm run build
```

## 3) Live local simulation mode (optional)

```bat
windows-app\scripts\start-live-control.bat
```

Stop it:

```bat
windows-app\scripts\stop-live-control.bat
```

## 4) Raspberry Pi child install

Copy folder to Pi and run:

```bash
sudo bash pi-agent/scripts/install-agent.sh <WINDOWS_IP>
```

## 5) Full stack deploy on Pi (optional)

```bash
sudo bash deploy-rpi.sh
```

## Notes

1. Parent dashboard host is your Windows machine.
2. Child monitored device should be Raspberry Pi in normal mode.
3. Keep custom-command work out of scope for current phase.
4. GUI overhaul and branding are tracked in docs/plan to success.md.
5. "Device online" in the dashboard does NOT mean screen mirroring works — the
   heartbeat is independent of the capture path. Use `GUARD.bat diag`.
6. If a socket-driven feature (the live mirror above all) is dead, check the
   `Dashboard UI:` line in `GUARD.bat diag` before anything else. A stale `web-ts/dist`
   breaks the socket handshake while the rest of the page — which is REST-driven — keeps
   working perfectly. Fix: `GUARD.bat build`, then hard-refresh with Ctrl+Shift+R.
