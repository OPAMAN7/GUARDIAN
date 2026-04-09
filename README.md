# Guardian Shield

Parent/child monitoring stack with clean folder separation.

## Current Structure

```text
GUARDIAN/
  parent-dashboard/
    server-ts/        # Parent backend (currently JS runtime, TS target)
    web-ts/           # Parent web dashboard (currently JS runtime, TS target)
    shared-types/

  windows-app/
    scripts/          # Windows launch/install scripts
    watchdog/         # watchdog_server.py + nuh_uh assets
    launcher/         # one-click launcher source/exe
    config/           # local runtime config files

  pi-agent/
    agent/            # Raspberry Pi monitored agent
    scripts/          # install-agent.sh, deploy-rpi.sh
    docs/

  docs/
    plan to success.md

  deploy-rpi.sh       # top-level wrapper to pi-agent/scripts/deploy-rpi.sh
```

## Run One-By-One (No Headache Mode)

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
